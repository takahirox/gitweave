"""Existing-PR contracts, with no network or live agents."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from gitweave.actions import GitHubActions
from gitweave.cli import main
from gitweave.git import Git
from gitweave.graph import validate_graph
from gitweave.model import Failure, Result
from gitweave.runtime import Runtime
from test_runtime import Fake, git, graph, node


HEAD = "a" * 40
BASE = "b" * 40
FIX = "c" * 40


def pull(head=HEAD, base=BASE):
    return {"number": 10, "state": "open", "merged": False, "html_url": "https://github.com/owner/repo/pull/10",
            "merge_commit_sha": None,
            "head": {"ref": "topic", "sha": head, "repo": {"id": 1, "full_name": "owner/repo"}},
            "base": {"ref": "main", "sha": base, "repo": {"id": 1, "full_name": "owner/repo"}}}


class InputActionTests(unittest.TestCase):
    def setUp(self):
        self.git = Mock()
        self.action = GitHubActions(self.git, "run")
        self.raw = pull()
        self.mutations = []
        self.action.gh = Mock(side_effect=self.gh)
        self.action.input_pr = self.action.read_pr("owner/repo", 10)
        self.action.remote_sha = HEAD
        self.git.command.side_effect = self.command
        self.sync = {"action": "sync_pr", "config": {}}
        self.merge = {"action": "merge_pr", "config": {}}
        self.context = {"workspace_base": FIX}

    def gh(self, *args):
        if args == ("api", "repos/owner/repo/pulls/10"):
            return json.dumps(self.raw)
        self.assertIn("PUT", args)
        self.mutations.append(args)
        return json.dumps({"merged": True, "sha": "merged"})

    def command(self, *args):
        if "push" in args:
            self.assertIn("--force-with-lease=refs/heads/topic:" + HEAD, args)
            self.assertEqual(args[-2:], (f"https://github.com/{self.raw['head']['repo']['full_name']}.git", FIX + ":refs/heads/topic"))
            self.raw["head"]["sha"] = FIX
        return "tree"

    def run_sync(self):
        return self.action.run("sync", self.sync, self.context)

    def test_sync_tracks_remote_separately_from_local_checkpoints(self):
        self.assertEqual(self.run_sync().data["commit"], FIX)
        self.assertEqual(self.action.remote_sha, FIX)
        self.assertEqual(self.action.input_pr["head_sha"], HEAD)
        # A review/action checkpoint may have another SHA but the identical tree.
        result = self.action.run("merge", self.merge, {"workspace_base": "d" * 40})
        self.assertEqual(result.data, {"merged": True, "url": self.raw["html_url"],
                                       "merge_commit": "merged"})
        self.assertEqual(self.mutations, [
            ("api", "--method", "PUT", "repos/owner/repo/pulls/10/merge",
             "-f", "sha=" + FIX, "-f", "merge_method=merge")])

    def test_sync_uses_one_lease_without_remote_or_post_push_checks(self):
        # A stale API head must not override the known lease or invalidate success.
        self.raw["head"]["sha"] = "d" * 40
        self.action.gh.reset_mock()
        self.run_sync()
        self.assertEqual([c.args for c in self.action.gh.call_args_list], [
            ("api", "repos/owner/repo/pulls/10")])
        self.assertEqual([c.args for c in self.git.command.call_args_list], [
            ("check-ref-format", "refs/heads/topic"),
            ("push",
             "--force-with-lease=refs/heads/topic:" + HEAD,
             "https://github.com/owner/repo.git", FIX + ":refs/heads/topic")])
        self.assertEqual(self.action.remote_sha, FIX)

    def test_sync_failure_preserves_known_head_and_retry_uses_same_lease(self):
        for diagnostic in ("stale info", "timeout", "permission denied"):
            with self.subTest(diagnostic=diagnostic):
                self.setUp()
                failure = Failure("git", diagnostic, retryable=True)
                def fail(*args):
                    if "push" in args:
                        if diagnostic == "timeout":
                            self.raw["head"]["sha"] = FIX
                        raise failure
                    return ""
                self.git.command.side_effect = fail
                for _ in range(2):
                    with self.assertRaises(Failure) as raised:
                        self.run_sync()
                    self.assertIs(raised.exception, failure)
                    self.assertEqual(self.action.remote_sha, HEAD)
                pushes = [c.args for c in self.git.command.call_args_list if "push" in c.args]
                self.assertEqual(len(pushes), 2)
                for push in pushes:
                    self.assertIn("--force-with-lease=refs/heads/topic:" + HEAD, push)

    def test_successive_sync_uses_last_successful_head(self):
        self.run_sync()
        self.git.command.reset_mock(side_effect=True)
        next_commit = "d" * 40
        self.action.run("sync", self.sync, {"workspace_base": next_commit})
        self.assertIn("--force-with-lease=refs/heads/topic:" + FIX,
                      self.git.command.call_args.args)
        self.assertEqual(self.action.remote_sha, next_commit)

    def test_fork_input_retains_head_repository_without_mutation(self):
        self.raw["head"]["repo"] = {"id": 2, "full_name": "fork/repo"}
        self.git.resolve.side_effect = [HEAD, BASE]
        metadata = self.action.resolve_input("owner/repo", 10)
        self.assertEqual(metadata["head_repository"], "fork/repo")
        self.assertEqual(metadata["repository"], "owner/repo")
        self.assertFalse(any("push" in c.args for c in self.git.command.call_args_list))

    def test_pr_identity_and_state_conflicts_stop_before_push(self):
        for change in ("closed", "base", "branch", "repository"):
            with self.subTest(change=change):
                self.setUp()
                if change == "closed": self.raw["state"] = "closed"
                if change == "base": self.raw["base"]["ref"] = "release"
                if change == "branch": self.raw["head"]["ref"] = "other"
                if change == "repository": self.raw["head"]["repo"]["id"] = 2
                with self.assertRaises(Failure): self.run_sync()
                self.assertFalse(any("push" in c.args for c in self.git.command.call_args_list))

    def test_fork_sync_targets_head_repository_and_exact_ref(self):
        self.raw["head"]["repo"] = {"id": 2, "full_name": "fork/repo"}
        self.action.input_pr = self.action.read_pr("owner/repo", 10)
        self.action.gh.reset_mock()
        self.assertEqual(self.run_sync().data["commit"], FIX)
        self.assertEqual(self.git.command.call_args.args, (
            "push", "--force-with-lease=refs/heads/topic:" + HEAD,
            "https://github.com/fork/repo.git", FIX + ":refs/heads/topic"))
        self.action.gh.assert_called_once_with("api", "repos/owner/repo/pulls/10")

    def test_merge_needs_no_head_repository_push_permission(self):
        for head_repo in ({"id": 2, "full_name": "fork/repo"}, None,
                          {"id": 1, "full_name": "owner/repo"}):
            with self.subTest(head_repo=head_repo):
                self.setUp()
                self.raw["head"]["repo"] = head_repo
                self.action.input_pr = self.action.read_pr("owner/repo", 10)
                self.action.gh.reset_mock()
                result = self.action.run("merge", self.merge, self.context)
                self.assertTrue(result.data["merged"])
                self.assertEqual([c.args for c in self.action.gh.call_args_list], [
                    ("api", "repos/owner/repo/pulls/10"),
                    ("api", "--method", "PUT", "repos/owner/repo/pulls/10/merge",
                     "-f", "sha=" + HEAD, "-f", "merge_method=merge")])
                self.assertFalse(any("push" in c.args for c in self.git.command.call_args_list))

    def test_sync_requires_identifiable_head_repository(self):
        self.raw["head"]["repo"] = None
        self.action.input_pr = self.action.read_pr("owner/repo", 10)
        with self.assertRaisesRegex(Failure, "cannot identify push target"):
            self.run_sync()
        self.git.command.assert_not_called()
        self.assertFalse(self.mutations)

    def test_merge_policy_rejection_and_exact_sha(self):
        for response, diagnostic, kind, retryable in (
                (Failure("github", "HTTP 403: permission denied", retryable=True),
                 "HTTP 403: permission denied", "github", True),
                (json.dumps({"merged": False, "message": "Required checks pending"}),
                 "Required checks pending", "merge_policy", False),
                (Failure("github", "HTTP 405: Required reviews missing", retryable=True),
                 "Required reviews missing", "github", True),
                (json.dumps({"merged": False, "message": "Merge commits are not allowed"}),
                 "Merge commits are not allowed", "merge_policy", False),
                (Failure("github", "HTTP 405: Merge commits are not allowed", retryable=True),
                 "HTTP 405: Merge commits are not allowed", "github", True)):
            with self.subTest(diagnostic=diagnostic):
                self.action.gh = Mock(side_effect=[json.dumps(self.raw), response])
                with self.assertRaisesRegex(Failure, diagnostic) as raised:
                    self.action.run("merge", self.merge, self.context)
                self.assertEqual(raised.exception.kind, kind)
                self.assertEqual(raised.exception.retryable, retryable)
                self.assertEqual(self.action.gh.call_count, 2)
                self.assertEqual(self.action.gh.call_args.args,
                                 ("api", "--method", "PUT", "repos/owner/repo/pulls/10/merge",
                                  "-f", "sha=" + HEAD, "-f", "merge_method=merge"))

    def test_merge_success_then_timeout_retry(self):
        original = self.gh
        def timeout(*args):
            if "PUT" in args:
                self.raw.update(merged=True, state="closed", merge_commit_sha="merged")
                raise Failure("github", "timeout", retryable=True)
            return original(*args)
        self.action.gh.side_effect = timeout
        with self.assertRaises(Failure): self.action.run("merge", self.merge, self.context)
        result = self.action.run("merge", self.merge, self.context)
        self.assertTrue(result.data["merged"])
        self.assertEqual(result.data["merge_commit"], "merged")
        self.assertEqual(sum("PUT" in c.args for c in self.action.gh.call_args_list), 1)

    def test_merge_rejects_unsynced_tree_and_changed_head(self):
        self.git.command.side_effect = ["local-tree", "remote-tree"]
        with self.assertRaisesRegex(Failure, "unpublished"):
            self.action.run("merge", self.merge, self.context)
        self.raw["head"]["sha"] = FIX
        with self.assertRaisesRegex(Failure, "known remote"):
            self.action.run("merge", self.merge, self.context)
        self.assertFalse(self.mutations)

    def test_fetch_freezes_actual_commits_with_one_metadata_read(self):
        # The PR head ref may differ from the earlier API value.
        self.git.resolve.side_effect = [FIX, BASE]
        self.action.gh.reset_mock()
        metadata = self.action.resolve_input("owner/repo", 10)
        self.assertEqual(metadata["head_sha"], FIX)
        self.assertEqual(metadata["base_sha"], BASE)
        self.assertEqual(self.action.input_pr, metadata)
        self.assertEqual(self.action.remote_sha, FIX)
        self.action.gh.assert_called_once_with("api", "repos/owner/repo/pulls/10")
        self.assertEqual([c.args for c in self.git.resolve.call_args_list],
                         [("FETCH_HEAD",), ("FETCH_HEAD",)])
        self.assertEqual([c.args for c in self.git.command.call_args_list], [
            ("fetch", "--no-tags", "https://github.com/owner/repo.git", "refs/pull/10/head"),
            ("update-ref", "refs/gitweave/run/input/head", FIX),
            ("fetch", "--no-tags", "https://github.com/owner/repo.git", BASE),
            ("update-ref", "refs/gitweave/run/input/base", BASE)])

    def test_fetch_rejects_closed_input(self):
        self.raw["state"] = "closed"
        with self.assertRaisesRegex(Failure, "must be open"):
            self.action.resolve_input("owner/repo", 10)
        self.git.command.assert_not_called()

    def test_fetch_failure_does_not_initialize_input(self):
        for failed_fetch in (1, 2):
            with self.subTest(failed_fetch=failed_fetch):
                self.setUp()
                self.action.input_pr = None
                self.action.remote_sha = None
                self.git.resolve.return_value = HEAD
                failure = Failure("git", "fetch failed", retryable=True)
                self.git.command.side_effect = ([failure] if failed_fetch == 1
                                                else ["", "", failure])
                with self.assertRaises(Failure) as raised:
                    self.action.resolve_input("owner/repo", 10)
                self.assertIs(raised.exception, failure)
                self.assertIsNone(self.action.input_pr)
                self.assertIsNone(self.action.remote_sha)


class CLITests(unittest.TestCase):
    def test_both_cli_input_modes(self):
        for flags, commit, pr in ((["--commit", "HEAD"], "HEAD", None), (["--pr", "10"], None, 10)):
            with self.subTest(flags=flags), patch("sys.argv", ["gitweave", "run", "--graph", "graph.json", "--repo", "owner/repo", *flags, "request"]), patch("gitweave.cli.Runtime") as runtime, patch.object(Path, "read_text", return_value="graph"), patch("sys.stdout", new_callable=io.StringIO):
                runtime.return_value.run.return_value = dict(run_id="run", status="completed", repository="storage", run_ref="ref", notes_ref="notes", outputs=[])
                self.assertEqual(main(), 0)
                runtime.assert_called_once_with("graph", "owner/repo", commit, "request", pr=pr, provenance_remote=None)

    def test_cli_requires_one_input(self):
        for flags in ([], ["--commit", "HEAD", "--pr", "10"]):
            with patch("sys.argv", ["gitweave", "run", "--graph", "g", "--repo", "r", *flags, "request"]), patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
                main()

    def test_pr_contract_rejects_checkout_and_invalid_numbers(self):
        text = graph({"work": node()}, ["work"])
        for repo, commit, pr in (("/tmp/repo", None, 10), ("owner/repo", "HEAD", 10), ("owner/repo", None, 0), ("owner/..", None, 10)):
            with self.subTest(repo=repo, commit=commit, pr=pr), self.assertRaises(Failure):
                Runtime(text, repo, commit, "request", pr=pr)

    def test_input_action_contract(self):
        for action in ("sync_pr", "merge_pr"):
            n = dict(kind="action", action=action, workspace_base=0, config={})
            validate_graph(json.loads(graph({"a": n}, ["a"])))
            n["config"] = {"repository": "owner/repo"}
            with self.assertRaises(Failure): validate_graph(json.loads(graph({"a": n}, ["a"])))


class RepositoryWorkflowTests(unittest.TestCase):
    """Real Git objects, fetches, worktrees and leased pushes; fake GitHub transport."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote"
        self.remote.mkdir()
        git(self.remote, "init", "-q")
        # Receive-side maintenance must finish before TemporaryDirectory cleanup,
        # including after the runtime's final provenance push on a failed sync.
        git(self.remote, "config", "maintenance.autoDetach", "false")
        git(self.remote, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "--allow-empty", "-qm", "base")
        self.base = git(self.remote, "rev-parse", "HEAD")
        git(self.remote, "branch", "main", self.base)
        git(self.remote, "checkout", "-qb", "topic")
        (self.remote / "artifact").write_text("needs fix")
        git(self.remote, "add", "artifact")
        git(self.remote, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "PR head")
        self.head = git(self.remote, "rev-parse", "HEAD")
        git(self.remote, "update-ref", "refs/pull/10/head", self.head)
        git(self.remote, "checkout", "--detach", "-q", self.base)
        self.merged = False
        self.merge_calls = []
        self.before_push = None
        self.example = json.loads((Path(__file__).parent.parent / "examples/review-fix-merge.json").read_text())
        original = Git.command
        def command(instance, *args, **kwargs):
            if "push" in args and args[-1].endswith(":refs/heads/topic") and self.before_push:
                self.before_push()
            args = tuple(str(self.remote) if a == "https://github.com/owner/repo.git" else a for a in args)
            return original(instance, *args, **kwargs)
        self.addCleanup(patch.stopall)
        patch.object(Git, "command", command).start()
        patch.object(GitHubActions, "gh", self.gh).start()
        patch.object(Path, "cwd", return_value=self.root).start()

    def gh(self, *args):
        if args == ("api", "repos/owner/repo/pulls/10"):
            result = pull(git(self.remote, "rev-parse", "refs/heads/topic"), self.base)
            result.update(merged=self.merged, state="closed" if self.merged else "open", merge_commit_sha="merged" if self.merged else None)
            return json.dumps(result)
        self.assertIn("PUT", args)
        self.assertIn("sha=" + git(self.remote, "rev-parse", "refs/heads/topic"), args)
        self.merge_calls.append(args)
        self.merged = True
        return json.dumps({"merged": True, "sha": "merged"})

    def runtime(self, text, work):
        return Runtime(text, "owner/repo", None, "request", pr=10, adapters={"codex": Fake(work)})

    def test_exact_input_metadata_is_retained_and_exposed(self):
        seen = []
        def work(n, c, w):
            seen.append(c)
            self.assertEqual(git(w, "rev-parse", "HEAD"), self.head)
            self.assertEqual((w / "artifact").read_text(), "needs fix")
            self.assertEqual(git(w, "rev-parse", c["input_pr"]["base_sha"]), self.base)
            c["input_pr"]["head_branch"] = "cannot mutate runtime metadata"
            return Result()
        run = self.runtime(graph({"work": dict(node(), provider="codex")}, ["work"]), work)
        record = run.run()
        self.assertEqual(record["status"], "completed", record.get("failure"))
        self.assertEqual(record["base_commit"], self.head)
        self.assertEqual(record["input_pr"]["head_branch"], "topic")
        self.assertEqual(record["pr_remote_sha"], self.head)
        retained = json.loads(git(run.git.repo, "show", record["run_ref"] + ":run.json"))
        self.assertEqual(retained["input_pr"], record["input_pr"])
        self.assertEqual(git(run.git.repo, "rev-parse", f"refs/gitweave/{run.id}/input/base"), self.base)
        self.assertEqual(git(self.remote, "rev-parse", "HEAD"), self.base)

    def test_runtime_uses_fetched_input_after_remote_refs_move(self):
        # Metadata reports the old head, while the PR ref already has the new one.
        with patch.object(GitHubActions, "gh", return_value=json.dumps(pull(self.base, self.base))) as api:
            def work(n, c, w):
                self.assertEqual(git(w, "rev-parse", "HEAD"), self.head)
                self.assertEqual((w / "artifact").read_text(), "needs fix")
                self.assertEqual(c["input_pr"]["head_sha"], self.head)
                self.assertEqual(c["input_pr"]["base_sha"], self.base)
                self.assertEqual(c["pr_remote_sha"], self.head)
                return Result()
            run = self.runtime(graph({"work": dict(node(), provider="codex")}, ["work"]), work)
            git(self.remote, "update-ref", "refs/pull/10/head", self.base)
            git(self.remote, "update-ref", "refs/heads/topic", self.base)
            git(self.remote, "update-ref", "refs/heads/main", self.head)
            record = run.run()
            api.assert_called_once_with("api", "repos/owner/repo/pulls/10")
        self.assertEqual(record["status"], "completed", record.get("failure"))
        self.assertEqual(record["base_commit"], self.head)
        for name, commit in (("head", self.head), ("base", self.base)):
            self.assertEqual(record["input_pr"][name + "_sha"], commit)
            self.assertEqual(git(run.git.repo, "rev-parse",
                                 f"refs/gitweave/{run.id}/input/{name}"), commit)

    def test_example_reviews_fixes_syncs_then_merges(self):
        calls = []
        def work(n, c, w):
            if "schema" in n:
                calls.append("review")
                self.assertEqual(c["input_pr"]["base_sha"], self.base)
                self.assertEqual(c["pr_remote_sha"], git(self.remote, "rev-parse", "topic"))
                approved = (w / "artifact").read_text() == "fixed"
                return Result(data={"approved": approved, "findings": [] if approved else ["Fix artifact"]})
            calls.append("fix")
            self.assertEqual(c["inputs"][0]["data"]["findings"], ["Fix artifact"])
            (w / "artifact").write_text("fixed")
            return Result()
        run = self.runtime(json.dumps(self.example), work)
        record = run.run()
        self.assertEqual(record["status"], "completed", record.get("failure"))
        self.assertEqual(calls, ["review", "fix", "review"])
        self.assertEqual(len(self.merge_calls), 1)
        self.assertTrue(record["outputs"][0]["data"]["merged"])
        archived = json.loads(git(self.remote, "show", record["run_ref"] + ":run.json"))
        self.assertEqual(archived, record)
        final_note = json.loads(git(self.remote, "notes", "--ref=" + record["notes_ref"], "show", record["outputs"][0]["commit"]))
        self.assertEqual(final_note["result"]["data"]["merge_commit"], "merged")
        remote = git(self.remote, "rev-parse", "topic")
        self.assertEqual(record["pr_remote_sha"], remote)
        self.assertNotEqual(record["outputs"][0]["commit"], remote)
        self.assertEqual(git(self.remote, "show", "topic:artifact"), "fixed")
        self.assertEqual(git(self.remote, "rev-parse", "HEAD"), self.base)

    def test_example_clean_review_does_not_push(self):
        run = self.runtime(json.dumps(self.example), lambda *args: Result(data={"approved": True, "findings": []}))
        self.assertEqual(run.run()["status"], "completed")
        self.assertEqual(git(self.remote, "rev-parse", "topic"), self.head)
        self.assertEqual(len(self.merge_calls), 1)

    def test_example_exhaustion_does_not_merge(self):
        self.example["max_steps"] = 14
        def work(n, c, w):
            return Result(data={"approved": False, "findings": ["Still broken"]}) if "schema" in n else Result()
        record = self.runtime(json.dumps(self.example), work).run()
        self.assertEqual(record["failure"]["kind"], "step_limit")
        self.assertFalse(self.merge_calls)

    def test_example_invalid_review_does_not_merge(self):
        record = self.runtime(json.dumps(self.example), lambda *args: Result(data={"approved": "yes", "findings": []})).run()
        self.assertEqual(record["failure"]["kind"], "result")
        self.assertFalse(self.merge_calls)

    def test_atomic_lease_rejects_move_or_deletion_after_metadata_check(self):
        for change in ("move", "delete"):
            with self.subTest(change=change):
                git(self.remote, "update-ref", "refs/heads/topic", self.head)
                races = []
                def race():
                    races.append(change)
                    if change == "move":
                        git(self.remote, "update-ref", "refs/heads/topic", self.base)
                    else:
                        git(self.remote, "update-ref", "-d", "refs/heads/topic")
                self.before_push = race
                def work(n, c, w):
                    (w / "artifact").write_text("fixed")
                    return Result()
                nodes = {"fix": dict(node(), provider="codex"), "sync": self.example["nodes"]["sync"]}
                record = self.runtime(graph(nodes, ["fix", "sync"]), work).run()
                self.assertEqual(record["status"], "failed")
                self.assertEqual(record["failure"]["kind"], "git")
                self.assertIn("stale info", record["failure"]["message"])
                self.assertEqual(races, [change])
                self.assertEqual(git(self.remote, "for-each-ref", "--format=%(objectname)",
                                     "refs/heads/topic"), self.base if change == "move" else "")
                self.assertEqual(record["pr_remote_sha"], self.head)

    def test_local_commit_flow_and_missing_pr_action(self):
        run = Runtime(graph({"work": dict(node(), provider="codex")}, ["work"]), self.remote, self.head, "request", adapters={"codex": Fake(lambda *args: Result())})
        self.assertEqual(run.run()["status"], "completed")
        self.assertIsNone(run.record["input_pr"])
        with self.assertRaisesRegex(Failure, "require --pr"):
            Runtime(graph({"sync": self.example["nodes"]["sync"]}, ["sync"]), self.remote, self.head, "request")
