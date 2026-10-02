"""Run input contracts (--commit, --pr, --issue) with real Git fetches; no network or live agents."""
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import sys
import unittest
from unittest.mock import ANY, Mock, patch

from gitweave.cli import main
from gitweave.git import Git
from gitweave.model import Failure, Result
from gitweave.runtime import Runtime
from test_runtime import Fake, git, graph, node


class CLITests(unittest.TestCase):
    def test_cli_input_modes(self):
        for flags, commit, pr, issue in ((["--commit", "HEAD"], "HEAD", None, None), (["--pr", "10"], None, 10, None),
                                         (["--issue", "123"], None, None, 123)):
            with self.subTest(flags=flags), patch("sys.argv", ["gitweave", "run", "--graph", "graph.json", "--repo", "owner/repo", *flags, "request"]), patch("gitweave.cli.Runtime") as runtime, patch.object(Path, "read_text", return_value="graph"), patch("sys.stdout", new_callable=io.StringIO):
                runtime.return_value.run.return_value = dict(run_id="run", status="completed", repository="storage", run_ref="ref", notes_ref="notes", outputs=[])
                self.assertEqual(main(), 0)
                runtime.assert_called_once_with("graph", "owner/repo", commit, "request", pr=pr, issue=issue, provenance_remote=None, event_sink=ANY, initialize_empty=None, base_branch=None)

    def test_cli_requires_one_input(self):
        for flags in ([], ["--commit", "HEAD", "--pr", "10"], ["--commit", "HEAD", "--issue", "1"],
                      ["--pr", "10", "--issue", "1"], ["--issue", "x"]):
            with patch("sys.argv", ["gitweave", "run", "--graph", "g", "--repo", "r", *flags, "request"]), patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
                main()


class RepositoryInputTests(unittest.TestCase):
    """GitHub URLs are redirected to a local repository; GitHub itself is never contacted."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote"
        self.remote.mkdir()
        git(self.remote, "init", "-q")
        # Receive-side maintenance must finish before TemporaryDirectory cleanup.
        git(self.remote, "config", "maintenance.autoDetach", "false")
        git(self.remote, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "--allow-empty", "-qm", "base")
        self.base = git(self.remote, "rev-parse", "HEAD")
        git(self.remote, "branch", "-M", "main")
        git(self.remote, "checkout", "-qb", "topic")
        (self.remote / "artifact").write_text("PR head")
        git(self.remote, "add", "artifact")
        git(self.remote, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "PR head")
        self.head = git(self.remote, "rev-parse", "HEAD")
        git(self.remote, "update-ref", "refs/pull/10/head", self.head)
        git(self.remote, "checkout", "-q", "main")  # the remote default branch
        original = Git.command
        def command(instance, *args, **kwargs):
            args = tuple(str(self.remote) if str(a).lower() == "https://github.com/owner/repo.git" else a for a in args)
            return original(instance, *args, **kwargs)
        self.addCleanup(patch.stopall)
        patch.object(Git, "command", command).start()
        patch.object(Path, "cwd", return_value=self.root).start()
        # Core never launches gh; only git is allowed.
        popen = __import__("subprocess").Popen
        def guarded(args, *a, **kw):
            if args and args[0] == "gh":
                raise AssertionError("GitWeave core must not call GitHub APIs")
            return popen(args, *a, **kw)
        patch("subprocess.Popen", guarded).start()

    def run_github(self, work, **source):
        run = Runtime(graph({"work": dict(node(), provider="codex")}, ["work"]), "owner/repo", None, "request",
                      adapters={"codex": Fake(work)}, **source)
        # Later moves of remote refs do not change the frozen Run base.
        git(self.remote, "update-ref", "refs/pull/10/head", self.base)
        git(self.remote, "update-ref", "refs/heads/main", self.head)
        return run, run.run()

    def test_pr_input_starts_from_pr_head_and_exposes_only_identity(self):
        seen = []
        def work(n, c, w):
            seen.append(c)
            self.assertEqual(git(w, "rev-parse", "HEAD"), self.head)
            self.assertEqual((w / "artifact").read_text(), "PR head")
            c["run_input"]["number"] = 11  # node-local copy
            return Result()
        run, record = self.run_github(work, pr=10)
        self.assertEqual(record["status"], "completed", record.get("failure"))
        self.assertEqual(seen[0]["run_input"], {"kind": "pull_request", "number": 11})
        self.assertEqual(seen[0]["github_repository"], "owner/repo")
        for legacy in ("input_pr", "pr_remote_sha"):
            self.assertNotIn(legacy, seen[0])
            self.assertNotIn(legacy, record)
        self.assertEqual(record["run_input"], {"kind": "pull_request", "number": 10})
        self.assertEqual(record["base_commit"], self.head)
        self.assertIsNone(record["base_branch"])
        self.assertIsNone(seen[0]["base_branch"])
        self.assertEqual(git(run.git.repo, "rev-parse", f"refs/gitweave/{run.id}/input/base"), self.head)
        self.assertEqual(record["provenance_destination"], "https://github.com/owner/repo.git")
        self.assertEqual(json.loads(git(self.remote, "show", record["run_ref"] + ":run.json")), record)

    def test_issue_input_starts_from_default_branch_without_reading_the_issue(self):
        seen = []
        def work(n, c, w):
            seen.append(c)
            self.assertEqual(git(w, "rev-parse", "HEAD"), self.base)
            return Result()
        run, record = self.run_github(work, issue=123)
        self.assertEqual(record["status"], "completed", record.get("failure"))
        self.assertEqual(seen[0]["run_input"], {"kind": "issue", "number": 123})
        self.assertEqual(seen[0]["github_repository"], "owner/repo")
        self.assertEqual(record["run_input"], {"kind": "issue", "number": 123})
        self.assertEqual(record["base_commit"], self.base)
        self.assertEqual(record["base_branch"], "main")
        self.assertEqual(seen[0]["base_branch"], "main")
        self.assertEqual(git(run.git.repo, "rev-parse", f"refs/gitweave/{run.id}/input/base"), self.base)
        self.assertEqual(record["provenance_destination"], "https://github.com/owner/repo.git")
        self.assertEqual(json.loads(git(self.remote, "show", record["run_ref"] + ":run.json")), record)

    def test_explicit_branch_is_frozen_and_exposed_to_agents_commands_and_provenance(self):
        branch = "release/Next"
        git(self.remote, "branch", branch, self.head)
        seen = []
        def work(n, c, w):
            seen.append(c.copy())
            self.assertEqual(git(w, "rev-parse", "HEAD"), self.head)
            self.assertEqual(c["base_branch"], branch)
            c["base_branch"] = "node mutation"
            return Result()
        command = {"kind": "command", "argv": [sys.executable, "-c",
                   'import json, sys; c = json.load(sys.stdin); print(json.dumps({"message": c["base_branch"], "data": c}))']}
        run = Runtime(graph({"work": node(), "publish": command}, ["work", "publish"]),
                      "owner/repo", None, issue=123, base_branch=branch, adapters={"fake": Fake(work)})
        git(self.remote, "update-ref", f"refs/heads/{branch}", self.base)
        git(self.remote, "symbolic-ref", "HEAD", "refs/heads/topic")
        record = run.run()
        self.assertEqual(record["status"], "completed", record.get("failure"))
        self.assertEqual(record["base_commit"], self.head)
        self.assertEqual(record["base_branch"], branch)
        self.assertEqual(record["run_input"], {"kind": "issue", "number": 123})
        self.assertEqual(record["outputs"][0]["data"]["base_branch"], branch)
        self.assertEqual(seen[0]["inputs"][0]["commit"], self.head)
        self.assertEqual(git(run.git.repo, "rev-parse", f"refs/gitweave/{run.id}/input/base"), self.head)
        for attempt in record["attempts"]:
            note = json.loads(git(run.git.repo, "notes", f"--ref={run.git.notes}", "show", attempt["commit"]))
            self.assertEqual(note["base_branch"], branch)
        self.assertEqual(json.loads(git(self.remote, "show", record["run_ref"] + ":run.json")), record)

    def test_default_branch_name_and_commit_are_frozen_even_if_default_changes(self):
        git(self.remote, "branch", "trunk", self.base)
        git(self.remote, "symbolic-ref", "HEAD", "refs/heads/trunk")
        seen = []
        run = Runtime(graph({"work": node()}, ["work"]), "owner/repo", None, issue=1,
                      adapters={"fake": Fake(lambda n, c, w: seen.append(c) or Result())})
        git(self.remote, "update-ref", "refs/heads/trunk", self.head)
        git(self.remote, "symbolic-ref", "HEAD", "refs/heads/topic")
        record = run.run()
        self.assertEqual(record["status"], "completed", record.get("failure"))
        self.assertEqual(run.base, self.base)
        self.assertEqual(record["base_branch"], "trunk")
        self.assertEqual(seen[0]["base_branch"], "trunk")

    def test_default_branch_switch_during_startup_keeps_selected_branch(self):
        original = Git.default_branch
        def switch(instance, remote):
            branch = original(instance, remote)
            git(self.remote, "symbolic-ref", "HEAD", "refs/heads/topic")
            return branch
        with patch.object(Git, "default_branch", switch):
            run = Runtime(graph({"work": node()}, ["work"]), "owner/repo", None, issue=1)
        self.assertEqual((run.base_branch, run.base), ("main", self.base))

    def test_explicit_branch_does_not_require_a_valid_remote_head(self):
        git(self.remote, "symbolic-ref", "HEAD", "refs/heads/missing")
        run = Runtime(graph({"work": node()}, ["work"]), "owner/repo", None, issue=1, base_branch="topic")
        self.assertEqual((run.base_branch, run.base), ("topic", self.head))

    def test_missing_branch_and_tags_or_shas_do_not_fall_back_or_execute_nodes(self):
        git(self.remote, "tag", "tag-only", self.head)
        for branch in ("missing", "Topic", "tag-only", self.head):
            work = Mock()
            with self.subTest(branch=branch), self.assertRaisesRegex(Failure, "couldn't find remote ref refs/heads/"):
                Runtime(graph({"work": node()}, ["work"]), "owner/repo", None, issue=1,
                        base_branch=branch, adapters={"fake": Fake(work)})
            work.assert_not_called()
        store = self.root / ".gitweave" / "repos" / "owner" / "repo.git"
        self.assertEqual(git(store, "for-each-ref", "refs/gitweave"), "")

    def test_invalid_base_branch_fails_before_remote_access(self):
        original = Git.command
        def guarded(instance, *args, **kwargs):
            self.assertNotIn(args[0], ("fetch", "ls-remote", "push", "worktree"))
            return original(instance, *args, **kwargs)
        for branch in ("", "HEAD", "-topic", "topic:other", "../topic", "topic.lock", "@{-1}", "a b", "a\0b", True):
            with self.subTest(branch=branch), patch.object(Git, "command", guarded), self.assertRaisesRegex(Failure, "--base-branch requires a valid branch name"):
                Runtime(graph({"work": node()}, ["work"]), "owner/repo", None, issue=1, base_branch=branch)

    def test_base_branch_is_rejected_in_pr_and_local_modes_before_git_access(self):
        for commit, source in ((None, {"pr": 10}), (self.head, {}), (None, {})):
            with self.subTest(source=source), patch("gitweave.runtime.Git") as storage, self.assertRaisesRegex(Failure, "--base-branch is only supported with --issue"):
                Runtime(graph({"work": node()}, ["work"]), "owner/repo", commit, base_branch="topic", **source)
            storage.assert_not_called()

    def test_resume_preserves_branch_and_base_without_refetching(self):
        definition = graph({"work": node(), "publish": node()}, ["work", "publish"])
        calls = []
        def work(n, c, w):
            calls.append(c["base_branch"])
            if len(calls) == 2:
                raise Failure("provider", "interrupted publish")
            return Result()
        run = Runtime(definition, "owner/repo", None, issue=1, base_branch="topic", adapters={"fake": Fake(work)})
        self.assertEqual(run.run()["status"], "failed")
        git(self.remote, "update-ref", "refs/heads/topic", self.base)
        git(self.remote, "symbolic-ref", "HEAD", "refs/heads/topic")
        def publish(n, c, w):
            self.assertEqual(c["base_branch"], "topic")
            self.assertEqual(c["inputs"][0]["node_id"], "work")
            return Result()
        original = Git.command
        def guarded(instance, *args, **kwargs):
            self.assertNotIn(args[0], ("fetch", "ls-remote"))
            return original(instance, *args, **kwargs)
        with patch.object(Git, "command", guarded):
            record = Runtime.resume(run.id, run.git.repo, adapters={"fake": Fake(publish)}).run()
        self.assertEqual(record["status"], "completed", record.get("failure"))
        self.assertEqual((record["base_branch"], record["base_commit"]), ("topic", self.head))
        self.assertEqual(len(record["attempts"]), 3)

    def objects(self, store):
        stats = dict(line.split(": ") for line in git(store, "count-objects", "-v").splitlines())
        return int(stats["count"]) + int(stats["in-pack"])

    def test_runs_share_one_store_per_repository(self):
        first, first_record = self.run_github(lambda *a: Result(), issue=1)
        store = self.root / ".gitweave" / "repos" / "owner" / "repo.git"
        self.assertEqual(first.git.repo, store.resolve())
        self.assertEqual(first_record["repository"], str(store.resolve()))
        self.assertFalse((self.root / ".gitweave" / "runs").exists())
        # GitHub names are case-insensitive: another spelling reuses the same store,
        # and objects already fetched are not fetched again.
        # The PR head is already in the store (the first Run's base).
        git(self.remote, "update-ref", "refs/pull/10/head", self.base)
        before = self.objects(store)
        second = Runtime(graph({"work": dict(node(), provider="codex")}, ["work"]), "Owner/Repo", None,
                         adapters={"codex": Fake(lambda *a: Result())}, pr=10)
        self.assertEqual(second.git.repo, store.resolve())
        self.assertEqual(self.objects(store), before)
        second_record = second.run()
        self.assertEqual(second_record["status"], "completed", second_record.get("failure"))
        self.assertEqual(second_record["github_repository"], "Owner/Repo")
        # Per-Run provenance stays separate and inspectable in the shared store.
        for run, record in ((first, first_record), (second, second_record)):
            self.assertEqual(json.loads(git(store, "show", record["run_ref"] + ":run.json")), record)
            refs = git(store, "for-each-ref", "--format=%(refname)", f"refs/gitweave/{run.id}/").splitlines()
            self.assertIn(f"refs/gitweave/{run.id}/input/base", refs)
            self.assertTrue(all(ref.startswith(f"refs/gitweave/{run.id}/") for ref in refs))
            note = json.loads(git(store, "notes", f"--ref={run.git.notes}", "show", record["outputs"][0]["commit"]))
            self.assertEqual(note["run_id"], run.id)
        self.assertEqual(git(store, "rev-parse", f"refs/gitweave/{second.id}/input/base"), self.base)
        # Each Run pushed only its own refs.
        pushed = git(self.remote, "for-each-ref", "--format=%(refname)", "refs/gitweave", "refs/notes/gitweave").splitlines()
        self.assertEqual({ref.split("/")[2] for ref in pushed if ref.startswith("refs/gitweave/")}, {first.id, second.id})
        self.assertEqual(git(store, "worktree", "list").count("\n"), 0)

    def test_concurrent_runs_share_the_store_safely(self):
        barrier = threading.Barrier(2)
        results = {}
        def work(n, c, w):
            barrier.wait(timeout=30)  # both Runs have worktrees in the same store at once
            (w / f"{c['run_input']['kind']}.txt").write_text("done")
            return Result()
        def start(key, **source):
            run = Runtime(graph({"work": dict(node(), provider="codex")}, ["work"]), "owner/repo", None,
                          adapters={"codex": Fake(work)}, **source)
            results[key] = (run, run.run())
        threads = [threading.Thread(target=start, args=("issue",), kwargs={"issue": 1}),
                   threading.Thread(target=start, args=("pr",), kwargs={"pr": 10})]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        self.assertEqual(set(results), {"issue", "pr"})
        store = (self.root / ".gitweave" / "repos" / "owner" / "repo.git").resolve()
        for kind, expected_base in (("issue", self.base), ("pr", self.head)):
            run, record = results[kind]
            self.assertEqual(record["status"], "completed", record.get("failure"))
            self.assertEqual(run.git.repo, store)
            self.assertEqual(record["base_commit"], expected_base)
            self.assertEqual(git(store, "show", f"{record['outputs'][0]['commit']}:{'issue' if kind == 'issue' else 'pull_request'}.txt"), "done")
        self.assertNotEqual(results["issue"][0].id, results["pr"][0].id)

    def test_store_creation_race_and_failure_branches(self):
        store = self.root / ".gitweave" / "repos" / "owner" / "repo.git"
        real_rename = os.rename
        # Lost race: another Run renamed its store into place first.
        def lose(src, dst):
            real_rename(src, dst)
            Git(dst, "winner")
            raise OSError(66, "Directory not empty")
        with patch("os.rename", side_effect=lose):
            storage = Git(store, "loser", initialize=True)
        self.assertEqual(storage.repo, store.resolve())
        self.assertEqual(git(store, "config", "gc.auto"), "0")
        self.assertEqual(git(store, "config", "maintenance.auto"), "false")
        self.assertEqual(sorted(p.name for p in store.parent.iterdir()), ["repo.git"])
        # A failed rename with no store in place raises and leaves no staging directory.
        other = self.root / ".gitweave" / "repos" / "owner" / "other.git"
        with patch("os.rename", side_effect=OSError(13, "Permission denied")), self.assertRaises(OSError):
            Git(other, "run", initialize=True)
        self.assertEqual(sorted(p.name for p in other.parent.iterdir()), ["repo.git"])

    def test_invalid_existing_store_does_not_fall_through_to_a_checkout(self):
        git(self.root, "init", "-q")  # the invoking directory is itself a checkout
        store = self.root / ".gitweave" / "repos" / "owner" / "repo.git"
        store.mkdir(parents=True)
        with self.assertRaisesRegex(Failure, "Not a GitWeave store"):
            Runtime(graph({"work": node()}, ["work"]), "owner/repo", None, issue=1)
        self.assertEqual(git(self.root, "for-each-ref", "refs/gitweave"), "")

    def test_github_input_contract(self):
        text = graph({"work": node()}, ["work"])
        for repo, commit, kwargs in (("/tmp/repo", None, {"issue": 1}), ("owner/repo", "HEAD", {"issue": 1}),
                                     ("owner/repo", None, {"issue": 0}), ("owner/repo", None, {"issue": -1}),
                                     ("owner/repo", None, {"issue": True}), ("owner/repo", None, {"issue": "1"}),
                                     ("owner/repo", None, {"issue": 1, "pr": 10}), ("/tmp/repo", None, {"pr": 10}),
                                     ("owner/repo", "HEAD", {"pr": 10}), ("owner/repo", None, {"pr": 0}),
                                     ("owner/..", None, {"pr": 10})):
            with self.subTest(repo=repo, commit=commit, kwargs=kwargs), self.assertRaises(Failure):
                Runtime(text, repo, commit, "request", **kwargs)
        self.assertFalse((self.root / ".gitweave").exists())

    def test_missing_pr_ref_fails_initialization(self):
        with self.assertRaises(Failure) as raised:
            Runtime(graph({"work": node()}, ["work"]), "owner/repo", None, "request", pr=99)
        self.assertEqual(raised.exception.kind, "git")

    def test_local_commit_flow(self):
        def work(n, c, w):
            self.assertIsNone(c["github_repository"])
            self.assertIsNone(c["base_branch"])
            self.assertEqual(c["run_input"], {"kind": "commit", "commit": self.head})
            return Result()
        run = Runtime(graph({"work": dict(node(), provider="codex")}, ["work"]), self.remote, self.head, "request", adapters={"codex": Fake(work)})
        self.assertEqual(run.run()["status"], "completed")
        self.assertEqual(run.record["run_input"], {"kind": "commit", "commit": self.head})
        self.assertIsNone(run.record["github_repository"])
        self.assertIsNone(run.record["base_branch"])
