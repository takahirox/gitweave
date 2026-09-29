"""Test doubles for Claude stream-json sessions."""
import io
import json


class FakeChild:
    """A Popen stand-in: records stdin messages and replays scripted stdout lines."""
    def __init__(self, stdout, returncode=0, pid=4242):
        self.stdin = io.StringIO()
        self.stdin.close = lambda: setattr(self, "closed_stdin", True)
        self.closed_stdin = False
        self.stdout = io.StringIO(stdout if stdout.endswith("\n") or not stdout else stdout + "\n")
        self.stderr = io.StringIO("")
        self.returncode = returncode
        self.pid = pid
        self.args = ["claude"]

    def wait(self, timeout=None):
        return self.returncode

    def messages(self):
        return [json.loads(line)["message"]["content"] for line in self.stdin.getvalue().splitlines()]
