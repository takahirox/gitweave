# GitWeave v0 runtime

Supports Linux and macOS. Requires Python 3.11+, Git, and the `codex` and/or `claude` executables used by your graph. GitHub actions additionally require `gh` with native GitHub authentication. Install with `python -m pip install .`, or run directly with `python -m gitweave`.

```sh
gitweave run --graph examples/single.json --repo /path/to/repository --commit HEAD "Implement the requested change"
```

The original checkout is not an output workspace. Each invocation receives an isolated detached worktree; inspect the returned terminal commit(s) to use its artifacts. Existing uncommitted files in the original checkout are not inputs. Local refs and notes retain Run and attempt history without a database. A successful Run means the graph executed normally, not that the task was approved.

## Graph contract

Version 1 uses JSON, with `version: 1`, a `nodes` object and a nonempty `flow` array. All node declarations have `kind` and explicit `workspace_base`: an upstream input index (starting at zero), or `"run"` for the Run's original commit. Input ordering follows graph branch/item order, never completion order. Graph files, instructions and target repositories are trusted operator inputs.

Agent nodes specify `provider`, `instruction`, optional `model` and `effort`, and optional `schema` for result data. Built-in providers are `codex` and `claude`; the Python runtime accepts additional adapters through dependency injection. Model and effort are passed to the chosen CLI; unsupported settings fail visibly rather than being silently substituted. Provider defaults apply if omitted.

Native sandbox restrictions are opt-in per node through the optional, provider-specific `sandbox` field. Codex nodes accept exactly `"read-only"`, `"workspace-write"`, or `"danger-full-access"`. If omitted, GitWeave explicitly passes `--sandbox danger-full-access`; omitting the CLI flag could restore a restrictive provider default. This replaces the previous unconditional `--sandbox workspace-write`. To retain that restriction on a node:

```json
{
  "kind": "agent",
  "provider": "codex",
  "sandbox": "workspace-write",
  "workspace_base": 0,
  "instruction": "Implement the requested change"
}
```

These modes were verified with installed `codex-cli 0.155.1` using `codex exec --help` on 2026-09-20. Explicit values are passed unchanged using native `--sandbox`, without a combined approval/sandbox bypass flag or approval-policy override. Invalid strings and non-string values (including `null`) fail graph validation. Any `sandbox` field on Claude, custom-provider, or System Action nodes is rejected; no equivalent Claude control is required. Claude's existing native permission invocation remains unchanged.

The dedicated worktree remains the official artifact boundary in every mode: agents must leave final files there and must not modify the original checkout or other worktrees. Sandboxing does not grant publication authority. Native authentication and agent subprocess environment filtering remain unchanged; publishing, pushing, and merging remote branches remain the responsibility of explicit System Actions. A worktree is not an OS security boundary.

The adapter receives a context containing the request, input commits/messages/data, selected workspace-base commit, and any fan-out item. Both agents and actions also receive `instance_id`, the same invocation identity recorded in attempt provenance: stable across retries, distinct across loop iterations and fan-out items. It must return a `Result`; provider-native events, stderr, session IDs, usage and cost fields are preserved when available. No token/cost estimates are invented. The configured model/effort and raw events retain both requested and provider-reported information.

An optional `schema` validates `data`. The supported JSON Schema subset is `type` (object, array, string, integer, number, boolean, null), `properties`, `required`, boolean `additionalProperties`, `items`, `enum`, and `description`. Unknown keywords are rejected. Provider-specific schema restrictions also apply; for portable structured outputs use fully specified objects with `required` and `additionalProperties: false`, as in the examples. Both adapters request an envelope containing a human-readable `message` plus `data`.

Flows are sequences of node IDs and control blocks. An `if` branch may be empty to pass its inputs through; other flows must be nonempty:

| Block | Behavior |
| --- | --- |
| `"node_id"` | Execute one agent or action with the current upstream inputs; replace inputs with its result. |
| `{"parallel": [["a"], ["b"]]}` | Execute independent branch flows concurrently; concatenate terminal results in branch order. |
| `{"map": {"path": "/0/data/tasks", "flow": ["worker"]}}` | Execute a declared flow per array item using the same upstream commits/results; each invocation receives its item and index. Concatenate terminal results in item order. |
| `{"if": {"condition": {"path": "/0/data/ok", "equals": true}, "then": ["a"], "else": ["b"]}}` | Execute exactly the selected branch, based on structured data. |
| `{"loop": {"flow": ["work", "review"], "while": {"path": "/0/data/retry", "equals": true}}}` | Execute the body at least once, then repeat while the condition matches, passing the previous body's terminal results into the next iteration. |

Control paths are JSON pointers into `/<input-index>/data[/...]`. Routing requires a schema-validated result; missing data/paths and non-array map results fail deterministically. Arbitrary expression evaluation and natural-language parsing are not used. Equality is type-sensitive.

The step after a parallel/map block is its explicit join: it receives all terminal commits and results. It starts only after all branches finish. An agent join has one designated workspace base and can inspect/merge the other input commits; the runtime performs no semantic merge. A map over an empty array passes its original inputs through, so a following join still runs once. Blocks may nest, including loops and maps, and a join never mixes results from different loop iterations. This structured graph grammar deliberately excludes arbitrary dynamic topology.

`concurrency` (default 4) bounds running node attempts across nested blocks. `max_steps` (default 100) is finite and counts node invocations, control-block activations, and loop continuations. Retries are separately bounded by `retries` (default 0, additional attempts per invocation). Oversized fan-out is rejected before scheduling it.

Agent execution timeout is opt-in. Omit the top-level graph `timeout` field to run each Agent Node until it completes, fails, or is stopped externally, without a GitWeave-imposed wall-clock limit. To bound each agent attempt, set a finite positive number of seconds at the graph level, for example `"timeout": 1800`. Explicit `null`, booleans, strings, zero, negative numbers, and nonfinite values are invalid; only omission disables the timeout. This replaces the previous 600-second default.

Adapters keep the `run(node, context, workspace, timeout)` interface: `timeout` is `None` when omitted, otherwise the configured number. Built-in adapters pass it directly to the subprocess wait. An explicit timeout still kills the process group and produces a retryable Runtime Failure (`timeout`), retaining available stdout/stderr and attempt provenance in Git records. Each retry receives the same timeout and follows the configured `retries` bound. Git/GitHub command timeouts, `max_steps`, and retry bounds are unchanged.

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

Raw logs can contain repository or prompt content; keep provenance under the same access controls as the repository. Runtime finalization publishes Run refs and notes when a provenance destination is resolved; see durable provenance and recovery below. There is no resume-after-process-crash command in v0; retained refs/notes support diagnosis. A hard process/host crash may leave the Run marked running and a worktree on disk. Git storage exhaustion can prevent record writes; those failures are surfaced rather than reported as completed execution.

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

`merge_pr` with `config.repository` and `config.publish_node` targets a publisher in this graph. With empty/omitted `config`, it targets the existing input PR. It requires the exact known remote head and the original base branch. The selected workspace artifact must have the same tree as that remote commit: empty review/action checkpoints are allowed, but unpublished file changes must be synchronized first.

Review/task correctness belongs to the graph. `merge_pr` has no independent `reviewDecision` gate. It requests an immediate merge commit through [GitHub's merge REST endpoint](https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request), with `sha` set to the exact known remote commit. Merge commits are the only supported merge behavior, preserving the original PR commits in the target branch ancestry. GitHub enforces repository policy, required checks/reviews, and allowed merge methods; repositories that disallow merge commits fail without falling back to another method. Rejections surface as failures with GitHub's diagnostic; GitWeave does not use admin bypass or queue auto-merge (including for repositories requiring a merge queue). Successful results contain `merged: true`, `url`, and `merge_commit`. A retry after a successful merge, including a success followed by a transport timeout, recognizes the already-merged exact head.

### Ordinary Issue and PR comments

`comment_issue` and `comment_pr` post ordinary comments to an explicit GitHub repository and positive integer Issue/PR number. They require neither a publisher nor an existing-PR Run input, managed branch, synchronization, approval, or merge. Fork PRs and closed targets can receive comments when GitHub permits it. The action verifies the target number and Issue/PR type before posting; GitHub enforces comment permissions. These actions never submit formal reviews or approval states.

```json
{
  "kind": "action",
  "action": "comment_pr",
  "workspace_base": 0,
  "config": {
    "repository": "owner/repo",
    "number": 9,
    "body_path": "/0/message"
  }
}
```

Use exactly one of `body_path` or literal `body`. `body_path` is an RFC 6901 JSON pointer into the ordered upstream **inputs array**, selecting `/<input-index>/message` or `/<input-index>/data[/...]`. For example, `/0/data/findings` posts a structured string field, and `/1/message` posts the second upstream human-readable message. Escapes `~1` and `~0` select keys containing `/` and `~`. Content must resolve to nonblank text; missing paths, objects, arrays, nulls, and other non-string bodies fail before GitHub calls. A data schema is optional for comments; control-flow routing still requires validated data. Text is sent literally without template, expression, or shell evaluation. Unknown configuration fields, invalid repository identities and nonpositive/noninteger numbers fail validation.

Each posted body ends with a hidden `gitweave-comment` marker derived from the Run ID, declared node ID, and runtime `instance_id`. Before every post, the action scans all comment pages for that exact invocation marker. A retry after a successful post with a lost or malformed response returns the existing comment's ID and URL instead of posting again. A later loop invocation of the same node has a different marker and can post a new comment. Reconciliation never edits or deletes comments, including human comments. Keep the marker intact for retry recognition; this is lookup-based reconciliation, not a GitHub atomic idempotency API or a crash-resume facility. Actions remain serialized within a Run.

Successful results expose `data.id`, `data.url`, `data.repository`, and `data.number`. Failures and successful retry results retain the normal attempt commits/notes. All requests use the runtime's native `gh` authentication; credentials are not included in node context or results. See [review-comment.json](../examples/review-comment.json) for a review-to-comment graph; change the action to `comment_issue` for an investigation-to-Issue workflow.

## Existing Pull Request input

```sh
gitweave run --graph examples/review-fix-merge.json --repo owner/repo --pr 10 \
  "Review this PR, fix remaining problems, and merge it when clean."
```

The input modes are deliberately small and mutually exclusive:

- `--repo /path/to/local/repository --commit COMMIT` preserves the existing local input flow.
- `--repo owner/repo --pr NUMBER` accepts a positive PR number on github.com. PR URLs, other hosts, and local checkout paths with `--pr` are not supported. A relative `owner/repo` string in PR mode always means a GitHub identity, even if a directory with that name exists.

PR mode creates a persistent bare object store at `.gitweave/runs/<run-id>/repository.git` under the invoking directory. The CLI includes its absolute `repository` path in its output; use `git -C PATH show RUN_REF:run.json` to inspect it. The store is retained for artifact/history access, including when execution fails, and is not automatically deleted. Initialization failures may leave a partial store. No local clone is required, and no existing checkout is modified by initialization.

Initialization resolves an open PR and fetches `refs/pull/<number>/head` from its base repository, checking that it is exactly the resolved head SHA; it never starts from GitHub's synthetic merge commit. It also fetches the resolved base commit and verifies the PR metadata again before scheduling nodes. A fetch mismatch or changed PR fails initialization. Both commits are retained under `refs/gitweave/<run-id>/input/{head,base}`.

`run.json.input_pr` and every node's `context.input_pr` expose the initial `number`, base `repository` and `repository_id`, `head_repository` and `head_repository_id` (nullable if deleted), `head_branch`, `head_sha`, `base_branch`, `base_sha`, `url`, `state`, and `merge_commit`. Only these selected fields are retained, not the raw API response or authentication material. `base_sha` is the frozen review comparison commit, not a moving branch name. Ordinary advancement of the same base branch after initialization is allowed; retargeting to another branch is a conflict. GitHub remains responsible for current mergeability and repository policy.

The initial `head_sha` stays immutable. `pr_remote_sha` in node context and the Run record tracks the last successfully verified remote head separately from local artifact/checkpoint commits. Nodes receive the current selected artifact in `workspace_base` as usual. Action result commits are provenance checkpoints, not necessarily the pushed SHA.

`sync_pr` requires existing-PR input, `workspace_base`, and empty/omitted `config`:

```json
{"kind": "action", "action": "sync_pr", "workspace_base": 0, "config": {}}
```

It updates that same PR's head branch with the selected artifact. Before mutation it verifies PR identity, repositories, head branch, base branch, open state, current permission to push, API head SHA, and the exact branch SHA from `ls-remote`. A single-ref `push --force-with-lease=refs/heads/BRANCH:KNOWN_SHA` provides the atomic compare-and-update; a competing write between inspection and push is rejected by Git. It never uses a tracking-ref-derived lease or an unconditional force push. It checks PR metadata/head again after pushing. A retry recognizes the exact target commit when a successful push timed out, and does not push it twice. An external move, closed PR, deleted head, or retarget fails instead of silently adopting the new state.

Cross-repository (fork) and deleted-repository heads may be read as inputs when GitHub's PR ref is available, but `sync_pr` and unmerged input `merge_pr` explicitly reject them. Same-repository mutations require GitHub's current `permissions.push`; insufficient permissions fail before mutation. GitWeave never substitutes the base repository's same-named branch for a fork head. These are intentionally unsupported mutation cases, not invitations to use another credential or manual push.

GitHub PR metadata and Git branch updates are separate operations: the lease atomically protects the head SHA, but cannot lock PR closure/retargeting. Metadata changes observed before or after synchronization stop the Run; a post-push failure may mean the exact artifact was already pushed. Merge's SHA precondition protects its head; base retargeting is checked before the request, not atomically locked by GitHub's merge API. No automatic rollback overwrites a concurrent actor's changes. Start a new Run to adopt a changed PR. Retry state is in-process; v0 still has no crash-resume command.

The review-fix-merge example graph reviews structured `{approved, findings}` output, passes concrete findings to Fix, synchronizes its checkpoint, and reviews again until approved. Its `max_steps: 30` includes control steps and bounds the loop; exhaustion or invalid output fails without merging. It uses at most one additional retry per invocation. A clean first review merges without a push. Review nodes are instructed to leave files unchanged; merge's tree check catches unpublished review edits. The example's approval is a graph decision, distinct from runtime completion and GitHub policy. Comment actions are independent and can be added explicitly when desired.

Agent subprocesses inherit a copy of the normal parent environment by default. Unknown development variables, toolchain paths, package registry settings and credentials, proxy settings (including `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY` and lowercase forms), and general certificate settings remain available. Native Codex and Claude authentication also inherits normally: home/config locations, API keys, OAuth tokens and provider-specific configuration are preserved. GitWeave does not maintain a development-variable allowlist.

The focused exclusions below remove publication credentials and overrides that grant or redirect repository, configuration, credential or transport authority. Names are exact except the two explicitly listed prefix families; GitWeave does not strip all `GIT_*`, `GH_*`, `SSH_*` or provider variables.

| Excluded names / families | Authority implication |
| --- | --- |
| `GH_TOKEN`, `GITHUB_TOKEN`, `GH_ENTERPRISE_TOKEN`, `GITHUB_ENTERPRISE_TOKEN` | GitHub and enterprise publication credentials. |
| `GH_HOST`, `GH_REPO`, `GH_CONFIG_DIR` | Select a GitHub host/repository or an alternate CLI configuration containing credentials. |
| `SSH_AUTH_SOCK`, `SSH_AGENT_PID`, `SSH_ASKPASS`, `SSH_ASKPASS_REQUIRE` | Access or control the parent's SSH agent, or select an SSH credential prompt program. |
| `GIT_DIR`, `GIT_WORK_TREE`, `GIT_COMMON_DIR`, `GIT_INDEX_FILE`, `GIT_OBJECT_DIRECTORY`, `GIT_ALTERNATE_OBJECT_DIRECTORIES`, `GIT_NAMESPACE`, `GIT_CEILING_DIRECTORIES`, `GIT_DISCOVERY_ACROSS_FILESYSTEM`, `GIT_SHALLOW_FILE`, `GIT_REPLACE_REF_BASE`, `GIT_NO_REPLACE_OBJECTS` | Redirect repository discovery, storage, refs or the object view away from the assigned worktree's normal Git context. |
| All names starting with `GIT_CONFIG` | Redirect configuration files or inject configuration, including credential helpers, remote URLs, headers and hooks. Covers `GIT_CONFIG`, `GIT_CONFIG_GLOBAL`, `GIT_CONFIG_SYSTEM`, `GIT_CONFIG_NOSYSTEM`, `GIT_CONFIG_PARAMETERS`, `GIT_CONFIG_COUNT` and every indexed `GIT_CONFIG_KEY_*` / `GIT_CONFIG_VALUE_*` entry. |
| `GIT_EXEC_PATH`, `GIT_TEMPLATE_DIR` | Select alternate Git executables/helpers or repository templates that can install configuration/hooks. |
| `GIT_ASKPASS`, `GIT_TERMINAL_PROMPT`, all names starting with `GIT_CREDENTIAL_` | Override credential acquisition, including settings consumed by credential helpers. |
| `GIT_SSH`, `GIT_SSH_COMMAND`, `GIT_SSH_VARIANT`, `GIT_PROXY_COMMAND`, `GIT_ALLOW_PROTOCOL`, `GIT_PROTOCOL`, `GIT_PROTOCOL_FROM_USER` | Replace Git transport commands or alter transport/protocol selection and restrictions. Ordinary development proxy variables still inherit. |
| `GIT_SSL_NO_VERIFY`, `GIT_SSL_CAINFO`, `GIT_SSL_CAPATH`, `GIT_SSL_CERT`, `GIT_SSL_KEY`, `GIT_SSL_CERT_PASSWORD_PROTECTED`, `GIT_PROXY_SSL_CAINFO`, `GIT_PROXY_SSL_CERT`, `GIT_PROXY_SSL_KEY`, `GIT_PROXY_SSL_CERT_PASSWORD_PROTECTED` | Override Git transport trust or provide client/proxy certificate authentication. |

Filtering creates a new mapping without changing the parent's `os.environ` and does not log environment values. Runtime-owned GitHub System Actions use a separate parent-environment copy and retain their publication credentials; their existing fixed `GH_HOST=github.com` behavior is unchanged. The runtime Git wrapper's existing environment handling is also unchanged.

This is authority separation, not an OS security boundary against a malicious agent or a general environment sandbox. Worktrees share a Git object store, native home/config files and on-disk credentials remain available according to the CLI's sandbox, and inherited development settings may themselves carry authority. Use trusted graphs/repositories and appropriate native sandbox policy. GitWeave does not require Docker or disable native permission checks.

## Validation and live smoke

```sh
python -m unittest discover -s tests -v
python scripts/smoke.py --provider both
```

Deterministic tests use temporary Git repositories, injected adapters/actions, native event fixtures and mocked GitHub calls. They require no agent login, network, or model allowance. CI runs them on Python 3.11 and 3.14.

The opt-in smoke creates a disposable repository, invokes both installed CLIs with their normal authentication, validates structured results, and checks that both file artifacts reach the terminal commit. It publishes nothing and prints the repository and Run ID for inspection. Use `--provider codex` or `--provider claude` to diagnose an individual adapter. Authenticate using the respective CLI before running; do not change providers, redeem reset tickets or buy allowance when a limit is encountered.

CLI interfaces were checked against [Codex non-interactive documentation](https://developers.openai.com/codex/noninteractive), [Claude programmatic execution documentation](https://code.claude.com/docs/en/headless), and installed CLI help. Smoke validation complements deterministic coverage; it does not prove general task correctness.

## Durable provenance and recovery

Runtime finalization archives the final Run record after all attempts and System
Actions (including merge) have been recorded. This is runtime-owned Git transfer,
not an agent action or an early `publish_pr` snapshot. The archive contains exactly
`refs/gitweave/RUN_ID/*` (Run, all successful/failed attempts, PR inputs and an
`archive` manifest) and `refs/notes/gitweave/RUN_ID`. Artifact branches are unchanged;
merging or deleting them does not delete provenance. Retained logs are included.

Destination selection is deterministic:

1. Existing-PR Runs use the PR's base repository; graphs with `publish_pr` use its
   publication repository. A single distinct repository takes precedence over
   local `origin`. Comment targets do not select an artifact repository.
2. Multiple artifact repositories require `run --provenance-remote` to select one.
   An override must match one of those repositories using its canonical
   `https://github.com/OWNER/REPO.git` URL (or a named remote resolving to that URL).
   For fork PR inputs this means the base repository; inability to write there is
   a visible persistence failure, not an implicit fallback to the fork.
3. For local Runs without a PR/publication repository, use the configured `origin`
   push URL. `--provenance-remote REMOTE_OR_URL` explicitly selects another artifact
   destination. A named remote must resolve to exactly one push URL.
4. With no destination, execution remains local/offline and the final record has
   `provenance_destination: null`. No durability is claimed. Export can be performed
   later, including for final Runs created by older runtimes.

GitHub transfers use the runtime's `gh auth git-credential` helper; other targets
use native Git/SSH credentials. URLs containing user information, query strings
or fragments are rejected. Credential values are never stored in the destination
field. Transfer diagnostics report a bounded error category rather than raw Git
stderr, which may contain credentials. Existing attempt logs are preserved.

```sh
# Explicit destination for a local Run
gitweave run --graph examples/single.json --repo /path/to/repo --commit HEAD \
   --provenance-remote origin "Implement the request"

# Archive an already final local Run (also works for older runtime records)
gitweave export --repo /path/to/repo --run RUN_ID --remote origin

# After the original local repository has been deleted
git clone REPOSITORY_URL recovered
gitweave fetch --repo recovered --run RUN_ID --remote origin
git -C recovered show refs/gitweave/RUN_ID/run:run.json
git -C recovered notes --ref=refs/notes/gitweave/RUN_ID show ATTEMPT_COMMIT
```

`export` without `--remote` uses the recorded destination, then the selection rules
above. `fetch` requires an explicit remote; it can also recover into an empty
`git init` repository. Ordinary clone does not fetch these namespaces. Discover
archived Run IDs with `git ls-remote REPOSITORY_URL 'refs/gitweave/*/archive'`.
The corresponding raw refspecs are
`refs/gitweave/RUN_ID/*:refs/gitweave/RUN_ID/*` and
`refs/notes/gitweave/RUN_ID:refs/notes/gitweave/RUN_ID`; prefer `gitweave fetch`,
which validates completeness and installs refs in a local transaction.

Archives are immutable snapshots. Export requires either an empty remote Run
namespace or an exact match of all refs and their object IDs. Creation uses one
atomic push with explicit per-ref empty-value leases and no tags; atomic-push
support is required. There is no non-atomic fallback, automatic retry, overwrite,
repository-setting change, or branch-protection bypass. Partial archives and
collisions fail visibly. Repeated export/fetch of the same snapshot is safe.
Fetch verifies the manifest and recorded attempts/notes and PR inputs before
installing refs; conflicting local refs are preserved. Matching subsets can be
completed locally. Neither command changes other Runs or artifact branches.

A transfer failure raises a `persistence` error (CLI exit 2), even when execution
completed. Local evidence remains available for inspection and a later explicit
export. The local final Run's `status` describes execution, not transfer success;
it is not rewritten after upload, avoiding recursive self-recording. A successful
export response is the durability acknowledgement. A timeout after a successful
push can be reconciled by repeating export. A hard crash before finalization may
leave a running Run, which cannot be archived with this entry point. Final failed
Runs are archived too when a destination is resolved. Durability does not imply
task approval.
