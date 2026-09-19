"""Existing-PR contracts, with no network or live agents."""
import copy
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
        self.writable = True
        self.action.gh = Mock(side_effect=self.gh)
        self.action.input_pr = self.action.read_pr("owner/repo", 10)
        self.action.remote_sha = HEAD
        self.remote = HEAD
        self.git.command.side_effect = self.command
        self.sync = {"action": "sync_pr", "config": {}}
        self.merge = {"action": "merge_pr", "config": {}}
        self.context = {"workspace_base": FIX}

    def gh(self, *args):
        if args == ("api", "repos/owner/repo/pulls/10"):
            return json.dumps(self.raw)
        if args == ("api", "repos/owner/repo"):
            return json.dumps({"id": 1, "permissions": {"push": self.writable}})
        self.mutations.append(args)
        return json.dumps({"merged": True, "sha": "merged"})

    def command(self, *args):
        if "ls-remote" in args:
            return self.remote + "\trefs/heads/topic" if self.remote else ""
        if "push" in args:
            self.assertIn("--force-with-lease=refs/heads/topic:" + HEAD, args)
            self.assertEqual(args[-2:], ("https://github.com/owner/repo.git", FIX + ":refs/heads/topic"))
            self.remote = FIX
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

    def test_sync_success_then_timeout_retry(self):
        original = self.command
        def timeout(*args):
            result = original(*args)
            if "push" in args:
                raise Failure("git", "timeout", retryable=True)
            return result
        self.git.command.side_effect = timeout
        with self.assertRaises(Failure):
            self.run_sync()
        self.assertEqual(self.action.remote_sha, HEAD)
        self.run_sync()
        self.assertEqual(self.action.remote_sha, FIX)
        self.assertEqual(sum("push" in c.args for c in self.git.command.call_args_list), 1)

    def test_sync_retry_after_post_push_api_timeout(self):
        original = self.gh
        timed_out = False
        def timeout(*args):
            nonlocal timed_out
            if args == ("api", "repos/owner/repo/pulls/10") and self.remote == FIX and not timed_out:
                timed_out = True
                raise Failure("github", "timeout", retryable=True)
            return original(*args)
        self.action.gh.side_effect = timeout
        with self.assertRaises(Failure): self.run_sync()
        self.run_sync()
        self.assertEqual(self.action.remote_sha, FIX)
        self.assertEqual(sum("push" in c.args for c in self.git.command.call_args_list), 1)

    def test_fork_input_retains_head_repository_without_mutation(self):
        self.raw["head"]["repo"] = {"id": 2, "full_name": "fork/repo"}
        self.git.resolve.side_effect = [HEAD, BASE]
        metadata = self.action.resolve_input("owner/repo", 10)
        self.assertEqual(metadata["head_repository"], "fork/repo")
        self.assertEqual(metadata["repository"], "owner/repo")
        self.assertFalse(any("push" in c.args for c in self.git.command.call_args_list))

    def test_remote_and_pr_conflicts_stop_before_push(self):
        for change in ("head", "remote", "deleted", "closed", "base", "branch", "repository"):
            with self.subTest(change=change):
                self.setUp()
                if change == "head": self.raw["head"]["sha"] = "external"
                if change == "remote": self.remote = "external"
                if change == "deleted": self.remote = ""
                if change == "closed": self.raw["state"] = "closed"
                if change == "base": self.raw["base"]["ref"] = "release"
                if change == "branch": self.raw["head"]["ref"] = "other"
                if change == "repository": self.raw["head"]["repo"]["id"] = 2
                with self.assertRaises(Failure): self.run_sync()
                self.assertFalse(any("push" in c.args for c in self.git.command.call_args_list))

    def test_fork_deleted_repository_and_nonwritable_heads_rejected(self):
        for head_repo in ({"id": 2, "full_name": "fork/repo"}, None, {"id": 1, "full_name": "owner/repo"}):
            for action in (self.sync, self.merge):
                with self.subTest(head_repo=head_repo, action=action):
                    self.raw["head"]["repo"] = head_repo
                    self.action.input_pr = self.action.read_pr("owner/repo", 10)
                    self.writable = False
                    with self.assertRaises(Failure):
                        self.action.run("action", action, self.context)
                    self.assertFalse(self.mutations)
                    self.assertFalse(any("push" in c.args for c in self.git.command.call_args_list))

    def test_post_push_retarget_is_detected(self):
        original = self.command
        def retarget(*args):
            result = original(*args)
            if "push" in args: self.raw["base"]["ref"] = "release"
            return result
        self.git.command.side_effect = retarget
        with self.assertRaisesRegex(Failure, "base branch changed"):
            self.run_sync()
        self.assertEqual(self.action.remote_sha, HEAD)

    def test_merge_policy_rejection_and_exact_sha(self):
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
                self.action.gh = Mock(side_effect=[json.dumps(self.raw), json.dumps({"id": 1, "permissions": {"push": True}}), response])
                with self.assertRaisesRegex(Failure, diagnostic) as raised:
                    self.action.run("merge", self.merge, self.context)
                self.assertEqual(raised.exception.kind, kind)
                self.assertEqual(raised.exception.retryable, retryable)
                self.assertEqual(self.action.gh.call_count, 3)
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

    def test_fetch_verifies_exact_head_and_retains_base(self):
        self.git.resolve.side_effect = [HEAD, BASE]
        metadata = self.action.resolve_input("owner/repo", 10)
        self.assertEqual(metadata["base_sha"], BASE)
        calls = [c.args for c in self.git.command.call_args_list]
        self.assertTrue(any(c[-1] == "refs/pull/10/head" for c in calls))
        self.assertIn(("update-ref", "refs/gitweave/run/input/base", BASE), calls)
        self.git.resolve.side_effect = [FIX]
        with self.assertRaisesRegex(Failure, "moved while fetching"):
            self.action.resolve_input("owner/repo", 10)

    def test_fetch_rejects_closed_input_and_mid_fetch_change(self):
        self.raw["state"] = "closed"
        with self.assertRaisesRegex(Failure, "must be open"):
            self.action.resolve_input("owner/repo", 10)
        self.git.command.assert_not_called()
        self.raw["state"] = "open"
        changed = copy.deepcopy(self.raw)
        changed["base"]["ref"] = "release"
        self.action.gh.side_effect = [json.dumps(self.raw), json.dumps(changed)]
        self.git.resolve.side_effect = [HEAD, BASE]
        with self.assertRaisesRegex(Failure, "changed while fetching"):
            self.action.resolve_input("owner/repo", 10)


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
            if "push" in args and self.before_push:
                self.before_push()
            args = tuple(str(self.remote) if a == "https://github.com/owner/repo.git" else a for a in args)
            return original(instance, *args, **kwargs)
        self.addCleanup(patch.stopall)
        patch.object(Git, "command", command).start()
        patch.object(GitHubActions, "gh", self.gh).start()
        patch.object(Path, "cwd", return_value=self.root).start()

    def gh(self, *args):
        if args == ("api", "repos/owner/repo"):
            return json.dumps({"id": 1, "permissions": {"push": True}})
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

    def test_atomic_lease_rejects_race_after_remote_check(self):
        def race():
            git(self.remote, "update-ref", "refs/heads/topic", self.base)
        self.before_push = race
        def work(n, c, w):
            (w / "artifact").write_text("fixed")
            return Result()
        nodes = {"fix": dict(node(), provider="codex"), "sync": self.example["nodes"]["sync"]}
        record = self.runtime(graph(nodes, ["fix", "sync"]), work).run()
        self.assertEqual(record["status"], "failed")
        self.assertIn("stale info", record["failure"]["message"])
        self.assertEqual(git(self.remote, "rev-parse", "topic"), self.base)
        self.assertEqual(record["pr_remote_sha"], self.head)

    def test_local_commit_flow_and_missing_pr_action(self):
        run = Runtime(graph({"work": dict(node(), provider="codex")}, ["work"]), self.remote, self.head, "request", adapters={"codex": Fake(lambda *args: Result())})
        self.assertEqual(run.run()["status"], "completed")
        self.assertIsNone(run.record["input_pr"])
        with self.assertRaisesRegex(Failure, "require --pr"):
            Runtime(graph({"sync": self.example["nodes"]["sync"]}, ["sync"]), self.remote, self.head, "request")
