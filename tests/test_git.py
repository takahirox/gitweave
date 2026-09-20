import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from gitweave.git import Git


IDENTITY = dict(GIT_AUTHOR_NAME="GitWeave", GIT_AUTHOR_EMAIL="gitweave@localhost",
                GIT_COMMITTER_NAME="GitWeave", GIT_COMMITTER_EMAIL="gitweave@localhost")


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
