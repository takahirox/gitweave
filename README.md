# GitWeave

GitWeave is a Git-native graph runtime for composing AI agents and deterministic commands into end-to-end workflows.

It is designed around a simple idea: **use a graph to control how AI agents collaborate, and use Git to carry artifact state and execution provenance between them.**

## Why GitWeave

Modern AI coding agents can already plan, edit files, run tools, and complete substantial tasks on their own. GitWeave does not try to replace that intelligence.

Instead, it provides a small orchestration layer for combining agents and models explicitly. A workflow might use one strong agent for a complete task, or mix planners, parallel workers, integrators, and reviewers from different providers and cost tiers.

## Vision

GitWeave aims to make AI-agent workflows:

- explicit
- composable
- reproducible
- inspectable
- easy to analyze after execution

The runtime should remain small and understandable. Task intelligence belongs in the agents and in the graph definition rather than in a large built-in planning or verification framework.

## Core Architecture

### Graph as the control plane

A workflow is a graph of explicit steps connected by edges.

Graph steps may include:

- **Agent Nodes** — one AI-agent execution, whose responsibility may be as narrow or as broad as the graph author chooses
- **Command Nodes** — one deterministic process, for repeated work where AI is unnecessary

Both kinds share one contract: they receive the Run input, upstream checkpoint commits and Results, and a selected workspace base; they produce one checkpoint commit plus a Result (`message` and optionally schema-validated `data`).

Edges define sequencing, conditions, loops, fan-out, joins, and parallel execution.

Roles such as planner, worker, reviewer, or integrator are not special runtime concepts. They are simply node instructions and configurations chosen by the graph author.

### Git as the state and provenance layer

Agent and Command Nodes work in dedicated Git worktrees.

A node receives one or more input commits, performs its work, and GitWeave checkpoints the resulting worktree state into an output commit.

Git therefore becomes the artifact handoff mechanism between nodes and provides natural lineage, diffing, rollback, branching, and integration.

### Checkpoint commits as execution identity

> One node invocation produces one checkpoint commit.

With retries, each attempt gets its own commit; a failed attempt's commit records the failure and is not a completed-node boundary (see the [runtime guide](docs/runtime.md#git-records)).

The output commit is not only an artifact snapshot. It is also the execution checkpoint for that invocation:

- it captures the artifact state after the node invocation
- it gives the invocation a stable Git identity, even when the tree is unchanged
- it is the natural point to attach the Git-note execution record
- it makes the execution path inspectable after the fact
- it supports later analysis of inputs, results, logs, model/provider usage, timing, and external effects
- it provides a concrete, completed node boundary from which future resume/restart behavior can continue
- it keeps execution history Git-native instead of requiring a separate database

A node that changes no files, for example a reviewer or a node that only causes external side effects, therefore still gets its own same-tree (empty) checkpoint commit. That commit is intentional, not redundant: it records that the invocation occurred and completed at that point in the graph. Do not optimize it away.

```text
checkpoint commit → execution identity + artifact state at the node boundary
Git note          → detailed execution metadata and structured Result
```

Resume is not implemented yet: v0 has no command to continue a Run after a process crash. Checkpoint commits and their attached provenance are designed so that such behavior can later start from a known completed node boundary.

### Git notes for execution results

Git notes attached to each checkpoint commit carry the execution information that does not belong in the file tree, such as:

- node results and structured messages
- agent/model identity
- timing and usage information
- session information
- runtime diagnostics
- available execution logs

This gives node-to-node communication two complementary channels:

```text
Git commit / tree  → files and artifact state
Git note result    → plans, reviews, messages, and structured decisions
```

### Runtime correctness vs. task correctness

GitWeave is responsible for executing the graph reliably.

Whether the work itself is correct belongs to the graph. Tests, reviews, validation, retries, and fixes should be modeled explicitly as workflow steps rather than hidden runtime policy.

Runtime failures, such as agent-launch or provider failures, are different from task outcomes and may be retried from the same input state while preserving the failed attempt for later analysis.

## Heterogeneous agents

Different nodes may use different agents, providers, models, and effort levels.

For example:

```text
Planner      → high-quality model
Workers × N  → cheaper models in parallel
Integrator   → high-quality model
Reviewer     → another strong agent
```

GitWeave is intended to support frontier agent products, API-backed models, and local models through the same graph abstraction.

## GitHub integration

GitHub Issues and Pull Requests sit above the core runtime.

A higher-level development flow may look like:

```text
Issue
  ↓
Graph Run
  ↓
Commits and node results
  ↓
Pull Request
  ↓
Review / Fix
  ↓
Merge
```

Internal node-to-node handoff uses commits and results. GitWeave has no built-in GitHub workflow policy: creating, reviewing, merging, commenting on Pull Requests and closing Issues are ordinary Agent or Command Nodes. One strong Agent may own the whole flow, or the graph may split it into several nodes; state such as PR identity is passed explicitly through Results. See [issue-to-merge.json](examples/issue-to-merge.json).

## Goal

The long-term goal of GitWeave is to provide a minimal Git-native foundation for building reliable, inspectable AI-agent workflows while reusing the capabilities of existing agents instead of rebuilding them inside the runtime.

See [Issue #1](https://github.com/takahirox/gitweave/issues/1) for the architectural vision and design principles in more detail.

## Run a graph

GitWeave v0 provides a Python CLI with Codex and Claude adapters:

```sh
python -m pip install .
gitweave run --graph examples/single.json --repo /path/to/repo --commit HEAD "Implement the request"

# Start from an existing GitHub PR
gitweave run --graph examples/review-fix-merge.json --repo owner/repo --pr 10

# Start from a GitHub Issue; nodes receive run_input {"kind": "issue", "number": 123}
gitweave run --graph examples/issue-to-merge.json --repo owner/repo --issue 123
```

The trailing free-form request is optional. When the graph's instructions and `run_input` already define the work, omit it; when supplied, nodes receive it as additional operator guidance.

See the [runtime guide](docs/runtime.md) for graph syntax, Command Nodes, parallel execution, fan-out, review/fix loops, Issue-driven development, Git records, and validation. [The parallel example](examples/parallel.json) combines both providers in one graph.

Final Runs automatically push their GitWeave refs and notes to the artifact
repository or configured origin. Offline Runs can be pushed later with Git;
native Git fetch restores a selected Run into a fresh repository.
See [destination selection and native Git fetch/retry](docs/runtime.md#durable-provenance).

## Validate a graph

Check a graph before executing it:

```sh
gitweave validate --graph examples/single.json
# Also available through the shared Python CLI:
python -m gitweave validate --graph examples/single.json
```

The command checks JSON syntax, graph structure, node references, and supported result schemas. It prints success to stdout and exits with status 0, or prints an input-error diagnostic to stderr and exits with status 2.

Validation needs no target repository, base commit, user request, installed agent CLIs, or agent/GitHub credentials. It creates no Run, invokes no agents or commands, accesses no network, and does not modify repository state.

Success means the graph passes static validation. It does not check provider availability, authentication, runtime-dependent input values, or semantic task correctness, and does not guarantee that agent execution or the task outcome will succeed.
