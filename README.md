# GitWeave

GitWeave is a Git-native graph runtime for composing AI agents into end-to-end workflows.

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

- **Agent Nodes** — one AI-agent execution
- **System Actions** — operations performed directly by GitWeave, such as creating or merging a Pull Request

Edges define sequencing, conditions, loops, fan-out, joins, and parallel execution.

Roles such as planner, worker, reviewer, or integrator are not special runtime concepts. They are simply node instructions and configurations chosen by the graph author.

### Git as the state and provenance layer

Agent Nodes work in dedicated Git worktrees.

A node receives one or more input commits, performs its work, and GitWeave checkpoints the resulting worktree state into an output commit.

Git therefore becomes the artifact handoff mechanism between nodes and provides natural lineage, diffing, rollback, branching, and integration.

### Git notes for execution results

Git notes carry the execution information that does not belong in the file tree, such as:

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

Internal node-to-node handoff uses commits and results. Repository-wide actions such as Pull Request creation and merge remain explicit system actions rather than ordinary agent behavior.

## Goal

The long-term goal of GitWeave is to provide a minimal Git-native foundation for building reliable, inspectable AI-agent workflows while reusing the capabilities of existing agents instead of rebuilding them inside the runtime.

See [Issue #1](https://github.com/takahirox/gitweave/issues/1) for the architectural vision and design principles in more detail.

## Run a graph

GitWeave v0 provides a Python CLI with Codex and Claude adapters:

```sh
python -m pip install .
gitweave run --graph examples/single.json --repo /path/to/repo --commit HEAD "Implement the request"

# Start from an existing GitHub PR
gitweave run --graph examples/review-fix-merge.json --repo owner/repo --pr 10 "Review, fix, and merge this PR"
```

See the [runtime guide](docs/runtime.md) for graph syntax, parallel execution, fan-out, review/fix loops, Git records, explicit PR actions, and validation. [The parallel example](examples/parallel.json) combines both providers in one graph.

Final Runs automatically push their GitWeave refs and notes to the artifact
repository or configured origin. Offline Runs can be pushed later with Git;
native Git fetch restores a selected Run into a fresh repository.
See [destination selection and native Git fetch/retry](docs/runtime.md#durable-provenance).
