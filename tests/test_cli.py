import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from gitweave import cli


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.path = self.directory / "graph.json"
        # An unavailable provider and runtime-dependent input index are static-valid.
        self.graph = {"version": 1, "nodes": {
            "work": {"kind": "agent", "provider": "uninstalled-provider",
                     "instruction": "work", "workspace_base": 42},
            "publish": {"kind": "action", "action": "publish_pr", "workspace_base": "run",
                        "config": {"repository": "owner/repo", "base": "main", "title": "Work"}},
        }, "flow": ["work", "publish"]}
        self.path.write_text(json.dumps(self.graph))

    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["gitweave", *map(str, args)]), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                status = cli.main()
            except SystemExit as exc:
                status = exc.code
        return status, stdout.getvalue(), stderr.getvalue()

    def assert_input_error(self, result, diagnostic):
        status, stdout, stderr = result
        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertIn(diagnostic, stderr)
        self.assertNotIn("Traceback", stderr)

    def test_valid_graph_is_read_only_and_does_not_execute(self):
        before = self.path.read_bytes()
        with contextlib.chdir(self.directory), patch.dict(os.environ, {}, clear=True), \
                patch.object(cli, "Runtime", side_effect=AssertionError("Runtime constructed")), \
                patch("subprocess.Popen", side_effect=AssertionError("Process launched")), \
                patch("socket.socket", side_effect=AssertionError("Network accessed")), \
                patch.object(cli, "validate_graph", wraps=cli.validate_graph) as validator:
            status, stdout, stderr = self.invoke("validate", "--graph", "graph.json")
        self.assertEqual(status, 0)
        self.assertIn("passes static validation", stdout)
        self.assertEqual(stderr, "")
        validator.assert_called_once_with(self.graph)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.directory.iterdir()), [self.path])

    def test_graph_argument_is_required(self):
        self.assert_input_error(self.invoke("validate"), "--graph")

    def test_missing_file(self):
        self.assert_input_error(self.invoke("validate", "--graph", self.directory / "missing.json"),
                                "missing.json")

    def test_directory_is_not_a_graph_file(self):
        self.assert_input_error(self.invoke("validate", "--graph", self.directory), str(self.directory))

    def test_unreadable_file(self):
        # Mock permission failure so this also works when tests run as root.
        with patch.object(Path, "read_text", side_effect=PermissionError(13, "Permission denied", str(self.path))):
            self.assert_input_error(self.invoke("validate", "--graph", self.path), "Permission denied")

    def test_malformed_json(self):
        self.path.write_text('{"version":')
        self.assert_input_error(self.invoke("validate", "--graph", self.path), "line 1 column")

    def test_invalid_text_encoding(self):
        self.path.write_bytes(b'\xff')
        self.assert_input_error(self.invoke("validate", "--graph", self.path), "decode")

    def test_graph_definition_errors(self):
        unknown = dict(self.graph, flow=["missing"])
        schema = json.loads(json.dumps(self.graph))
        schema["nodes"]["work"]["schema"] = {"type": "string", "pattern": "a"}
        for graph, diagnostic in [([], "Graph must be an object"),
                                  (unknown, "Unknown node: missing"),
                                  (schema, "Unsupported result schema keyword")]:
            with self.subTest(diagnostic=diagnostic):
                self.path.write_text(json.dumps(graph))
                self.assert_input_error(self.invoke("validate", "--graph", self.path), diagnostic)

    def check_entry_point(self, command, *, source=False):
        # Empty PATH/home prevent dependency on Git, agent CLIs, or credentials.
        environment = {"PATH": "", "HOME": str(self.directory), "PYTHONDONTWRITEBYTECODE": "1"}
        if source:
            environment["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
        result = subprocess.run([*command, "validate", "--graph", str(self.path)],
                                cwd=self.directory, env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("passes static validation", result.stdout)
        self.assertEqual(result.stderr, "")
        self.assertEqual(list(self.directory.iterdir()), [self.path])
        result = subprocess.run([*command, "validate"], cwd=self.directory, env=environment,
                                capture_output=True, text=True)
        self.assert_input_error((result.returncode, result.stdout, result.stderr), "--graph")

    def test_python_module_entry_point(self):
        self.check_entry_point([sys.executable, "-m", "gitweave"], source=True)

    @unittest.skipUnless(shutil.which("gitweave"), "Requires package installation (as in CI)")
    def test_installed_entry_point(self):
        self.check_entry_point([shutil.which("gitweave")])

    def test_run_behavior_is_preserved(self):
        for status in ("completed", "failed"):
            with self.subTest(status=status), patch.object(cli, "Runtime") as runtime:
                record = dict(run_id="id", status=status, repository="owner/repo", run_ref="run", notes_ref="notes",
                              outputs=[], failure={"kind": "test", "message": "failed"})
                runtime.return_value.run.return_value = record
                code, stdout, stderr = self.invoke("run", "--graph", self.path, "--repo", self.directory,
                                                   "--commit", "HEAD", "request")
                runtime.assert_called_once_with(self.path.read_text(), str(self.directory), "HEAD", "request",
                                                pr=None, issue=None, provenance_remote=None)
                self.assertEqual(code, 0 if status == "completed" else 1)
                self.assertEqual(json.loads(stdout), {key: record[key] for key in
                                                     ("run_id", "status", "repository", "run_ref", "notes_ref", "outputs")})
                self.assertEqual(stderr, "" if status == "completed" else json.dumps(record["failure"]) + "\n")

    def test_run_pr_and_provenance_options_are_forwarded(self):
        with patch.object(cli, "Runtime") as runtime:
            record = dict(run_id="id", status="completed", repository="owner/repo",
                          run_ref="run", notes_ref="notes", outputs=[])
            runtime.return_value.run.return_value = record
            code, stdout, stderr = self.invoke(
                "run", "--graph", self.path, "--repo", "owner/repo", "--pr", "8",
                "--provenance-remote", "origin", "request")
            runtime.assert_called_once_with(self.path.read_text(), "owner/repo", None, "request",
                                            pr=8, issue=None, provenance_remote="origin")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(stdout), record)
            self.assertEqual(stderr, "")

    def test_run_requires_exactly_one_commit_or_pr(self):
        for source, diagnostic in [([], "required"),
                                   (["--commit", "HEAD", "--pr", "8"], "not allowed")]:
            with self.subTest(source=source), patch.object(cli, "Runtime") as runtime:
                self.assert_input_error(self.invoke(
                    "run", "--graph", self.path, "--repo", "owner/repo", *source, "request"),
                    diagnostic)
                runtime.assert_not_called()
