import json
import signal
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch
from gitweave.adapters import CLIAdapter
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

    def test_node_requirements_are_checked_only_on_selected_branch(self):
        cases = [
            (dict(node(), provider="missing"), "graph", "Provider is not registered: missing"),
        ]
        for optional, kind, message in cases:
            for selected in (False, True):
                with self.subTest(optional=optional, selected=selected):
                    nodes = {"route": node(schema={"type": "boolean"}),
                             "optional": optional, "after": node()}
                    flow = ["route", {"if": {"condition": {"path": "/0/data", "equals": True},
                                             "then": ["optional"], "else": []}}, "after"]
                    work = Mock(return_value=Result(data=selected))
                    run = self.runtime(nodes, flow, work, retries=2)
                    record = run.run()
                    self.assertEqual(record["status"], "failed" if selected else "completed")
                    self.assertEqual(work.call_count, 1 if selected else 2)
                    notes = [self.note(run, attempt["commit"]) for attempt in record["attempts"]]
                    self.assertEqual([note["node_id"] for note in notes],
                                     ["route", "optional" if selected else "after"])
                    if selected:
                        self.assertEqual(record["failure"], {"kind": kind, "message": message})
                        self.assertEqual(notes[-1]["failure"],
                                         {"kind": kind, "message": message, "retryable": False})
                        self.assertEqual(notes[-1]["status"], "failed")
                    stored = json.loads(git(self.repo, "show", record["run_ref"] + ":run.json"))
                    self.assertEqual(stored, record)

    def test_static_errors_in_unselected_branch_still_fail_initialization(self):
        nodes = {"route": node(schema={"type": "boolean"}),
                 "invalid": dict(kind="command", workspace_base=0, argv=[])}
        flow = ["route", {"if": {"condition": {"path": "/0/data", "equals": True},
                                 "then": ["invalid"], "else": []}}]
        work = Mock(return_value=Result(data=False))
        with self.assertRaisesRegex(Failure, "argv must be a nonempty list"):
            self.runtime(nodes, flow, work)
        work.assert_not_called()

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

    def test_run_record_is_written_only_at_finalization(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                calls = []

                def work(n, c, w):
                    self.assertEqual(git(self.repo, "for-each-ref", "--format=%(refname)",
                                         f"refs/gitweave/{run.id}/run"), "")
                    # Earlier attempts are already durable before the Run is finalized.
                    for attempt in run.record["attempts"]:
                        ref = (f"refs/gitweave/{run.id}/attempts/"
                               f"{attempt['instance_id']}/{attempt['attempt']}")
                        self.assertEqual(git(self.repo, "rev-parse", ref), attempt["commit"])
                        self.assertEqual(self.note(run, attempt["commit"])["status"], attempt["status"])
                    calls.append(c)
                    if len(calls) == 1 or fail:
                        raise Failure("provider", "transient", retryable=True)
                    return Result(message="done")

                run = self.runtime({"a": node(), "b": node()}, ["a", "b"], work, retries=1)
                with patch.object(run.git, "run_record", wraps=run.git.run_record) as write:
                    record = run.run()
                write.assert_called_once_with(record)
                self.assertEqual(record["status"], "failed" if fail else "completed")
                self.assertEqual(len(record["attempts"]), 2 if fail else 3)
                stored = json.loads(git(self.repo, "show", record["run_ref"] + ":run.json"))
                self.assertEqual(stored, record)
                if fail:
                    self.assertEqual(stored["failure"]["kind"], "provider")
                else:
                    self.assertEqual(stored["outputs"][0]["message"], "done")

    def test_timeout_controls_session_and_reaches_subprocess_wait_unchanged(self):
        for provider in ("codex", "claude"):
            raw = (Path(__file__).parent / "fixtures" / f"{provider}.jsonl").read_text()
            for options in ({}, {"timeout": 1800}, {"timeout": 0.25}):
                with self.subTest(provider=provider, options=options):
                    agent = node()
                    agent["provider"] = provider
                    run = Runtime(graph({"a": agent}, ["a"], **options), self.repo,
                                  self.base, "request", adapters={provider: CLIAdapter(provider)})
                    child = Mock(returncode=0)
                    child.communicate.return_value = (raw, "")
                    launch = Mock(return_value=child)
                    with patch("gitweave.adapters.subprocess",
                               Mock(Popen=launch, PIPE=subprocess.PIPE,
                                    TimeoutExpired=subprocess.TimeoutExpired)), \
                            patch("gitweave.adapters.os.killpg") as kill:
                        record = run.run()
                    self.assertEqual(record["status"], "completed", record.get("failure"))
                    launch.assert_called_once()
                    self.assertIs(launch.call_args.kwargs["start_new_session"], "timeout" in options)
                    child.communicate.assert_called_once()
                    self.assertEqual(child.communicate.call_args.kwargs, {"timeout": options.get("timeout")})
                    kill.assert_not_called()
                    self.assertEqual("timeout" in run.graph, "timeout" in options)

    def test_explicit_timeout_retains_evidence_and_obeys_retry_policy(self):
        for retries, recover in ((0, False), (2, False), (1, True)):
            with self.subTest(retries=retries, recover=recover):
                agent = node()
                agent["provider"] = "codex"
                run = Runtime(graph({"a": agent}, ["a"], timeout=0.25, retries=retries),
                              self.repo, self.base, "request", adapters={"codex": CLIAdapter("codex")})
                children = []
                for index in range(retries + 1):
                    child = Mock(pid=12345 + index, returncode=-signal.SIGKILL)
                    child.communicate.side_effect = [
                        subprocess.TimeoutExpired("codex", 0.25),
                        (f"partial stdout {index}", f"partial stderr {index}"),
                    ]
                    children.append(child)
                if recover:
                    children[-1].communicate.side_effect = [('{"type":"turn.completed","usage":{}}', "")]
                    children[-1].returncode = 0
                launch = Mock(side_effect=children)
                with patch("gitweave.adapters.subprocess",
                           Mock(Popen=launch, PIPE=subprocess.PIPE,
                                TimeoutExpired=subprocess.TimeoutExpired)), \
                        patch("gitweave.adapters.os.killpg") as kill:
                    record = run.run()
                self.assertEqual(record["status"], "completed" if recover else "failed")
                if not recover:
                    self.assertEqual(record["failure"]["kind"], "timeout")
                self.assertEqual(len(record["attempts"]), retries + 1)
                failures = children[:-1] if recover else children
                self.assertEqual(kill.call_args_list, [call(c.pid, signal.SIGKILL) for c in failures])
                self.assertTrue(all(c.kwargs["start_new_session"] for c in launch.call_args_list))
                notes = [self.note(run, a["commit"]) for a in record["attempts"]]
                self.assertEqual(len({n["instance_id"] for n in notes}), 1)
                for index, (child, attempt, note) in enumerate(zip(children, record["attempts"], notes)):
                    self.assertEqual(child.communicate.call_args_list[0].kwargs, {"timeout": 0.25})
                    self.assertEqual(note["attempt"], index + 1)
                    self.assertEqual(note["workspace_base"], self.base)
                    self.assertEqual(note["input_commits"], [self.base])
                    self.assertEqual(note["node_id"], "a")
                    self.assertEqual(note["provider"], "codex")
                    self.assertIn("started_at", note)
                    self.assertIn("ended_at", note)
                    self.assertGreaterEqual(note["duration_seconds"], 0)
                    self.assertEqual(git(self.repo, "rev-parse", attempt["commit"] + "^"), self.base)
                    ref = f"refs/gitweave/{run.id}/attempts/{note['instance_id']}/{index + 1}"
                    self.assertEqual(git(self.repo, "rev-parse", ref), attempt["commit"])
                    if index < len(failures):
                        self.assertEqual(child.communicate.call_args_list[1], call())
                        self.assertEqual(note["status"], "failed")
                        self.assertEqual(note["failure"]["kind"], "timeout")
                        self.assertTrue(note["failure"]["retryable"])
                        self.assertEqual(note["result"]["raw_stdout"], f"partial stdout {index}")
                        self.assertEqual(note["result"]["raw_stderr"], f"partial stderr {index}")
                stored = json.loads(git(self.repo, "show", f"{record['run_ref']}:run.json"))
                self.assertEqual(stored["attempts"], record["attempts"])
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

    def test_omitted_workspace_base_defaults_to_first_input(self):
        def work(n, c, w):
            name = n["instruction"]
            if name in ("left", "right"):
                (w / "file").write_text(name)
            elif name == "join":
                self.assertEqual((w / "file").read_text(), "left")
                self.assertEqual(git(w, "rev-parse", "HEAD"), c["inputs"][0]["commit"])
            else:
                self.assertEqual(name, "from-run")
                self.assertFalse((w / "file").exists())
            return Result(message=name)
        nodes = {"a": node("left"), "b": node("right"), "j": node("join"), "r": dict(node("from-run"), workspace_base="run")}
        for n in ("a", "b", "j"):
            del nodes[n]["workspace_base"]
        run = self.runtime(nodes, [{"parallel": [["a"], ["b"]]}, "j", "r"], work)
        record = run.run()
        self.assertEqual(record["status"], "completed", record.get("failure"))
        notes = [self.note(run, a["commit"]) for a in record["attempts"]]
        join = next(n for n in notes if n["node_id"] == "j")
        self.assertEqual(join["workspace_base"], join["input_commits"][0])

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

    def test_retryability_flag_controls_retries_regardless_of_kind(self):
        for retryable in (False, True):
            with self.subTest(retryable=retryable):
                def limited(*args):
                    raise Failure("usage_limit", "limit", retryable=retryable)
                run = self.runtime({"a": node()}, ["a"], limited, retries=2)
                record = run.run()
                self.assertEqual(len(record["attempts"]), 3 if retryable else 1)
                self.assertEqual(record["failure"]["kind"], "usage_limit")

    def test_provider_failure_text_follows_configured_retries(self):
        for provider in ("codex", "claude"):
            success = (Path(__file__).parent / "fixtures" / f"{provider}.jsonl").read_text()
            for retries, recover in ((0, False), (2, False), (1, True)):
                with self.subTest(provider=provider, retries=retries, recover=recover):
                    agent = dict(node(), provider=provider)
                    run = Runtime(graph({"a": agent}, ["a"], retries=retries), self.repo,
                                  self.base, "request", adapters={provider: CLIAdapter(provider)})
                    outputs = [(1, "usage limit reached", "insufficient credits")] * (retries + 1)
                    if recover:
                        outputs[-1] = (0, success, "")
                    with patch("gitweave.adapters.process", side_effect=outputs) as invoke:
                        record = run.run()
                    self.assertEqual(record["status"], "completed" if recover else "failed")
                    self.assertEqual(invoke.call_count, retries + 1)
                    if not recover:
                        self.assertEqual(record["failure"]["message"], "insufficient credits")
                    for attempt in record["attempts"][:1 if recover else retries + 1]:
                        note = self.note(run, attempt["commit"])
                        self.assertEqual(note["failure"]["message"], "insufficient credits")
                        self.assertEqual(note["failure"]["kind"], "provider")
                        self.assertTrue(note["failure"]["retryable"])
                        self.assertEqual(note["result"]["raw_stdout"], "usage limit reached")
                        self.assertEqual(note["result"]["raw_stderr"], "insufficient credits")

    def test_native_provider_diagnostic_reaches_run_and_provenance(self):
        message = "Authentication failed: please sign in again"
        for provider, event in [
            ("codex", {"type": "turn.failed", "error": {"message": message}}),
            ("claude", {"type": "result", "subtype": "error_during_execution",
                        "is_error": True, "errors": [message]}),
        ]:
            with self.subTest(provider=provider):
                run = Runtime(graph({"a": dict(node(), provider=provider)}, ["a"]),
                              self.repo, self.base, "request", adapters={provider: CLIAdapter(provider)})
                raw = json.dumps(event)
                with patch("gitweave.adapters.process", return_value=(1, raw, "stderr context")):
                    record = run.run()
                self.assertEqual(record["status"], "failed")
                self.assertEqual(record["failure"]["message"], message)
                for attempt in record["attempts"]:
                    note = self.note(run, attempt["commit"])
                    self.assertEqual(note["failure"]["message"], message)
                    self.assertEqual(note["result"]["native"]["events"], [event])
                    self.assertEqual(note["result"]["raw_stdout"], raw)
                    self.assertEqual(note["result"]["raw_stderr"], "stderr context")

    def test_empty_map_preserves_upstream(self):
        def work(n, c, w):
            if n["instruction"] == "plan":
                return Result(data=[])
            self.assertEqual(c["inputs"][0]["data"], [])
            return Result(message="joined")
        run = self.runtime({"plan": node("plan", schema={"type": "array"}), "worker": node(), "join": node()},
                           ["plan", {"map": {"path": "/0/data", "flow": ["worker"]}}, "join"], work)
        self.assertEqual(len(run.run()["attempts"]), 2)

    def test_running_sibling_is_retained_before_failure_finishes(self):
        barrier = threading.Barrier(2)
        def work(n, c, w):
            barrier.wait(timeout=5)
            if n["instruction"] == "bad":
                raise Failure("provider", "failed")
            return Result(message="done")
        run = self.runtime({"a": node("bad"), "b": node()}, [{"parallel": [["a"], ["b"]]}], work)
        with patch.object(run.git, "run_record", wraps=run.git.run_record) as write:
            record = run.run()
        write.assert_called_once_with(record)
        self.assertEqual(record["status"], "failed")
        self.assertEqual({a["status"] for a in record["attempts"]}, {"failed", "completed"})
        stored = json.loads(git(self.repo, "show", record["run_ref"] + ":run.json"))
        self.assertEqual(stored, record)
        for attempt in stored["attempts"]:
            self.assertEqual(self.note(run, attempt["commit"])["status"], attempt["status"])

    def test_storage_failure_still_cleans_workspace(self):
        for operation in ("retain", "empty"):
            with self.subTest(operation=operation):
                paths = []
                def work(n, c, w):
                    paths.append(w)
                    (w / "artifact").write_text("saved")
                    if operation == "empty":
                        raise Failure("provider", "execution failed")
                    return Result()
                run = self.runtime({"a": node()}, ["a"], work)
                with patch.object(run.git, operation, side_effect=Failure("storage", "disk unavailable")), \
                     patch.object(run.git, "remove_worktree", wraps=run.git.remove_worktree) as remove:
                    record = run.run()
                self.assertEqual(record["status"], "failed")
                self.assertEqual(record["failure"], {"kind": "storage", "message": "disk unavailable"})
                remove.assert_called_once_with(paths[0])
                self.assertFalse(paths[0].parent.exists())
                self.assertEqual(git(self.repo, "worktree", "list", "--porcelain").count("worktree "), 1)

    def test_cleanup_failure_surfaces_without_rerecording(self):
        for earlier in (None, "provider", "storage"):
            with self.subTest(earlier=earlier):
                paths = []
                def work(n, c, w):
                    paths.append(w)
                    (w / "artifact").write_text("saved")
                    if earlier == "provider":
                        raise Failure("provider", "execution failed")
                    return Result()
                run = self.runtime({"a": node()}, ["a", "a"], work)
                try:
                    with patch.object(run.git, "remove_worktree", side_effect=Failure("cleanup", "busy")) as remove, \
                         patch.object(run.git, "retain", wraps=run.git.retain) as retain:
                        if earlier == "storage":
                            retain.side_effect = Failure("storage", "disk unavailable")
                        if earlier is None:
                            record = run.run()
                        else:
                            with self.assertLogs(level="WARNING") as logs:
                                record = run.run()
                            self.assertIn("Workspace cleanup failed: busy", logs.output[0])
                    self.assertEqual(record["status"], "failed")
                    self.assertEqual(record["failure"], {
                        "kind": earlier or "cleanup",
                        "message": {None: "busy", "provider": "execution failed",
                                    "storage": "disk unavailable"}[earlier],
                    })
                    remove.assert_called_once_with(paths[0])
                    retain.assert_called_once()
                    commit, _, attempt = retain.call_args.args
                    self.assertNotIn("cleanup_warning", attempt)
                    if earlier != "storage":
                        note = self.note(run, commit)
                        self.assertNotIn("cleanup_warning", note)
                        self.assertEqual(note["status"], "failed" if earlier == "provider" else "completed")
                    self.assertEqual((paths[0] / "artifact").read_text(), "saved")
                    self.assertEqual(git(self.repo, "worktree", "list", "--porcelain").count("worktree "), 2)
                finally:
                    if paths:
                        run.git.remove_worktree(paths[0])
                        paths[0].parent.rmdir()

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
        barrier = threading.Barrier(2)
        count, peak = 0, 0
        lock = threading.Lock()
        def work(n, c, w):
            nonlocal count, peak
            with lock:
                count += 1
                peak = max(peak, count)
            barrier.wait(timeout=5)
            with lock:
                count -= 1
            return Result()
        run = self.runtime({"a": node()}, [{"parallel": [[{"parallel": [["a"], ["a"], ["a"]]}], ["a"]]}], work, concurrency=2)
        self.assertEqual(run.run()["status"], "completed")
        self.assertEqual(peak, 2)
