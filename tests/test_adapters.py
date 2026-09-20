import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from gitweave.adapters import CLIAdapter, normalize, process
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

    def assert_environment_inherited(self, parent):
        with patch.dict(os.environ, parent, clear=True), tempfile.TemporaryDirectory() as cwd:
            code, stdout, stderr = process(
                ["/usr/bin/env"], "", cwd, 10)
            self.assertEqual(code, 0, stderr)
            self.assertEqual(dict(line.split("=", 1) for line in stdout.splitlines()), parent)
            self.assertEqual(dict(os.environ), parent)

    def test_environment_inherits_development_and_native_authentication(self):
        keys = (
            "PATH", "HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "LANG", "LC_CTYPE",
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "https_proxy",
            "NPM_CONFIG_REGISTRY", "NPM_TOKEN", "PIP_INDEX_URL", "UV_INDEX_URL",
            "CARGO_HOME", "RUSTUP_HOME", "JAVA_HOME", "VIRTUAL_ENV", "PYTHONPATH",
            "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CUSTOM_DEVELOPMENT_SETTING",
            "CODEX_HOME", "OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL",
            "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
            "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR",
            "AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
            "GOOGLE_APPLICATION_CREDENTIALS", "CLAUDE_CODE_USE_BEDROCK",
            "GIT_AUTHOR_NAME", "GIT_COMMITTER_EMAIL", "GIT_EDITOR", "GIT_PAGER",
            "GIT_LFS_SKIP_SMUDGE", "GIT_OPTIONAL_LOCKS", "GH_PAGER", "SSH_TTY",
        )
        parent = {key: "fake-" + key for key in keys}
        self.assert_environment_inherited(parent)

    def test_environment_inherits_github_ssh_and_git_families(self):
        families = {
            "github": (
                "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
                "GH_HOST", "GH_REPO", "GH_CONFIG_DIR"),
            "ssh": ("SSH_AUTH_SOCK", "SSH_AGENT_PID", "SSH_ASKPASS", "SSH_ASKPASS_REQUIRE"),
            "repository": (
                "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
                "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE",
                "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM", "GIT_SHALLOW_FILE",
                "GIT_REPLACE_REF_BASE", "GIT_NO_REPLACE_OBJECTS"),
            "config": (
                "GIT_CONFIG", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM",
                "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0",
                "GIT_CONFIG_VALUE_0", "GIT_CONFIG_KEY_123", "GIT_CONFIG_VALUE_123",
                "GIT_EXEC_PATH", "GIT_TEMPLATE_DIR"),
            "credential": (
                "GIT_ASKPASS", "GIT_TERMINAL_PROMPT", "GIT_CREDENTIAL_HELPER",
                "GIT_CREDENTIAL_INTERACTIVE"),
            "transport": (
                "GIT_SSH", "GIT_SSH_COMMAND", "GIT_SSH_VARIANT", "GIT_PROXY_COMMAND",
                "GIT_ALLOW_PROTOCOL", "GIT_PROTOCOL", "GIT_PROTOCOL_FROM_USER",
                "GIT_SSL_NO_VERIFY", "GIT_SSL_CAINFO", "GIT_SSL_CAPATH", "GIT_SSL_CERT",
                "GIT_SSL_KEY", "GIT_SSL_CERT_PASSWORD_PROTECTED", "GIT_PROXY_SSL_CAINFO",
                "GIT_PROXY_SSL_CERT", "GIT_PROXY_SSL_KEY", "GIT_PROXY_SSL_CERT_PASSWORD_PROTECTED"),
        }
        for family, keys in families.items():
            with self.subTest(family=family):
                parent = {key: "fake-authority" for key in keys}
                parent["CUSTOM_TOOLCHAIN_SETTING"] = "fake-development"
                self.assert_environment_inherited(parent)

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

    def test_sandbox_commands_preserve_boundary_and_inherit_environment(self):
        parent = {"HOME": "/fake/home", "CODEX_HOME": "/fake/codex",
                  "OPENAI_API_KEY": "fake-native", "GH_TOKEN": "fake-system-action",
                  "GITHUB_TOKEN": "fake-github", "SSH_AUTH_SOCK": "/fake/socket",
                  "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader",
                  "GIT_CONFIG_VALUE_0": "fake-authority"}
        for provider, options, expected in [
                ("codex", {}, "danger-full-access"),
                *(("codex", {"sandbox": mode}, mode) for mode in
                  ("read-only", "workspace-write", "danger-full-access")),
                ("claude", {}, None)]:
            raw = (Path(__file__).parent / "fixtures" / f"{provider}.jsonl").read_text()
            with self.subTest(provider=provider, options=options), patch.dict(os.environ, parent, clear=True), \
                    patch("gitweave.adapters.subprocess.Popen") as popen:
                popen.return_value.communicate.return_value = (raw, "")
                popen.return_value.returncode = 0
                CLIAdapter(provider).run(dict(instruction="work", **options), {}, Path("/fake/worktree"), 10)
                command = popen.call_args.args[0]
                if provider == "codex":
                    self.assertEqual(command, ["codex", "exec", "--json", "--sandbox", expected,
                                               "-C", "/fake/worktree", "-"])
                else:
                    self.assertEqual(command, ["claude", "-p", "--output-format", "stream-json",
                                               "--verbose"])
                self.assertEqual(popen.call_args.kwargs["cwd"], Path("/fake/worktree"))
                self.assertNotIn("env", popen.call_args.kwargs)
                self.assertEqual(dict(os.environ), parent)
                prompt = popen.return_value.communicate.call_args.args[0]
                self.assertIn("official artifact boundary", prompt)
                self.assertIn("Do not publish, push, or merge remote branches", prompt)

    def test_adapter_rejects_unsupported_configuration_before_launch(self):
        cases = [("codex", {"sandbox": value}) for value in
                 (None, True, False, 1, [], {}, "", "unrestricted")]
        cases += [(provider, {"sandbox": value}) for provider in ("claude", "unknown")
                  for value in (None, "read-only", "workspace-write", "danger-full-access")]
        cases += [("unknown", {}), ("codex", {"provider": "claude"})]
        for provider, options in cases:
            with self.subTest(provider=provider, options=options), \
                    patch("gitweave.adapters.process") as invoke:
                with self.assertRaises(Failure) as raised:
                    CLIAdapter(provider).run(dict(instruction="work", **options), {}, Path("/tmp"), 10)
                self.assertFalse(raised.exception.retryable)
                invoke.assert_not_called()

    def test_claude_permission_modes_pass_through_with_native_options(self):
        import json
        raw = (Path(__file__).parent / "fixtures" / "claude.jsonl").read_text()
        for mode in (None, "acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"):
            options = {} if mode is None else {"permission_mode": mode}
            schema = {"type": "object"}
            envelope = {"type": "object", "properties": {"message": {"type": "string"}, "data": schema},
                        "required": ["message", "data"], "additionalProperties": False}
            with self.subTest(mode=mode), patch("gitweave.adapters.process", return_value=(0, raw, "")) as invoke:
                result = CLIAdapter("claude").run(
                    dict(kind="agent", provider="claude", instruction="work", model="chosen",
                         effort="high", schema=schema, **options), {}, Path("/fake/worktree"), 10)
                expected = ["claude", "-p", "--output-format", "stream-json", "--verbose"]
                if mode is not None:
                    expected += ["--permission-mode", mode]
                expected += ["--effort", "high", "--json-schema", json.dumps(envelope), "--model", "chosen"]
                self.assertEqual(invoke.call_args.args[0], expected)
                self.assertEqual(invoke.call_args.args[2:], (Path("/fake/worktree"), 10))
                self.assertEqual(result.data, {"ok": True})

    def test_invalid_permission_modes_fail_before_launch(self):
        cases = [("claude", {"permission_mode": value}) for value in
                 (None, True, False, 1, 1.5, [], {}, "", "default", "AUTO", " auto", "unknown")]
        cases += [(provider, {"permission_mode": value}) for provider in ("codex", "custom")
                  for value in (None, "auto", "acceptEdits")]
        cases += [("claude", {"kind": "action", "permission_mode": "auto"})]
        for provider, options in cases:
            with self.subTest(provider=provider, options=options), patch("gitweave.adapters.process") as invoke:
                with self.assertRaises(Failure) as raised:
                    CLIAdapter(provider).run(dict(instruction="work", **options), {}, Path("/tmp"), 10)
                self.assertFalse(raised.exception.retryable)
                invoke.assert_not_called()

    def test_native_limit_variants_do_not_retry(self):
        examples = [
            ("codex", '{"type":"item.completed","item":{"type":"agent_message","text":"limit message"}}\n{"type":"turn.failed","error":{"message":"You have hit your limit"}}'),
            ("claude", '{"type":"rate_limit_event","rate_limit_info":{"status":"rejected"}}'),
            ("claude", '{"type":"result","subtype":"error_during_execution","is_error":true,"result":"Your credit balance is too low"}')]
        for provider, raw in examples:
            with self.subTest(provider=provider, raw=raw), self.assertRaises(Failure) as raised:
                normalize(provider, raw, structured=True)
            self.assertEqual(raised.exception.kind, "usage_limit")
            self.assertFalse(raised.exception.retryable)

    def test_missing_structured_output_is_not_a_valid_null(self):
        for provider, raw in [("codex", '{"type":"turn.completed","usage":{}}'),
                              ("claude", '{"type":"result","subtype":"success","result":"no structured result"}')]:
            with self.subTest(provider=provider), self.assertRaises(Failure) as raised:
                normalize(provider, raw, structured=True)
            self.assertEqual(raised.exception.kind, "protocol")
