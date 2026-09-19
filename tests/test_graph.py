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
