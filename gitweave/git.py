"""Git storage. Run-specific notes avoid cross-run read/modify/write races."""
import contextlib
import fcntl
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
        if initialize:
            if not self.repo.exists():
                # Runs share one store and may create it concurrently: initialize it
                # beside the target and rename it into place atomically; a Run that
                # loses the race discards its copy and uses the winner's.
                self.repo.parent.mkdir(parents=True, exist_ok=True)
                staging = Path(tempfile.mkdtemp(prefix=f".{self.repo.name}-", dir=self.repo.parent))
                try:
                    self.command("init", "--bare", "--quiet", cwd=staging)
                    # Automatic gc/maintenance could run while other Runs are writing.
                    self.command("config", "gc.auto", "0", cwd=staging)
                    self.command("config", "maintenance.auto", "false", cwd=staging)
                    os.rename(staging, self.repo)
                except OSError:
                    if not self.repo.exists():
                        raise
                finally:
                    shutil.rmtree(staging, ignore_errors=True)
            # Never fall through to an enclosing checkout if the store is not a repository.
            if Path(self.command("rev-parse", "--absolute-git-dir")).resolve() != self.repo:
                raise Failure("git", f"Not a GitWeave store: {self.repo}")
        common = Path(self.command("rev-parse", "--git-common-dir"))
        self.common_dir = common if common.is_absolute() else self.repo / common

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

    def fetch_input(self, remote, source, target, *, initialize_empty=None):
        """Freeze a remote input, optionally creating an empty remote's base branch."""
        if initialize_empty is not None:
            branch = initialize_empty
            if not isinstance(branch, str) or not branch or branch.startswith("-"):
                raise Failure("input", "--initialize-empty requires a valid default branch name")
            ref = f"refs/heads/{branch}"
            try:
                self.command("check-ref-format", ref)
            except Failure as exc:
                raise Failure("input", "--initialize-empty requires a valid default branch name") from exc
            # Query *all* advertised refs. A missing/broken HEAD, tags-only remote,
            # or failed transport is not evidence of an empty repository.
            if not self.command("ls-remote", "--symref", remote):
                root = self.commit(self.command("mktree", input=""), [],
                                   "Initialize empty repository for GitWeave")
                try:
                    # A root has no ancestors: an ordinary push can only create
                    # the branch or leave an identical root unchanged. It cannot
                    # fast-forward/overwrite another initializer's history.
                    self.command("push", "--no-force", "--no-follow-tags", remote, f"{root}:{ref}")
                except Failure as push_error:
                    # Another Run/user may have won, or the server may have
                    # accepted our push before the connection was interrupted.
                    # Adopt that branch only if a successful query finds it;
                    # otherwise keep the original push failure. Never retry push.
                    try:
                        exists = self.command("ls-remote", "--refs", remote, ref)
                    except Failure:
                        raise push_error
                    if not exists:
                        raise push_error
                source = ref
        self.command("fetch", "--no-tags", "--no-write-fetch-head", remote, f"{source}:{target}")
        return self.resolve(target)

    def commit(self, tree, parents, message):
        args = ["commit-tree", tree]
        for parent in dict.fromkeys(parents):
            args.extend(["-p", parent])
        return self.command(*args, input=message)

    def retain(self, commit, suffix, record):
        self._retain(commit, suffix, record)

    def _retain(self, commit, suffix, record):
        with self.lock:
            self.command("notes", f"--ref={self.notes}", "add", "-F", "-", commit,
                         input=json.dumps(record, ensure_ascii=False))
            self.command("update-ref", f"refs/gitweave/{self.run_id}/{suffix}", commit, "")

    def start_attempt(self, record):
        suffix = f"started/{record['instance_id']}/{record['attempt']}"
        base = record["workspace_base"]
        commit = self.commit(self.command("rev-parse", f"{base}^{{tree}}"), [base],
                             f"GitWeave started {self.run_id} {suffix}")
        # A separate immutable marker survives termination before a checkpoint.
        self._retain(commit, suffix, dict(record, status="running"))

    def load_run(self):
        return json.loads(self.command("show", f"refs/gitweave/{self.run_id}/run:run.json"))

    def load_attempts(self):
        prefix = f"refs/gitweave/{self.run_id}/"
        refs = self.command("for-each-ref", "--format=%(refname) %(objectname)",
                            prefix + "started/", prefix + "attempts/").splitlines()
        attempts = {}
        for line in refs:
            ref, commit = line.split()
            record = json.loads(self.command("notes", f"--ref={self.notes}", "show", commit))
            key = (record["instance_id"], record["attempt"])
            # Terminal records supersede start markers in the aggregate only.
            if ref.startswith(prefix + "attempts/") or key not in attempts:
                attempts[key] = dict(record, commit=commit)
        return sorted(attempts.values(), key=lambda a: (a["started_at"], a["instance_id"], a["attempt"]))

    def run_record(self, record):
        # Store a real file tree as well as notes so a Run needs only its ref to inspect.
        with self.lock:
            blob = self.command("hash-object", "-w", "--stdin", input=json.dumps(record, ensure_ascii=False))
            tree = self.command("mktree", input=f"100644 blob {blob}\trun.json\n")
            ref = f"refs/gitweave/{self.run_id}/run"
            previous = self.command("for-each-ref", "--format=%(objectname)", ref)
            commit = self.commit(tree, [previous or record["base_commit"]],
                                 f"GitWeave run {self.run_id}: {record['status']}")
            self.command("update-ref", f"refs/gitweave/{self.run_id}/run", commit)
            return commit

    @contextlib.contextmanager
    def run_lock(self):
        # Locking is ephemeral coordination, not scheduler state. Different Runs
        # remain concurrent; two processes must never execute the same Run.
        with open(self.common_dir / f"gitweave-run-{self.run_id}.lock", "a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Failure("resume", "Run is already executing") from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    @contextlib.contextmanager
    def worktree_lock(self):
        # Worktree metadata is shared by every Run (and process) using this
        # repository; Git does not serialize concurrent add/remove.
        with self.lock, open(self.common_dir / "gitweave-worktrees.lock", "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def add_worktree(self, path, base):
        with self.worktree_lock():
            self.command("worktree", "add", "--detach", str(path), base)

    def remove_worktree(self, path):
        with self.worktree_lock():
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
