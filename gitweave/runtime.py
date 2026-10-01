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
from .events import AttemptEvents, observe_output
from .git import Git
from .persistence import destination, persist
from .graph import validate_graph
from .model import Failure, Result, equal, pointer, validate


def now():
    return datetime.now(timezone.utc).isoformat()


class Runtime:
    def __init__(self, graph_text, repo, commit, request=None, *, adapters=None, pr=None, issue=None, provenance_remote=None,
                 event_sink=None):
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
        self.record = {"version": 2, "run_id": self.id, "repository": str(self.git.repo),
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
        self.event_sink = event_sink
        self.invocations = {}
        self.visits = 0
        self.resuming = False

    @classmethod
    def resume(cls, run_id, repo=None, *, adapters=None, event_sink=None):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
            raise Failure("resume", "Invalid Run ID")
        candidates = [Path(repo)] if repo is not None else [Path.cwd(), *sorted(
            (Path.cwd() / ".gitweave" / "repos").glob("*/*.git"))]
        found = []
        for candidate in candidates:
            try:
                storage = Git(candidate, run_id)
                record = storage.load_run()
            except Failure:
                continue
            found.append((storage, record))
        if len(found) != 1:
            raise Failure("resume", "Run not found or ambiguous; specify its repository with --repo PATH")
        runtime = cls.__new__(cls)
        runtime.git, record = found[0]
        runtime.id = run_id
        runtime.adapters = adapters if adapters is not None else {name: CLIAdapter(name) for name in ("codex", "claude")}
        runtime.event_sink = event_sink
        runtime.resuming = True
        runtime.restore(record)
        return runtime

    @staticmethod
    def attempt_summary(record):
        return {"instance_id": record["instance_id"], "invocation_id": record["invocation_id"],
                "attempt": record["attempt"], "commit": record["commit"],
                "status": "interrupted" if record["status"] == "running" else record["status"]}

    def restore(self, record):
        if record.get("version") != 2:
            raise Failure("resume", "Run predates resume support and has no stable invocation identities")
        if record["status"] == "completed":
            raise Failure("resume", "Run is already completed")
        if record["run_id"] != self.id or hashlib.sha256(record["graph"].encode()).hexdigest() != record["graph_digest"]:
            raise Failure("resume", "Run definition identity or graph digest does not match")
        self.record = record
        self.graph = validate_graph(json.loads(record["graph"]))
        self.base = self.git.resolve(record["base_commit"])
        self.github_repository = record["github_repository"]
        self.run_input = record["run_input"]
        self.provenance_remote = record["provenance_destination"]
        history = self.git.load_attempts()
        self.invocations = {}
        for attempt in history:
            self.invocations.setdefault(attempt["invocation_id"], []).append(attempt)
        self.steps = len(self.invocations)
        self.instances = max((int(a["instance_id"].rsplit("-", 1)[1]) for a in history), default=0)
        self.record["attempts"] = [self.attempt_summary(a) for a in history]
        self.errors = record.get("errors", [])
        self.visits = 0
        self.stopped = False

    def tick(self, invocation):
        if self.stopped:
            raise Failure("stopped", "Run has stopped scheduling work")
        self.visits += 1
        if invocation in self.invocations:
            return
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

    async def branches(self, flows, inputs):
        results = await asyncio.gather(*(self.flow(flow, inputs, branch_item, branch_origin, path)
                                        for flow, branch_item, branch_origin, path in flows), return_exceptions=True)
        errors = [r for r in results if isinstance(r, BaseException)]
        if errors:
            raise next((e for e in errors if not isinstance(e, Failure) or e.kind != "stopped"), errors[0])
        return [value for branch in results for value in branch]

    async def flow(self, flow, inputs, item=None, origin=None, path="flow"):
        try:
            for index, step in enumerate(flow):
                position = f"{path}/{index}"
                if isinstance(step, str):
                    inputs = [await self.node(step, inputs, item, origin, f"{position}/{step}")]
                    continue
                if self.stopped:
                    raise Failure("stopped", "Run has stopped scheduling work")
                op, spec = next(iter(step.items()))
                if op == "parallel":
                    inputs = await self.branches([(branch, item, origin, f"{position}/parallel/{i}")
                                                  for i, branch in enumerate(spec)], inputs)
                elif op == "map":
                    items = self.control_value(inputs, spec["path"])
                    if not isinstance(items, list):
                        raise Failure("result", "map source must be an array")
                    # Only preflight invocations known to execute: control blocks
                    # can pass an item through without ever invoking a node.
                    # Those flows use tick's budget check as nodes are reached.
                    first = spec["flow"][0]
                    if isinstance(first, str):
                        new_invocations = sum(f"{position}/map/{i}/0/{first}" not in self.invocations
                                              for i in range(len(items)))
                        if new_invocations > self.graph.get("max_steps", 100) - self.steps:
                            raise Failure("step_limit", "fan-out exceeds remaining step budget")
                    # Empty maps preserve context, allowing a following join to run once.
                    if items:
                        inputs = await self.branches([(spec["flow"], value, {"index": i, "parent": origin},
                                                       f"{position}/map/{i}")
                                                      for i, value in enumerate(items)], inputs)
                elif op == "if":
                    branch = "then" if self.matches(inputs, spec["condition"]) else "else"
                    inputs = await self.flow(spec[branch], inputs, item, origin, f"{position}/if/{branch}")
                elif op == "loop":
                    iteration = 0
                    while True:
                        invoked = self.visits
                        inputs = await self.flow(spec["flow"], inputs, item, origin, f"{position}/loop/{iteration}")
                        if not self.matches(inputs, spec["while"]):
                            break
                        # Without a node invocation the condition's result cannot change.
                        # (Invocations elsewhere still consume max_steps, so this terminates.)
                        if self.visits == invoked:
                            raise Failure("loop", "Loop iteration invoked no node while its condition still matches")
                        iteration += 1
            return inputs
        except BaseException:
            self.stopped = True
            raise

    async def node(self, name, inputs, item, origin, invocation):
        async with self.semaphore:
            self.tick(invocation)
            history = self.invocations.get(invocation, [])
            completed = next((a for a in history if a["status"] == "completed"), None)
            node = self.graph["nodes"][name]
            if completed is not None:
                result = completed["result"]
                return {"node_id": name, "instance_id": completed["instance_id"], "commit": completed["output_commit"],
                        "message": result["message"], "data": result["data"], "data_validated": "schema" in node}
            if history:
                instance = history[0]["instance_id"]
            else:
                self.instances += 1
                instance = f"{name}-{self.instances}"
            choice = node.get("workspace_base", 0)
            if choice != "run" and choice >= len(inputs):
                raise Failure("graph", f"{name}: workspace_base index outside inputs")
            base = self.base if choice == "run" else inputs[choice]["commit"]
            retries = node.get("retries", self.graph.get("retries", 0))
            first_attempt = max((a["attempt"] for a in history), default=0) + 1
            for attempt in range(first_attempt, first_attempt + retries + 1):
                if self.stopped:
                    raise Failure("stopped", "Run stopped before retry")
                record = {"run_id": self.id, "node_id": name, "instance_id": instance, "invocation_id": invocation,
                          "attempt": attempt, "fan_out_origin": origin, "item": item,
                          "kind": node["kind"], "provider": node.get("provider"), "model": node.get("model"),
                          "effort": node.get("effort"), "instruction": node.get("instruction"),
                          "argv": node.get("argv"), "config": node.get("config"),
                          "input_commits": [v["commit"] for v in inputs], "inputs": inputs,
                          "workspace_base": base, "started_at": now()}
                # A fresh copy per attempt: retries start from the original context.
                context = self.context(inputs, item)
                self.git.start_attempt(record)
                result, commit, error = await asyncio.to_thread(self.attempt, name, node, context, record)
                self.record["attempts"].append(self.attempt_summary(dict(record, commit=commit)))
                if error is None:
                    return {"node_id": name, "instance_id": instance, "commit": commit,
                            "message": result.message, "data": result.data, "data_validated": "schema" in node}
                if not error.retryable or attempt == first_attempt + retries:
                    self.errors.append({"kind": error.kind, "message": str(error), "instance_id": instance})
                    self.stopped = True
                    raise error

    def attempt(self, name, node, context, record):
        events = AttemptEvents(self.event_sink, record)
        events.emit("node_started")
        with observe_output(events.output if self.event_sink is not None else None):
            try:
                result, commit, error = self._attempt(name, node, context, record)
            except BaseException:
                events.emit("node_failed")
                raise
            events.emit("node_completed" if error is None else "node_failed")
            return result, commit, error

    def _attempt(self, name, node, context, record):
        started = time.monotonic()
        result = Result()
        error = None
        workspace = None
        temp = Path(tempfile.mkdtemp(prefix=f"gitweave-{self.id[:8]}-"))
        suffix = f"attempts/{record['instance_id']}/{record['attempt']}"
        try:
            if node["kind"] == "agent" and node["provider"] not in self.adapters:
                raise Failure("graph", f"Provider is not registered: {node['provider']}")
            # Unique names keep worktree metadata distinct across Runs.
            workspace = temp / temp.name
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
        with self.git.run_lock():
            if self.resuming:
                # Refresh under the process lock: a prior executor may have finished
                # between resume lookup and execution.
                self.restore(self.git.load_run())
            self.record.update(status="running", repository=str(self.git.repo), outputs=[],
                               notes_ref=self.git.notes, run_ref=f"refs/gitweave/{self.id}/run")
            for key in ("ended_at", "failure"):
                self.record.pop(key, None)
            if not self.resuming:
                self.record["provenance_destination"] = destination(self.git, self.record, self.provenance_remote)
            self.git.run_record(self.record)
            return await self._execute()

    async def _execute(self):
        self.semaphore = asyncio.Semaphore(self.graph.get("concurrency", 4))
        try:
            self.record["outputs"] = await self.flow(self.graph["flow"], [{"commit": self.base, "message": self.record["request"], "data": None}])
            self.record["status"] = "completed"
        except Exception as exc:
            self.record["status"] = "failed"
            self.record["failure"] = {"kind": exc.kind if isinstance(exc, Failure) else "internal", "message": str(exc)}
        finally:
            # Refs/notes also cover storage or cleanup exceptions that escaped
            # before node() could append its in-memory summary.
            self.record["attempts"] = [self.attempt_summary(a) for a in self.git.load_attempts()]
            self.record.update(ended_at=now(), steps=self.steps, errors=self.errors,
                               notes_ref=self.git.notes, run_ref=f"refs/gitweave/{self.id}/run")
            self.git.run_record(self.record)
        if self.record["provenance_destination"] is not None:
            persist(self.git, self.record["provenance_destination"])
        return self.record

    def run(self):
        return asyncio.run(self.execute())
