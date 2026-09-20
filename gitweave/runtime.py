"""Bounded structured graph scheduling with Git-backed attempt provenance."""
import asyncio
import copy
from datetime import datetime, timezone
import hashlib
import json
import logging
import re
from pathlib import Path
import tempfile
import sys
import time
import uuid
from .actions import GitHubActions
from .adapters import CLIAdapter
from .git import Git
from .persistence import destination, persist
from .graph import validate_graph
from .model import Failure, Result, equal, pointer, validate


def now():
    return datetime.now(timezone.utc).isoformat()


class Runtime:
    def __init__(self, graph_text, repo, commit, request, *, adapters=None, actions=None, pr=None, provenance_remote=None):
        self.graph = validate_graph(json.loads(graph_text))
        self.id = uuid.uuid4().hex
        if pr is not None:
            if commit is not None or type(pr) is not int or pr <= 0:
                raise Failure("pr_input", "Use a positive PR number without --commit")
            if not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", str(repo)) or str(repo).split("/")[1] in (".", ".."):
                raise Failure("pr_input", "--pr requires a GitHub owner/repo identity, not a checkout path")
            storage = Path.cwd() / ".gitweave" / "runs" / self.id / "repository.git"
            self.git = Git(storage, self.id, initialize=True)
        else:
            if commit is None:
                raise Failure("input", "A local repository requires --commit")
            self.git = Git(repo, self.id)
        self.actions = actions if actions is not None else GitHubActions(self.git, self.id)
        self.input_pr = self.actions.resolve_input(str(repo), pr) if pr is not None else None
        self.base = self.input_pr["head_sha"] if self.input_pr else self.git.resolve(commit)
        self.adapters = adapters if adapters is not None else {name: CLIAdapter(name) for name in ("codex", "claude")}
        self.record = {"version": 1, "run_id": self.id, "repository": str(self.git.repo),
                       "base_commit": self.base, "input_pr": self.input_pr,
                       "pr_remote_sha": self.input_pr["head_sha"] if self.input_pr else None,
                       "request": request, "graph": graph_text,
                       "graph_digest": hashlib.sha256(graph_text.encode()).hexdigest(),
                       "started_at": now(), "status": "running", "attempts": [], "outputs": []}
        self.provenance_remote = provenance_remote
        self.publication_repositories = set()
        self.record["provenance_destination"] = None
        self.steps = 0
        self.instances = 0
        self.stopped = False
        self.errors = []

    def tick(self):
        if self.stopped:
            raise Failure("stopped", "Run has stopped scheduling work")
        if self.steps >= self.graph.get("max_steps", 100):
            self.stopped = True
            raise Failure("step_limit", "Run exceeded max_steps")
        self.steps += 1

    def context(self, inputs, item, origin, base):
        return {"run_id": self.id, "request": self.record["request"], "inputs": inputs,
                "item": item, "fan_out_origin": origin, "workspace_base": base,
                "input_pr": copy.deepcopy(self.input_pr),
                "pr_remote_sha": getattr(self.actions, "remote_sha", None)}

    @staticmethod
    def control_value(inputs, path):
        # Every input exposes commit, declared node/instance identity and result.
        parts = path.split("/")
        if len(parts) < 3 or not parts[1].isdigit() or parts[2] != "data":
            raise Failure("graph", "Control paths must select /<input-index>/data[/...]")
        index = int(parts[1])
        if index >= len(inputs) or not inputs[index].get("data_validated", False):
            raise Failure("result", "Control flow requires a schema-validated structured result")
        return pointer(inputs, path)

    def matches(self, inputs, condition):
        actual = self.control_value(inputs, condition["path"])
        expected = condition["equals"]
        return equal(actual, expected)

    async def branches(self, flows, inputs, item, origin):
        results = await asyncio.gather(*(self.flow(flow, inputs, branch_item, branch_origin)
                                        for flow, branch_item, branch_origin in flows), return_exceptions=True)
        errors = [r for r in results if isinstance(r, BaseException)]
        if errors:
            raise next((e for e in errors if not isinstance(e, Failure) or e.kind != "stopped"), errors[0])
        return [value for branch in results for value in branch]

    async def flow(self, flow, inputs, item=None, origin=None):
        try:
            for step in flow:
                if isinstance(step, str):
                    inputs = [await self.node(step, inputs, item, origin)]
                    continue
                self.tick()
                op, spec = next(iter(step.items()))
                if op == "parallel":
                    inputs = await self.branches([(branch, item, origin) for branch in spec], inputs, item, origin)
                elif op == "map":
                    items = self.control_value(inputs, spec["path"])
                    if not isinstance(items, list):
                        raise Failure("result", "map source must be an array")
                    if len(items) > self.graph.get("max_steps", 100) - self.steps:
                        raise Failure("step_limit", "fan-out exceeds remaining step budget")
                    # Empty maps preserve context, allowing a following join to run once.
                    if items:
                        inputs = await self.branches([(spec["flow"], value, {"index": i, "parent": origin})
                                                      for i, value in enumerate(items)], inputs, item, origin)
                elif op == "if":
                    inputs = await self.flow(spec["then"] if self.matches(inputs, spec["condition"]) else spec["else"], inputs, item, origin)
                elif op == "loop":
                    while True:
                        inputs = await self.flow(spec["flow"], inputs, item, origin)
                        if not self.matches(inputs, spec["while"]):
                            break
                        self.tick()
            return inputs
        except BaseException:
            self.stopped = True
            raise

    async def node(self, name, inputs, item, origin):
        async with self.semaphore:
            self.tick()
            self.instances += 1
            instance = f"{name}-{self.instances}"
            node = self.graph["nodes"][name]
            choice = node["workspace_base"]
            if choice != "run" and choice >= len(inputs):
                raise Failure("graph", f"{name}: workspace_base index outside inputs")
            base = self.base if choice == "run" else inputs[choice]["commit"]
            context = self.context(inputs, item, origin, base)
            context["instance_id"] = instance
            for attempt in range(1, self.graph.get("retries", 0) + 2):
                if self.stopped:
                    raise Failure("stopped", "Run stopped before retry")
                record = {"run_id": self.id, "node_id": name, "instance_id": instance,
                          "attempt": attempt, "fan_out_origin": origin, "item": item,
                          "kind": node["kind"], "provider": node.get("provider"), "model": node.get("model"),
                          "effort": node.get("effort"), "instruction": node.get("instruction"),
                          "input_commits": [v["commit"] for v in inputs], "inputs": inputs,
                          "workspace_base": base, "started_at": now()}
                result, commit, error = await asyncio.to_thread(self.attempt, name, node, context, record)
                self.record["attempts"].append({"instance_id": instance, "attempt": attempt,
                                                 "commit": commit, "status": record["status"]})
                self.record["pr_remote_sha"] = getattr(self.actions, "remote_sha", None)
                if error is None:
                    return {"node_id": name, "instance_id": instance, "commit": commit,
                            "message": result.message, "data": result.data, "data_validated": "schema" in node}
                if not error.retryable or attempt > self.graph.get("retries", 0):
                    self.errors.append({"kind": error.kind, "message": str(error), "instance_id": instance})
                    self.stopped = True
                    raise error

    def attempt(self, name, node, context, record):
        started = time.monotonic()
        result = Result()
        error = None
        workspace = None
        temp = Path(tempfile.mkdtemp(prefix=f"gitweave-{self.id[:8]}-"))
        suffix = f"attempts/{record['instance_id']}/{record['attempt']}"
        try:
            if node["kind"] == "agent":
                if node["provider"] not in self.adapters:
                    raise Failure("graph", f"Provider is not registered: {node['provider']}")
                workspace = temp / "workspace"
                self.git.add_worktree(workspace, context["workspace_base"])
                result = self.adapters[node["provider"]].run(node, context, workspace, self.graph.get("timeout"))
            else:
                if node["action"] == "publish_pr":
                    self.publication_repositories.add(node["config"]["repository"])
                result = self.actions.run(name, node, context)
            if "schema" in node:
                validate(result.data, node["schema"])
            message = f"GitWeave {self.id} {record['instance_id']} attempt {record['attempt']}"
            commit = (self.git.checkpoint(workspace, context["workspace_base"], message) if workspace is not None
                      else self.git.empty(context["workspace_base"], message))
            record.update(status="completed", output_commit=commit)
        except Exception as exc:
            error = exc if isinstance(exc, Failure) else Failure("internal", str(exc))
            result = error.result or result
            commit = self.git.empty(context["workspace_base"], f"GitWeave failed {self.id} {suffix}")
            record.update(status="failed", failure_commit=commit,
                          failure={"kind": error.kind, "message": str(error), "retryable": error.retryable})
        finally:
            record.update(result=result.record(), ended_at=now(), duration_seconds=time.monotonic() - started)
            try:
                if "status" in record:
                    self.git.retain(commit, suffix, record)
            finally:
                primary_error = error or sys.exception()
                try:
                    if workspace is not None and workspace.exists():
                        self.git.remove_worktree(workspace)
                    temp.rmdir()
                except Exception as cleanup:
                    if primary_error is None:
                        raise
                    logging.warning("Workspace cleanup failed: %s", cleanup)
        return result, commit, error

    async def execute(self):
        self.semaphore = asyncio.Semaphore(self.graph.get("concurrency", 4))
        try:
            self.record["outputs"] = await self.flow(self.graph["flow"], [{"commit": self.base, "message": self.record["request"], "data": None}])
            self.record["status"] = "completed"
        except Exception as exc:
            self.record["status"] = "failed"
            self.record["failure"] = {"kind": exc.kind if isinstance(exc, Failure) else "internal", "message": str(exc)}
        finally:
            self.record.update(ended_at=now(), steps=self.steps, errors=self.errors,
                               notes_ref=self.git.notes, run_ref=f"refs/gitweave/{self.id}/run")
            self.record["publication_repositories"] = sorted(self.publication_repositories)
            try:
                self.record["provenance_destination"] = destination(self.git, self.record, self.provenance_remote)
            finally:
                self.git.run_record(self.record)
        if self.record["provenance_destination"] is not None:
            persist(self.git, self.record["provenance_destination"])
        return self.record

    def run(self):
        return asyncio.run(self.execute())
