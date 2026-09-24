"""The shipped example graphs, executed with scripted agents and a fake gh; no network or live agents."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gitweave.git import Git
from gitweave.model import Result
from gitweave.runtime import Runtime
from test_runtime import git

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


class Scripted:
    """Dispatches each Agent Node invocation to a per-node function by declared node."""
    def __init__(self, nodes, handlers, calls):
        self.nodes, self.handlers, self.calls = nodes, handlers, calls

    def run(self, n, context, workspace, timeout):
        name = next(key for key, value in self.nodes.items() if value == n)
        self.calls.append(name)
        return self.handlers[name](context, workspace)


class ExampleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote"
        self.remote.mkdir()
        git(self.remote, "init", "-q")
        git(self.remote, "config", "maintenance.autoDetach", "false")
        git(self.remote, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "--allow-empty", "-qm", "base")
        git(self.remote, "branch", "-M", "main")
        self.base = git(self.remote, "rev-parse", "HEAD")
        git(self.remote, "update-ref", "refs/pull/42/head", self.base)
        original = Git.command
        def command(instance, *args, **kwargs):
            args = tuple(str(self.remote) if a == "https://github.com/owner/repo.git" else a for a in args)
            return original(instance, *args, **kwargs)
        self.addCleanup(patch.stopall)
        patch.object(Git, "command", command).start()
        patch.object(Path, "cwd", return_value=self.root).start()

    def run_example(self, name, handlers, **source):
        text = (EXAMPLES / name).read_text()
        calls = []
        adapter = Scripted(json.loads(text)["nodes"], handlers, calls)
        run = Runtime(text, "owner/repo", None, "request", adapters={"codex": adapter, "claude": adapter}, **source)
        record = run.run()
        self.assertEqual(record["status"], "completed", record.get("failure"))
        return run, record, calls

    def notes(self, run, record):
        return [json.loads(git(run.git.repo, "notes", "--ref=" + record["notes_ref"], "show", a["commit"]))
                for a in record["attempts"]]

    def test_issue_to_merge_flow_is_expressed_with_agent_nodes_and_audited(self):
        pr = {"number": 42, "url": "https://github.com/owner/repo/pull/42", "head_sha": ""}
        reviews = []
        def implement(c, w):
            self.assertEqual(c["run_input"], {"kind": "issue", "number": 7})
            (w / "feature").write_text("draft")
            return Result(message="implemented")
        def publish(c, w):
            previous = c["inputs"][0]["data"]
            self.assertEqual(previous and previous["pr"]["number"], None if not reviews else 42)
            return Result(data={"pr": dict(pr, head_sha=git(w, "rev-parse", "HEAD"))})
        def review(c, w):
            reviews.append(c["inputs"][0]["data"]["pr"])
            ok = (w / "feature").read_text() == "done"
            return Result(data={"pr": c["inputs"][0]["data"]["pr"], "approved": ok, "findings": [] if ok else ["finish it"]})
        def fix(c, w):
            self.assertEqual(c["inputs"][0]["data"]["findings"], ["finish it"])
            (w / "feature").write_text("done")
            return Result(data={"pr": c["inputs"][0]["data"]["pr"], "summary": "finished"})
        def merge(c, w):
            self.assertTrue(c["inputs"][0]["data"]["approved"])
            return Result(data={"pr": c["inputs"][0]["data"]["pr"], "merged": True, "merge_commit": "m" * 40})
        def close(c, w):
            self.assertTrue(c["inputs"][0]["data"]["merged"])
            self.assertEqual(c["run_input"], {"kind": "issue", "number": 7})
            return Result(data={"closed": True})
        run, record, calls = self.run_example("issue-to-merge.json", {
            "implement": implement, "publish": publish, "review": review, "fix": fix,
            "merge": merge, "close_issue": close}, issue=7)
        self.assertEqual(calls, ["implement", "publish", "review", "fix", "publish", "review", "merge", "close_issue"])
        # PR identity is forwarded explicitly; the second publication reports the fixed head.
        self.assertNotEqual(reviews[0]["head_sha"], reviews[1]["head_sha"])
        self.assertEqual(record["outputs"][0]["data"], {"closed": True})
        # Every invocation has its own checkpoint commit and note, including same-tree nodes.
        notes = self.notes(run, record)
        self.assertEqual([n["node_id"] for n in notes], calls)
        commits = [a["commit"] for a in record["attempts"]]
        self.assertEqual(len(set(commits)), len(commits))
        for note, commit in zip(notes, commits):
            same_tree = git(run.git.repo, "rev-parse", commit + "^{tree}") == git(run.git.repo, "rev-parse", note["workspace_base"] + "^{tree}")
            self.assertEqual(same_tree, note["node_id"] not in ("implement", "fix"), note["node_id"])
        self.assertEqual(git(run.git.repo, "show", record["outputs"][0]["commit"] + ":feature"), "done")

    def test_review_fix_merge_example_for_an_existing_pr(self):
        def review(c, w):
            self.assertEqual(c["run_input"], {"kind": "pull_request", "number": 42})
            ok = (w / "artifact").exists()
            return Result(data={"approved": ok, "findings": [] if ok else ["add artifact"]})
        def fix(c, w):
            (w / "artifact").write_text("fixed")
            return Result(message="fixed")
        run, record, calls = self.run_example("review-fix-merge.json", {
            "review": review, "fix": fix, "push": lambda c, w: Result(message="pushed"),
            "merge": lambda c, w: Result(data={"merged": True, "merge_commit": "m" * 40})}, pr=42)
        self.assertEqual(calls, ["review", "fix", "push", "review", "merge"])
        self.assertTrue(record["outputs"][0]["data"]["merged"])

    def test_review_comment_example_posts_through_its_command_node(self):
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        log = self.root / "gh.json"
        (bin_dir / "gh").write_text(f"""#!{__import__("sys").executable}
import json, sys
json.dump({{"args": sys.argv[1:], "body": sys.stdin.read()}}, open({str(log)!r}, "w"))
print("https://github.com/owner/repo/pull/42#issuecomment-1")
""")
        (bin_dir / "gh").chmod(0o755)
        with patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}):
            run, record, calls = self.run_example("review-comment.json", {
                "review": lambda c, w: Result(message="Looks good except X")}, pr=42)
        self.assertEqual(calls, ["review"])
        posted = json.loads(log.read_text())
        self.assertEqual(posted["args"], ["pr", "comment", "42", "--repo", "owner/repo", "--body-file", "-"])
        self.assertEqual(posted["body"], "Looks good except X")
        self.assertEqual(record["outputs"][0]["data"], {"url": "https://github.com/owner/repo/pull/42#issuecomment-1"})
        self.assertEqual(self.notes(run, record)[-1]["kind"], "command")
