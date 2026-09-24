"""Persist one Run's refs and notes with ordinary Git push semantics."""
from .model import Failure


def destination(git, record, remote=None):
    if remote is not None:
        return remote
    repositories = set(record.get("publication_repositories", []))
    if record.get("github_repository"):
        repositories.add(record["github_repository"])
    if len(repositories) > 1:
        raise Failure("persistence", "Multiple artifact repositories require --provenance-remote selection")
    if repositories:
        return f"https://github.com/{repositories.pop()}.git"
    return "origin" if "origin" in git.command("remote").splitlines() else None


def persist(git, target):
    prefix = f"refs/gitweave/{git.run_id}/"
    refs = git.command("for-each-ref", "--format=%(refname)", prefix, git.notes).splitlines()
    # Notes may not exist when execution fails before its first attempt.
    refs = [ref for ref in refs if ref.startswith(prefix) or ref == git.notes]
    try:
        git.command("push", "--no-follow-tags", "--", target,
                    *[f"{ref}:{ref}" for ref in refs])
    except Failure as exc:
        raise Failure("persistence", f"Provenance push failed; local refs and notes retained for Git inspection and retry\n{exc}") from exc
