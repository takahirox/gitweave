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
from .adapters import CLIAdapter
from .command import run_command
from .git import Git
from .persistence import destination, persist
from .graph import validate_graph
from .model import Failure, Result, equal, pointer, validate


def now():
    return datetime.now(timezone.utc).isoformat()


class Runtime:
    def __init__(self, graph_text, repo, commit, request=None, *, adapters=None, pr=None, issue=None, provenance_remote=None):
        self.graph = validate_graph(json.loads(graph_text))
        self.id = uuid.uuid4().hex
        self.github_repository = None
        if pr is not None or issue is not None:
            source, number = ("pr_input", pr) if pr is not None else ("issue_input", issue)
            if commit is not None or (pr is not None and issue is not None) or type(number) is not int or number <= 0:
                raise Failure(source, "Use one positive PR or Issue number without --commit")
            if not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", str(repo)) or str(repo).split("/")[1] in (".", ".."):
                raise Failure(source, "--pr and --issue require a GitHub owner/repo identity, not a checkout path")
            self.github_repository = str(repo)
            self.run_input = {"kind": "pull_request" if pr is not None else "issue", "number": number}
            # One store per GitHub repository (names are case-insensitive), shared by
            # Runs; all per-Run state lives under refs/gitweave/<run-id>/ and its notes ref.
            owner, name = self.github_repository.lower().split("/")
            storage = Path.cwd() / ".gitweave" / "repos" / owner / f"{name}.git"
            self.git = Git(storage, self.id, initialize=True)
        else:
            if commit is None:
                raise Failure("input", "A local repository requires --commit")
            self.git = Git(repo, self.id)
        if self.github_repository:
            # Run inputs are identities; nodes read Issue/PR content themselves.
            # The base is the PR head or the default branch HEAD, frozen at Run start.
            # Fetch straight into this Run's ref, not FETCH_HEAD, which concurrent Runs share.
            source = f"refs/pull/{pr}/head" if pr is not None else "HEAD"
            target = f"refs/gitweave/{self.id}/input/base"
            self.git.command("fetch", "--no-tags", "--no-write-fetch-head",
                             f"https://github.com/{self.github_repository}.git", f"{source}:{target}")
            self.base = self.git.resolve(target)
        else:
            self.base = self.git.resolve(commit)
            self.run_input = {"kind": "commit", "commit": self.base}
        self.adapters = adapters if adapters is not None else {name: CLIAdapter(name) for name in ("codex", "claude")}
        self.record = {"version": 1, "run_id": self.id, "repository": str(self.git.repo),
                       "github_repository": self.github_repository,
                       "base_commit": self.base, "run_input": self.run_input,
                       "request": request, "graph": graph_text,
                       "graph_digest": hashlib.sha256(graph_text.encode()).hexdigest(),
                       "started_at": now(), "status": "running", "attempts": [], "outputs": []}
        self.provenance_remote = provenance_remote
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

    def context(self, inputs, item):
        # Only task-relevant data; Run/instance identity, fan-out origin and the
        # workspace base stay in provenance.
        return copy.deepcopy({"request": self.record["request"], "github_repository": self.github_repository,
                              "run_input": self.run_input, "item": item,
                              "inputs": [{"node_id": i.get("node_id"), "commit": i["commit"],
                                          "message": i["message"], "data": i["data"]} for i in inputs]})

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
                if self.stopped:
                    raise Failure("stopped", "Run has stopped scheduling work")
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
                        invoked = self.steps
                        inputs = await self.flow(spec["flow"], inputs, item, origin)
                        if not self.matches(inputs, spec["while"]):
                            break
                        # Without a node invocation the condition's result cannot change.
                        # (Invocations elsewhere still consume max_steps, so this terminates.)
                        if self.steps == invoked:
                            raise Failure("loop", "Loop iteration invoked no node while its condition still matches")
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
            choice = node.get("workspace_base", 0)
            if choice != "run" and choice >= len(inputs):
                raise Failure("graph", f"{name}: workspace_base index outside inputs")
            base = self.base if choice == "run" else inputs[choice]["commit"]
            retries = node.get("retries", self.graph.get("retries", 0))
            for attempt in range(1, retries + 2):
                if self.stopped:
                    raise Failure("stopped", "Run stopped before retry")
                record = {"run_id": self.id, "node_id": name, "instance_id": instance,
                          "attempt": attempt, "fan_out_origin": origin, "item": item,
                          "kind": node["kind"], "provider": node.get("provider"), "model": node.get("model"),
                          "effort": node.get("effort"), "instruction": node.get("instruction"),
                          "argv": node.get("argv"), "config": node.get("config"),
                          "input_commits": [v["commit"] for v in inputs], "inputs": inputs,
                          "workspace_base": base, "started_at": now()}
                # A fresh copy per attempt: retries start from the original context.
                context = self.context(inputs, item)
                result, commit, error = await asyncio.to_thread(self.attempt, name, node, context, record)
                self.record["attempts"].append({"instance_id": instance, "attempt": attempt,
                                                 "commit": commit, "status": record["status"]})
                if error is None:
                    return {"node_id": name, "instance_id": instance, "commit": commit,
                            "message": result.message, "data": result.data, "data_validated": "schema" in node}
                if not error.retryable or attempt > retries:
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
            if node["kind"] == "agent" and node["provider"] not in self.adapters:
                raise Failure("graph", f"Provider is not registered: {node['provider']}")
            workspace = temp / "workspace"
            self.git.add_worktree(workspace, record["workspace_base"])
            timeout = node.get("timeout", self.graph.get("timeout"))
            if node["kind"] == "agent":
                result = self.adapters[node["provider"]].run(node, context, workspace, timeout)
            else:
                result = run_command(node, context, workspace, timeout)
            if "schema" in node:
                try:
                    validate(result.data, node["schema"])
                except Failure as exc:
                    # Invalid Command output is a Runtime Failure governed by retries.
                    exc.retryable = node["kind"] == "command"
                    raise
            message = f"GitWeave {self.id} {record['instance_id']} attempt {record['attempt']}"
            commit = self.git.checkpoint(workspace, record["workspace_base"], message)
            record.update(status="completed", output_commit=commit)
        except Exception as exc:
            error = exc if isinstance(exc, Failure) else Failure("internal", str(exc))
            result = error.result or result
            commit = self.git.empty(record["workspace_base"], f"GitWeave failed {self.id} {suffix}")
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
            try:
                self.record["provenance_destination"] = destination(self.git, self.record, self.provenance_remote)
            finally:
                self.git.run_record(self.record)
        if self.record["provenance_destination"] is not None:
            persist(self.git, self.record["provenance_destination"])
        return self.record

    def run(self):
        return asyncio.run(self.execute())
