import json
import subprocess
import unittest
from unittest.mock import Mock, patch
from gitweave.actions import GitHubActions
from gitweave.model import Failure


class ActionTests(unittest.TestCase):
    def setUp(self):
        self.git = Mock()
        self.actions = GitHubActions(self.git, "run")
        self.actions.gh = Mock()
        self.pub = {"action": "publish_pr", "config": {"repository": "owner/repo", "base": "main", "title": "Title"}}
        self.merge = {"action": "merge_pr", "config": {"repository": "owner/repo", "publish_node": "pub"}}
        self.context = {"workspace_base": "a" * 40}

    def test_publish_and_sync_use_managed_branch_and_lease(self):
        self.git.command.return_value = ""
        self.actions.gh.side_effect = ["[]", "https://github.com/owner/repo/pull/1"]
        result = self.actions.run("pub", self.pub, self.context)
        self.assertEqual(result.data["branch"], "gitweave/run/pub")
        self.assertEqual([c.args for c in self.git.command.call_args_list], [
            ("ls-remote", "https://github.com/owner/repo.git", "refs/heads/gitweave/run/pub"),
            ("push", "--force-with-lease=refs/heads/gitweave/run/pub:",
             "https://github.com/owner/repo.git", "a" * 40 + ":refs/heads/gitweave/run/pub")])
        self.git.command.return_value = "a" * 40 + "\trefs/heads/gitweave/run/pub"
        self.actions.gh.side_effect = [json.dumps([{"number": 1, "state": "OPEN", "url": "url", "baseRefName": "main"}]), ""]
        self.actions.run("pub", self.pub, {"workspace_base": "b" * 40})
        self.assertIn("--force-with-lease=refs/heads/gitweave/run/pub:" + "a" * 40, self.git.command.call_args.args)
        self.assertEqual(self.actions.gh.call_args.args[:2], ("pr", "edit"))

    def test_publish_retry_after_remote_success(self):
        # A timed-out push may have succeeded. Recognize our exact artifact.
        self.git.command.return_value = "a" * 40 + "\tref"
        self.actions.gh.side_effect = ["[]", "url"]
        self.actions.run("pub", self.pub, self.context)
        self.assertEqual(self.git.command.call_count, 1)

    def test_external_branch_update_is_not_overwritten(self):
        self.git.command.return_value = "external\tref"
        self.actions.gh.return_value = "[]"
        with self.assertRaises(Failure) as raised:
            self.actions.run("pub", self.pub, self.context)
        self.assertEqual(raised.exception.kind, "publication_conflict")
        self.assertEqual(self.git.command.call_count, 1)

    def test_merge_uses_github_policy_and_exact_head(self):
        self.actions.published["pub"] = "a" * 40
        self.actions.publish_bases["pub"] = "main"
        pr = dict(number=1, state="OPEN", reviewDecision="REVIEW_REQUIRED", headRefOid="a" * 40,
                  url="url", mergeCommit=None, baseRefName="main")
        self.actions.gh.side_effect = [json.dumps(pr), json.dumps({"merged": True, "sha": "integrated"})]
        result = self.actions.run("merge", self.merge, self.context)
        self.assertTrue(result.data["merged"])
        self.assertEqual(result.data["merge_commit"], "integrated")
        merge_args = self.actions.gh.call_args.args
        self.assertEqual(merge_args, ("api", "--method", "PUT", "repos/owner/repo/pulls/1/merge",
                                     "-f", "sha=" + "a" * 40, "-f", "merge_method=merge"))

    def test_merge_retry_is_idempotent(self):
        self.actions.publish_bases["pub"] = "main"
        self.actions.published["pub"] = "a" * 40
        self.actions.gh.return_value = json.dumps(dict(number=1, state="MERGED", baseRefName="main", headRefOid="a" * 40, url="url", mergeCommit={"oid": "merged"}))
        self.assertTrue(self.actions.run("merge", self.merge, self.context).data["merged"])
        self.assertEqual(self.actions.gh.call_count, 1)

    def test_closed_pr_is_rejected_before_pushing(self):
        self.actions.gh.return_value = json.dumps([{"state": "CLOSED", "baseRefName": "main"}])
        with self.assertRaises(Failure):
            self.actions.run("pub", self.pub, self.context)
        self.git.command.assert_not_called()

    def test_changed_pr_head_is_not_merged(self):
        self.actions.published["pub"] = "expected"
        self.actions.gh.return_value = json.dumps({"headRefOid": "external"})
        with self.assertRaises(Failure):
            self.actions.run("merge", self.merge, self.context)
        self.assertEqual(self.actions.gh.call_count, 1)

    def test_managed_merge_policy_failure_and_retarget(self):
        self.actions.published["pub"] = "a" * 40
        self.actions.publish_bases["pub"] = "main"
        pr = dict(number=1, state="OPEN", headRefOid="a" * 40, url="url", baseRefName="release")
        self.actions.gh.return_value = json.dumps(pr)
        with self.assertRaisesRegex(Failure, "base changed"):
            self.actions.run("merge", self.merge, self.context)
        self.assertEqual(self.actions.gh.call_count, 1)
        pr["baseRefName"] = "main"
        for response, diagnostic, kind, retryable in (
                (json.dumps({"merged": False, "message": "Required checks pending"}),
                 "Required checks pending", "merge_policy", False),
                (Failure("github", "HTTP 405: Required reviews missing", retryable=True),
                 "Required reviews missing", "github", True),
                (json.dumps({"merged": False, "message": "Merge commits are not allowed"}),
                 "Merge commits are not allowed", "merge_policy", False),
                (Failure("github", "HTTP 405: Merge commits are not allowed", retryable=True),
                 "HTTP 405: Merge commits are not allowed", "github", True)):
            with self.subTest(diagnostic=diagnostic):
                self.actions.gh = Mock(side_effect=[json.dumps(pr), response])
                with self.assertRaisesRegex(Failure, diagnostic) as raised:
                    self.actions.run("merge", self.merge, self.context)
                self.assertEqual(raised.exception.kind, kind)
                self.assertEqual(raised.exception.retryable, retryable)
                self.assertEqual(self.actions.gh.call_count, 2)
                self.assertEqual(self.actions.gh.call_args.args,
                                 ("api", "--method", "PUT", "repos/owner/repo/pulls/1/merge",
                                  "-f", "sha=" + "a" * 40, "-f", "merge_method=merge"))

    def test_managed_merge_timeout_after_success(self):
        self.actions.published["pub"] = "a" * 40
        self.actions.publish_bases["pub"] = "main"
        pr = dict(number=1, state="OPEN", headRefOid="a" * 40, url="url", baseRefName="main")
        merged = dict(pr, state="MERGED", mergeCommit={"oid": "merged"})
        self.actions.gh.side_effect = [json.dumps(pr), Failure("github", "timeout", retryable=True), json.dumps(merged)]
        with self.assertRaises(Failure):
            self.actions.run("merge", self.merge, self.context)
        self.assertTrue(self.actions.run("merge", self.merge, self.context).data["merged"])
        self.assertEqual(sum("PUT" in c.args for c in self.actions.gh.call_args_list), 1)

    def test_github_commands_have_no_implicit_timeout(self):
        action = GitHubActions(self.git, "run")
        with patch("gitweave.actions.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 0, " {}\n", "")) as invoke:
            self.assertEqual(action.gh("api", "repos/owner/repo"), "{}")
        self.assertNotIn("timeout", invoke.call_args.kwargs)
        self.assertEqual(invoke.call_args.args, (["gh", "api", "repos/owner/repo"],))
        self.assertEqual(invoke.call_args.kwargs["cwd"], self.git.repo)
        self.assertTrue(invoke.call_args.kwargs["text"])
        self.assertTrue(invoke.call_args.kwargs["capture_output"])

    def test_github_command_failures_preserve_diagnostics(self):
        action = GitHubActions(self.git, "run")
        for failure in (OSError("gh unavailable"), subprocess.TimeoutExpired("gh", 7)):
            with self.subTest(failure=failure), \
                    patch("gitweave.actions.subprocess.run", side_effect=failure):
                with self.assertRaises(Failure) as raised:
                    action.gh("api", "repos/owner/repo")
                self.assertEqual(raised.exception.kind, "github")
                self.assertTrue(raised.exception.retryable)
                self.assertEqual(str(raised.exception), str(failure))
                self.assertIs(raised.exception.__cause__, failure)
        with patch("gitweave.actions.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 1, "", " HTTP 403: forbidden\n")):
            with self.assertRaises(Failure) as raised:
                action.gh("api", "repos/owner/repo")
        self.assertEqual(raised.exception.kind, "github")
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(str(raised.exception), "HTTP 403: forbidden")

    def test_github_transport_matches_git_remote_host(self):
        action = GitHubActions(self.git, "run")
        with patch("gitweave.actions.subprocess.run", return_value=Mock(returncode=0, stdout="{}")) as command:
            action.gh("api", "repos/owner/repo")
        self.assertEqual(command.call_args.kwargs["env"]["GH_HOST"], "github.com")

    def test_comments_coexist_with_publication_and_merge(self):
        self.git.command.return_value = ""
        self.actions.gh.side_effect = ["[]", "https://example.test/pr"]
        self.actions.run("pub", self.pub, self.context)
        known = dict(self.actions.published)
        for action in ("comment_issue", "comment_pr"):
            target = {"number": 42}
            if action == "comment_pr":
                target["pull_request"] = {}
            self.actions.gh.side_effect = [json.dumps(target), json.dumps({"id": 7, "html_url": "comment"})]
            self.actions.run("comment", {"action": action, "config": {
                "repository": "other/repository", "number": 42, "body": "Findings"}},
                dict(self.context, instance_id=action))
            self.assertEqual(self.actions.published, known)
        pr = dict(number=1, state="OPEN", headRefOid="a" * 40, url="url", baseRefName="main")
        self.actions.gh.side_effect = [json.dumps(pr), json.dumps({"merged": True, "sha": "merged"})]
        self.assertTrue(self.actions.run("merge", self.merge, self.context).data["merged"])
