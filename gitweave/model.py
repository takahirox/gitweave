"""Portable result contracts and a deliberately small JSON Schema dialect."""
from dataclasses import asdict, dataclass, field
from typing import Any
import math
import re


class Failure(Exception):
    def __init__(self, kind, message, *, retryable=False, result=None):
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable
        self.result = result


@dataclass
class Result:
    message: str = ""
    data: Any = None
    usage: dict = field(default_factory=dict)
    session_id: str | None = None
    native: dict = field(default_factory=dict)
    raw_stdout: str = ""
    raw_stderr: str = ""

    def record(self):
        return asdict(self)


def pointer(value, path):
    """Resolve an RFC 6901 JSON pointer; never evaluate graph-supplied code."""
    if not isinstance(path, str) or (path and not path.startswith("/")):
        raise Failure("graph", f"Invalid JSON pointer: {path!r}")
    try:
        for part in path.split("/")[1:]:
            if re.search(r"~(?![01])", part):
                raise ValueError("Invalid JSON pointer escape")
            key = part.replace("~1", "/").replace("~0", "~")
            if isinstance(value, list) and not re.fullmatch(r"0|[1-9][0-9]*", key):
                raise ValueError("Invalid array index")
            value = value[int(key)] if isinstance(value, list) else value[key]
        return value
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise Failure("result", f"Missing result path: {path}") from exc


TYPES = {"object": dict, "array": list, "string": str, "integer": int,
         "number": (int, float), "boolean": bool, "null": type(None)}


def equal(left, right):
    """JSON equality with strict types, including nested booleans vs numbers."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(equal(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(equal(a, b) for a, b in zip(left, right))
    return left == right


def check_schema(schema):
    if not isinstance(schema, dict):
        raise Failure("graph", "A result schema must be an object")
    allowed = {"type", "properties", "required", "additionalProperties", "items", "enum", "description"}
    if set(schema) - allowed or not isinstance(schema.get("type"), str) or schema["type"] not in TYPES:
        raise Failure("graph", "Unsupported result schema keyword or type")
    if "description" in schema and not isinstance(schema["description"], str):
        raise Failure("graph", "Schema description must be text")
    if "additionalProperties" in schema and type(schema["additionalProperties"]) is not bool:
        raise Failure("graph", "additionalProperties must be a boolean")
    props = schema.get("properties", {})
    required = schema.get("required", [])
    if not isinstance(props, dict) or not isinstance(required, list) or any(not isinstance(k, str) or k not in props for k in required):
        raise Failure("graph", "Invalid schema properties/required")
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        raise Failure("graph", "enum must be a nonempty list")
    for child in props.values():
        check_schema(child)
    if "items" in schema:
        check_schema(schema["items"])


def validate(value, schema, path="data"):
    kind = schema["type"]
    if not isinstance(value, TYPES[kind]) or (kind in ("integer", "number") and isinstance(value, bool)):
        raise Failure("result", f"{path} must be {kind}")
    if isinstance(value, float) and not math.isfinite(value):
        raise Failure("result", f"{path} must be finite")
    if "enum" in schema and not any(equal(value, v) for v in schema["enum"]):
        raise Failure("result", f"{path} is outside enum")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                raise Failure("result", f"{path}.{key} is required")
        if schema.get("additionalProperties") is False and set(value) - set(props):
            raise Failure("result", f"{path} has unexpected properties")
        for key in value.keys() & props.keys():
            validate(value[key], props[key], f"{path}.{key}")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            validate(item, schema["items"], f"{path}[{i}]")
