import unittest
from gitweave.graph import validate_graph
from gitweave.model import Failure, validate


class GraphTests(unittest.TestCase):
    def good(self):
        return {"version": 1, "nodes": {"a": {"kind": "agent", "provider": "custom", "instruction": "work", "workspace_base": 0}}, "flow": ["a"]}

    def test_invalid_graphs_fail_before_execution(self):
        changes = [{"flow": ["missing"]}, {"flow": []}, {"max_steps": True}, {"concurrency": 0},
                   {"retries": -1}, {"timeout": float("inf")}, {"flow": [{"parallel": []}]},
                   {"flow": [{"loop": {"flow": [], "while": {"path": "", "equals": True}}}]},
                   {"unknown": True}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(Failure):
                validate_graph(dict(self.good(), **change))

    def test_optional_provider_specific_sandbox(self):
        for provider in ("codex", "claude", "custom"):
            graph = self.good()
            graph["nodes"]["a"]["provider"] = provider
            self.assertNotIn("sandbox", validate_graph(graph)["nodes"]["a"])
        for mode in ("read-only", "workspace-write", "danger-full-access"):
            graph = self.good()
            graph["nodes"]["a"].update(provider="codex", sandbox=mode)
            self.assertEqual(validate_graph(graph)["nodes"]["a"]["sandbox"], mode)

    def test_invalid_sandbox_configuration(self):
        for provider in ("codex", "claude", "custom"):
            for mode in (None, True, False, 1, [], {}, "", "unrestricted", "workspace-write", "danger-full-access", "read-only"):
                if provider == "codex" and isinstance(mode, str) and mode in (
                        "read-only", "workspace-write", "danger-full-access"):
                    continue
                graph = self.good()
                graph["nodes"]["a"].update(provider=provider, sandbox=mode)
                with self.subTest(provider=provider, mode=mode), self.assertRaises(Failure) as raised:
                    validate_graph(graph)
                self.assertEqual(raised.exception.kind, "graph")
                self.assertIn("sandbox", str(raised.exception))
        graph = self.good()
        graph["nodes"]["a"] = dict(kind="action", action="sync_pr", workspace_base=0,
                                    sandbox="workspace-write")
        with self.assertRaisesRegex(Failure, "sandbox is only supported"):
            validate_graph(graph)

    def test_optional_claude_permission_mode(self):
        for provider in ("claude", "codex", "custom"):
            graph = self.good()
            graph["nodes"]["a"]["provider"] = provider
            self.assertNotIn("permission_mode", validate_graph(graph)["nodes"]["a"])
        for mode in ("acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"):
            with self.subTest(mode=mode):
                graph = self.good()
                graph["nodes"]["a"].update(provider="claude", permission_mode=mode)
                self.assertEqual(validate_graph(graph)["nodes"]["a"]["permission_mode"], mode)

    def test_invalid_permission_mode_configuration(self):
        for provider in ("claude", "codex", "custom"):
            values = [None, True, False, 1, 1.5, [], {}, "", "default", "AUTO", " auto", "unknown"]
            if provider != "claude":
                values += ["acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"]
            for value in values:
                graph = self.good()
                graph["nodes"]["a"].update(provider=provider, permission_mode=value)
                with self.subTest(provider=provider, value=value), self.assertRaisesRegex(Failure, "permission_mode") as raised:
                    validate_graph(graph)
                self.assertEqual(raised.exception.kind, "graph")
        for action in ("publish_pr", "sync_pr", "merge_pr", "comment_issue", "comment_pr"):
            for value in (None, "auto"):
                graph = self.good()
                graph["nodes"]["a"] = dict(kind="action", action=action, workspace_base=0,
                                          provider="claude", permission_mode=value)
                with self.subTest(action=action, value=value), self.assertRaisesRegex(
                        Failure, "permission_mode is only supported on Claude agent nodes"):
                    validate_graph(graph)

    def test_timeout_omission_and_explicit_positive_values(self):
        validated = validate_graph(self.good())
        self.assertNotIn("timeout", validated)
        for timeout in (1, 0.25, 1800):
            with self.subTest(timeout=timeout):
                self.assertEqual(validate_graph(dict(self.good(), timeout=timeout))["timeout"], timeout)

    def test_invalid_explicit_timeouts(self):
        for timeout in (None, True, False, "1800", 0, -1, -0.5,
                        float("inf"), float("-inf"), float("nan"), [], {}):
            with self.subTest(timeout=timeout), self.assertRaises(Failure) as raised:
                validate_graph(dict(self.good(), timeout=timeout))
            self.assertEqual(raised.exception.kind, "graph")
            self.assertEqual(str(raised.exception), "timeout must be finite and positive")

    def test_unknown_schema_keywords_are_rejected(self):
        graph = self.good()
        graph["nodes"]["a"]["schema"] = {"type": "string", "pattern": "a"}
        with self.assertRaises(Failure):
            validate_graph(graph)

    def test_schema_checks_nested_data_and_boolean_is_not_integer(self):
        schema = {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "integer"}}}, "required": ["items"], "additionalProperties": False}
        validate({"items": [1, 2]}, schema)
        for data in ({}, {"items": [True]}, {"items": [], "other": 1}):
            with self.assertRaises(Failure):
                validate(data, schema)

    def test_malformed_types_have_deterministic_graph_errors(self):
        for value in (True, {}, [], "1"):
            with self.assertRaises(Failure):
                validate_graph(dict(self.good(), version=value))
        graph = self.good()
        for schema in ({"type": []}, {"type": "string", "description": []}):
            graph["nodes"]["a"]["schema"] = schema
            with self.assertRaises(Failure):
                validate_graph(graph)

    def test_examples_are_valid(self):
        import json
        from pathlib import Path
        for path in (Path(__file__).parent.parent / "examples").glob("*.json"):
            validate_graph(json.loads(path.read_text()))

    def test_json_pointer_array_indices_and_nested_equality(self):
        from gitweave.model import equal, pointer
        self.assertEqual(pointer({"a/b": {"~": ["value"]}}, "/a~1b/~0/0"), "value")
        for path in ("/-1", "/01", "/~2"):
            with self.assertRaises(Failure):
                pointer([1, 2], path)
        self.assertFalse(equal({"ok": [True]}, {"ok": [1]}))
        self.assertTrue(equal({"ok": [1]}, {"ok": [1]}))
