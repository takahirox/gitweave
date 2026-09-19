"""Explicit GitHub publication and merge operations, separate from agents."""
import json
import subprocess
import threading
from .model import Failure, Result


class GitHubActions:
    def __init__(self, git, run_id):
        self.git = git
        self.run_id = run_id
        self.published = {}
        self.lock = threading.Lock()

    def gh(self, *args):
        try:
            reply = subprocess.run(["gh", *args], cwd=self.git.repo, text=True,
                                   capture_output=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Failure("github", str(exc), retryable=True) from exc
        if reply.returncode:
            raise Failure("github", reply.stderr.strip(), retryable=True)
        return reply.stdout.strip()

    def run(self, node_id, node, context):
        with self.lock:
            cfg = node["config"]
            repo = cfg["repository"]
            publisher = node_id if node["action"] == "publish_pr" else cfg["publish_node"]
            branch = f"gitweave/{self.run_id}/{publisher}"
            if node["action"] == "publish_pr":
                commit = context["workspace_base"]
                remote = f"https://github.com/{repo}.git"
                remote_ref = f"refs/heads/{branch}"
                current = self.git.command("ls-remote", remote, remote_ref).split()
                current = current[0] if current else ""
                expected = self.published.get(publisher, "")
                if current != commit:
                    if current != expected:
                        raise Failure("publication_conflict", "Managed PR branch changed externally")
                    self.git.command("-c", "credential.helper=!gh auth git-credential", "push",
                                     f"--force-with-lease={remote_ref}:{expected}", remote, f"{commit}:{remote_ref}")
                self.published[publisher] = commit
                prs = json.loads(self.gh("pr", "list", "--repo", repo, "--head", branch,
                                         "--state", "all", "--json", "number,state,url,baseRefName"))
                if prs:
                    pr = prs[0]
                    if pr["state"] != "OPEN" or pr["baseRefName"] != cfg["base"]:
                        raise Failure("publication_conflict", "Managed PR is closed or has a different base")
                    self.gh("pr", "edit", str(pr["number"]), "--repo", repo,
                            "--title", cfg["title"], "--body", cfg.get("body", ""))
                    url = pr["url"]
                else:
                    url = self.gh("pr", "create", "--repo", repo, "--head", branch,
                                  "--base", cfg["base"], "--title", cfg["title"], "--body", cfg.get("body", ""))
                return Result(message="Published managed PR", data={"url": url, "branch": branch, "commit": commit})
            expected = self.published.get(publisher)
            if not expected:
                raise Failure("approval", "Publisher has not executed in this Run")
            pr = json.loads(self.gh("pr", "view", branch, "--repo", repo, "--json",
                                   "number,state,reviewDecision,headRefOid,url,mergeCommit"))
            if pr["headRefOid"] != expected:
                raise Failure("publication_conflict", "PR head differs from the published artifact")
            if pr["state"] != "MERGED":
                if pr["state"] != "OPEN" or pr["reviewDecision"] != "APPROVED":
                    # Approval absence is a task outcome, so the graph can route or finish.
                    return Result(message="PR is not approved", data={"merged": False, "url": pr["url"]})
                self.gh("pr", "merge", str(pr["number"]), "--repo", repo,
                        "--squash", "--match-head-commit", expected)
                pr = json.loads(self.gh("pr", "view", str(pr["number"]), "--repo", repo,
                                       "--json", "state,mergeCommit,url"))
            return Result(message="PR merge checked", data={"merged": pr["state"] == "MERGED",
                          "url": pr["url"], "merge_commit": (pr.get("mergeCommit") or {}).get("oid")})
