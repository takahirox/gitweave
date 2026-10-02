"""Empty GitHub Issue startup via real local Git transports; no GitHub/model calls."""
from concurrent.futures import ThreadPoolExecutor
import io
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import ANY, Mock, patch

from gitweave.cli import main
from gitweave.git import Git
from gitweave.model import Failure, Result
from gitweave.runtime import Runtime
from test_runtime import Fake, git, graph, node


class EmptyRepositoryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        environment = patch.dict(os.environ, GIT_CONFIG_NOSYSTEM="1",
                                 GIT_CONFIG_GLOBAL=str(self.root / "config"))
        environment.start()
        self.addCleanup(environment.stop)
        self.remote = self.root / "remote.git"
        self.remote.mkdir()
        git(self.remote, "init", "--bare", "-q")
        git(self.remote, "symbolic-ref", "HEAD", "refs/heads/trunk")
        git(self.remote, "config", "maintenance.autoDetach", "false")
        self.commands = []
        self.original = Git.command

        def command(instance, *args, **kwargs):
            self.commands.append(args)
            args = tuple(str(self.remote) if a == "https://github.com/owner/repo.git" else a for a in args)
            return self.original(instance, *args, **kwargs)

        self.redirect = command
        redirect = patch.object(Git, "command", command)
        redirect.start()
        self.addCleanup(redirect.stop)
        cwd = patch.object(Path, "cwd", return_value=self.root)
        cwd.start()
        self.addCleanup(cwd.stop)

    def runtime(self, work=None, **kwargs):
        return Runtime(graph({"work": node()}, ["work"]), "owner/repo", None,
                       adapters={"fake": Fake(work or (lambda *a: Result()))}, issue=1, **kwargs)

    def seed(self, ref="refs/heads/trunk", message="User's initial commit"):
        remote = Git(self.remote, "seed")
        commit = remote.commit(remote.command("mktree", input=""), [], message)
        remote.command("update-ref", ref, commit)
        return commit

    def assert_no_initialization(self):
        self.assertFalse(any(args[0] in ("push", "commit-tree", "mktree") for args in self.commands))

    def test_empty_remote_requires_opt_in(self):
        with self.assertRaisesRegex(Failure, "couldn't find remote ref HEAD"):
            self.runtime()
        self.assertEqual(git(self.remote, "for-each-ref"), "")
        self.assert_no_initialization()

    def test_initialized_base_supports_publication_merge_and_later_runs(self):
        def work(n, c, w):
            self.assertEqual(c["run_input"], {"kind": "issue", "number": 1})
            self.assertEqual(c["base_branch"], "trunk")
            self.assertEqual(list(w.iterdir()), [w / ".git"])
            (w / "artifact.txt").write_text("Issue implementation\n")
            git(w, "-c", "user.name=Test", "-c", "user.email=test@localhost", "add", ".")
            git(w, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "Implement Issue #1")
            return Result()

        run = self.runtime(work, initialize_empty="trunk")
        base = run.base
        self.assertEqual(git(self.remote, "rev-parse", "HEAD"), base)
        self.assertEqual(git(self.remote, "rev-list", "--parents", "-n", "1", base), base)
        self.assertEqual(git(self.remote, "ls-tree", "-r", base), "")
        self.assertEqual(git(run.git.repo, "rev-parse", f"refs/gitweave/{run.id}/input/base"), base)
        record = run.run()
        self.assertEqual(record["status"], "completed", record.get("failure"))
        self.assertEqual(record["base_branch"], "trunk")
        head = record["outputs"][0]["commit"]
        run.git.command("push", "--no-force", str(self.remote), f"{head}:refs/heads/issue-1")
        self.assertEqual(git(self.remote, "merge-base", "trunk", "issue-1"), base)
        self.assertEqual(git(self.remote, "show", "issue-1:artifact.txt"), "Issue implementation")
        self.assertEqual(git(self.remote, "rev-parse", "trunk"), base)
        # The published branch is comparable against trunk and merges normally,
        # retaining the implementation and checkpoint commits. No live PR test.
        merge = run.git.commit(run.git.command("rev-parse", f"{head}^{{tree}}"), [base, head], "Merge Issue #1")
        run.git.command("push", "--no-force", str(self.remote), f"{merge}:refs/heads/trunk")
        self.commands.clear()
        later = self.runtime(initialize_empty="trunk")
        self.assertEqual(later.base, merge)
        self.assertEqual(run.base, base)
        self.assert_no_initialization()
        # PR inputs also use their ordinary head fetch after initialization.
        git(self.remote, "update-ref", "refs/pull/2/head", head)
        pr = Runtime(graph({"work": node()}, ["work"]), "owner/repo", None, pr=2)
        self.assertEqual(pr.base, head)
        self.assert_no_initialization()

    def test_explicit_branch_on_empty_remote_fails_without_initialization(self):
        with self.assertRaisesRegex(Failure, "couldn't find remote ref refs/heads/trunk"):
            self.runtime(base_branch="trunk")
        self.assert_no_initialization()

    def test_matching_branch_and_initialization_options(self):
        run = self.runtime(base_branch="trunk", initialize_empty="trunk")
        self.assertEqual((run.base_branch, run.base), ("trunk", git(self.remote, "rev-parse", "HEAD")))
        record = run.run()
        self.assertEqual(record["status"], "completed", record.get("failure"))
        self.assertEqual(record["base_branch"], "trunk")

    def test_conflicting_branch_and_initialization_options_fail_before_git_access(self):
        with self.assertRaisesRegex(Failure, "must select the same branch"):
            self.runtime(base_branch="topic", initialize_empty="trunk")
        self.assertEqual(self.commands, [])

    def test_nonempty_remote_uses_head_without_initializing(self):
        base = self.seed()
        self.commands.clear()
        self.assertEqual(self.runtime(initialize_empty="trunk").base, base)
        self.assert_no_initialization()

    def test_broken_head_and_tags_only_are_not_empty(self):
        for ref in ("refs/heads/other", "refs/tags/v1"):
            with self.subTest(ref=ref):
                self.seed(ref)
                self.commands.clear()
                with self.assertRaisesRegex(Failure, "couldn't find remote ref HEAD"):
                    self.runtime(initialize_empty="trunk")
                self.assert_no_initialization()
                self.assertEqual(git(self.remote, "for-each-ref", "--format=%(refname)"), ref)
                git(self.remote, "update-ref", "-d", ref)

    def test_remote_probe_errors_never_initialize(self):
        for diagnostic in ("Authentication failed", "Could not resolve host", "Repository not found"):
            def fail(instance, *args, **kwargs):
                if args[0] == "ls-remote":
                    raise Failure("git", diagnostic, retryable=True)
                return self.redirect(instance, *args, **kwargs)
            with self.subTest(diagnostic=diagnostic), patch.object(Git, "command", fail):
                self.commands.clear()
                with self.assertRaisesRegex(Failure, diagnostic):
                    self.runtime(initialize_empty="trunk")
                self.assert_no_initialization()
                self.assertEqual(git(self.remote, "for-each-ref"), "")

    def test_nonempty_fetch_error_never_initializes(self):
        self.seed()
        self.commands.clear()
        def fail(instance, *args, **kwargs):
            if args[0] == "fetch":
                raise Failure("git", "Authentication failed", retryable=True)
            return self.redirect(instance, *args, **kwargs)
        with patch.object(Git, "command", fail), self.assertRaisesRegex(Failure, "Authentication failed"):
            self.runtime(initialize_empty="trunk")
        self.assert_no_initialization()

    def test_concurrent_initializers_adopt_one_remote_root(self):
        barrier = threading.Barrier(2)
        candidates = []
        def race(instance, *args, **kwargs):
            if args[0] == "commit-tree":
                # Force distinct candidates even if timestamps coincide.
                kwargs["input"] += f"\n{instance.run_id}"
            if args[0] == "push":
                candidates.append(args[-1].split(":")[0])
                barrier.wait(timeout=10)
            return self.redirect(instance, *args, **kwargs)
        with patch.object(Git, "command", race), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.runtime, initialize_empty="trunk") for _ in range(2)]
            runs = [future.result(timeout=20) for future in futures]
        self.assertEqual(len(set(candidates)), 2)
        winner = git(self.remote, "rev-parse", "HEAD")
        self.assertIn(winner, candidates)
        for run in runs:
            self.assertEqual(run.base, winner)
            self.assertEqual(run.run()["status"], "completed")
        self.assertEqual(git(self.remote, "rev-list", "--count", "trunk"), "1")
        self.assertEqual(git(self.remote, "for-each-ref", "--format=%(refname)", "refs/heads"), "refs/heads/trunk")

    def test_user_wins_race_without_being_overwritten(self):
        winner = None
        def race(instance, *args, **kwargs):
            nonlocal winner
            if args[0] == "push":
                winner = self.seed(message="Concurrent user commit")
            return self.redirect(instance, *args, **kwargs)
        with patch.object(Git, "command", race):
            run = self.runtime(initialize_empty="trunk")
        self.assertEqual(run.base, winner)
        self.assertEqual(git(self.remote, "rev-parse", "HEAD"), winner)
        self.assertEqual(git(self.remote, "rev-list", "--count", "trunk"), "1")

    def test_interrupted_after_creation_reruns_normally(self):
        def interrupt(instance, *args, **kwargs):
            if args[0] == "fetch":
                raise KeyboardInterrupt()
            return self.redirect(instance, *args, **kwargs)
        with patch.object(Git, "command", interrupt), self.assertRaises(KeyboardInterrupt):
            self.runtime(initialize_empty="trunk")
        base = git(self.remote, "rev-parse", "HEAD")
        self.commands.clear()
        run = self.runtime()
        self.assertEqual(run.base, base)
        self.assert_no_initialization()
        self.assertEqual(run.run()["status"], "completed")

    def test_push_accepted_then_connection_lost_recovers(self):
        def disconnect(instance, *args, **kwargs):
            result = self.redirect(instance, *args, **kwargs)
            if args[0] == "push":
                raise Failure("git", "Connection lost", retryable=True)
            return result
        with patch.object(Git, "command", disconnect):
            run = self.runtime(initialize_empty="trunk")
        self.assertEqual(run.base, git(self.remote, "rev-parse", "HEAD"))
        self.assertEqual(sum(args[0] == "push" for args in self.commands), 1)

    def test_rejected_push_preserves_error_and_does_not_run_nodes(self):
        work = Mock()
        def reject(instance, *args, **kwargs):
            if args[0] == "push":
                raise Failure("git", "Permission denied", retryable=True)
            return self.redirect(instance, *args, **kwargs)
        with patch.object(Git, "command", reject), self.assertRaisesRegex(Failure, "Permission denied"):
            self.runtime(work, initialize_empty="trunk")
        work.assert_not_called()
        self.assertEqual(git(self.remote, "for-each-ref"), "")

    def test_failed_race_probe_preserves_push_diagnostic_without_retry(self):
        pushes = []
        def fail(instance, *args, **kwargs):
            if args[0] == "push":
                pushes.append(args)
                raise Failure("git", "Permission denied", retryable=True)
            if args[0] == "ls-remote" and "--refs" in args:
                raise Failure("git", "Connection lost", retryable=True)
            return self.redirect(instance, *args, **kwargs)
        with patch.object(Git, "command", fail), self.assertRaisesRegex(Failure, "Permission denied"):
            self.runtime(initialize_empty="trunk")
        self.assertEqual(len(pushes), 1)
        self.assertEqual(git(self.remote, "for-each-ref"), "")

    def test_initialization_option_is_issue_only(self):
        for source in ({"pr": 2}, {"issue": 1, "pr": 2}, {"commit": "HEAD"}):
            with self.subTest(source=source), self.assertRaisesRegex(Failure, "only supported with --issue"):
                Runtime(graph({"work": node()}, ["work"]), "owner/repo", source.get("commit"),
                        pr=source.get("pr"), issue=source.get("issue"), initialize_empty="trunk")
        self.assertEqual(self.commands, [])

    def test_invalid_branch_names_fail_before_remote_access(self):
        for branch in ("", "-trunk", "trunk:other", "../trunk", "trunk.lock", "@{-1}", True):
            with self.subTest(branch=branch):
                self.commands.clear()
                with self.assertRaisesRegex(Failure, "valid default branch name"):
                    self.runtime(initialize_empty=branch)
                self.assertFalse(any(args[0] in ("ls-remote", "fetch", "push") for args in self.commands))


class EmptyRepositoryCLITests(unittest.TestCase):
    def test_cli_forwards_explicit_initialization_branch(self):
        with patch("sys.argv", ["gitweave", "run", "--graph", "graph.json", "--repo", "owner/repo",
                                "--issue", "1", "--initialize-empty", "trunk"]), \
                patch("gitweave.cli.Runtime") as runtime, patch.object(Path, "read_text", return_value="graph"), \
                patch("sys.stdout", new_callable=io.StringIO):
            runtime.return_value.run.return_value = dict(run_id="run", status="completed", repository="storage",
                                                        run_ref="ref", notes_ref="notes", outputs=[])
            self.assertEqual(main(), 0)
            runtime.assert_called_once_with("graph", "owner/repo", None, None, pr=None, issue=1,
                                            provenance_remote=None, event_sink=ANY, initialize_empty="trunk", base_branch=None)
