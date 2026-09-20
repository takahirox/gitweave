"""Explicit GitHub publication, comment and merge operations, separate from agents."""
import json
import os
import re
import subprocess
import threading
from .graph import validate_comment_config
from .model import Failure, Result, pointer


class GitHubActions:
    def __init__(self, git, run_id):
        self.git = git
        self.run_id = run_id
        self.published = {}
        self.publish_bases = {}
        self.input_pr = None
        self.remote_sha = None
        self.lock = threading.Lock()

    def gh(self, *args):
        try:
            reply = subprocess.run(["gh", *args], cwd=self.git.repo, text=True,
                                   capture_output=True,
                                   env=dict(os.environ, GH_HOST="github.com"))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Failure("github", str(exc), retryable=True) from exc
        if reply.returncode:
            raise Failure("github", reply.stderr.strip(), retryable=True)
        return reply.stdout.strip()

    def read_pr(self, repository, number):
        raw = json.loads(self.gh("api", f"repos/{repository}/pulls/{number}"))
        head, base = raw["head"], raw["base"]
        return {"number": raw["number"], "repository": base["repo"]["full_name"],
                "repository_id": base["repo"]["id"],
                "head_repository": (head.get("repo") or {}).get("full_name"),
                "head_repository_id": (head.get("repo") or {}).get("id"),
                "head_branch": head["ref"], "head_sha": head["sha"],
                "base_branch": base["ref"], "base_sha": base["sha"],
                "state": "MERGED" if raw["merged"] else raw["state"].upper(),
                "url": raw["html_url"], "merge_commit": raw.get("merge_commit_sha")}

    def resolve_input(self, repository, number):
        pr = self.read_pr(repository, number)
        if pr["state"] != "OPEN" or pr["number"] != number:
            raise Failure("pr_input", "Input PR must be open and match the requested number")
        if pr["repository"].lower() != repository.lower():
            raise Failure("pr_input", "Input repository identity changed")
        for key in ("head_sha", "base_sha"):
            if not re.fullmatch(r"[0-9a-f]{40}", pr[key]):
                raise Failure("pr_input", "GitHub returned an invalid commit SHA")
        remote = f"https://github.com/{pr['repository']}.git"
        # Fetch GitHub's PR head, never its synthetic test-merge commit.
        self.git.command("fetch", "--no-tags",
                         remote, f"refs/pull/{number}/head")
        pr["head_sha"] = self.git.resolve("FETCH_HEAD")
        self.git.command("update-ref", f"refs/gitweave/{self.run_id}/input/head", pr["head_sha"])
        self.git.command("fetch", "--no-tags",
                         remote, pr["base_sha"])
        pr["base_sha"] = self.git.resolve("FETCH_HEAD")
        self.git.command("update-ref", f"refs/gitweave/{self.run_id}/input/base", pr["base_sha"])
        self.input_pr = pr
        self.remote_sha = pr["head_sha"]
        return dict(pr)

    def check_input(self, *, allow_merged=False):
        if self.input_pr is None:
            raise Failure("pr_input", "This action requires an existing-PR Run input")
        original = self.input_pr
        pr = self.read_pr(original["repository"], original["number"])
        identity = ("number", "repository", "repository_id", "head_repository",
                    "head_repository_id", "head_branch", "base_branch")
        if any(pr[key] != original[key] for key in identity):
            raise Failure("publication_conflict", "PR identity, head branch, or base branch changed")
        if pr["state"] != "OPEN" and not (allow_merged and pr["state"] == "MERGED"):
            raise Failure("publication_conflict", "Input PR is closed")
        return pr

    def sync_input(self, context):
        pr = self.check_input()
        if not pr["head_repository"]:
            raise Failure("pr_input", "Input PR head repository is unavailable; cannot identify push target")
        commit = context["workspace_base"]
        expected = self.remote_sha
        remote = f"https://github.com/{pr['head_repository']}.git"
        ref = f"refs/heads/{pr['head_branch']}"
        self.git.command("check-ref-format", ref)
        self.git.command("push",
                         f"--force-with-lease={ref}:{expected}", remote, f"{commit}:{ref}")
        self.remote_sha = commit
        return Result(message="Synchronized input PR", data={"url": pr["url"],
                      "branch": pr["head_branch"], "commit": commit})

    def merge_exact(self, repository, number, expected, pr):
        if pr["state"] == "MERGED":
            return Result(message="PR already merged", data={"merged": True, "url": pr["url"],
                          "merge_commit": pr.get("merge_commit")})
        if pr["state"] != "OPEN":
            raise Failure("publication_conflict", "PR is closed")
        # REST merges immediately or rejects; unlike `gh pr merge`, it cannot queue.
        reply = json.loads(self.gh("api", "--method", "PUT",
                                  f"repos/{repository}/pulls/{number}/merge",
                                  "-f", f"sha={expected}", "-f", "merge_method=merge"))
        if reply.get("merged") is not True:
            raise Failure("merge_policy", reply.get("message", "GitHub rejected the merge"))
        return Result(message="PR merged", data={"merged": True, "url": pr["url"],
                      "merge_commit": reply.get("sha")})

    def check_artifact(self, context, expected):
        actual_tree = self.git.command("rev-parse", f"{context['workspace_base']}^{{tree}}")
        if actual_tree != self.git.command("rev-parse", f"{expected}^{{tree}}"):
            raise Failure("publication_conflict", "Selected artifact has unpublished changes; synchronize before merge")

    def comment(self, node, context):
        cfg = node.get("config", {})
        validate_comment_config(cfg)
        body = cfg["body"] if "body" in cfg else pointer(context.get("inputs", []), cfg["body_path"])
        if not isinstance(body, str) or not body.strip():
            raise Failure("result", "Comment body must be nonblank text")
        repo, number = cfg["repository"], cfg["number"]
        endpoint = f"repos/{repo}/issues/{number}"

        def read(*args):
            try:
                return json.loads(self.gh("api", *args))
            except json.JSONDecodeError as exc:
                raise Failure("github", "Invalid GitHub comment response", retryable=True) from exc

        def result(comment):
            if (not isinstance(comment, dict) or type(comment.get("id")) is not int
                    or comment["id"] <= 0 or not isinstance(comment.get("html_url"), str)
                    or not comment["html_url"]):
                raise Failure("github", "Missing GitHub comment ID/URL", retryable=True)
            return Result(message="Posted GitHub comment", data={"id": comment["id"],
                          "url": comment["html_url"], "repository": repo, "number": number})

        target = read(endpoint)
        if (not isinstance(target, dict) or type(target.get("number")) is not int
                or target["number"] != number
                or ("pull_request" in target) != (node["action"] == "comment_pr")):
            raise Failure("comment_target", "GitHub target does not match the requested Issue/PR")
        return result(read("--method", "POST", endpoint + "/comments", "-f", f"body={body}"))

    def run(self, node_id, node, context):
        with self.lock:
            cfg = node.get("config", {})
            if node["action"] in ("comment_issue", "comment_pr"):
                return self.comment(node, context)
            if node["action"] == "sync_pr":
                return self.sync_input(context)
            if node["action"] == "merge_pr" and "publish_node" not in cfg:
                pr = self.check_input(allow_merged=True)
                if pr["head_sha"] != self.remote_sha:
                    raise Failure("publication_conflict", "PR head differs from the known remote artifact")
                self.check_artifact(context, self.remote_sha)
                return self.merge_exact(pr["repository"], pr["number"], self.remote_sha, pr)
            repo = cfg["repository"]
            publisher = node_id if node["action"] == "publish_pr" else cfg["publish_node"]
            branch = f"gitweave/{self.run_id}/{publisher}"
            if node["action"] == "publish_pr":
                commit = context["workspace_base"]
                remote = f"https://github.com/{repo}.git"
                remote_ref = f"refs/heads/{branch}"
                # GitWeave owns this branch; force only the selected managed ref.
                self.git.command("push", "--force", remote, f"{commit}:{remote_ref}")
                self.published[publisher] = commit
                self.publish_bases[publisher] = cfg["base"]
                prs = json.loads(self.gh("pr", "list", "--repo", repo, "--head", branch,
                                         "--state", "all", "--json", "number,url"))
                if prs:
                    pr = prs[0]
                    self.gh("pr", "edit", str(pr["number"]), "--repo", repo,
                            "--base", cfg["base"], "--title", cfg["title"], "--body", cfg.get("body", ""))
                    url = pr["url"]
                else:
                    url = self.gh("pr", "create", "--repo", repo, "--head", branch,
                                  "--base", cfg["base"], "--title", cfg["title"], "--body", cfg.get("body", ""))
                return Result(message="Published managed PR", data={"url": url, "branch": branch, "commit": commit})
            expected = self.published.get(publisher)
            if not expected:
                raise Failure("approval", "Publisher has not executed in this Run")
            pr = json.loads(self.gh("pr", "view", branch, "--repo", repo, "--json",
                                   "number,state,headRefOid,url,mergeCommit,baseRefName"))
            if pr["headRefOid"] != expected:
                raise Failure("publication_conflict", "PR head differs from the published artifact")
            if pr["baseRefName"] != self.publish_bases[publisher]:
                raise Failure("publication_conflict", "Managed PR base changed")
            self.check_artifact(context, expected)
            pr["merge_commit"] = (pr.get("mergeCommit") or {}).get("oid")
            return self.merge_exact(repo, pr["number"], expected, pr)
