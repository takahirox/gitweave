import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from gitweave.runtime import Runtime
from gitweave.model import Failure, Result


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def node(instruction="work", **options):
    return dict(kind="agent", provider="fake", instruction=instruction, workspace_base=0, **options)


def graph(nodes, flow, **options):
    return json.dumps(dict(version=1, nodes=nodes, flow=flow, **options))


class Fake:
    def __init__(self, fn):
        self.fn = fn

    def run(self, node, context, workspace, timeout):
        return self.fn(node, context, workspace)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "--allow-empty", "-qm", "base")
        self.base = git(self.repo, "rev-parse", "HEAD")

    def runtime(self, nodes, flow, fn, **options):
        return Runtime(graph(nodes, flow, **options), self.repo, self.base, "request", adapters={"fake": Fake(fn)})

    def note(self, run, commit):
        return json.loads(git(self.repo, "notes", f"--ref={run.git.notes}", "show", commit))

    def test_sequential_artifacts_results_and_run_record(self):
        def work(n, c, w):
            if n["instruction"] == "first":
                (w / "artifact.txt").write_text("hello")
                return Result(message="plan", data={"ok": True}, usage={"input_tokens": 10})
            self.assertEqual((w / "artifact.txt").read_text(), "hello")
            self.assertEqual(c["inputs"][0]["message"], "plan")
            return Result(message="finished")
        run = self.runtime({"a": node("first"), "b": node()}, ["a", "b"], work)
        record = run.run()
        self.assertEqual(record["status"], "completed")
        a, b = [x["commit"] for x in record["attempts"]]
        self.assertEqual(git(self.repo, "rev-parse", f"{b}^"), a)
        self.assertEqual(git(self.repo, "rev-parse", f"{a}^{{tree}}"), git(self.repo, "rev-parse", f"{b}^{{tree}}"))
        self.assertEqual(self.note(run, a)["result"]["usage"]["input_tokens"], 10)
        stored = json.loads(git(self.repo, "show", f"{record['run_ref']}:run.json"))
        self.assertEqual(stored["graph"], run.record["graph"])
        self.assertEqual(stored["status"], "completed")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.base)
        self.assertFalse((self.repo / "artifact.txt").exists())
        self.assertEqual(len(git(self.repo, "worktree", "list").splitlines()), 1)

    def test_parallel_isolation_join_and_input_order(self):
        barrier = threading.Barrier(2)
        def work(n, c, w):
            name = n["instruction"]
            if name in ("left", "right"):
                self.assertFalse((w / "file").exists())
                (w / "file").write_text(name)
                barrier.wait(timeout=5)
                return Result(message=name)
            self.assertEqual([i["message"] for i in c["inputs"]], ["left", "right"])
            self.assertEqual((w / "file").read_text(), "right")
            self.assertEqual(git(w, "show", c["inputs"][0]["commit"] + ":file"), "left")
            (w / "file").write_text("integrated")
            return Result(message="joined")
        join = node("join")
        join["workspace_base"] = 1
        run = self.runtime({"a": node("left"), "b": node("right"), "j": join},
                           [{"parallel": [["a"], ["b"]]}, "j"], work, concurrency=2)
        self.assertEqual(run.run()["status"], "completed")

    def test_map_loop_and_conditional(self):
        def work(n, c, w):
            if n["instruction"] == "plan":
                return Result(data={"tasks": ["a", "b", "c"]})
            if n["instruction"] == "worker":
                return Result(message=c["item"], data={"repeat": False})
            self.assertEqual(len(c["inputs"]), 3)
            return Result(data={"approved": True})
        schema = {"type": "object", "properties": {"tasks": {"type": "array", "items": {"type": "string"}}}, "required": ["tasks"]}
        flow = ["plan", {"map": {"path": "/0/data/tasks", "flow": [{"loop": {"flow": ["worker"], "while": {"path": "/0/data/repeat", "equals": True}}}]}},
                "join", {"if": {"condition": {"path": "/0/data/approved", "equals": True}, "then": ["plan"], "else": ["bad"]}}]
        run = self.runtime({"plan": node("plan", schema=schema), "worker": node("worker", schema={"type": "object"}), "join": node("join", schema={"type": "object"}), "bad": node("bad")}, flow, work)
        record = run.run()
        self.assertEqual(record["status"], "completed", record.get("failure"))
        attempts = [self.note(run, a["commit"]) for a in record["attempts"]]
        workers = [a for a in attempts if a["node_id"] == "worker"]
        self.assertEqual(len({a["instance_id"] for a in workers}), 3)
        self.assertEqual({a["fan_out_origin"]["index"] for a in workers}, {0, 1, 2})
        self.assertEqual(len({a["workspace_base"] for a in workers}), 1)
        self.assertFalse(any(a["node_id"] == "bad" for a in attempts))

    def test_retry_is_from_original_input_and_failure_is_retained(self):
        calls = []
        def work(n, c, w):
            self.assertFalse((w / "bad").exists())
            calls.append(w)
            if len(calls) == 1:
                (w / "bad").write_text("partial")
                raise Failure("provider", "transient", retryable=True, result=Result(raw_stdout="log", session_id="session"))
            return Result(message="ok")
        run = self.runtime({"a": node()}, ["a"], work, retries=1)
        record = run.run()
        self.assertEqual(record["status"], "completed")
        failed, success = [a["commit"] for a in record["attempts"]]
        for sha in (failed, success):
            self.assertEqual(git(self.repo, "rev-parse", f"{sha}^"), self.base)
        self.assertNotEqual(failed, success)
        self.assertEqual(self.note(run, failed)["result"]["session_id"], "session")
        self.assertIn(failed, git(self.repo, "show-ref"))
        self.assertEqual(git(self.repo, "rev-list", success).splitlines(), [success, self.base])

    def test_limit_and_invalid_result_do_not_silently_retry(self):
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
        run = self.runtime({"a": node(schema=schema)}, ["a"], lambda *args: Result(data={"ok": "yes"}), retries=3)
        record = run.run()
        self.assertEqual(record["failure"]["kind"], "result")
        self.assertEqual(len(record["attempts"]), 1)
        run = self.runtime({"a": node(schema={"type": "boolean"})}, [{"loop": {"flow": ["a"], "while": {"path": "/0/data", "equals": True}}}], lambda *args: Result(data=True), max_steps=5)
        record = run.run()
        self.assertEqual(record["failure"]["kind"], "step_limit")
        self.assertLessEqual(record["steps"], 5)

    def test_usage_limit_never_retries(self):
        def limited(*args):
            raise Failure("usage_limit", "limit", retryable=False)
        run = self.runtime({"a": node()}, ["a"], limited, retries=3)
        record = run.run()
        self.assertEqual(len(record["attempts"]), 1)
        self.assertEqual(record["failure"]["kind"], "usage_limit")

    def test_empty_map_preserves_upstream(self):
        def work(n, c, w):
            if n["instruction"] == "plan":
                return Result(data=[])
            self.assertEqual(c["inputs"][0]["data"], [])
            return Result(message="joined")
        run = self.runtime({"plan": node("plan", schema={"type": "array"}), "worker": node(), "join": node()},
                           ["plan", {"map": {"path": "/0/data", "flow": ["worker"]}}, "join"], work)
        self.assertEqual(len(run.run()["attempts"]), 2)

    def test_system_action_retry_and_result_handoff(self):
        class Actions:
            count = 0
            def run(self, name, node, context):
                self.count += 1
                if self.count == 1:
                    raise Failure("github", "transient", retryable=True)
                return Result(data={"url": "https://example.test/pr"})
        publish = dict(kind="action", action="publish_pr", workspace_base=0,
                       config={"repository": "owner/repo", "base": "main", "title": "PR"})
        def work(n, c, w):
            self.assertEqual(c["inputs"][0]["data"]["url"], "https://example.test/pr")
            return Result(message="done")
        run = self.runtime({"pub": publish, "a": node()}, ["pub", "a"], work, retries=1)
        run.actions = Actions()
        record = run.run()
        self.assertEqual(record["status"], "completed")
        self.assertEqual(len(record["attempts"]), 3)

    def test_running_sibling_is_retained_before_failure_finishes(self):
        barrier = threading.Barrier(2)
        def work(n, c, w):
            barrier.wait(timeout=5)
            if n["instruction"] == "bad":
                raise Failure("provider", "failed")
            return Result(message="done")
        run = self.runtime({"a": node("bad"), "b": node()}, [{"parallel": [["a"], ["b"]]}], work)
        record = run.run()
        self.assertEqual(record["status"], "failed")
        self.assertEqual({a["status"] for a in record["attempts"]}, {"failed", "completed"})

    def test_storage_failure_keeps_workspace(self):
        from unittest.mock import patch
        import shutil
        run = self.runtime({"a": node()}, ["a"], lambda n, c, w: (w / "artifact").write_text("saved") and Result())
        with patch.object(run.git, "retain", side_effect=Failure("storage", "disk unavailable")):
            record = run.run()
        self.assertEqual(record["status"], "failed")
        entries = git(self.repo, "worktree", "list", "--porcelain").splitlines()
        paths = [Path(e.removeprefix("worktree ")) for e in entries if e.startswith("worktree ")][1:]
        self.assertEqual(len(paths), 1)
        self.assertEqual((paths[0] / "artifact").read_text(), "saved")
        run.git.remove_worktree(paths[0])
        shutil.rmtree(paths[0].parent)

    def test_cleanup_failure_keeps_registered_worktree(self):
        from unittest.mock import patch
        import shutil
        run = self.runtime({"a": node()}, ["a"], lambda *args: Result())
        with patch.object(run.git, "remove_worktree", side_effect=Failure("cleanup", "busy")):
            record = run.run()
        self.assertEqual(record["status"], "completed")
        note = self.note(run, record["attempts"][0]["commit"])
        self.assertEqual(note["cleanup_warning"], "busy")
        paths = [Path(e.removeprefix("worktree ")) for e in git(self.repo, "worktree", "list", "--porcelain").splitlines() if e.startswith("worktree ")][1:]
        self.assertTrue(paths[0].exists())
        run.git.remove_worktree(paths[0])
        shutil.rmtree(paths[0].parent)

    def test_control_rejects_unvalidated_data(self):
        run = self.runtime({"a": node()}, ["a", {"if": {"condition": {"path": "/0/data", "equals": True}, "then": [], "else": []}}], lambda *args: Result(data=True))
        self.assertEqual(run.run()["failure"]["kind"], "result")

    def test_loop_converges_and_empty_conditional_passes_through(self):
        calls = []
        def work(n, c, w):
            calls.append(c)
            return Result(data=len(calls) < 3)
        run = self.runtime({"a": node(schema={"type": "boolean"})},
                           [{"loop": {"flow": ["a"], "while": {"path": "/0/data", "equals": True}}},
                            {"if": {"condition": {"path": "/0/data", "equals": False}, "then": [], "else": ["a"]}}], work)
        record = run.run()
        self.assertEqual(record["status"], "completed")
        self.assertEqual(len(calls), 3)
        self.assertFalse(record["outputs"][0]["data"])
        self.assertEqual(calls[1]["workspace_base"], record["attempts"][0]["commit"])

    def test_nested_parallel_obeys_global_concurrency_bound(self):
        import time
        count, peak = 0, 0
        lock = threading.Lock()
        def work(n, c, w):
            nonlocal count, peak
            with lock:
                count += 1
                peak = max(peak, count)
            time.sleep(0.03)
            with lock:
                count -= 1
            return Result()
        run = self.runtime({"a": node()}, [{"parallel": [[{"parallel": [["a"], ["a"], ["a"]]}], ["a"]]}], work, concurrency=2)
        self.assertEqual(run.run()["status"], "completed")
        self.assertEqual(peak, 2)
