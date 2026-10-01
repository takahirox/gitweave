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
        self.record = {"version": 1, "run_id": self.id, "repository": str(self.git.repo),
                       "github_repository": self.github_repository,
                       "base_commit": self.base, "run_input": self.run_input,
                       "request": request, "graph": graph_text,
                       "graph_digest": hashlib.sha256(graph_text.encode()).hexdigest(),
                       "started_at": now(), "status": "running", "attempts": [], "outputs": []}
        self.provenance_remote = provenance_remote
        self.record["provenance_destination"] = destination(self.git, self.record, provenance_remote)
        self.record["resume_version"] = 1
        self.steps = 0
        self.replay_steps = 0
        self.instances = 0
        self.invocations = set()
        self.history = {}
        self.resume_ref = None
        self.stopped = False
        self.errors = []
        self.event_sink = event_sink

    @classmethod
    def resume(cls, run_id, repo=None, *, adapters=None, event_sink=None):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", run_id):
            raise Failure("resume", "Invalid Run ID")
        if repo is None:
            candidates = [Path.cwd(), *sorted((Path.cwd() / ".gitweave" / "repos").glob("*/*.git"))]
        elif Path(repo).exists():
            candidates = [Path(repo)]
        elif re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", str(repo)):
            owner, name = str(repo).lower().split("/")
            candidates = [Path.cwd() / ".gitweave" / "repos" / owner / f"{name}.git"]
        else:
            raise Failure("resume", f"Repository not found: {repo}")
        stores = {}
        ref = f"refs/gitweave/{run_id}/run"
        for candidate in candidates:
            if not candidate.exists():
                continue
            try:
                storage = Git(candidate, run_id)
                if ref in storage.command("for-each-ref", "--format=%(refname)", ref).splitlines():
                    stores[storage.common_dir.resolve()] = storage
            except Failure:
                if repo is not None:
                    raise
        if len(stores) != 1:
            raise Failure("resume", "Run not found; use --repo to select its repository" if not stores
                          else "Run found in multiple repositories; select one with --repo")
        self = cls.__new__(cls)
        self.git = next(iter(stores.values()))
        # Read a consistent snapshot before releasing the lock. execute() checks
        # the Run ref again so a second resumer cannot run from a stale snapshot.
        with self.git.run_lock():
            self._restore(adapters, event_sink)
        return self

    def _restore(self, adapters, event_sink):
        run_id = self.git.run_id
        self.resume_ref = self.git.resolve(f"refs/gitweave/{run_id}/run")
        self.record = self.git.load_run()
        if self.record.get("run_id") != run_id or self.record.get("resume_version") != 1:
            raise Failure("resume", "Run does not contain a resumable definition")
        if self.record["status"] == "completed":
            raise Failure("resume", "Run is already completed")
        graph_text = self.record["graph"]
        if hashlib.sha256(graph_text.encode()).hexdigest() != self.record["graph_digest"]:
            raise Failure("resume", "Saved graph digest does not match")
        self.graph = validate_graph(json.loads(graph_text))
        self.id = run_id
        self.base = self.git.resolve(self.record["base_commit"])
        self.github_repository = self.record["github_repository"]
        self.run_input = self.record["run_input"]
        self.adapters = adapters if adapters is not None else {name: CLIAdapter(name) for name in ("codex", "claude")}
        self.provenance_remote = self.record["provenance_destination"]
        self.event_sink = event_sink
        self.history = {}
        self.record["attempts"] = []
        self.instances = 0
        for commit, attempt in self.git.load_attempts():
            self.history.setdefault(attempt["invocation_id"], []).append((commit, attempt))
            index = self.attempt_index(commit, attempt)
            if index["status"] == "running":
                index["status"] = "interrupted"
            self.record["attempts"].append(index)
            self.instances = max(self.instances, int(attempt["instance_id"].rsplit("-", 1)[1]))
        self.invocations = set(self.history)
        self.steps = len(self.invocations)
        self.replay_steps = 0
        self.stopped = False
        self.errors = list(self.record.get("errors", []))
        # Keep earlier terminal Run records reachable through the Run ref's history.
        self.record["repository"] = str(self.git.repo)
        self.record["status"] = "running"
        self.record["outputs"] = []
        self.record.pop("failure", None)
        self.record.pop("ended_at", None)

    @staticmethod
    def attempt_index(commit, record):
        return {"instance_id": record["instance_id"], "invocation_id": record["invocation_id"],
                "attempt": record["attempt"], "commit": commit, "status": record["status"]}

    def tick(self, invocation):
        if self.stopped:
            raise Failure("stopped", "Run has stopped scheduling work")
        if invocation not in self.invocations:
            if self.steps >= self.graph.get("max_steps", 100):
                self.stopped = True
                raise Failure("step_limit", "Run exceeded max_steps")
            self.invocations.add(invocation)
            self.steps += 1
        self.replay_steps += 1

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
                    if len(items) > self.graph.get("max_steps", 100) - self.replay_steps:
                        raise Failure("step_limit", "fan-out exceeds remaining step budget")
                    # Empty maps preserve context, allowing a following join to run once.
                    if items:
                        inputs = await self.branches([(spec["flow"], value, {"index": i, "parent": origin},
                                                       f"{position}/map/{i}")
                                                      for i, value in enumerate(items)], inputs)
                elif op == "if":
                    selected = "then" if self.matches(inputs, spec["condition"]) else "else"
                    inputs = await self.flow(spec[selected], inputs, item, origin, f"{position}/if/{selected}")
                elif op == "loop":
                    iteration = 0
                    while True:
                        invoked = self.replay_steps
                        inputs = await self.flow(spec["flow"], inputs, item, origin, f"{position}/loop/{iteration}")
                        if not self.matches(inputs, spec["while"]):
                            break
                        # Without a node invocation the condition's result cannot change.
                        # (Invocations elsewhere still consume max_steps, so this terminates.)
                        if self.replay_steps == invoked:
                            raise Failure("loop", "Loop iteration invoked no node while its condition still matches")
                        iteration += 1
            return inputs
        except BaseException:
            self.stopped = True
            raise

    async def node(self, name, inputs, item, origin, invocation):
        async with self.semaphore:
            self.tick(invocation)
            node = self.graph["nodes"][name]
            history = self.history.get(invocation, [])
            for commit, previous in history:
                if previous["status"] == "completed":
                    result = Result(**previous["result"])
                    if "schema" in node:
                        validate(result.data, node["schema"])
                    return self.output(name, previous["instance_id"], commit, result, node)
            if history:
                instance = history[0][1]["instance_id"]
            else:
                self.instances += 1
                instance = f"{name}-{self.instances}"
            choice = node.get("workspace_base", 0)
            if choice != "run" and choice >= len(inputs):
                raise Failure("graph", f"{name}: workspace_base index outside inputs")
            base = self.base if choice == "run" else inputs[choice]["commit"]
            retries = node.get("retries", self.graph.get("retries", 0))
            offset = max((a["attempt"] for _, a in history), default=0)
            for retry in range(retries + 1):
                attempt = offset + retry + 1
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
                result, commit, error = await asyncio.to_thread(self.attempt, name, node, context, record)
                self.record["attempts"].append(self.attempt_index(commit, record))
                if error is None:
                    return self.output(name, instance, commit, result, node)
                if not error.retryable or retry == retries:
                    self.errors.append({"kind": error.kind, "message": str(error), "instance_id": instance})
                    self.stopped = True
                    raise error

    @staticmethod
    def output(name, instance, commit, result, node):
        return {"node_id": name, "instance_id": instance, "commit": commit,
                "message": result.message, "data": result.data, "data_validated": "schema" in node}

    def attempt(self, name, node, context, record):
        events = AttemptEvents(self.event_sink, record)
        events.emit("node_started")
        with observe_output(events.output if self.event_sink is not None else None):
            try:
                # Immutable start markers survive SIGKILL. Only a terminal successful
                # ref + note qualifies as a reusable checkpoint.
                started = dict(record, status="running")
                suffix = f"starts/{record['instance_id']}/{record['attempt']}"
                marker = self.git.empty(record["workspace_base"], f"GitWeave started {self.id} {suffix}")
                self.git.retain(marker, suffix, started)
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
            if self.resume_ref is not None and self.git.resolve(f"refs/gitweave/{self.id}/run") != self.resume_ref:
                raise Failure("resume", "Run changed since it was loaded; reload before resuming")
            return await self._execute()

    async def _execute(self):
        self.record.update(notes_ref=self.git.notes, run_ref=f"refs/gitweave/{self.id}/run")
        # Make the exact definition recoverable before any node is launched.
        self.git.command("update-ref", f"refs/gitweave/{self.id}/input/base", self.base)
        self.git.run_record(self.record)
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
            self.git.run_record(self.record)
        if self.record["provenance_destination"] is not None:
            persist(self.git, self.record["provenance_destination"])
        return self.record

    def run(self):
        return asyncio.run(self.execute())
