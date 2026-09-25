"""Git storage. Run-specific notes avoid cross-run read/modify/write races."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
from .model import Failure


class Git:
    def __init__(self, repo, run_id, *, initialize=False):
        self.repo = Path(repo).resolve()
        self.run_id = run_id
        self.notes = f"refs/notes/gitweave/{run_id}"
        self.lock = threading.RLock()
        self.env = dict(os.environ)
        self.env.update(GIT_AUTHOR_NAME="GitWeave", GIT_AUTHOR_EMAIL="gitweave@localhost",
                        GIT_COMMITTER_NAME="GitWeave", GIT_COMMITTER_EMAIL="gitweave@localhost")
        if initialize and not self.repo.exists():
            # Runs share one store and may create it concurrently: initialize it
            # beside the target and rename it into place atomically; a Run that
            # loses the race discards its copy and uses the winner's.
            self.repo.parent.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=f".{self.repo.name}-", dir=self.repo.parent))
            try:
                self.command("init", "--bare", "--quiet", cwd=staging)
                os.rename(staging, self.repo)
            except OSError:
                if not self.repo.exists():
                    raise
            finally:
                shutil.rmtree(staging, ignore_errors=True)
        self.command("rev-parse", "--git-common-dir")

    def command(self, *args, cwd=None, input=None, env=None):
        try:
            result = subprocess.run(["git", *args],
                                    cwd=cwd or self.repo, input=input, text=True,
                                    capture_output=True, env=env or self.env)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Failure("git", str(exc), retryable=True) from exc
        if result.returncode:
            raise Failure("git", result.stderr.strip(), retryable=True)
        return result.stdout.strip()

    def resolve(self, commit):
        return self.command("rev-parse", "--verify", "--end-of-options", f"{commit}^{{commit}}")

    def commit(self, tree, parents, message):
        args = ["commit-tree", tree]
        for parent in dict.fromkeys(parents):
            args.extend(["-p", parent])
        return self.command(*args, input=message)

    def retain(self, commit, suffix, record):
        with self.lock:
            self.command("update-ref", f"refs/gitweave/{self.run_id}/{suffix}", commit)
            self.command("notes", f"--ref={self.notes}", "add", "-f", "-F", "-", commit,
                         input=json.dumps(record, ensure_ascii=False))

    def run_record(self, record):
        # Store a real file tree as well as notes so a Run needs only its ref to inspect.
        with self.lock:
            blob = self.command("hash-object", "-w", "--stdin", input=json.dumps(record, ensure_ascii=False))
            tree = self.command("mktree", input=f"100644 blob {blob}\trun.json\n")
            commit = self.commit(tree, [], f"GitWeave run {self.run_id}: {record['status']}")
            self.command("update-ref", f"refs/gitweave/{self.run_id}/run", commit)
            return commit

    def add_worktree(self, path, base):
        with self.lock:
            self.command("worktree", "add", "--detach", str(path), base)

    def remove_worktree(self, path):
        with self.lock:
            self.command("worktree", "remove", "--force", str(path))

    def checkpoint(self, path, base, message):
        if Path(self.command("rev-parse", "--show-toplevel", cwd=path)).resolve() != Path(path).resolve():
            raise Failure("workspace", "Assigned worktree is no longer valid")
        head = self.command("rev-parse", "HEAD", cwd=path)
        # A private index checkpoints final files without changing the agent's index.
        with tempfile.TemporaryDirectory(prefix="gitweave-index-") as temp:
            env = dict(self.env, GIT_INDEX_FILE=str(Path(temp) / "index"))
            self.command("read-tree", head, cwd=path, env=env)
            self.command("add", "-A", "--", ".", cwd=path, env=env)
            tree = self.command("write-tree", cwd=path, env=env)
        parents = [head]
        merge_file = Path(self.command("rev-parse", "--git-path", "MERGE_HEAD", cwd=path))
        if not merge_file.is_absolute():
            merge_file = Path(path) / merge_file
        if merge_file.exists():
            parents.extend(merge_file.read_text().splitlines())
        return self.commit(tree, parents, message)

    def empty(self, base, message):
        return self.commit(self.command("rev-parse", f"{base}^{{tree}}"), [base], message)
