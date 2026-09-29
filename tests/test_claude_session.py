"""Claude stream-json sessions stay open until background tasks finish (real processes)."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

from gitweave.adapters import normalize, process
from gitweave.model import Failure

FAKE_CLAUDE = r'''
import json, sys, time
scenario, log = sys.argv[1], sys.argv[2]
def emit(**event):
    print(json.dumps(event), flush=True)
def result(text, cost, output, **extra):
    emit(type="result", subtype="success", is_error=False, result=text, session_id="s",
         usage={"input_tokens": 1, "output_tokens": output}, total_cost_usd=cost,
         structured_output={"message": text, "data": {"turn": text}}, **extra)
def receive():
    line = sys.stdin.readline()
    if line:
        open(log, "a").write(json.dumps(json.loads(line)["message"]["content"]) + "\n")
    return line
receive()
emit(type="system", subtype="init", session_id="s")
if scenario == "none":
    result("done", 0.01, 10)
elif scenario == "background":
    emit(type="system", subtype="background_tasks_changed", tasks=[{"task_id": "t1", "description": "build"}])
    result("started", 0.01, 10)
    time.sleep(0.5)
    emit(type="system", subtype="background_tasks_changed", tasks=[])
    emit(type="system", subtype="task_updated", task_id="t1", patch={"status": "completed"})
    result("finished", 0.03, 7)
elif scenario == "midturn":  # the task finishes, then a long silent foreground step
    emit(type="system", subtype="background_tasks_changed", tasks=[{"task_id": "t1", "description": "build"}])
    result("started", 0.01, 10)
    time.sleep(0.2)
    emit(type="system", subtype="background_tasks_changed", tasks=[])
    emit(type="assistant", message={"content": []})
    time.sleep(2.5)
    result("tested", 0.02, 5)
elif scenario == "chatty":  # a monitor keeps waking the agent; the task never ends
    import os, threading
    def listen():
        for _ in iter(receive, ""):
            pass
        emit(type="system", subtype="task_updated", task_id="srv", patch={"status": "killed"})
        os._exit(0)
    threading.Thread(target=listen, daemon=True).start()
    emit(type="system", subtype="background_tasks_changed", tasks=[{"task_id": "srv", "description": "dev server"}])
    while True:
        emit(type="assistant", message={"content": []})
        result("log line seen", 0.01, 1)
        time.sleep(0.05)
elif scenario == "deaf":  # Claude stops reading stdin, then still finishes
    emit(type="system", subtype="background_tasks_changed", tasks=[{"task_id": "t1", "description": "build"}])
    result("started", 0.01, 10)
    import os
    os.close(0)
    time.sleep(0.6)
    emit(type="system", subtype="background_tasks_changed", tasks=[])
    result("late finish", 0.02, 5)
    sys.exit(0)
elif scenario == "noexit":  # ignores the closed session
    result("done", 0.01, 10)
    while receive():
        pass
    time.sleep(60)
else:  # a dev server that never exits
    emit(type="system", subtype="background_tasks_changed", tasks=[{"task_id": "srv", "description": "dev server"}])
    result("serving", 0.01, 10)
    while receive():
        result("still serving", 0.02, 3)
    emit(type="system", subtype="task_updated", task_id="srv", patch={"status": "killed"})
    sys.exit(0)
while receive():
    pass
'''


class ClaudeSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.script = self.root / "claude.py"
        self.script.write_text(FAKE_CLAUDE)
        self.log = self.root / "stdin.log"

    def run_session(self, scenario, timeout=30, idle=10):
        """Run the fake Claude as a real process through the adapter's session driver."""
        started = time.monotonic()
        code, stdout, stderr = process([sys.executable, str(self.script), scenario, str(self.log)],
                                       "the prompt", self.root, timeout, stream=True, idle=idle)
        return code, stdout, time.monotonic() - started

    def messages(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_session_ends_after_a_turn_with_nothing_pending(self):
        code, stdout, elapsed = self.run_session("none")
        self.assertEqual(code, 0)
        self.assertLess(elapsed, 5)
        self.assertEqual(self.messages(), ["the prompt"])
        self.assertEqual(normalize("claude", stdout, structured=True).data, {"turn": "done"})

    def test_session_waits_for_background_task_and_agent_continues(self):
        code, stdout, elapsed = self.run_session("background")
        self.assertEqual(code, 0)
        self.assertEqual(self.messages(), ["the prompt"])  # no nudge was needed
        result = normalize("claude", stdout, structured=True)
        # The final turn is the Result; tokens sum across turns, cost is cumulative.
        self.assertEqual((result.message, result.data), ("finished", {"turn": "finished"}))
        self.assertEqual(result.usage, {"input_tokens": 2, "output_tokens": 17, "cost_usd": 0.03})
        self.assertNotIn("killed_background_tasks", result.native)

    def test_never_ending_task_is_nudged_once_then_the_session_closes(self):
        code, stdout, elapsed = self.run_session("stuck", idle=0.3)
        self.assertEqual(code, 0)
        messages = self.messages()
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0], "the prompt")
        self.assertIn("srv (dev server)", messages[1])
        self.assertIn("TaskStop", messages[1])
        self.assertLess(elapsed, 10)
        result = normalize("claude", stdout, structured=True)
        self.assertEqual(result.message, "still serving")
        self.assertEqual(result.native["killed_background_tasks"], ["srv"])

    def test_silent_turn_after_background_work_is_not_interrupted(self):
        code, stdout, elapsed = self.run_session("midturn", idle=1.0)
        self.assertEqual(self.messages(), ["the prompt"])  # no nudge mid-turn
        self.assertEqual(normalize("claude", stdout, structured=True).message, "tested")

    def test_chatty_never_ending_task_is_still_bounded(self):
        code, stdout, elapsed = self.run_session("chatty", idle=0.3)
        self.assertLess(elapsed, 10)
        messages = self.messages()
        self.assertEqual(len(messages), 2)
        self.assertIn("srv (dev server)", messages[1])
        self.assertIn("srv", normalize("claude", stdout, structured=True).native["killed_background_tasks"])

    def test_output_is_drained_when_claude_stops_reading(self):
        code, stdout, elapsed = self.run_session("deaf", idle=0.2)
        self.assertEqual(code, 0)
        self.assertEqual(normalize("claude", stdout, structured=True).message, "late finish")

    def test_claude_that_does_not_exit_after_close_is_killed(self):
        started = time.monotonic()
        code, stdout, stderr = process([sys.executable, str(self.script), "noexit", str(self.log)],
                                       "the prompt", self.root, None, stream=True, idle=10, grace=0.3)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(code, 0)  # the session was over; its final turn succeeded
        self.assertEqual(normalize("claude", stdout, returncode=code, structured=True).message, "done")

    def test_final_turn_decides_the_outcome(self):
        error = {"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "transient",
                 "usage": {"input_tokens": 1, "output_tokens": 1}}
        success = {"type": "result", "subtype": "success", "is_error": False, "result": "ok",
                   "usage": {"input_tokens": 1, "output_tokens": 2}, "total_cost_usd": 0.5}
        result = normalize("claude", "\n".join(json.dumps(e) for e in (error, success)))
        self.assertEqual((result.message, result.usage["output_tokens"]), ("ok", 3))
        with self.assertRaises(Failure):
            normalize("claude", "\n".join(json.dumps(e) for e in (success, error)))

    def test_node_timeout_still_bounds_a_waiting_session(self):
        with self.assertRaises(Failure) as raised:
            self.run_session("stuck", timeout=0.5, idle=30)
        self.assertEqual(raised.exception.kind, "timeout")
        self.assertTrue(raised.exception.retryable)
        self.assertIn("serving", raised.exception.result.raw_stdout)


if __name__ == "__main__":
    unittest.main()
