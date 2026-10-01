"""Recovery from real process termination and deterministic control-flow replay."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from gitweave.git import Git
from gitweave.model import Failure, Result
from gitweave.runtime import Runtime
import test_runtime as fixtures
from test_runtime import Fake, git, graph, node


class Interrupted(BaseException):
    pass


class ResumeTests(unittest.TestCase):
    setUp = fixtures.RuntimeTests.setUp
    note = fixtures.RuntimeTests.note

    def runtime(self, nodes, flow, work, **options):
        return Runtime(graph(nodes, flow, **options), self.repo, self.base, "guidance",
                       adapters={"fake": Fake(work)})

    def interrupt(self, run):
        # Drop finalization exactly as process loss does, leaving the startup record.
        original = run.git.run_record
        writes = []
        def write(record):
            writes.append(1)
            if len(writes) == 1:
                return original(record)
        with patch.object(run.git, "run_record", side_effect=write), self.assertRaises(Interrupted):
            run.run()
        saved = run.git.load_run()
        self.assertEqual(saved["status"], "running")
        self.assertEqual(saved["attempts"], [])
        self.assertEqual(saved["graph"], run.record["graph"])

    def resume(self, run, work):
        resumed = Runtime.resume(run.id, self.repo, adapters={"fake": Fake(work)})
        record = resumed.run()
        self.assertEqual(record["status"], "completed", record.get("failure"))
        self.assertEqual(record["run_id"], run.id)
        return resumed, record

    def snapshot(self, run):
        refs = git(self.repo, "for-each-ref", "--format=%(refname) %(objectname)",
                   f"refs/gitweave/{run.id}/attempts/", f"refs/gitweave/{run.id}/starts/")
        return {ref: (commit, self.note(run, commit)) for ref, commit in
                (line.split() for line in refs.splitlines())}

    def assert_preserved(self, run, before):
        after = self.snapshot(run)
        for ref, value in before.items():
            self.assertEqual(after[ref], value)

    def test_linear_artifacts_results_interrupted_history_and_identity(self):
        calls = []
        def work(n, c, w):
            label = n["instruction"]
            calls.append(label)
            if label == "a":
                (w / "artifact").write_text("completed")
                return Result(message="from a", data={"value": 42}, usage={"tokens": 7})
            self.assertEqual((w / "artifact").read_text(), "completed")
            self.assertFalse((w / "partial").exists())
            self.assertEqual(c["inputs"][0]["message"], "from a")
            self.assertEqual(c["inputs"][0]["data"], {"value": 42})
            (w / "partial").write_text("discard me")
            raise Interrupted()
        run = self.runtime({"a": node("a"), "b": node("b"), "c": node("c")}, ["a", "b", "c"], work)
        self.interrupt(run)
        before = self.snapshot(run)
        a = run.record["attempts"][0]["commit"]
        def finish(n, c, w):
            calls.append(n["instruction"])
            if n["instruction"] == "b":
                self.assertEqual(c["inputs"][0]["commit"], a)
                self.assertEqual(c["inputs"][0]["data"], {"value": 42})
                self.assertFalse((w / "partial").exists())
                self.assertEqual((w / "artifact").read_text(), "completed")
            return Result(message="done")
        resumed, record = self.resume(run, finish)
        self.assertEqual(calls, ["a", "b", "b", "c"])
        self.assertEqual(record["steps"], 3)
        attempts = record["attempts"]
        self.assertEqual([a["status"] for a in attempts], ["completed", "interrupted", "completed", "completed"])
        self.assertEqual(attempts[1]["instance_id"], attempts[2]["instance_id"])
        self.assertEqual(attempts[1]["invocation_id"], "flow/1/b")
        self.assertEqual([a["attempt"] for a in attempts[1:3]], [1, 2])
        self.assert_preserved(resumed, before)
        self.assertEqual(resumed.git.load_run(), record)
        with self.assertRaisesRegex(Failure, "already completed"):
            Runtime.resume(run.id, self.repo)

    def test_partial_parallel_and_map_reuse_at_exact_step_limit(self):
        for op in ("parallel", "map"):
            with self.subTest(op=op):
                nodes = {"plan": node("plan", schema={"type": "array"}), "worker": node("worker"),
                         "join": node("join")}
                block = {"parallel": [["worker"], ["worker"]]} if op == "parallel" else {
                    "map": {"path": "/0/data", "flow": ["worker"]}}
                calls = []
                run = None
                def work(n, c, w):
                    if n["instruction"] == "plan":
                        return Result(data=["left", "right"])
                    calls.append(c["item"])
                    if len(calls) == 1:
                        return Result(message="left")
                    raise Interrupted()
                run = self.runtime(nodes, ["plan", block, "join"], work, concurrency=1, max_steps=4)
                self.interrupt(run)
                before = self.snapshot(run)
                finished = []
                def finish(n, c, w):
                    finished.append((n["instruction"], c["item"]))
                    if n["instruction"] == "join":
                        self.assertEqual([i["message"] for i in c["inputs"]], ["left", "right"])
                        return Result(message="joined")
                    return Result(message="right")
                resumed, record = self.resume(run, finish)
                self.assertEqual(finished, [("worker", "right" if op == "map" else None), ("join", None)])
                self.assertEqual(record["steps"], 4)
                self.assert_preserved(resumed, before)
                workers = [a for a in record["attempts"] if a["instance_id"].startswith("worker-")]
                self.assertEqual(len({a["invocation_id"] for a in workers}), 2)

    def test_else_branch_and_repeated_sequence_node_have_distinct_paths(self):
        calls = []
        def work(n, c, w):
            calls.append(n["instruction"])
            if n["instruction"] == "route":
                return Result(data=False)
            if len(calls) == 2:
                return Result(message="selected else")
            raise Interrupted()
        nodes = {"route": node("route", schema={"type": "boolean"}), "worker": node("worker")}
        flow = ["route", {"if": {"condition": {"path": "/0/data", "equals": True},
                                "then": ["worker", "worker"], "else": ["worker"]}}, "worker"]
        run = self.runtime(nodes, flow, work, max_steps=3)
        self.interrupt(run)
        finished = []
        def finish(n, c, w):
            finished.append(n["instruction"])
            self.assertEqual(c["inputs"][0]["message"], "selected else")
            return Result(message="done")
        _, record = self.resume(run, finish)
        self.assertEqual(finished, ["worker"])
        self.assertEqual({a["invocation_id"] for a in record["attempts"]},
                         {"flow/0/route", "flow/1/if/else/0/worker", "flow/2/worker"})

    def test_definition_and_base_survive_interruption_before_first_invocation(self):
        run = self.runtime({"a": node()}, ["a"], lambda *a: Result(message="done"))
        with patch.object(run, "flow", side_effect=Interrupted()):
            self.interrupt(run)
        self.assertEqual(run.git.load_attempts(), [])
        self.assertEqual(git(self.repo, "rev-parse", f"refs/gitweave/{run.id}/input/base"), self.base)
        _, record = self.resume(run, lambda *a: Result(message="done"))
        self.assertEqual(record["steps"], 1)

    def test_loop_nested_if_map_parallel_replay_and_retry(self):
        flow = ["plan", {"map": {"path": "/0/data", "flow": [
            {"loop": {"flow": ["review", {"if": {"condition": {"path": "/0/data/again", "equals": True},
                "then": [{"parallel": [["fix"], ["fix"]]}], "else": []}}],
                "while": {"path": "/0/data/again", "equals": True}}}]}}, "join"]
        nodes = {"plan": node("plan", schema={"type": "array"}),
                 "review": node("review", schema={"type": "object"}),
                 "fix": node("fix", schema={"type": "object"}), "join": node("join")}
        calls = []
        failures = []
        def work(n, c, w):
            label = n["instruction"]
            calls.append((label, c["item"]))
            if label == "plan":
                return Result(data=["one", "two"])
            if label == "fix":
                return Result(data={"again": True, "iteration": 1})
            iteration = c["inputs"][0]["data"].get("iteration", 0) if isinstance(c["inputs"][0]["data"], dict) else 0
            if c["item"] == "two" and iteration == 1:
                if not failures:
                    failures.append(1)
                    raise Failure("provider", "transient", retryable=True)
                (w / "partial").write_text("interrupted")
                raise Interrupted()
            return Result(data={"again": iteration == 0})
        run = self.runtime(nodes, flow, work, concurrency=1, retries=1, max_steps=10)
        self.interrupt(run)
        before = self.snapshot(run)
        finished = []
        def finish(n, c, w):
            finished.append((n["instruction"], c["item"]))
            self.assertFalse((w / "partial").exists())
            if n["instruction"] == "join":
                self.assertEqual([i["data"] for i in c["inputs"]], [{"again": False}, {"again": False}])
                return Result(message="joined")
            return Result(data={"again": False})
        resumed, record = self.resume(run, finish)
        self.assertEqual(finished, [("review", "two"), ("join", None)])
        self.assertEqual(record["steps"], 10)
        self.assert_preserved(resumed, before)
        attempts = [a for a in record["attempts"] if a["invocation_id"] == "flow/1/map/1/0/loop/1/0/review"]
        self.assertEqual([a["attempt"] for a in attempts], [1, 2, 3])
        self.assertEqual([a["status"] for a in attempts], ["failed", "interrupted", "completed"])
        self.assertEqual(len({a["instance_id"] for a in attempts}), 1)

    def test_parallel_completion_order_cannot_change_invocation_identity(self):
        results = []
        labels = []
        for first in ("left", "right"):
            ready = threading.Event()
            def work(n, c, w):
                if n["instruction"] == first:
                    ready.set()
                else:
                    self.assertTrue(ready.wait(5))
                return Result(message=n["instruction"])
            run = self.runtime({"left": node("left"), "right": node("right"), "same": node()},
                               [{"parallel": [["left", "same"], ["right", "same"]]}], work)
            if first == "right":
                branches = run.branches
                async def reversed_schedule(flows, inputs):
                    return await branches(list(reversed(flows)), inputs)
                run.branches = reversed_schedule
            record = run.run()
            self.assertEqual(record["status"], "completed")
            results.append({a["invocation_id"] for a in record["attempts"]})
            labels.append({a["invocation_id"]: a["instance_id"] for a in record["attempts"]})
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(results[0]), 4)
        self.assertNotEqual(labels[0]["flow/0/parallel/0/0/left"], labels[1]["flow/0/parallel/0/0/left"])

    def test_repeated_resume_cannot_extend_logical_step_budget(self):
        run = self.runtime({"a": node(schema={"type": "boolean"})},
                           [{"loop": {"flow": ["a"], "while": {"path": "/0/data", "equals": True}}}],
                           lambda *args: Result(data=True), max_steps=2)
        first = run.run()
        self.assertEqual(first["failure"]["kind"], "step_limit")
        before = self.snapshot(run)
        for _ in range(2):
            def unexpected(*args):
                self.fail("Completed loop iterations must be reused")
            resumed = Runtime.resume(run.id, self.repo, adapters={"fake": Fake(unexpected)})
            record = resumed.run()
            self.assertEqual(record["failure"]["kind"], "step_limit")
            self.assertEqual(record["steps"], 2)
            self.assert_preserved(run, before)

    def test_failed_node_is_rerun_and_retries_keep_original_inputs(self):
        run = self.runtime({"a": node()}, ["a"], lambda *a: (_ for _ in ()).throw(Failure("provider", "failed")),
                           retries=1, max_steps=1)
        self.assertEqual(run.run()["status"], "failed")
        calls = []
        def finish(n, c, w):
            calls.append(c)
            self.assertFalse((w / "partial").exists())
            self.assertEqual(git(w, "rev-parse", "HEAD"), self.base)
            if len(calls) == 1:
                (w / "partial").write_text("failed retry")
                raise Failure("provider", "transient", retryable=True)
            return Result(message="done")
        _, record = self.resume(run, finish)
        self.assertEqual([a["attempt"] for a in record["attempts"]], [1, 2, 3])
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(record["steps"], 1)

    def test_unknown_legacy_corrupt_and_active_runs_are_rejected(self):
        with self.assertRaisesRegex(Failure, "Run not found"):
            Runtime.resume("unknown", self.repo)
        for invalid in ("../run", "--all", ""):
            with self.assertRaisesRegex(Failure, "Invalid Run ID"):
                Runtime.resume(invalid, self.repo)
        run = self.runtime({"a": node()}, ["a"], lambda *a: (_ for _ in ()).throw(Interrupted()))
        self.interrupt(run)
        with run.git.run_lock():
            with self.assertRaisesRegex(Failure, "already executing"):
                Runtime.resume(run.id, self.repo)
        resumed = Runtime.resume(run.id, self.repo)
        saved = run.git.load_run()
        run.git.run_record(saved)
        with self.assertRaisesRegex(Failure, "changed since it was loaded"):
            resumed.run()
        saved["graph_digest"] = "incorrect"
        run.git.run_record(saved)
        with self.assertRaisesRegex(Failure, "digest"):
            Runtime.resume(run.id, self.repo)
        saved.pop("resume_version")
        run.git.run_record(saved)
        with self.assertRaisesRegex(Failure, "resumable definition"):
            Runtime.resume(run.id, self.repo)

    def test_discovery_and_recovery_from_fetched_git_only_state(self):
        run = self.runtime({"a": node("a"), "b": node("b")}, ["a", "b"],
                           lambda n, *a: Result(message="saved") if n["instruction"] == "a" else (_ for _ in ()).throw(Interrupted()))
        self.interrupt(run)
        fresh = self.repo.parent / "fresh"
        git(self.repo, "init", "-q", str(fresh))
        prefix = f"refs/gitweave/{run.id}/"
        git(fresh, "fetch", "--quiet", str(self.repo), prefix + "*:" + prefix + "*", run.git.notes + ":" + run.git.notes)
        with patch("gitweave.runtime.Path.cwd", return_value=fresh):
            resumed = Runtime.resume(run.id, adapters={"fake": Fake(lambda n, c, w: Result(message=c["inputs"][0]["message"]))})
        record = resumed.run()
        self.assertEqual(record["status"], "completed", record.get("failure"))
        self.assertEqual(record["outputs"][0]["message"], "saved")
        self.assertEqual(record["repository"], str(fresh.resolve()))


class ProcessResumeTests(unittest.TestCase):
    setUp = fixtures.RuntimeTests.setUp
    note = fixtures.RuntimeTests.note
    snapshot = ResumeTests.snapshot
    assert_preserved = ResumeTests.assert_preserved

    def command(self, label, *, stop=False, data=None):
        # Real command nodes record calls outside their artifact worktrees so we
        # can prove completed invocations were not executed a second time.
        script = """
import json, sys, time
from pathlib import Path
c = json.load(sys.stdin)
cfg = c['config']
key = cfg['label'] + (':' + str(c['item']) if c['item'] is not None else '')
root = Path(cfg['root'])
with (root / 'calls').open('a') as f: f.write(key + '\\n')
if cfg['label'] == 'b' or c['item'] == 'right':
    assert not Path('partial').exists(), 'Interrupted files leaked'
    if cfg['label'] == 'b':
        assert Path('artifact').read_text() == 'saved'
        assert c['inputs'][0]['message'] == 'a'
        assert c['inputs'][0]['data'] == {'value': 42}
if cfg['stop'] and not (root / 'continue').exists():
    Path('partial').write_text('interrupted')
    (root / 'blocked').touch()
    while True: time.sleep(.01)
if cfg['label'] == 'a': Path('artifact').write_text('saved')
print(json.dumps({'message': key, 'data': cfg['data']}))
"""
        return dict(kind="command", argv=[sys.executable, "-c", script],
                    config={"label": label, "stop": stop, "data": data, "root": str(self.repo.parent)},
                    schema={"type": "array" if isinstance(data, list) else "object"})

    def terminate_and_resume(self, nodes, flow, *, completed, max_steps):
        root = self.repo.parent
        path = root / "graph.json"
        text = graph(nodes, flow, concurrency=2, max_steps=max_steps)
        path.write_text(text)
        # All descendants inherit this process group, which the test kills too.
        with (root / "events").open("w") as events:
            child = subprocess.Popen([sys.executable, "-m", "gitweave", "run", "--graph", str(path),
                                      "--repo", str(self.repo), "--commit", self.base, "guidance"],
                                     stdout=subprocess.DEVNULL, stderr=events, start_new_session=True)
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    refs = git(self.repo, "for-each-ref", "--format=%(refname)", "refs/gitweave").splitlines()
                    if (root / "blocked").exists() and sum("/attempts/" in ref for ref in refs) >= completed:
                        break
                    self.assertIsNone(child.poll(), (root / "events").read_text())
                    time.sleep(.02)
                else:
                    self.fail("Command did not reach interruption boundary")
                run_ref = next(ref for ref in refs if ref.endswith("/run"))
                run_id = run_ref.split("/")[2]
                saved = json.loads(git(self.repo, "show", run_ref + ":run.json"))
                self.assertEqual(saved["status"], "running")
                self.assertEqual(saved["graph"], text)
                self.assertEqual(saved["attempts"], [])
                # A concurrent resume must fail without executing additional work.
                concurrent = subprocess.run([sys.executable, "-m", "gitweave", "resume", "--run", run_id,
                                             "--repo", str(self.repo)], capture_output=True, text=True, timeout=5)
                self.assertEqual(concurrent.returncode, 2)
                self.assertIn("already executing", concurrent.stderr)
            finally:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)
        run = Runtime.resume(run_id, self.repo)
        before = self.snapshot(run)
        calls_before = (root / "calls").read_text().splitlines()
        # Edits to the graph path cannot affect recovery.
        path.write_text("invalid graph replaced after process loss")
        (root / "continue").touch()
        result = subprocess.run([sys.executable, "-m", "gitweave", "resume", "--run", run_id,
                                 "--repo", str(self.repo)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["run_id"], run_id)
        self.assertEqual(summary["status"], "completed")
        self.assert_preserved(run, before)
        record = run.git.load_run()
        self.assertEqual(record["graph"], text)
        self.assertEqual(record["steps"], max_steps)
        self.assertIn("interrupted", [a["status"] for a in record["attempts"]])
        self.assertEqual(len(record["attempts"]), max_steps + 1)
        self.assertEqual(len([json.loads(line) for line in result.stderr.splitlines()
                              if json.loads(line)["type"] == "node_started"]), max_steps - completed)
        # SIGKILL leaves old worktrees behind. Recovery does not use or delete them.
        for line in git(self.repo, "worktree", "list", "--porcelain").splitlines():
            if line.startswith("worktree "):
                workspace = Path(line[len("worktree "):])
                if workspace.resolve() != self.repo.resolve():
                    run.git.remove_worktree(workspace)
                    workspace.parent.rmdir()
        return calls_before, (root / "calls").read_text().splitlines(), record

    def test_sigkill_linear_cli_uses_original_graph_results_and_files(self):
        before, after, record = self.terminate_and_resume(
            {"a": self.command("a", data={"value": 42}), "b": self.command("b", stop=True, data={}),
             "c": self.command("c", data={})}, ["a", "b", "c"], completed=1, max_steps=3)
        self.assertEqual(before, ["a", "b"])
        self.assertEqual(after, ["a", "b", "b", "c"])
        self.assertEqual(record["outputs"][0]["message"], "c")

    def test_sigkill_parallel_reuses_finished_sibling(self):
        before, after, _ = self.terminate_and_resume(
            {"left": self.command("left", data={}), "right": self.command("right", stop=True, data={})},
            [{"parallel": [["left"], ["right"]]}], completed=1, max_steps=2)
        self.assertCountEqual(before, ["left", "right"])
        self.assertEqual(after.count("left"), 1)
        self.assertEqual(after.count("right"), 2)

    def test_sigkill_map_reuses_finished_item(self):
        # Stop only the right map item; the left item finishes first.
        worker = self.command("worker", stop=True, data={})
        worker["argv"][2] = worker["argv"][2].replace("if cfg['stop'] and", "if c['item'] == 'right' and cfg['stop'] and")
        before, after, _ = self.terminate_and_resume(
            {"plan": self.command("plan", data=["left", "right"]), "worker": worker},
            ["plan", {"map": {"path": "/0/data", "flow": ["worker"]}}], completed=2, max_steps=3)
        self.assertCountEqual(before, ["plan", "worker:left", "worker:right"])
        self.assertEqual(after.count("plan"), 1)
        self.assertEqual(after.count("worker:left"), 1)
        self.assertEqual(after.count("worker:right"), 2)
