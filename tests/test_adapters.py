import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from gitweave.adapters import CLIAdapter, agent_environment, normalize, process
from gitweave.model import Failure


class AdapterTests(unittest.TestCase):
    def test_native_fixtures(self):
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                raw = (Path(__file__).parent / "fixtures" / f"{provider}.jsonl").read_text()
                result = normalize(provider, raw, structured=True)
                self.assertEqual(result.message, "done")
                self.assertEqual(result.data, {"ok": True})
                self.assertEqual(result.usage["cached_input_tokens"], 80)
                self.assertEqual(result.session_id, f"{provider}-session")
                self.assertEqual(result.raw_stdout, raw)
                self.assertTrue(result.native["events"])

    def test_failure_diagnostics_and_limits(self):
        for provider, raw in [("codex", '{"type":"turn.failed","error":{"message":"usage limit reached"}}'),
                              ("claude", '{"type":"result","subtype":"error_during_execution","is_error":true,"errors":["rate_limit"]}')]:
            with self.assertRaises(Failure) as raised:
                normalize(provider, raw, returncode=1)
            self.assertEqual(raised.exception.kind, "usage_limit")
            self.assertFalse(raised.exception.retryable)
            self.assertEqual(raised.exception.result.raw_stdout, raw)
        with self.assertRaises(Failure):
            normalize("codex", '{"type":"thread.started"}')

    def test_environment_excludes_publication_credentials(self):
        with patch.dict(os.environ, {"GH_TOKEN": "private", "GITHUB_TOKEN": "private", "GIT_CONFIG_COUNT": "1", "SSH_AUTH_SOCK": "private", "OPENAI_API_KEY": "agent"}):
            env = agent_environment()
            for key in ("GH_TOKEN", "GITHUB_TOKEN", "GIT_CONFIG_COUNT", "SSH_AUTH_SOCK"):
                self.assertNotIn(key, env)
            self.assertEqual(env["OPENAI_API_KEY"], "agent")

    def test_commands_use_native_cli_and_structured_envelope(self):
        for provider in ("codex", "claude"):
            raw = (Path(__file__).parent / "fixtures" / f"{provider}.jsonl").read_text()
            with patch("gitweave.adapters.process", return_value=(0, raw, "")) as invoke:
                CLIAdapter(provider).run({"instruction": "work", "model": "chosen", "effort": "high", "schema": {"type": "object"}}, {}, Path("/tmp"), 10)
                command = invoke.call_args.args[0]
                self.assertIn("--model", command)
                self.assertIn("chosen", command)
                self.assertNotIn("--dangerously-skip-permissions", command)
                self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)

    def test_timeout_preserves_partial_output(self):
        import sys
        with tempfile.TemporaryDirectory() as cwd:
            with self.assertRaises(Failure) as raised:
                process([sys.executable, "-c", "import time; print('partial', flush=True); time.sleep(5)"], "", cwd, 0.1)
        self.assertEqual(raised.exception.kind, "timeout")
        self.assertIn("partial", raised.exception.result.raw_stdout)
