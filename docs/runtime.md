# GitWeave v0 runtime

Supports Linux and macOS. Requires Python 3.11+, Git, and the `codex` and/or `claude` executables used by executed nodes. GitWeave core does not call GitHub APIs; nodes that use GitHub bring their own tools (for example `gh`) and authentication. Install with `python -m pip install .`, or run directly with `python -m gitweave`.

```sh
gitweave run --graph examples/single.json --repo /path/to/repository --commit HEAD "Implement the requested change"
```

The original checkout is not an output workspace. Each invocation receives an isolated detached worktree; inspect the returned terminal commit(s) to use its artifacts. Existing uncommitted files in the original checkout are not inputs. Local refs and notes retain Run and attempt history without a database. A successful Run means the graph executed normally, not that the task was approved.

## Graph contract

Version 1 uses JSON, with `version: 1`, a `nodes` object and a nonempty `flow` array. All node declarations have `kind`. The optional `workspace_base` selects the commit the node's worktree starts from: an upstream input index (starting at zero), or `"run"` for the Run's original commit. When omitted it defaults to `0`, the checkpoint commit of the node's first upstream input; set it explicitly only to choose another input or the Run base. Input ordering follows graph branch/item order, never completion order. Graph files, instructions and target repositories are trusted operator inputs.

Graph structure and node configuration are validated at Run initialization. Node-specific runtime requirements are checked only when the node executes: an unselected branch may contain an unregistered agent provider or a Command whose executable does not exist. If selected, the failure is retained in Run/attempt provenance. Run inputs themselves are resolved at initialization.

There are two node kinds, Agent Nodes (`"kind": "agent"`) and Command Nodes (`"kind": "command"`, see below). Both are executed in a dedicated worktree, produce one checkpoint commit per attempt, return the same Result (`message` + optional schema-validated `data`), and accept optional per-node `retries` and `timeout` overrides. There are no built-in GitHub System Actions: publishing, reviewing, merging, commenting or closing Issues are ordinary Agent or Command Nodes chosen by the Graph author, at whatever granularity suits the task and model.

Agent nodes specify `provider`, `instruction`, optional `model` and `effort`, and optional `schema` for result data. Built-in providers are `codex` and `claude`; the Python runtime accepts additional adapters through dependency injection. Model and effort are passed to the chosen CLI; unsupported settings fail visibly rather than being silently substituted. Provider defaults apply if omitted.

Native sandbox restrictions are opt-in per node through the optional, provider-specific `sandbox` field. Codex nodes accept any string; the native CLI determines which values it supports. If omitted, GitWeave explicitly passes `--sandbox danger-full-access`; omitting the CLI flag could restore a restrictive provider default. This replaces the previous unconditional `--sandbox workspace-write`. To retain that restriction on a node:

```json
{
  "kind": "agent",
  "provider": "codex",
  "sandbox": "workspace-write",
  "instruction": "Implement the requested change"
}
```

Explicit values are passed unchanged using native `--sandbox`, without a combined approval/sandbox bypass flag or approval-policy override. Only non-string values (including `null`) fail value validation. Any `sandbox` field on Claude, custom-provider, or Command nodes is rejected. Codex sandbox and approval behavior is independent of Claude permission configuration.

Claude agent nodes accept an optional `permission_mode` field. When omitted, GitWeave omits `--permission-mode` entirely and defers to Claude's native settings and normal permission behavior. Omission does **not** force `auto`. This replaces the previous unconditional `--permission-mode acceptEdits`. To retain that explicit mode on a node:

```json
{
  "kind": "agent",
  "provider": "claude",
  "permission_mode": "acceptEdits",
  "instruction": "Implement the requested change"
}
```

Any string is accepted; Claude determines which values it supports. Explicit values pass unchanged as `--permission-mode <value>`. Non-string values (including `null`) fail validation; any `permission_mode` field on Codex, custom-provider, or Command nodes is rejected.

GitWeave does not allowlist native option values. Explicit `sandbox` and `permission_mode` strings, including empty strings, are passed without trimming, case normalization, or substitution. The native CLI accepts or rejects them.

GitWeave continues to use Claude's non-interactive `-p` invocation without changing native settings, permission prompts, or other permission controls. Mode availability and execution remain subject to the installed CLI and native configuration; GitWeave does not substitute another mode or automatically fall back if Claude rejects a mode or denies an operation. There is no shared permission policy across providers.

The dedicated worktree remains the official artifact boundary in every mode: agents must leave final files there. The common Codex/Claude prompt contains a short node-contract preamble, the node's declared instruction, and serialized execution inputs labeled as data. The preamble only explains the contract: the worktree at `workspace_base` is the artifact boundary checkpointed as the node's commit; `github_repository`, `run_input`, `request`, `inputs[]` (upstream checkpoint commit, message and data) and `item` mean what this guide describes; and the final message/data is the Result, the only non-file output passed downstream, so it should include any state later nodes need. Its exact wording is an implementation detail. GitWeave does not add behavioral directives about publication, usage limits, provider switching, or task approval. Graph authors are responsible for task-specific instructions. Agent subprocesses inherit the parent environment unchanged. A worktree is not an OS security boundary.

The adapter receives a context containing the request, `run_input`, input commits/messages/data, selected workspace-base commit, and any fan-out item. `run_input` is the Run's lightweight source identity with a `kind` discriminator: `{"kind": "commit", "commit": SHA}`, `{"kind": "pull_request", "number": N}`, or `{"kind": "issue", "number": N}`. The repository is part of the Run and is not repeated there; nodes receive it separately as `github_repository` (`owner/repo` for `--pr`/`--issue` Runs, `null` for local Runs). GitWeave does not embed Issue or PR content in node context; nodes read it themselves when the graph needs it. Agent and Command Nodes also receive `instance_id`, the same invocation identity recorded in attempt provenance: stable across retries, distinct across loop iterations and fan-out items. It must return a `Result`; provider-native events, stderr, session IDs, usage and cost fields are preserved when available. No token/cost estimates are invented. The configured model/effort and raw events retain both requested and provider-reported information.

An optional `schema` validates `data`. The supported JSON Schema subset is `type` (object, array, string, integer, number, boolean, null), `properties`, `required`, boolean `additionalProperties`, `items`, `enum`, and `description`. Unknown keywords are rejected. Provider-specific schema restrictions also apply; for portable structured outputs use fully specified objects with `required` and `additionalProperties: false`, as in the examples. Both adapters request an envelope containing a human-readable `message` plus `data`.

Flows are sequences of node IDs and control blocks. An `if` branch may be empty to pass its inputs through; other flows must be nonempty:

| Block | Behavior |
| --- | --- |
| `"node_id"` | Execute one Agent or Command Node with the current upstream inputs; replace inputs with its result. |
| `{"parallel": [["a"], ["b"]]}` | Execute independent branch flows concurrently; concatenate terminal results in branch order. |
| `{"map": {"path": "/0/data/tasks", "flow": ["worker"]}}` | Execute a declared flow per array item using the same upstream commits/results; each invocation receives its item and index. Concatenate terminal results in item order. |
| `{"if": {"condition": {"path": "/0/data/ok", "equals": true}, "then": ["a"], "else": ["b"]}}` | Execute exactly the selected branch, based on structured data. |
| `{"loop": {"flow": ["work", "review"], "while": {"path": "/0/data/retry", "equals": true}}}` | Execute the body at least once, then repeat while the condition matches, passing the previous body's terminal results into the next iteration. |

Control paths are JSON pointers into `/<input-index>/data[/...]`. Routing requires a schema-validated result; missing data/paths and non-array map results fail deterministically. Arbitrary expression evaluation and natural-language parsing are not used. Equality is type-sensitive.

The step after a parallel/map block is its explicit join: it receives all terminal commits and results. It starts only after all branches finish. An agent join has one designated workspace base and can inspect/merge the other input commits; the runtime performs no semantic merge. A map over an empty array passes its original inputs through, so a following join still runs once. Blocks may nest, including loops and maps, and a join never mixes results from different loop iterations. This structured graph grammar deliberately excludes arbitrary dynamic topology.

`concurrency` (default 4) bounds running node attempts across nested blocks. `max_steps` (default 100) is finite and counts node invocations, control-block activations, and loop continuations. Retries are separately bounded by `retries` (default 0, additional attempts per invocation). A node's own `retries` overrides the graph value for that node; choose it according to the node's idempotency and external side effects (for example `"retries": 0` for a node that merges a PR). Oversized fan-out is rejected before scheduling it.

Node execution timeout is opt-in. Omit `timeout` to run each Agent or Command Node until it completes, fails, or is stopped externally, without a GitWeave-imposed wall-clock limit. To bound each attempt, set a finite positive number of seconds at the graph level, for example `"timeout": 1800`; a node-level `timeout` overrides the graph value for that node. Explicit `null`, booleans, strings, zero, negative numbers, and nonfinite values are invalid; only omission disables the timeout. This replaces the previous 600-second default.

Adapters keep the `run(node, context, workspace, timeout)` interface: `timeout` is `None` when omitted, otherwise the configured number. Built-in adapters pass it directly to the subprocess wait. Without a timeout, Codex and Claude use normal subprocess session and process-group inheritance. Only an explicit timeout creates a new session so GitWeave can kill the process group when the timeout expires. This produces a retryable Runtime Failure (`timeout`), retaining available stdout/stderr and attempt provenance in Git records. Each retry receives the same timeout and follows the configured `retries` bound. `max_steps` and retry bounds are unchanged.

Runtime-owned Git commands have no GitWeave-imposed wall-clock timeout. They run until completion, command failure, or external termination. The graph and node `timeout` apply only to Agent and Command Nodes. Git launch errors and nonzero exits remain retryable Runtime Failures (`git`) with their diagnostics.

Agent provider failures expose native diagnostics: Codex error/failed-turn messages and Claude failed-result errors (or result text) take precedence over stderr. When no error text is available, stderr, the native failure event, or the provider result text is retained; the generic completion failure is used only when none is useful. Nonzero exits with unparseable output expose stderr or stdout instead of a JSON parsing error. Raw events and logs remain in provenance. Agent failure classification and retryability are unchanged.

On an exhausted/nonretryable failure, scheduling stops. Already-running siblings finish and retain their results before the Run is finalized as failed. No later dependent steps run. Runtime retries start from the original commit and input context with a new worktree. Task outcomes must be handled explicitly in the graph. Invalid Agent structured output is a nonretryable contract failure; invalid Command output (including schema failures) is a retryable Runtime Failure, see Command Nodes. Provider execution failures follow configured retries regardless of usage/quota wording in stdout or stderr. Adapters may mark failures nonretryable using explicit provider-native signals (currently Claude’s `rate_limit_event` with `rate_limit_info.status: rejected`). Retry decisions use the failure’s retryable flag, not its kind or free-form text; the runtime never purchases allowance, resets limits, or falls back to another model/provider.

## Git records

Git operations inherit the parent process environment, including `GIT_*` variables, and use normal Git configuration and hooks. GitWeave does not force `core.hooksPath` or sandbox the Git environment. It sets author and committer name/email to `GitWeave <gitweave@localhost>` for its provenance commits. Checkpoint tree construction uses a private `GIT_INDEX_FILE` to capture final files without changing the worktree's index or an inherited index; these child-process overrides do not modify the parent environment. Checkpoints use `commit-tree`, which retains Git's normal plumbing behavior rather than running porcelain commit hooks.

- `refs/gitweave/<run-id>/run` is written once at finalization for a completed or failed Run. It points to an independent record commit containing `run.json`, including the exact graph text, digest, request, base, timestamps, status, attempt index and terminal outputs. No aggregate Run record is written at startup or after individual attempts; attempt refs and notes retain provenance during execution.
- `refs/gitweave/<run-id>/attempts/<instance-id>/<attempt>` retains every completed or failed attempt. The instance ID is distinct for each invocation, including loop iterations and fan-out items; the note retains declared node ID, item and nested fan-out origin.
- `refs/notes/gitweave/<run-id>` stores execution records on attempt commits. Notes contain original inputs, workspace base, instruction/configuration, result, raw logs, timing, usage, sessions and failure diagnostics.
- Each node invocation attempt, successful or failed, produces exactly one commit, which carries its attempt ref and Git note and is its stable execution identity. A successful attempt's commit is the node's checkpoint: its artifact state at the node boundary. Same-tree (empty) checkpoints are intentional: a node that changes no files, or only causes external side effects, still records that it ran and completed at that graph boundary, so they must not be optimized away. Successful checkpoints make Runs inspectable and analyzable later and are the intended completed-node boundaries for future resume/restart; resume itself is not implemented (see below). A failure commit (below) records the failed attempt but is not a completed-node boundary.
- Success checkpoints the final assigned worktree, including an empty commit if unchanged. The checkpoint uses the Agent's final HEAD and any pending merge heads as parents; the original workspace base need not remain an ancestor. A private index captures final files even when the Agent's index has unresolved entries, without modifying that index. Conflict markers left in files are captured as file content. The original workspace base remains recorded in attempt provenance. A failure commit has the original workspace base as parent and the same tree as that base; a retry never uses the failure commit. Agent and Command attempts use the same commit, ref and note model.

```sh
git show refs/gitweave/RUN_ID/run:run.json
git notes --ref=refs/notes/gitweave/RUN_ID show OUTPUT_COMMIT
git diff BASE_COMMIT OUTPUT_COMMIT
```

Raw logs can contain repository or prompt content; keep provenance under the same access controls as the repository. Runtime finalization publishes Run refs and notes when a provenance destination is resolved; see durable provenance below. There is no resume-after-process-crash command in v0; retained checkpoint refs/notes support diagnosis and are intended as the completed-node boundaries a future resume would continue from. A hard process/host crash before finalization may leave retained attempt refs/notes and a worktree on disk, but no final Run record. Git storage exhaustion can prevent record writes; those failures are surfaced rather than reported as completed execution.

After attempting to record provenance, GitWeave attempts normal temporary worktree removal even if recording failed. Cleanup failures fail the Run when there is no earlier failure; otherwise the earlier failure stays primary and cleanup diagnostics are logged. Cleanup does not rewrite attempt provenance or add a `cleanup_warning`. Failed removal may leave filesystem/worktree remnants; there is no fallback deletion or intentional retention for diagnosis.

## Command Nodes

A Command Node runs a deterministic process instead of an AI agent, for repeated work where AI is unnecessary: tests, publication, merges, deployment, or any external tooling.

```json
{
  "kind": "command",
  "argv": ["./scripts/some-operation", "--flag"],
  "config": {"any": "static JSON"},
  "retries": 0,
  "timeout": 300
}
```

`argv` is a nonempty list of strings executed directly (no shell). `config` is optional static JSON. `schema`, `retries` and `timeout` work as for Agent Nodes; Agent-only fields (`provider`, `instruction`, `model`, `effort`, `sandbox`, `permission_mode`) are rejected. The process contract is:

| Channel | Contract |
| --- | --- |
| cwd | The node's dedicated worktree at `workspace_base`. A relative `argv[0]` such as `./scripts/op` resolves there. |
| stdin | The node context as one JSON object (the same logical context an Agent sees: `run_id`, `request`, `github_repository`, `run_input`, `inputs[]`, `item`, `fan_out_origin`, `workspace_base`, `instance_id`) plus `config`. |
| stdout | Exactly one UTF-8 Result JSON object: `{"message": "...", "data": ...}`. `message` must be text; `data` may be null. `NaN`/`Infinity` are not JSON and are rejected. |
| stderr | Logs. |
| exit 0 | Normal completion. Task outcomes (for example "tests failed") are reported with exit 0 and a valid Result. |
| nonzero exit | Runtime Failure. |
| environment | Inherited unchanged from the GitWeave process; there is no separate `env` configuration. |

A nonzero exit, launch failure, timeout, non-UTF-8, empty or non-JSON stdout, an invalid Result envelope, or `data` that fails the node's `schema` is a retryable Runtime Failure, governed by the node's effective `retries`. On success GitWeave checkpoints the final worktree exactly like an Agent Node, including a same-tree checkpoint when nothing changed. The attempt note uses the same record as Agent attempts (Run/node/instance/attempt identity, node kind, input commits, output checkpoint, Result, status, timing), with `argv`, `config`, the exit code (`result.native.returncode`) and full stdout/stderr (`result.raw_stdout`, `result.raw_stderr`) as command-specific details.

## Git transport

Git transport operations (`fetch` and `push`), including Run input fetches and
provenance persistence, use normal Git configuration and authentication.
GitWeave does not inject a credential helper; configured SSH, URL rewrites, and
credential helpers apply normally, and Git failures retain their diagnostics.

## Existing Pull Request input

```sh
gitweave run --graph examples/review-fix-merge.json --repo owner/repo --pr 10 \
  "Review this PR, fix remaining problems, and merge it when clean."
```

The input modes are deliberately small and mutually exclusive (`--commit`, `--pr`, `--issue`):

- `--repo /path/to/local/repository --commit COMMIT` preserves the existing local input flow.
- `--repo owner/repo --pr NUMBER` accepts a positive PR number on github.com. PR URLs, other hosts, and local checkout paths with `--pr` are not supported. A relative `owner/repo` string in PR mode always means a GitHub identity, even if a directory with that name exists.
- `--repo owner/repo --issue NUMBER` starts an Issue-driven Run; see below.

PR and Issue modes create a persistent bare object store at `.gitweave/runs/<run-id>/repository.git` under the invoking directory. The CLI includes its absolute `repository` path in its output; use `git -C PATH show RUN_REF:run.json` to inspect it. The store is retained for artifact/history access, including when execution fails, and is not automatically deleted. Initialization failures may leave a partial store. No local clone is required, and no existing checkout is modified by initialization.

Initialization fetches `refs/pull/<number>/head` once with Git and uses it as the Run base, retained as `refs/gitweave/<run-id>/input/base`. It never starts from GitHub's synthetic merge commit and does not call the GitHub API: PR state, title, base branch and other metadata are not read or validated by GitWeave core. Later movement of the PR does not change the Run. `run.json.run_input` and every node's `context.run_input` are `{"kind": "pull_request", "number": N}`, and `github_repository` is `owner/repo`.

Nodes decide how to inspect, update, review, merge or comment on the PR. For example, the [review-fix-merge example](../examples/review-fix-merge.json) reviews structured `{approved, findings}` output, passes concrete findings to Fix, has a Push Agent update the PR head branch, reviews again until approved, and then has a Merge Agent merge it. Its `max_steps: 30` includes control steps and bounds the loop; exhaustion or invalid output fails without merging. Push and Merge set `retries: 0` because they have external side effects. The example's approval is a graph decision, distinct from runtime completion and GitHub policy. The [review-comment example](../examples/review-comment.json) posts an Agent's review message with a Command Node that calls `gh pr comment` (it needs `python3` and an authenticated `gh` on `PATH`).

A node's checkpoint commit and an external object's commit are different concepts. For example, a Publish Agent may push artifact commit `B` as a PR head, and GitWeave then records that node's checkpoint `P`. Carry external identities such as `{"pr": {"number": 42, "url": "...", "head_sha": "B"}}` explicitly in Result data; do not assume a checkpoint SHA is the SHA an external system uses.

Codex and Claude Agent Node subprocesses, and Command Node processes, inherit the parent process environment unchanged through normal subprocess inheritance. GitWeave does not supply an environment mapping, maintain an allowlist or denylist, or strip GitHub credentials, SSH agent variables, Git configuration overrides, credential-helper settings, native agent authentication, or ordinary development variables. The default launch environment is the same as invoking the underlying CLI directly from the same shell.

GitWeave remains a thin runtime, not a general credential or environment sandbox. The assigned worktree defines official artifact state. Task-specific behavior, including any GitHub operation, is defined by node instructions or commands. Any future environment restriction should address a concrete observed problem with the smallest necessary change. The runtime Git wrapper's environment handling is unchanged. Worktrees share a Git object store; GitWeave does not require Docker or disable native permission checks.

## GitHub Issue input

```sh
gitweave run --graph graph.json --repo owner/repo --issue 123 "Implement this Issue"
```

`--issue NUMBER` accepts a positive integer and uses the same `owner/repo` rules and bare object store as PR mode. GitWeave records the number verbatim as `run_input: {"kind": "issue", "number": 123}` and exposes it in every node's context. It does not verify that the Issue exists, fetch its title/body/state, or interpret it; the Graph decides how nodes read and process the Issue (for example, an Agent instructed to read it with available tools). The same graph can be reused for different Issues.

The Run base is the remote default branch HEAD, fetched once from `https://github.com/OWNER/REPO.git` at initialization and retained as `refs/gitweave/<run-id>/input/base`. Later branch movement does not change the Run. The Run's checkpoint commits, notes and refs are persisted to that same repository unless `--provenance-remote` overrides it.

## Issue-driven development

The Graph, not the runtime, defines the workflow. With `--issue`, the [issue-to-merge example](../examples/issue-to-merge.json) expresses:

```text
Issue input → Implement → Publish PR → Review ─ approved? ─ yes → Merge → Close Issue
                               ↑                    │
                               └─ Fix ←──── no ─────┘
```

Each step is an Agent Node whose instruction states the goal (for example "create or update the PR following the repository's conventions and template") rather than exact CLI commands. Node-to-node state is explicit: Publish returns `{pr}`, Review returns `{pr, approved, findings}`, Fix returns `{pr, summary}`, and Merge returns `{pr, merged, merge_commit}`. Because sequential execution replaces the active inputs with the latest node output, each intervening node forwards the state later nodes need; schemas with field descriptions define these handoff contracts. There is no hidden PR/Issue state. Every invocation, including same-tree Publish/Review/Merge/Close checkpoints, keeps its own checkpoint commit and note for audit. The same flow can be collapsed into one strong Agent, or deterministic steps can become Command Nodes.

## Validation and live smoke

```sh
python -m unittest discover -s tests -v
python scripts/smoke.py --provider both
```

Deterministic tests use temporary Git repositories, injected adapters, real local Command processes, native event fixtures and a fake `gh` executable for the comment example. They require no agent login, network, or model allowance. CI runs them on Python 3.11 and 3.14.

The opt-in smoke creates a disposable repository, invokes both installed CLIs with their normal authentication, validates structured results, and checks that both file artifacts reach the terminal commit. It publishes nothing and prints the repository and Run ID for inspection. Use `--provider codex` or `--provider claude` to diagnose an individual adapter. Authenticate using the respective CLI before running; do not change providers, redeem reset tickets or buy allowance when a limit is encountered.

CLI interfaces were checked against [Codex non-interactive documentation](https://developers.openai.com/codex/noninteractive), [Claude programmatic execution documentation](https://code.claude.com/docs/en/headless), and installed CLI help. Smoke validation complements deterministic coverage; it does not prove general task correctness.

## Durable provenance

After recording all attempts, runtime
finalization pushes `refs/gitweave/RUN_ID/*` and `refs/notes/gitweave/RUN_ID`.
This includes the final Run record, successful and failed attempts, Run input refs,
and notes with retained logs. Only the selected Run's refs are pushed; artifact
branches, tags and other Runs are unchanged. Merging or deleting an artifact branch
does not delete these refs.

Destination selection uses `run --provenance-remote REMOTE_OR_URL` when supplied.
Otherwise, the Run's own GitHub repository (`--pr` or `--issue`) is used
(`https://github.com/OWNER/REPO.git`); a local `--commit` Run falls back to its
`origin` remote. Repositories touched by nodes' external side effects are not
considered. This keeps GitHub Runs durable
even when their storage has no origin, while allowing named remotes, SSH URLs and local bare
repositories without requiring canonical URL matching. All destinations, including
direct GitHub HTTPS URLs, use ordinary Git push configuration and credentials.
Keep credentials in Git helpers rather than URLs, since the selected destination
is recorded in the Run.

With no GitHub Run repository, origin or override, the Run remains local/offline
and records `provenance_destination: null`. Its refs can be pushed later with Git.
An unavailable configured destination is a visible failure, not an offline fallback.

```sh
# Explicit destination for a local Run
gitweave run --graph examples/single.json --repo /path/to/repo --commit HEAD \
   --provenance-remote origin "Implement the request"

# Push a local Run later, or retry a failed transfer
git push --no-follow-tags origin \
   'refs/gitweave/RUN_ID/*:refs/gitweave/RUN_ID/*' \
   refs/notes/gitweave/RUN_ID:refs/notes/gitweave/RUN_ID

# After merge and deletion of the original checkout
git clone REPOSITORY_URL recovered
git -C recovered fetch --no-tags origin \
   'refs/gitweave/RUN_ID/*:refs/gitweave/RUN_ID/*' \
   refs/notes/gitweave/RUN_ID:refs/notes/gitweave/RUN_ID
git -C recovered show refs/gitweave/RUN_ID/run:run.json
git -C recovered notes --ref=refs/notes/gitweave/RUN_ID show ATTEMPT_COMMIT
```

Ordinary clone does not fetch these namespaces. Discover Run IDs with
`git ls-remote REPOSITORY_URL 'refs/gitweave/*/run'`. Fetch also works in an empty
`git init` repository. If a Run failed before creating any attempt notes, omit
the notes refspec. Existing historical refs, including older archive markers,
remain inspectable and transferable through Git; no migration is needed.

Push and fetch use normal Git semantics, with no manifest, atomic publication
requirement, completeness validation or recovery protocol. A failed push may have
transferred some refs; inspect and retry with native Git commands. Git determines
whether ref updates are accepted. GitWeave does not force updates or reconcile
conflicts automatically.

Persistence failure raises a `persistence` error (CLI exit 2), even if execution
completed. The local final Run record, refs and notes remain available for
inspection and retry. The record's `status` describes execution, not transfer
success; it is saved before the push. Transfer errors preserve the underlying Git
diagnostic alongside the persistence failure context. Final failed Runs are pushed
too. A hard crash before finalization can leave local attempt provenance without a
final Run record. Successful persistence does not imply task approval.
