"""Immutable, atomic archives of one Run's Git-owned refs."""
import json
import re
from pathlib import Path
from urllib.parse import urlsplit
from .model import Failure


def fail(message):
    raise Failure("persistence", message) from None


def safe_target(target):
    # Credentials belong in helpers/SSH, never in archive metadata or diagnostics.
    if not target or target.startswith("-"):
        fail("Invalid provenance destination")
    if "://" in target:
        url = urlsplit(target)
        if url.username or url.password or url.query or url.fragment:
            fail("Provenance URLs must not contain credentials, queries or fragments")
    return target


def destination(git, record, remote=None):
    graph = json.loads(record.get("graph", "{}"))
    repositories = {n["config"]["repository"] for n in graph.get("nodes", {}).values()
                    if n.get("action") == "publish_pr"}
    if record.get("input_pr"):
        repositories.add(record["input_pr"]["repository"])
    targets = {f"https://github.com/{r}.git" for r in repositories}
    if remote is None and record.get("provenance_destination"):
        remote = record["provenance_destination"]
    if remote is None and len(targets) == 1:
        return safe_target(targets.pop())
    if remote is None and len(targets) > 1:
        fail("Multiple artifact repositories require --provenance-remote selection")
    if remote is None:
        if "origin" not in git.command("remote").splitlines():
            return None
        remote = "origin"
    if remote in git.command("remote").splitlines():
        urls = git.command("remote", "get-url", "--push", "--all", remote).splitlines()
        if len(urls) != 1:
            fail("Provenance remote must have exactly one push URL")
        remote = urls[0]
    remote = safe_target(remote)
    if ":" not in remote:
        remote = str((git.repo / Path(remote)).resolve())
    if targets and remote not in targets:
        fail("Provenance destination must match a Run artifact repository (canonical HTTPS URL)")
    return remote


class Archive:
    def __init__(self, git):
        self.git = git
        if not re.fullmatch(r"[A-Za-z0-9_-]+", git.run_id):
            fail("Invalid Run ID")
        self.prefix = f"refs/gitweave/{git.run_id}/"
        self.marker = self.prefix + "archive"

    def owned(self, ref):
        return ref.startswith(self.prefix) or ref == self.git.notes

    def local(self):
        lines = self.git.command("for-each-ref", "--format=%(objectname) %(refname)",
                                 self.prefix, self.git.notes).splitlines()
        return {ref: sha for sha, ref in map(str.split, lines) if self.owned(ref)}

    def network(self, target, *args):
        safe_target(target)
        options = ["-c", "credential.helper=!gh auth git-credential"] if target.startswith("https://github.com/") else []
        try:
            return self.git.command(*options, *args)
        except Failure as exc:
            # Git stderr can contain URLs, helper output and credentials. Do not retain it.
            detail = str(exc).lower()
            reason = "destination unavailable or transport error"
            if "atomic" in detail:
                reason = "atomic push unavailable or rejected (required; no fallback)"
            elif any(word in detail for word in ("authentication", "permission denied", "could not read username", "403", "401")):
                reason = "authentication or permission denied"
            elif any(word in detail for word in ("rejected", "stale info", "hook declined")):
                reason = "remote rejected refs or a concurrent update"
            fail(f"Provenance Git transfer failed: {reason}; local evidence retained")

    def remote(self, target):
        out = self.network(target, "ls-remote", "--refs", target, self.prefix + "*", self.git.notes)
        return {ref: sha for sha, ref in map(str.split, out.splitlines()) if self.owned(ref)}

    def record(self):
        try:
            record = json.loads(self.git.command("show", self.prefix + "run:run.json"))
        except (Failure, ValueError):
            fail("Selected Run record is missing or invalid")
        if not isinstance(record, dict):
            fail("Selected Run record is invalid")
        if record.get("run_id") != self.git.run_id or record.get("status") not in ("completed", "failed"):
            fail("Only final Runs can be archived")
        return record

    def validate(self, refs):
        try:
            manifest = json.loads(self.git.command("show", refs[self.marker] + ":archive.json"))
            expected = {k: v for k, v in refs.items() if k != self.marker}
            if manifest != expected or any(not self.owned(k) for k in manifest):
                fail("Incomplete or conflicting provenance archive")
            record = json.loads(self.git.command("show", refs[self.prefix + "run"] + ":run.json"))
            if record["run_id"] != self.git.run_id or record["status"] not in ("completed", "failed"):
                fail("Archive does not contain a final Run")
            if record.get("input_pr"):
                for name, field in (("head", "head_sha"), ("base", "base_sha")):
                    if refs.get(self.prefix + "input/" + name) != record["input_pr"][field]:
                        fail("Archive is missing PR input refs")
            paths = self.git.command("ls-tree", "-r", "--name-only", refs[self.git.notes]).splitlines() if record["attempts"] else []
            noted = {path.replace("/", "") for path in paths}
            for attempt in record["attempts"]:
                ref = self.prefix + f"attempts/{attempt['instance_id']}/{attempt['attempt']}"
                if refs.get(ref) != attempt["commit"]:
                    fail("Archive is missing a recorded attempt")
                if attempt["commit"] not in noted:
                    fail("Archive is missing attempt notes")
        except (KeyError, TypeError, ValueError, Failure):
            fail("Incomplete or invalid provenance archive")

    def export(self, remote=None):
        record = self.record()
        target = destination(self.git, record, remote)
        if target is None:
            fail("No provenance destination; configure origin or pass --remote")
        with self.git.lock:
            refs = self.local()
            if self.marker not in refs:
                blob = self.git.command("hash-object", "-w", "--stdin", input=json.dumps(refs, sort_keys=True))
                tree = self.git.command("mktree", input=f"100644 blob {blob}\tarchive.json\n")
                marker = self.git.commit(tree, [], "GitWeave provenance archive")
                self.git.command("update-ref", self.marker, marker, "")
                refs[self.marker] = marker
            self.validate(refs)
            existing = self.remote(target)
            if existing == refs:
                return {"run_id": self.git.run_id, "status": "persisted", "refs": len(refs)}
            if existing:
                fail("Remote Run namespace is conflicting or partially published; no refs changed")
            leases = [f"--force-with-lease={ref}:" for ref in sorted(refs)]
            self.network(target, "push", "--atomic", "--no-follow-tags", *leases, target,
                         *[f"{sha}:{ref}" for ref, sha in sorted(refs.items())])
            if self.remote(target) != refs:
                fail("Remote archive verification failed; local evidence retained")
            return {"run_id": self.git.run_id, "status": "persisted", "refs": len(refs)}

    def recover(self, remote):
        target = destination(self.git, {}, remote)
        refs = self.remote(target)
        if self.marker not in refs:
            fail("Remote Run has no complete archive marker")
        # Fetch objects only. Install named refs together after validating the manifest.
        self.network(target, "fetch", "--no-tags", "--no-write-fetch-head", target, *sorted(set(refs.values())))
        self.validate(refs)
        with self.git.lock:
            existing = self.local()
            if any(ref not in refs or refs[ref] != sha for ref, sha in existing.items()):
                fail("Local Run namespace conflicts with archive; no refs changed")
            commands = [f"verify {ref} {sha}" if ref in existing else f"create {ref} {sha}"
                        for ref, sha in sorted(refs.items())]
            if commands:
                self.git.command("update-ref", "--stdin", input="start\n" + "\n".join(commands) + "\nprepare\ncommit\n")
        return {"run_id": self.git.run_id, "status": "recovered", "refs": len(refs)}
