"""Live events with real local subprocesses; no model allowance or network."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from gitweave.adapters import process
from gitweave.events import AttemptEvents, JSONEventSink, observe_output, output_callback
from gitweave.model import Failure, Result
from gitweave.runtime import Runtime
from test_command import command
from test_runtime import Fake, git, graph, node


class EventTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@localhost",
            "commit", "--allow-empty", "-qm", "base")
        self.events = io.StringIO()
        self.sink = JSONEventSink(self.events)

    def runtime(self, nodes, flow, **kwargs):
        return Runtime(graph(nodes, flow), self.repo, "HEAD", event_sink=self.sink, **kwargs)

    def parsed(self):
        events = [json.loads(line) for line in self.events.getvalue().splitlines()]
        for event in events:
            self.assertEqual(set(event) - {"stream", "text"},
                             {"type", "timestamp", "run_id", "node_id", "instance_id", "attempt"})
            self.assertIsNotNone(datetime.fromisoformat(event["timestamp"]).utcoffset())
        return events

    def check_attempts(self, run, record, events=None):
        events = self.parsed() if events is None else events
        for attempt in record["attempts"]:
            selected = [e for e in events if (e["run_id"], e["instance_id"], e["attempt"]) ==
                        (run.id, attempt["instance_id"], attempt["attempt"])]
            note = json.loads(git(self.repo, "notes", f"--ref={run.git.notes}", "show", attempt["commit"]))
            self.assertEqual(selected[0]["type"], "node_started")
            self.assertEqual(selected[-1]["type"], "node_" + attempt["status"])
            self.assertTrue(all(e["type"] == "node_output" for e in selected[1:-1]))
            self.assertTrue(all(e["node_id"] == note["node_id"] for e in selected))
            for stream in ("stdout", "stderr"):
                self.assertEqual("".join(e["text"] for e in selected if e.get("stream") == stream),
                                 note["result"]["raw_" + stream])

    def test_command_output_is_live_without_a_newline_and_preserved(self):
        gate = self.root / "continue"
        script = '''import json, sys, time
from pathlib import Path
c = json.load(sys.stdin)
sys.stdout.write('{"message":"雪'); sys.stdout.flush()
sys.stderr.write('building 雪'); sys.stderr.flush()
while not Path(c['config']['gate']).exists(): time.sleep(0.01)
sys.stdout.write('","data":null}'); sys.stdout.flush()
sys.stderr.write(' done\\n'); sys.stderr.flush()
'''
        seen = set()
        lock = threading.Lock()

        def observe(event):
            self.sink(event)
            if event["type"] == "node_output":
                with lock:
                    seen.add(event["stream"])
                    if seen == {"stdout", "stderr"}:
                        gate.touch()

        run = self.runtime({"cmd": command(script, config={"gate": str(gate)}, timeout=5)}, ["cmd"])
        run.event_sink = observe
        record = run.run()
        self.assertEqual(record["status"], "completed", record.get("failure"))
        self.assertEqual(record["outputs"][0]["message"], "雪")
        self.check_attempts(run, record)

    def test_success_with_no_subprocess_output(self):
        run = self.runtime({"a": node()}, ["a"], adapters={"fake": Fake(lambda *args: Result(message="done"))})
        record = run.run()
        self.assertEqual(record["status"], "completed")
        self.assertEqual([e["type"] for e in self.parsed()], ["node_started", "node_completed"])
        self.check_attempts(run, record)

    def test_failed_attempt_then_retry_has_distinct_attempt_identity(self):
        counter = self.root / "count"
        script = '''import json, sys
from pathlib import Path
c = json.load(sys.stdin)
p = Path(c['config']['counter'])
print('diagnostic', file=sys.stderr, flush=True)
if not p.exists():
    p.touch(); print('partial', flush=True); sys.exit(3)
print('{"message":"recovered","data":null}', flush=True)
'''
        run = self.runtime({"cmd": command(script, config={"counter": str(counter)}, retries=1)}, ["cmd"])
        record = run.run()
        self.assertEqual(record["status"], "completed")
        self.assertEqual([(e["type"], e["attempt"]) for e in self.parsed() if e["type"] != "node_output"],
                         [("node_started", 1), ("node_failed", 1), ("node_started", 2), ("node_completed", 2)])
        self.assertEqual(len({e["instance_id"] for e in self.parsed()}), 1)
        self.check_attempts(run, record)

    def test_parallel_instances_and_concurrent_runs_keep_output_attribution(self):
        gate = self.root / "parallel"
        script = '''import json, os, sys, time
from pathlib import Path
c = json.load(sys.stdin)
print(json.dumps({'message': c['config']['label'], 'data': os.getcwd()}), flush=True)
print(os.getcwd(), file=sys.stderr, flush=True)
while not Path(c['config']['gate']).exists(): time.sleep(0.01)
'''
        seen = set()
        lock = threading.Lock()

        def observe(event):
            self.sink(event)
            if event["type"] == "node_output":
                with lock:
                    seen.add((event["run_id"], event["instance_id"], event["stream"]))
                    if len(seen) == 8:  # Both streams of all four still-running instances.
                        gate.touch()

        runs = [self.runtime({"cmd": command(script, config={"label": label, "gate": str(gate)}, timeout=10)},
                             [{"parallel": [["cmd"], ["cmd"]]}]) for label in ("first", "second")]
        for run in runs:
            run.event_sink = observe
        with ThreadPoolExecutor(max_workers=2) as pool:
            records = list(pool.map(lambda run: run.run(), runs))
        events = self.parsed()
        self.assertEqual(len(seen), 8)
        self.assertEqual({e["run_id"] for e in events}, {run.id for run in runs})
        for label, run, record in zip(("first", "second"), runs, records):
            self.assertEqual(record["status"], "completed", record.get("failure"))
            self.assertEqual([o["message"] for o in record["outputs"]], [label, label])
            self.check_attempts(run, record, events)

    def test_agent_streams_reach_normalization_and_provenance_unchanged(self):
        gate = self.root / "agent-continue"
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                gate.unlink(missing_ok=True)
                self.events.seek(0); self.events.truncate()
                raw = (Path(__file__).parent / "fixtures" / f"{provider}.jsonl").read_text()
                script = self.root / provider
                # Replace only the provider executable; its real adapter/session driver runs.
                script.write_text(f'#!{sys.executable}\n' +
                                  'import sys, time\nfrom pathlib import Path\n' +
                                  ('sys.stdin.readline()\n' if provider == "claude" else 'sys.stdin.read()\n') +
                                  f'sys.stdout.write({raw!r}); sys.stdout.flush()\n' +
                                  "sys.stderr.write('live diagnostic'); sys.stderr.flush()\n" +
                                  f'while not Path({str(gate)!r}).exists(): time.sleep(0.01)\n' +
                                  ('sys.stdin.read()\n' if provider == "claude" else ''))
                script.chmod(0o755)
                seen = set()
                lock = threading.Lock()

                def observe(event):
                    self.sink(event)
                    if event["type"] == "node_output":
                        with lock:
                            seen.add(event["stream"])
                            if len(seen) == 2:
                                gate.touch()

                agent = dict(kind="agent", provider=provider, instruction="work", timeout=5,
                             schema={"type": "object"})
                run = self.runtime({"agent": agent}, ["agent"])
                run.event_sink = observe
                with patch.dict(os.environ, {"PATH": str(self.root) + os.pathsep + os.environ.get("PATH", "")}):
                    record = run.run()
                self.assertEqual(record["status"], "completed", record.get("failure"))
                self.assertEqual(record["outputs"][0]["data"], {"ok": True})
                self.check_attempts(run, record)
                note = json.loads(git(self.repo, "notes", f"--ref={run.git.notes}", "show", record["outputs"][0]["commit"]))
                self.assertEqual(note["result"]["raw_stdout"], raw)
                self.assertEqual(note["result"]["raw_stderr"], "live diagnostic")
                self.assertEqual(note["result"]["session_id"], f"{provider}-session")
                self.assertEqual(note["result"]["usage"]["cached_input_tokens"], 80)

    def test_timeout_drains_output_and_emits_failure(self):
        run = self.runtime({"cmd": command("import sys,time; print('partial',flush=True); "
                                           "print('error',file=sys.stderr,flush=True); time.sleep(30)", timeout=0.3)}, ["cmd"])
        record = run.run()
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["failure"]["kind"], "timeout")
        self.check_attempts(run, record)
        self.assertEqual(self.parsed()[-1]["type"], "node_failed")
        self.assertEqual({e["stream"] for e in self.parsed() if e["type"] == "node_output"}, {"stdout", "stderr"})

    def test_launch_and_validation_failures_emit_terminal_events(self):
        for cmd in (dict(kind="command", argv=["./missing"]), command('print("invalid")'),
                    command('print(\'{"message":"m","data":null}\')', schema={"type": "object"})):
            with self.subTest(cmd=cmd):
                self.events.seek(0); self.events.truncate()
                run = self.runtime({"cmd": cmd}, ["cmd"])
                record = run.run()
                self.assertEqual(record["status"], "failed")
                self.check_attempts(run, record)

    def test_storage_error_emits_failure(self):
        run = self.runtime({"a": node()}, ["a"], adapters={"fake": Fake(lambda *args: Result())})
        with patch.object(run.git, "retain", side_effect=Failure("git", "storage unavailable")):
            record = run.run()
        self.assertEqual(record["status"], "failed")
        self.assertEqual([e["type"] for e in self.parsed()], ["node_started", "node_failed"])
        self.assertEqual(len(git(self.repo, "worktree", "list").splitlines()), 1)

    def test_cli_stdout_remains_only_final_json(self):
        path = self.root / "graph.json"
        for fail in (False, True):
            with self.subTest(fail=fail):
                path.write_text(graph({"cmd": command("import sys; print('log',file=sys.stderr); " +
                    ("sys.exit(3)" if fail else 'print(\'{"message":"done","data":null}\')'))}, ["cmd"]))
                result = subprocess.run([sys.executable, "-m", "gitweave", "run", "--graph", str(path),
                                         "--repo", str(self.repo), "--commit", "HEAD"],
                                        capture_output=True, text=True, timeout=10)
                record = json.loads(result.stdout)
                self.assertEqual(set(record), {"run_id", "status", "repository", "run_ref", "notes_ref", "outputs"})
                self.assertEqual(result.returncode, 1 if fail else 0)
                lines = [json.loads(line) for line in result.stderr.splitlines()]
                events = lines[:-1] if fail else lines
                self.assertEqual(events[0]["type"], "node_started")
                self.assertEqual(events[-1]["type"], "node_failed" if fail else "node_completed")
                self.assertTrue(all(e["run_id"] == record["run_id"] for e in events))
                self.assertEqual("".join(e["text"] for e in events if e.get("stream") == "stderr"), "log\n")
                if fail:
                    self.assertEqual(lines[-1]["kind"], "command")  # Existing failure diagnostic.


class ProcessEventTests(unittest.TestCase):
    def test_split_utf8_and_newlines_match_captured_text(self):
        chunks = []
        script = "import os,time; os.write(1,b'\\xe9'); time.sleep(.05); os.write(1,b'\\x9b\\xaa\\r'); " \
                 "time.sleep(.05); os.write(1,b'\\nend\\r'); os.write(2,b'error\\r\\n')"
        with observe_output(lambda stream, text: chunks.append((stream, text))):
            code, stdout, stderr = process([sys.executable, "-c", script], "", None, 5)
        self.assertEqual((code, stdout, stderr), (0, "雪\nend\n", "error\n"))
        self.assertEqual("".join(t for s, t in chunks if s == "stdout"), stdout)
        self.assertEqual("".join(t for s, t in chunks if s == "stderr"), stderr)
        self.assertIsNone(output_callback.get())

    def test_large_input_and_both_output_pipes_are_drained(self):
        script = "import sys; sys.stdout.write('x'*200000); sys.stdout.flush(); " \
                 "sys.stderr.write('y'*200000); sys.stderr.flush(); print(len(sys.stdin.read()))"
        received = []
        with observe_output(lambda stream, text: received.append((stream, text))):
            code, stdout, stderr = process([sys.executable, "-c", script], "p" * 200000, None, 5)
        self.assertEqual((code, stdout, stderr), (0, "x" * 200000 + "200000\n", "y" * 200000))
        for stream, expected in (("stdout", stdout), ("stderr", stderr)):
            self.assertEqual("".join(t for s, t in received if s == stream), expected)

    @unittest.skipUnless(hasattr(os, "fork"), "POSIX process groups required")
    def test_timeout_also_bounds_descendants_holding_pipes_after_parent_exit(self):
        script = "import os,time; pid=os.fork(); time.sleep(30) if pid==0 else None"
        with observe_output(lambda *args: None), self.assertRaises(Failure) as raised:
            process([sys.executable, "-c", script], "", None, 0.3)
        self.assertEqual(raised.exception.kind, "timeout")

    def test_closed_event_destination_does_not_discard_captured_output(self):
        events = io.StringIO()
        sink = JSONEventSink(events)
        events.close()
        attempt = AttemptEvents(sink, dict(run_id="r", node_id="n", instance_id="n-1", attempt=1))
        with observe_output(attempt.output):
            self.assertEqual(process([sys.executable, "-c", "print('done')"], "", None, 5), (0, "done\n", ""))
