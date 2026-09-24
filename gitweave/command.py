"""Command Nodes: a deterministic process with the same Result contract as Agents."""
import json
from .adapters import process
from .model import Failure, Result


def reject(constant):
    raise ValueError(f"{constant} is not JSON")


def run_command(node, context, workspace, timeout):
    # cwd is the worktree, so relative argv paths resolve there.
    stdin = json.dumps(dict(context, config=node.get("config", {})), ensure_ascii=False)
    try:
        code, stdout, stderr = process(node["argv"], stdin, workspace, timeout)
    except UnicodeDecodeError as exc:
        raise Failure("command", f"Command output is not valid UTF-8: {exc}", retryable=True) from exc
    result = Result(raw_stdout=stdout, raw_stderr=stderr, native={"argv": node["argv"], "returncode": code})
    if code:
        raise Failure("command", f"Command exited with status {code}" + (f"\n{stderr.strip()}" if stderr.strip() else ""),
                      retryable=True, result=result)
    try:
        envelope = json.loads(stdout, parse_constant=reject)
    except ValueError:
        envelope = None
    if not isinstance(envelope, dict) or set(envelope) != {"message", "data"} or not isinstance(envelope["message"], str):
        raise Failure("command", 'Command stdout must be one {"message": text, "data": value} JSON object',
                      retryable=True, result=result)
    result.message, result.data = envelope["message"], envelope["data"]
    return result
