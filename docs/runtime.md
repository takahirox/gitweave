# GitWeave v0 runtime

Requires Python 3.11+, Git, and the `codex` and/or `claude` executables used by your graph. GitHub actions additionally require `gh` with native GitHub authentication. Install with `python -m pip install .`, or run directly with `python -m gitweave`.

```sh
gitweave run --graph examples/single.json --repo /path/to/repository --commit HEAD "Implement the requested change"
```

The original checkout is not an output workspace. Each invocation receives an isolated detached worktree; inspect the returned terminal commit(s) to use its artifacts. Existing uncommitted files in the original checkout are not inputs. Local refs and notes retain Run and attempt history without a database. A successful Run means the graph executed normally, not that the task was approved.

## Graph contract

Version 1 uses JSON, with `version: 1`, a `nodes` object and a nonempty `flow` array. All node declarations have `kind` and explicit `workspace_base`: an upstream input index (starting at zero), or `"run"` for the Run's original commit. Input ordering follows graph branch/item order, never completion order. Graph files, instructions and target repositories are trusted operator inputs.

Agent nodes specify `provider`, `instruction`, optional `model` and `effort`, and optional `schema` for result data. Built-in providers are `codex` and `claude`; the Python runtime accepts additional adapters through dependency injection. Model and effort are passed to the chosen CLI; unsupported settings fail visibly rather than being silently substituted. Provider defaults apply if omitted.

The adapter receives a context containing the request, input commits/messages/data, selected workspace-base commit, and any fan-out item. It must return a `Result`; provider-native events, stderr, session IDs, usage and cost fields are preserved when available. No token/cost estimates are invented. The configured model/effort and raw events retain both requested and provider-reported information.

An optional `schema` validates `data`. The supported JSON Schema subset is `type` (object, array, string, integer, number, boolean, null), `properties`, `required`, boolean `additionalProperties`, `items`, `enum`, and `description`. Unknown keywords are rejected. Provider-specific schema restrictions also apply; for portable structured outputs use fully specified objects with `required` and `additionalProperties: false`, as in the examples. Both adapters request an envelope containing a human-readable `message` plus `data`.

Flows are sequences of node IDs and control blocks:

| Block | Behavior |
| --- | --- |
| `"node_id"` | Execute one agent or action with the current upstream inputs; replace inputs with its result. |
| `{"parallel": [["a"], ["b"]]}` | Execute independent branch flows concurrently; concatenate terminal results in branch order. |
| `{"map": {"path": "/0/data/tasks", "flow": ["worker"]}}` | Execute a declared flow per array item using the same upstream commits/results; each invocation receives its item and index. Concatenate terminal results in item order. |
| `{"if": {"condition": {"path": "/0/data/ok", "equals": true}, "then": ["a"], "else": ["b"]}}` | Execute exactly the selected branch, based on structured data. |
| `{"loop": {"flow": ["work", "review"], "while": {"path": "/0/data/retry", "equals": true}}}` | Execute the body at least once, then repeat while the condition matches, passing the previous body's terminal results into the next iteration. |

Control paths are JSON pointers into `/<input-index>/data[/...]`. Routing requires a schema-validated result; missing data/paths and non-array map results fail deterministically. Arbitrary expression evaluation and natural-language parsing are not used. Equality is type-sensitive.

The step after a parallel/map block is its explicit join: it receives all terminal commits and results. It starts only after all branches finish. An agent join has one designated workspace base and can inspect/merge the other input commits; the runtime performs no semantic merge. A map over an empty array passes its original inputs through, so a following join still runs once. Blocks may nest, including loops and maps, and a join never mixes results from different loop iterations. This structured graph grammar deliberately excludes arbitrary dynamic topology.

`concurrency` (default 4) bounds running node attempts across nested blocks. `max_steps` (default 100) is finite and counts node invocations, control-block activations, and loop continuations. Retries are separately bounded by `retries` (default 0, additional attempts per invocation). Oversized fan-out is rejected before scheduling it. Each CLI invocation has a finite `timeout` in seconds (default 600).

On an exhausted/nonretryable failure, scheduling stops. Already-running siblings finish and retain their results before the Run is finalized as failed. No later dependent steps run. Runtime retries start from the original commit and input context with a new worktree. Task outcomes must be handled explicitly in the graph. Invalid structured output is a nonretryable contract failure. Recognized usage/rate limits stop retries; the runtime never purchases allowance, resets limits, or falls back to another model/provider.

## Git records

- `refs/gitweave/<run-id>/run` points to an independent record commit containing `run.json`, including the exact graph text, digest, request, base, timestamps, status, attempt index and terminal outputs.
- `refs/gitweave/<run-id>/attempts/<instance-id>/<attempt>` retains every completed or failed attempt. The instance ID is distinct for each invocation, including loop iterations and fan-out items; the note retains declared node ID, item and nested fan-out origin.
- `refs/notes/gitweave/<run-id>` stores execution records on attempt commits. Notes contain original inputs, workspace base, instruction/configuration, result, raw logs, timing, usage, sessions and failure diagnostics.
- Success checkpoints the final assigned worktree, including an empty commit if unchanged. Agent-created commits remain in ancestry. A failure commit has the original workspace base as parent and the same tree as that base; a retry never uses the failure commit. Action attempts also get commits and notes.

```sh
git show refs/gitweave/RUN_ID/run:run.json
git notes --ref=refs/notes/gitweave/RUN_ID show OUTPUT_COMMIT
git diff BASE_COMMIT OUTPUT_COMMIT
```

Raw logs can contain repository or prompt content; keep these local records under the same access controls as the repository. Publishing an artifact does not automatically publish the Run refs or notes. There is no resume-after-process-crash command in v0; retained refs/notes support diagnosis. A hard process/host crash may leave the Run marked running and a worktree on disk. Git storage exhaustion can prevent record writes; those failures are surfaced rather than reported as completed execution.

## Explicit GitHub actions

```json
{
  "kind": "action",
  "action": "publish_pr",
  "workspace_base": 0,
  "config": {
    "repository": "owner/repo",
    "base": "main",
    "title": "Implement request",
    "body": "Changes produced by this graph."
  }
}
```

`publish_pr` pushes the selected workspace-base artifact to `gitweave/<run-id>/<declared-node-id>` and creates or updates its PR. Revisit the same publisher node after fixes to synchronize the same PR. Updates use an exact force-with-lease and refuse externally changed heads; a retry recognizes its already-published exact commit. Actions are serialized within a Run.

`merge_pr` takes `config.repository` and `config.publish_node` identifying a publisher in this graph. It checks the exact published head and GitHub's `reviewDecision == APPROVED`, then requests a squash merge with `--match-head-commit`, without admin bypass. An unapproved PR returns `data.merged=false` as a task outcome. Declare a result schema if routing on this result. A retry of a completed merge is idempotent. Repository branch protection remains authoritative; no background auto-merge is scheduled.

GitHub credentials stay with runtime-owned System Actions. Agent subprocesses receive an environment allowlist that excludes `GH_TOKEN`, `GITHUB_TOKEN`, Git configuration overrides and SSH agent sockets. Native agent authentication uses the existing home/config locations. This is authority separation, not an OS security boundary against a malicious agent: worktrees share a Git object store, and native home/config files remain available according to the CLI's sandbox. Use trusted graphs/repositories and appropriate native sandbox policy. GitWeave does not require Docker or disable native permission checks.

## Validation and live smoke

```sh
python -m unittest discover -s tests -v
python scripts/smoke.py --provider both
```

Deterministic tests use temporary Git repositories, injected adapters/actions, native event fixtures and mocked GitHub calls. They require no agent login, network, or model allowance. CI runs them on Python 3.11 and 3.14.

The opt-in smoke creates a disposable repository, invokes both installed CLIs with their normal authentication, validates structured results, and checks that both file artifacts reach the terminal commit. It publishes nothing and prints the repository and Run ID for inspection. Use `--provider codex` or `--provider claude` to diagnose an individual adapter. Authenticate using the respective CLI before running; do not change providers, redeem reset tickets or buy allowance when a limit is encountered.

CLI interfaces were checked against [Codex non-interactive documentation](https://developers.openai.com/codex/noninteractive), [Claude programmatic execution documentation](https://code.claude.com/docs/en/headless), and installed CLI help. Smoke validation complements deterministic coverage; it does not prove general task correctness.
