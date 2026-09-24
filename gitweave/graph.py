"""Validation for versioned, structured graphs."""
import math
import re
from .model import Failure, check_schema


def require(condition, message):
    if not condition:
        raise Failure("graph", message)


def validate_sandbox(node, provider):
    if "sandbox" in node:
        require(provider == "codex", f"sandbox is unsupported for provider {provider!r}")
        require(isinstance(node["sandbox"], str), "sandbox must be text")


def validate_permission_mode(node, provider):
    if "permission_mode" in node:
        require(node.get("kind", "agent") == "agent",
                "permission_mode is only supported on Claude agent nodes")
        require(provider == "claude", f"permission_mode is unsupported for provider {provider!r}")
        require(isinstance(node["permission_mode"], str), "permission_mode must be text")


def validate_timeout(timeout, message):
    require(type(timeout) in (float, int) and math.isfinite(timeout) and timeout > 0, message)


def validate_graph(graph):
    require(isinstance(graph, dict), "Graph must be an object")
    require(set(graph) <= {"version", "nodes", "flow", "concurrency", "max_steps", "retries", "timeout"}, "Unknown graph field")
    require(type(graph.get("version")) is int and graph["version"] == 1, "Graph version must be 1")
    for key, default, minimum in [("concurrency", 4, 1), ("max_steps", 100, 1), ("retries", 0, 0)]:
        value = graph.get(key, default)
        require(type(value) is int and value >= minimum, f"{key} must be an integer >= {minimum}")
    if "timeout" in graph:
        validate_timeout(graph["timeout"], "timeout must be finite and positive")
    nodes = graph.get("nodes")
    require(isinstance(nodes, dict) and bool(nodes), "nodes must be a nonempty object")
    for name, node in nodes.items():
        require(isinstance(name, str) and bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", name)), "Invalid node ID")
        require(isinstance(node, dict), f"{name}: node must be an object")
        require(set(node) <= {"kind", "provider", "model", "effort", "sandbox", "permission_mode", "instruction", "schema", "workspace_base", "argv", "config", "retries", "timeout"}, f"{name}: unknown node field")
        require(node.get("kind") in ("agent", "command"), f"{name}: unknown kind")
        base = node.get("workspace_base")
        require(base == "run" or (type(base) is int and base >= 0), f"{name}: workspace_base must be 'run' or an input index")
        if "retries" in node:
            require(type(node["retries"]) is int and node["retries"] >= 0, f"{name}: retries must be an integer >= 0")
        if "timeout" in node:
            validate_timeout(node["timeout"], f"{name}: timeout must be finite and positive")
        if node["kind"] == "agent":
            require(not {"argv", "config"} & set(node), f"{name}: argv and config are only supported on command nodes")
            require(isinstance(node.get("provider"), str) and bool(node["provider"]), f"{name}: provider required")
            validate_sandbox(node, node["provider"])
            validate_permission_mode(node, node["provider"])
            require(isinstance(node.get("instruction"), str), f"{name}: instruction required")
            for option in ("model", "effort"):
                require(option not in node or isinstance(node[option], str), f"{name}: {option} must be text")
        else:
            require(not {"provider", "model", "effort", "sandbox", "permission_mode", "instruction"} & set(node),
                    f"{name}: agent options are not supported on command nodes")
            argv = node.get("argv")
            require(isinstance(argv, list) and bool(argv) and all(isinstance(arg, str) for arg in argv) and bool(argv[0]),
                    f"{name}: argv must be a nonempty list of strings")
        if "schema" in node:
            check_schema(node["schema"])

    def condition(value):
        require(isinstance(value, dict) and set(value) == {"path", "equals"}, "Condition requires path and equals")
        require(isinstance(value["path"], str) and (not value["path"] or value["path"].startswith("/")), "Condition path must be a JSON pointer")

    def flow(items, allow_empty=False):
        require(isinstance(items, list) and (allow_empty or bool(items)), "Flow must be a nonempty list (except if branches)")
        for item in items:
            if isinstance(item, str):
                require(item in nodes, f"Unknown node: {item}")
                continue
            require(isinstance(item, dict) and len(item) == 1, "Control block must have exactly one key")
            op, spec = next(iter(item.items()))
            if op == "parallel":
                require(isinstance(spec, list) and bool(spec), "parallel requires branches")
                for branch in spec:
                    flow(branch)
            elif op == "map":
                require(isinstance(spec, dict) and set(spec) == {"path", "flow"}, "map requires path and flow")
                require(isinstance(spec["path"], str) and (not spec["path"] or spec["path"].startswith("/")), "map path must be a JSON pointer")
                flow(spec["flow"])
            elif op == "if":
                require(isinstance(spec, dict) and set(spec) == {"condition", "then", "else"}, "if requires condition, then, else")
                condition(spec["condition"])
                flow(spec["then"], allow_empty=True)
                flow(spec["else"], allow_empty=True)
            elif op == "loop":
                require(isinstance(spec, dict) and set(spec) == {"flow", "while"}, "loop requires flow and while")
                condition(spec["while"])
                flow(spec["flow"])
            else:
                raise Failure("graph", f"Unknown control block: {op}")
    flow(graph.get("flow"))
    return graph
