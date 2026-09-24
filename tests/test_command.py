"""Command Nodes run real local processes in their worktree; no network or live agents."""
import json
import sys
from pathlib import Path
import tempfile
import time
import unittest

from gitweave.model import Result
from gitweave.runtime import Runtime
from test_runtime import Fake, git, graph, node


def command(script, **options):
    return dict(kind="command", workspace_base=0, argv=[sys.executable, "-c", script], **options)


ECHO = """import json, sys
context = json.load(sys.stdin)
print("log line", file=sys.stderr)
print(json.dumps({"message": "done", "data": context}))
"""


class CommandNodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        (self.repo / "scripts").mkdir(parents=True)
        (self.repo / "scripts" / "op").write_text("#!/bin/sh\necho \"$PWD\" > op-ran\n"
                                                 "echo '{\"message\": \"ran\", \"data\": null}'\n")
        (self.repo / "scripts" / "op").chmod(0o755)
        git(self.repo, "init", "-q")
        git(self.repo, "add", ".")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-qm", "base")
        self.base = git(self.repo, "rev-parse", "HEAD")

    def runtime(self, nodes, flow, work=None, **options):
        return Runtime(graph(nodes, flow, **options), self.repo, self.base, "request",
                       adapters={"fake": Fake(work or (lambda *a: Result()))})

    def note(self, run, commit):
        return json.loads(git(self.repo, "notes", f"--ref={run.git.notes}", "show", commit))

    def test_context_on_stdin_result_on_stdout_and_same_audit_model(self):
        write = ECHO.replace('print("log line"', 'open("made", "w").write("file");print("log line"')
        downstream = []
        def work(n, c, w):
            downstream.append(c["inputs"][0])
            self.assertEqual((w / "made").read_text(), "file")
            return Result(message="agent")
        run = self.runtime({"cmd": command(write, config={"key": ["value"]}, schema={"type": "object"}),
                            "agent": node()}, ["cmd", "agent"], work)
        record = run.run()
        self.assertEqual(record["status"], "completed", record.get("failure"))
        context = downstream[0]["data"]
        self.assertEqual(downstream[0]["message"], "done")
        self.assertEqual(context["config"], {"key": ["value"]})
        self.assertEqual(context["run_input"], {"kind": "commit", "commit": self.base})
        self.assertIsNone(context["github_repository"])
        self.assertEqual(context["request"], "request")
        self.assertEqual(set(context), {"request", "github_repository", "run_input", "item", "inputs", "config"})
        self.assertEqual(context["inputs"], [{"node_id": None, "commit": self.base, "message": "request", "data": None}])
        cmd = self.note(run, record["attempts"][0]["commit"])
        agent = self.note(run, record["attempts"][1]["commit"])
        self.assertEqual(set(agent) - {"argv", "config"}, set(cmd) - {"argv", "config"})
        self.assertEqual((cmd["kind"], cmd["argv"], cmd["config"], cmd["status"]),
                         ("command", [sys.executable, "-c", write], {"key": ["value"]}, "completed"))
        self.assertEqual(cmd["result"]["native"]["returncode"], 0)
        self.assertEqual(cmd["result"]["raw_stderr"], "log line\n")
        self.assertIn('"message": "done"', cmd["result"]["raw_stdout"])
        self.assertEqual(git(self.repo, "show", record["attempts"][0]["commit"] + ":made"), "file")

    def test_cwd_is_the_worktree_and_relative_argv_resolves_there(self):
        run = self.runtime({"cmd": dict(kind="command", workspace_base=0, argv=["./scripts/op"])}, ["cmd"])
        record = run.run()
        self.assertEqual(record["status"], "completed", record.get("failure"))
        ran = git(self.repo, "show", record["outputs"][0]["commit"] + ":op-ran")
        self.assertNotEqual(Path(ran).resolve(), self.repo.resolve())
        self.assertIn("gitweave-", ran)

    def test_unchanged_tree_still_gets_its_own_checkpoint(self):
        run = self.runtime({"cmd": command('print(\'{"message": "", "data": null}\')')}, ["cmd"])
        commit = run.run()["outputs"][0]["commit"]
        self.assertNotEqual(commit, self.base)
        self.assertEqual(git(self.repo, "rev-parse", commit + "^"), self.base)
        self.assertEqual(git(self.repo, "rev-parse", commit + "^{tree}"), git(self.repo, "rev-parse", self.base + "^{tree}"))
        self.assertEqual(self.note(run, commit)["kind"], "command")

    def test_invalid_process_outcomes_are_retryable_runtime_failures(self):
        cases = {
            "nonzero exit": ('import sys; print("boom", file=sys.stderr); sys.exit(3)', "status 3\nboom"),
            "empty stdout": ("pass", "must be one"),
            "non-JSON": ('print("hello")', "must be one"),
            "not an object": ('print("[]")', "must be one"),
            "missing data": ('print(\'{"message": "m"}\')', "must be one"),
            "extra field": ('print(\'{"message": "m", "data": 1, "x": 2}\')', "must be one"),
            "message not text": ('print(\'{"message": 1, "data": 1}\')', "must be one"),
            "non-UTF-8": ('import sys; sys.stdout.buffer.write(b"\\xff\\xfe")', "not valid UTF-8"),
            "NaN": ('print(\'{"message": "m", "data": NaN}\')', "must be one"),
            "schema failure": ('print(\'{"message": "m", "data": "text"}\')', "data must be object"),
        }
        for label, (script, diagnostic) in cases.items():
            with self.subTest(label=label):
                run = self.runtime({"cmd": command(script, schema={"type": "object"})}, ["cmd"], retries=1)
                record = run.run()
                self.assertEqual(record["status"], "failed")
                self.assertIn(diagnostic, record["failure"]["message"])
                notes = [self.note(run, a["commit"]) for a in record["attempts"]]
                self.assertEqual([n["status"] for n in notes], ["failed", "failed"])
                self.assertTrue(all(n["failure"]["retryable"] for n in notes))

    def test_launch_failure_is_retryable(self):
        run = self.runtime({"cmd": dict(kind="command", workspace_base=0, argv=["./missing"])}, ["cmd"], retries=1)
        record = run.run()
        self.assertEqual(record["failure"]["kind"], "launch")
        self.assertEqual(len(record["attempts"]), 2)

    def test_node_retries_override_graph_retries(self):
        fail = 'import sys; sys.exit(1)'
        for graph_retries, node_retries, attempts in ((3, 0, 1), (0, 2, 3), (1, None, 2)):
            with self.subTest(graph_retries=graph_retries, node_retries=node_retries):
                options = {} if node_retries is None else {"retries": node_retries}
                record = self.runtime({"cmd": command(fail, **options)}, ["cmd"], retries=graph_retries).run()
                self.assertEqual(len(record["attempts"]), attempts)
        calls = []
        def flaky(n, c, w):
            calls.append(1)
            raise __import__("gitweave.model").model.Failure("provider", "flaky", retryable=True)
        self.runtime({"a": node(retries=1)}, ["a"], flaky, retries=5).run()
        self.assertEqual(len(calls), 2)

    def test_node_timeout_overrides_graph_timeout(self):
        started = time.monotonic()
        record = self.runtime({"cmd": command("import time; time.sleep(30)", timeout=0.3)}, ["cmd"], timeout=60).run()
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(record["failure"]["kind"], "timeout")
        seen = []
        class Adapter:
            def run(self, n, c, w, timeout):
                seen.append(timeout)
                return Result()
        for options, expected in (({"timeout": 5}, 5), ({}, 60)):
            Runtime(graph({"a": node(**options)}, ["a"], timeout=60), self.repo, self.base, "request",
                    adapters={"fake": Adapter()}).run()
        self.assertEqual(seen, [5, 60])
