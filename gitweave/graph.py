"""Validation for versioned, structured graphs."""
import math
import re
from .model import Failure, check_schema


def require(condition, message):
    if not condition:
        raise Failure("graph", message)


def validate_graph(graph):
    require(isinstance(graph, dict), "Graph must be an object")
    require(set(graph) <= {"version", "nodes", "flow", "concurrency", "max_steps", "retries", "timeout"}, "Unknown graph field")
    require(type(graph.get("version")) is int and graph["version"] == 1, "Graph version must be 1")
    for key, default, minimum in [("concurrency", 4, 1), ("max_steps", 100, 1), ("retries", 0, 0)]:
        value = graph.get(key, default)
        require(type(value) is int and value >= minimum, f"{key} must be an integer >= {minimum}")
    timeout = graph.get("timeout", 600)
    require(type(timeout) in (float, int) and math.isfinite(timeout) and timeout > 0, "timeout must be finite and positive")
    nodes = graph.get("nodes")
    require(isinstance(nodes, dict) and bool(nodes), "nodes must be a nonempty object")
    for name, node in nodes.items():
        require(isinstance(name, str) and bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", name)), "Invalid node ID")
        require(isinstance(node, dict), f"{name}: node must be an object")
        require(set(node) <= {"kind", "provider", "model", "effort", "instruction", "schema", "workspace_base", "action", "config"}, f"{name}: unknown node field")
        require(node.get("kind") in ("agent", "action"), f"{name}: unknown kind")
        base = node.get("workspace_base")
        require(base == "run" or (type(base) is int and base >= 0), f"{name}: workspace_base must be 'run' or an input index")
        if node["kind"] == "agent":
            require(isinstance(node.get("provider"), str) and bool(node["provider"]), f"{name}: provider required")
            require(isinstance(node.get("instruction"), str), f"{name}: instruction required")
            for option in ("model", "effort"):
                require(option not in node or isinstance(node[option], str), f"{name}: {option} must be text")
        else:
            require(node.get("action") in ("publish_pr", "merge_pr"), f"{name}: unknown action")
            cfg = node.get("config", {})
            require(isinstance(cfg, dict), f"{name}: config must be an object")
            require(set(cfg) <= {"repository", "base", "title", "body", "publish_node"}, f"{name}: unknown action option")
            require(isinstance(cfg.get("repository"), str) and bool(re.fullmatch(r"[\w.-]+/[\w.-]+", cfg["repository"])), f"{name}: repository must be owner/name")
            require("body" not in cfg or isinstance(cfg["body"], str), f"{name}: body must be text")
            if node["action"] == "publish_pr":
                require(isinstance(cfg.get("base"), str) and bool(cfg["base"]) and not cfg["base"].startswith("-"), f"{name}: base branch required")
                require(isinstance(cfg.get("title"), str) and bool(cfg["title"]), f"{name}: title required")
            else:
                require(isinstance(cfg.get("publish_node"), str), f"{name}: publish_node must be a node ID")
                pub = nodes.get(cfg["publish_node"], {})
                require(isinstance(pub, dict) and isinstance(pub.get("config"), dict), f"{name}: invalid publisher")
                require(pub.get("action") == "publish_pr" and pub.get("config", {}).get("repository") == cfg["repository"], f"{name}: matching publish_node required")
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
