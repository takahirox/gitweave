import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from gitweave.git import Git
from gitweave.model import Failure


IDENTITY = dict(GIT_AUTHOR_NAME="GitWeave", GIT_AUTHOR_EMAIL="gitweave@localhost",
                GIT_COMMITTER_NAME="GitWeave", GIT_COMMITTER_EMAIL="gitweave@localhost")


class GitInvocationTests(unittest.TestCase):
    def test_commands_have_no_implicit_timeout(self):
        with patch("gitweave.git.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 0, " ok\n", "")) as invoke:
            git = Git(Path.cwd(), "invocation")
            environment = {"CUSTOM_SETTING": "test"}
            self.assertEqual(git.command("hash-object", "--stdin", cwd=Path.cwd(),
                                         input="artifact", env=environment), "ok")
        self.assertEqual(invoke.call_count, 2)
        for invocation in invoke.call_args_list:
            self.assertNotIn("timeout", invocation.kwargs)
        invoke.assert_called_with(["git", "hash-object", "--stdin"], cwd=Path.cwd(),
                                  input="artifact", text=True, capture_output=True,
                                  env=environment)

    def test_command_failures_preserve_diagnostics(self):
        with patch("gitweave.git.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 0, "", "")) as invoke:
            git = Git(Path.cwd(), "failures")
            for failure in (OSError("git unavailable"), subprocess.TimeoutExpired("git", 7)):
                with self.subTest(failure=failure):
                    invoke.side_effect = failure
                    with self.assertRaises(Failure) as raised:
                        git.command("status")
                    self.assertEqual(raised.exception.kind, "git")
                    self.assertTrue(raised.exception.retryable)
                    self.assertEqual(str(raised.exception), str(failure))
                    self.assertIs(raised.exception.__cause__, failure)
            invoke.side_effect = None
            invoke.return_value = subprocess.CompletedProcess([], 1, "", " fatal: command failed\n")
            with self.assertRaises(Failure) as raised:
                git.command("status")
            self.assertEqual(raised.exception.kind, "git")
            self.assertTrue(raised.exception.retryable)
            self.assertEqual(str(raised.exception), "fatal: command failed")


class GitEnvironmentTests(unittest.TestCase):
    def test_command_inherits_parent_environment_without_mutating_it(self):
        keys = ("PATH", "HOME", "CUSTOM_SETTING", "SSH_AUTH_SOCK", "GIT_DIR",
                "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_SSH_COMMAND",
                "GIT_CONFIG_GLOBAL", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0",
                "GIT_CONFIG_VALUE_0", "GIT_CONFIG_PARAMETERS", "GIT_EDITOR",
                "GIT_ASKPASS", "GIT_TERMINAL_PROMPT", "GIT_FUTURE_SETTING",
                *IDENTITY)
        parent = {key: "fake-" + key for key in keys}
        with patch.dict(os.environ, parent, clear=True), \
                patch("gitweave.git.subprocess.run",
                      return_value=subprocess.CompletedProcess([], 0, "ok\n", "")) as invoke:
            git = Git(Path.cwd(), "environment")
            self.assertEqual(git.command("config", "--get", "test.value"), "ok")
            self.assertEqual(invoke.call_args.args[0], ["git", "config", "--get", "test.value"])
            for invocation in invoke.call_args_list:
                self.assertEqual(invocation.kwargs["env"], dict(parent, **IDENTITY))
            self.assertEqual(dict(os.environ), parent)


class GitRepositoryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        # Isolate tests from personal Git configuration; production inherits it.
        parent = {"PATH": os.environ["PATH"], "HOME": str(self.root),
                  "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(self.root / "config"),
                  "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+0000",
                  "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+0000"}
        environment = patch.dict(os.environ, parent, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.repo = self.root / "repo"
        self.git = Git(self.repo, "test", initialize=True)
        self.base = self.git.commit(self.git.command("mktree", input=""), [], "base")
        self.worktree = self.root / "worktree"

    def test_checkpoint_accepts_unrelated_final_head(self):
        self.git.add_worktree(self.worktree, self.base)
        unrelated = self.git.commit(self.git.command("mktree", input=""), [], "unrelated root")
        self.git.command("reset", "--hard", unrelated, cwd=self.worktree)
        (self.worktree / "artifact.txt").write_text("final artifact\n")

        commit = self.git.checkpoint(self.worktree, self.base, "checkpoint")

        self.assertEqual(self.git.command("rev-list", "--parents", "-n", "1", commit),
                         f"{commit} {unrelated}")
        self.assertEqual(self.git.command("show", f"{commit}:artifact.txt"), "final artifact")
        self.assertNotIn(self.base, self.git.command("rev-list", commit).splitlines())
        record = {"status": "completed", "workspace_base": self.base, "output_commit": commit}
        self.git.retain(commit, "attempts/test/1", record)
        self.assertEqual(self.git.command("rev-parse", "refs/gitweave/test/attempts/test/1"), commit)
        self.assertEqual(json.loads(self.git.command("notes", f"--ref={self.git.notes}", "show", commit)),
                         record)
        self.assertEqual(self.git.command("rev-parse", "HEAD", cwd=self.worktree), unrelated)

    def test_checkpoint_captures_files_with_unresolved_merge_index(self):
        self.git.add_worktree(self.worktree, self.base)
        artifact = self.worktree / "artifact.txt"
        removed = self.worktree / "removed.txt"
        artifact.write_text("base\n")
        removed.write_text("remove me\n")
        self.git.command("add", ".", cwd=self.worktree)
        self.git.command("commit", "-m", "base files", cwd=self.worktree)
        base = self.git.command("rev-parse", "HEAD", cwd=self.worktree)
        artifact.write_text("other side\n")
        self.git.command("commit", "-am", "other", cwd=self.worktree)
        other = self.git.command("rev-parse", "HEAD", cwd=self.worktree)
        self.git.command("reset", "--hard", base, cwd=self.worktree)
        artifact.write_text("agent side\n")
        self.git.command("commit", "-am", "agent", cwd=self.worktree)
        head = self.git.command("rev-parse", "HEAD", cwd=self.worktree)
        with self.assertRaises(Failure):
            self.git.command("merge", other, cwd=self.worktree)
        self.assertTrue(self.git.command("ls-files", "--unmerged", cwd=self.worktree))
        index = Path(self.git.command("rev-parse", "--git-path", "index", cwd=self.worktree))
        merge_head = Path(self.git.command("rev-parse", "--git-path", "MERGE_HEAD", cwd=self.worktree))
        index_before = index.read_bytes()
        merge_before = merge_head.read_bytes()
        conflicted_contents = artifact.read_text()
        self.assertIn("<<<<<<<", conflicted_contents)
        removed.unlink()
        (self.worktree / "new.txt").write_text("untracked\n")

        for contents in (conflicted_contents, "resolved on disk without staging\n"):
            with self.subTest(contents=contents):
                artifact.write_text(contents)
                commit = self.git.checkpoint(self.worktree, base, "checkpoint")
                self.assertEqual(self.git.command("show", f"{commit}:artifact.txt"), contents.strip())
                self.assertEqual(self.git.command("show", f"{commit}:new.txt"), "untracked")
                self.assertEqual(self.git.command("ls-tree", "--name-only", commit).splitlines(),
                                 ["artifact.txt", "new.txt"])
                self.assertEqual(self.git.command("rev-list", "--parents", "-n", "1", commit),
                                 f"{commit} {head} {other}")
                self.assertEqual(self.git.command("rev-parse", "HEAD", cwd=self.worktree), head)
                self.assertEqual(index.read_bytes(), index_before)
                self.assertEqual(merge_head.read_bytes(), merge_before)

    def test_checkpoint_requires_assigned_worktree_root(self):
        self.git.add_worktree(self.worktree, self.base)
        nested = self.worktree / "nested"
        nested.mkdir()
        with self.assertRaises(Failure) as raised:
            self.git.checkpoint(nested, self.base, "checkpoint")
        self.assertEqual(raised.exception.kind, "workspace")

    def test_inherited_configuration_and_local_checkout_hook(self):
        (self.root / "config").write_text('[test]\n    value = fake-global\n')
        hooks = self.repo / "local-hooks"
        hooks.mkdir()
        hook = hooks / "post-checkout"
        hook.write_text('#!/bin/sh\nprintf "harmless checkout\\n" > hook-ran.txt\n')
        hook.chmod(0o755)
        self.git.command("config", "core.hooksPath", str(hooks))
        with patch.dict(os.environ, GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="test.injected",
                        GIT_CONFIG_VALUE_0="fake-inherited"):
            parent = dict(os.environ)
            git = Git(self.repo, "inherited")
            self.assertEqual(git.command("config", "--get", "test.value"), "fake-global")
            self.assertEqual(git.command("config", "--get", "test.injected"), "fake-inherited")
            self.assertEqual(git.command("config", "--get", "core.hooksPath"), str(hooks))
            git.add_worktree(self.worktree, self.base)
            self.assertEqual((self.worktree / "hook-ran.txt").read_text(), "harmless checkout\n")
            self.assertEqual(dict(os.environ), parent)

    def test_checkpoint_keeps_private_index_identity_and_provenance(self):
        self.git.add_worktree(self.worktree, self.base)
        artifact = self.worktree / "artifact.txt"
        artifact.write_text("agent commit\n")
        self.git.command("add", ".", cwd=self.worktree)
        self.git.command("commit", "-m", "agent", cwd=self.worktree)
        head = self.git.command("rev-parse", "HEAD", cwd=self.worktree)
        artifact.write_text("staged\n")
        self.git.command("add", ".", cwd=self.worktree)
        index = Path(self.git.command("rev-parse", "--git-path", "index", cwd=self.worktree))
        if not index.is_absolute():
            index = self.worktree / index
        index_before = index.read_bytes()
        inherited_index = self.root / "inherited-index"
        inherited_index.write_bytes(index_before)
        artifact.write_text("final unstaged\n")
        (self.worktree / "new.txt").write_text("untracked\n")
        with patch.dict(os.environ, GIT_INDEX_FILE=str(inherited_index),
                        **{key: "fake-parent" for key in IDENTITY}):
            parent = dict(os.environ)
            git = Git(self.repo, "checkpoint")
            commit = git.checkpoint(self.worktree, self.base, "checkpoint")
            self.assertEqual(git.command("rev-parse", f"{commit}^"), head)
            self.assertEqual(git.command("show", f"{commit}:artifact.txt"), "final unstaged")
            self.assertEqual(git.command("show", f"{commit}:new.txt"), "untracked")
            self.assertEqual(git.command("show", "-s", "--format=%an <%ae>|%cn <%ce>", commit),
                             "GitWeave <gitweave@localhost>|GitWeave <gitweave@localhost>")
            git.retain(commit, "attempts/test/1", {"status": "completed"})
            self.assertEqual(json.loads(git.command("notes", f"--ref={git.notes}", "show", commit)),
                             {"status": "completed"})
            self.assertEqual(git.command("rev-parse", "refs/gitweave/checkpoint/attempts/test/1"), commit)
            self.assertEqual(git.command("rev-parse", "HEAD", cwd=self.worktree), head)
            self.assertEqual(index.read_bytes(), index_before)
            self.assertEqual(inherited_index.read_bytes(), index_before)
            self.assertEqual(git.env["GIT_INDEX_FILE"], str(inherited_index))
            self.assertEqual(dict(os.environ), parent)


WORKTREE_CHURN = """
import sys, tempfile
from pathlib import Path
from gitweave.git import Git
store, base, label = sys.argv[1:4]
git = Git(store, label, initialize=True)
for _ in range(16):
    with tempfile.TemporaryDirectory() as temp:
        # The same basename in every process stresses Git's shared worktree metadata.
        path = Path(temp) / "workspace"
        git.add_worktree(path, base)
        git.remove_worktree(path)
"""


class SharedStoreProcessTests(unittest.TestCase):
    def test_worktree_add_and_remove_are_safe_across_processes(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Path(temp) / "repos" / "owner" / "repo.git"
            seed = Path(temp) / "seed"
            subprocess.run(["git", "init", "-q", str(seed)], check=True)
            subprocess.run(["git", "-C", str(seed), "-c", "user.name=T", "-c", "user.email=t@localhost",
                            "commit", "--allow-empty", "-qm", "base"], check=True)
            base = subprocess.check_output(["git", "-C", str(seed), "rev-parse", "HEAD"], text=True).strip()
            Git(store, "seed", initialize=True).command("fetch", "-q", str(seed), f"{base}:refs/seed")
            env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent))
            processes = [subprocess.Popen([sys.executable, "-c", WORKTREE_CHURN, str(store), base, f"p{i}"],
                                          env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                         for i in range(8)]
            failures = [p.communicate(timeout=120)[1] for p in processes if p.wait(timeout=120)]
            self.assertEqual(failures, [])
            listed = subprocess.check_output(["git", "-C", str(store), "worktree", "list"], text=True)
            self.assertEqual(len(listed.splitlines()), 1)
