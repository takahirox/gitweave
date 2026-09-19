import json
import unittest
from unittest.mock import Mock
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
        push = self.git.command.call_args.args
        self.assertIn("--force-with-lease=refs/heads/gitweave/run/pub:", push)
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
        with self.assertRaises(Failure) as raised:
            self.actions.run("pub", self.pub, self.context)
        self.assertEqual(raised.exception.kind, "publication_conflict")
        self.actions.gh.assert_not_called()

    def test_merge_requires_approval_and_exact_head(self):
        self.actions.published["pub"] = "a" * 40
        pr = dict(number=1, state="OPEN", reviewDecision="REVIEW_REQUIRED", headRefOid="a" * 40, url="url", mergeCommit=None)
        self.actions.gh.return_value = json.dumps(pr)
        result = self.actions.run("merge", self.merge, self.context)
        self.assertFalse(result.data["merged"])
        self.assertEqual(self.actions.gh.call_count, 1)
        pr["reviewDecision"] = "APPROVED"
        self.actions.gh.side_effect = [json.dumps(pr), "", json.dumps(dict(pr, state="MERGED", mergeCommit={"oid": "integrated"}))]
        result = self.actions.run("merge", self.merge, self.context)
        self.assertTrue(result.data["merged"])
        self.assertEqual(result.data["merge_commit"], "integrated")
        merge_args = self.actions.gh.call_args_list[-2].args
        self.assertIn("--match-head-commit", merge_args)
        self.assertNotIn("--admin", merge_args)

    def test_merge_retry_is_idempotent(self):
        self.actions.published["pub"] = "a" * 40
        self.actions.gh.return_value = json.dumps(dict(number=1, state="MERGED", headRefOid="a" * 40, url="url", mergeCommit={"oid": "merged"}))
        self.assertTrue(self.actions.run("merge", self.merge, self.context).data["merged"])
        self.assertEqual(self.actions.gh.call_count, 1)
