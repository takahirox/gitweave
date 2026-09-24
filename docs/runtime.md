# GitWeave v0 runtime

Supports Linux and macOS. Requires Python 3.11+, Git, and the `codex` and/or `claude` executables used by executed nodes. GitHub actions additionally require `gh` with native GitHub authentication. Install with `python -m pip install .`, or run directly with `python -m gitweave`.

```sh
gitweave run --graph examples/single.json --repo /path/to/repository --commit HEAD "Implement the requested change"
```

The original checkout is not an output workspace. Each invocation receives an isolated detached worktree; inspect the returned terminal commit(s) to use its artifacts. Existing uncommitted files in the original checkout are not inputs. Local refs and notes retain Run and attempt history without a database. A successful Run means the graph executed normally, not that the task was approved.

## Graph contract

Version 1 uses JSON, with `version: 1`, a `nodes` object and a nonempty `flow` array. All node declarations have `kind` and explicit `workspace_base`: an upstream input index (starting at zero), or `"run"` for the Run's original commit. Input ordering follows graph branch/item order, never completion order. Graph files, instructions and target repositories are trusted operator inputs.

Graph structure and node configuration are validated at Run initialization. Node-specific runtime requirements are checked only when the node executes: an unselected branch may contain an unregistered agent provider or an input-PR action without `--pr`. If selected, a missing adapter or missing existing-PR input fails that node attempt clearly and is retained in Run/attempt provenance. `sync_pr` and existing-PR `merge_pr` require `--pr` only when executed. Run inputs themselves (including an explicitly supplied PR) are still resolved at initialization.

Agent nodes specify `provider`, `instruction`, optional `model` and `effort`, and optional `schema` for result data. Built-in providers are `codex` and `claude`; the Python runtime accepts additional adapters through dependency injection. Model and effort are passed to the chosen CLI; unsupported settings fail visibly rather than being silently substituted. Provider defaults apply if omitted.

Native sandbox restrictions are opt-in per node through the optional, provider-specific `sandbox` field. Codex nodes accept any string; the native CLI determines which values it supports. If omitted, GitWeave explicitly passes `--sandbox danger-full-access`; omitting the CLI flag could restore a restrictive provider default. This replaces the previous unconditional `--sandbox workspace-write`. To retain that restriction on a node:

```json
{
  "kind": "agent",
  "provider": "codex",
  "sandbox": "workspace-write",
  "workspace_base": 0,
  "instruction": "Implement the requested change"
}
```

Explicit values are passed unchanged using native `--sandbox`, without a combined approval/sandbox bypass flag or approval-policy override. Only non-string values (including `null`) fail value validation. Any `sandbox` field on Claude, custom-provider, or System Action nodes is rejected. Codex sandbox and approval behavior is independent of Claude permission configuration.

Claude agent nodes accept an optional `permission_mode` field. When omitted, GitWeave omits `--permission-mode` entirely and defers to Claude's native settings and normal permission behavior. Omission does **not** force `auto`. This replaces the previous unconditional `--permission-mode acceptEdits`. To retain that explicit mode on a node:

```json
{
  "kind": "agent",
  "provider": "claude",
  "permission_mode": "acceptEdits",
  "workspace_base": 0,
  "instruction": "Implement the requested change"
}
```

Any string is accepted; Claude determines which values it supports. Explicit values pass unchanged as `--permission-mode <value>`. Non-string values (including `null`) fail validation; any `permission_mode` field on Codex, custom-provider, or System Action nodes is rejected.

GitWeave does not allowlist native option values. Explicit `sandbox` and `permission_mode` strings, including empty strings, are passed without trimming, case normalization, or substitution. The native CLI accepts or rejects them.

GitWeave continues to use Claude's non-interactive `-p` invocation without changing native settings, permission prompts, or other permission controls. Mode availability and execution remain subject to the installed CLI and native configuration; GitWeave does not substitute another mode or automatically fall back if Claude rejects a mode or denies an operation. There is no shared permission policy across providers.

The dedicated worktree remains the official artifact boundary in every mode: agents must leave final files there. The common Codex/Claude prompt contains only this node/worktree context, the node's declared instruction, and serialized execution inputs labeled as data. GitWeave does not add behavioral directives about publication, usage limits, provider switching, or task approval. Graph authors are responsible for task-specific instructions. Agent subprocesses inherit the parent environment unchanged. A worktree is not an OS security boundary.

The adapter receives a context containing the request, `run_input`, input commits/messages/data, selected workspace-base commit, and any fan-out item. `run_input` is the Run's lightweight source identity with a `kind` discriminator: `{"kind": "commit", "commit": SHA}`, `{"kind": "pull_request", "number": N}`, or `{"kind": "issue", "number": N}`. The repository is part of the Run and is not repeated there; nodes receive it separately as `github_repository` (`owner/repo` for `--pr`/`--issue` Runs, `null` for local Runs). GitWeave does not embed Issue or PR content in node context; nodes read it themselves when the graph needs it. Both agents and actions also receive `instance_id`, the same invocation identity recorded in attempt provenance: stable across retries, distinct across loop iterations and fan-out items. It must return a `Result`; provider-native events, stderr, session IDs, usage and cost fields are preserved when available. No token/cost estimates are invented. The configured model/effort and raw events retain both requested and provider-reported information.

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

Adapters keep the `run(node, context, workspace, timeout)` interface: `timeout` is `None` when omitted, otherwise the configured number. Built-in adapters pass it directly to the subprocess wait. Without a timeout, Codex and Claude use normal subprocess session and process-group inheritance. Only an explicit timeout creates a new session so GitWeave can kill the process group when the timeout expires. This produces a retryable Runtime Failure (`timeout`), retaining available stdout/stderr and attempt provenance in Git records. Each retry receives the same timeout and follows the configured `retries` bound. `max_steps` and retry bounds are unchanged.

Runtime-owned Git and GitHub (`gh`) commands have no GitWeave-imposed wall-clock timeout; the former fixed 120-second limits have been removed. They run until completion, command failure, or external termination. The graph `timeout` applies only to Agent Nodes, not Git or GitHub System Actions. There is no separate runtime command timeout setting. Command launch errors and nonzero exits remain retryable Runtime Failures (`git` or `github`) with their existing diagnostics.

Agent provider failures expose native diagnostics: Codex error/failed-turn messages and Claude failed-result errors (or result text) take precedence over stderr. When no error text is available, stderr, the native failure event, or the provider result text is retained; the generic completion failure is used only when none is useful. Nonzero exits with unparseable output expose stderr or stdout instead of a JSON parsing error. Raw events and logs remain in provenance. Failure classification and retryability are unchanged.

On an exhausted/nonretryable failure, scheduling stops. Already-running siblings finish and retain their results before the Run is finalized as failed. No later dependent steps run. Runtime retries start from the original commit and input context with a new worktree. Task outcomes must be handled explicitly in the graph. Invalid structured output is a nonretryable contract failure. Provider execution failures follow configured retries regardless of usage/quota wording in stdout or stderr. Adapters may mark failures nonretryable using explicit provider-native signals (currently Claude’s `rate_limit_event` with `rate_limit_info.status: rejected`). Retry decisions use the failure’s retryable flag, not its kind or free-form text; the runtime never purchases allowance, resets limits, or falls back to another model/provider.

## Git records

Git operations inherit the parent process environment, including `GIT_*` variables, and use normal Git configuration and hooks. GitWeave does not force `core.hooksPath` or sandbox the Git environment. It sets author and committer name/email to `GitWeave <gitweave@localhost>` for its provenance commits. Checkpoint tree construction uses a private `GIT_INDEX_FILE` to capture final files without changing the worktree's index or an inherited index; these child-process overrides do not modify the parent environment. Checkpoints use `commit-tree`, which retains Git's normal plumbing behavior rather than running porcelain commit hooks.

- `refs/gitweave/<run-id>/run` is written once at finalization for a completed or failed Run. It points to an independent record commit containing `run.json`, including the exact graph text, digest, request, base, timestamps, status, attempt index and terminal outputs. No aggregate Run record is written at startup or after individual attempts; attempt refs and notes retain provenance during execution.
- `refs/gitweave/<run-id>/attempts/<instance-id>/<attempt>` retains every completed or failed attempt. The instance ID is distinct for each invocation, including loop iterations and fan-out items; the note retains declared node ID, item and nested fan-out origin.
- `refs/notes/gitweave/<run-id>` stores execution records on attempt commits. Notes contain original inputs, workspace base, instruction/configuration, result, raw logs, timing, usage, sessions and failure diagnostics.
- Each node invocation attempt, successful or failed, produces exactly one commit, which carries its attempt ref and Git note and is its stable execution identity. A successful attempt's commit is the node's checkpoint: its artifact state at the node boundary. Same-tree (empty) checkpoints are intentional: a node that changes no files, or only causes external side effects, still records that it ran and completed at that graph boundary, so they must not be optimized away. Successful checkpoints make Runs inspectable and analyzable later and are the intended completed-node boundaries for future resume/restart; resume itself is not implemented (see below). A failure commit (below) records the failed attempt but is not a completed-node boundary.
- Success checkpoints the final assigned worktree, including an empty commit if unchanged. The checkpoint uses the Agent's final HEAD and any pending merge heads as parents; the original workspace base need not remain an ancestor. A private index captures final files even when the Agent's index has unresolved entries, without modifying that index. Conflict markers left in files are captured as file content. The original workspace base remains recorded in attempt provenance. A failure commit has the original workspace base as parent and the same tree as that base; a retry never uses the failure commit. Action attempts also get commits and notes.

```sh
git show refs/gitweave/RUN_ID/run:run.json
git notes --ref=refs/notes/gitweave/RUN_ID show OUTPUT_COMMIT
git diff BASE_COMMIT OUTPUT_COMMIT
```

Raw logs can contain repository or prompt content; keep provenance under the same access controls as the repository. Runtime finalization publishes Run refs and notes when a provenance destination is resolved; see durable provenance below. There is no resume-after-process-crash command in v0; retained checkpoint refs/notes support diagnosis and are intended as the completed-node boundaries a future resume would continue from. A hard process/host crash before finalization may leave retained attempt refs/notes and a worktree on disk, but no final Run record. Git storage exhaustion can prevent record writes; those failures are surfaced rather than reported as completed execution.

After attempting to record provenance, GitWeave attempts normal temporary worktree removal even if recording failed. Cleanup failures fail the Run when there is no earlier failure; otherwise the earlier failure stays primary and cleanup diagnostics are logged. Cleanup does not rewrite attempt provenance or add a `cleanup_warning`. Failed removal may leave filesystem/worktree remnants; there is no fallback deletion or intentional retention for diagnosis.

## Explicit GitHub actions

Git transport operations (`fetch`, `push`, and `ls-remote`), including provenance
persistence, use normal Git configuration and authentication. GitWeave does not
inject a credential helper; configured SSH, URL rewrites, and credential helpers
apply normally, and Git failures retain their diagnostics. To use GitHub CLI
credentials for Git transport, configure them through your normal Git/GitHub
setup. GitHub API operations continue to use the `gh` CLI.

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

`publish_pr` pushes the selected workspace-base artifact to `gitweave/<run-id>/<declared-node-id>` and creates or updates its PR. Revisit the same publisher node after fixes to synchronize the same PR. Each attempt force-pushes only that GitWeave-owned branch, then looks up the corresponding PR and creates it or edits its base, title, and body. Git and GitHub errors are returned directly; there is no remote-head preflight or special retry-success recognition. The last pushed commit and configured base remain recorded for the exact-head checks in `merge_pr`. Publication and merge operations for the same publisher are synchronized to keep its managed branch, PR, and recorded head/base coherent. Different publishers can execute concurrently.

Input-PR synchronization and merge operations share a lock for that input PR's known remote head; they do not block managed-PR actions or comments.

`merge_pr` with `config.repository` and `config.publish_node` targets a publisher in this graph. With empty/omitted `config`, it targets the existing input PR. It requires the exact known remote head and the original base branch. It does not compare the selected local artifact with the PR head or reject unpublished local changes. To include local changes in an input PR, the graph must explicitly run `sync_pr` before `merge_pr`; for a managed PR, revisit its `publish_pr` node. Graph correctness belongs in the graph, while GitHub decides whether the requested PR merge is allowed.

Review/task correctness belongs to the graph. `merge_pr` has no independent `reviewDecision` gate. It requests an immediate merge commit through [GitHub's merge REST endpoint](https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request), with `sha` set to the exact known remote commit. Merge commits are the only supported merge behavior, preserving the original PR commits in the target branch ancestry. GitHub enforces repository policy, required checks/reviews, and allowed merge methods; repositories that disallow merge commits fail without falling back to another method. Rejections surface as failures with GitHub's diagnostic; GitWeave does not use admin bypass or queue auto-merge (including for repositories requiring a merge queue). Successful results contain `merged: true`, `url`, and `merge_commit`. Each invocation requires an open PR and makes one merge request after the target/head checks pass. Transport failures surface directly; GitWeave does not reconcile ambiguous completion or recognize an already-merged PR as retry success.

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

Each invocation posts the resolved body unchanged with one comment POST, without hidden markers or comment-history lookups. Repeated invocations post new comments. If GitHub accepts a comment but its response is lost or malformed, the action reports a failure; a runtime retry can post another comment. Comments can execute concurrently with each other and with publication, synchronization, and merge actions when scheduled concurrently by the Graph.

Successful results expose `data.id`, `data.url`, `data.repository`, and `data.number`. Failures and successful results retain the normal attempt commits/notes. All requests use the runtime's native `gh` authentication; credentials are not included in node context or results. See [review-comment.json](../examples/review-comment.json) for a review-to-comment graph; change the action to `comment_issue` for an investigation-to-Issue workflow.

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

Initialization reads the open PR metadata once, fetches `refs/pull/<number>/head` from its base repository, and fetches the resolved base commit. It never starts from GitHub's synthetic merge commit. The actual fetched commits are recorded as the Run's head/base SHAs and retained under `refs/gitweave/<run-id>/input/{head,base}` before scheduling nodes. Initialization does not compare the fetched head with the earlier API value or reread metadata to detect changes during fetching. Execution uses these frozen commits; later PR actions check the current PR state when they execute.

`run.json.run_input` and every node's `context.run_input` are `{"kind": "pull_request", "number": N}`, and `run.json.github_repository` records `owner/repo`. The PR metadata read at initialization is kept only inside the runtime for `sync_pr`/`merge_pr` checks; it is not a node-context field. The frozen head and base commits remain available through the input refs above. Ordinary advancement of the same base branch after initialization is allowed; retargeting to another branch is a conflict. GitHub remains responsible for current mergeability and repository policy.

The initial head stays immutable. `pr_remote_sha` in the Run record tracks the last successfully verified remote head separately from local artifact/checkpoint commits. Nodes receive the current selected artifact in `workspace_base` as usual. Action result commits are provenance checkpoints, not necessarily the pushed SHA.

`sync_pr` requires existing-PR input, `workspace_base`, and empty/omitted `config`:

```json
{"kind": "action", "action": "sync_pr", "workspace_base": 0, "config": {}}
```

It updates that same PR's head branch with the selected artifact. Before mutation it verifies PR identity, repositories, head branch, base branch, open state, and an identifiable head repository. A single-ref `push --force-with-lease=refs/heads/BRANCH:KNOWN_SHA` is the authoritative compare-and-update guard: Git rejects an update if the branch was unexpectedly moved or deleted. The known SHA starts at the resolved input head and advances only after a successful push. It never uses a tracking-ref-derived lease or an unconditional force push. There is no separate API head comparison, `ls-remote` check, post-push verification, or special successful-push/failed-response reconciliation; each attempt uses the known SHA and returns Git's success or failure. Closed or retargeted PRs detected by the pre-push metadata check fail before pushing.

Cross-repository (fork) heads are supported: `sync_pr` pushes to the head repository and exact head ref, never the base repository's same-named branch. A deleted or unavailable head repository prevents synchronization because its push target cannot be identified, but does not itself prevent `merge_pr` from requesting a merge in the base repository. Neither action preflights repository `permissions.push`; the actual Git push or GitHub merge request determines permission and surfaces its failure. Inputs with deleted head repositories can still be read when GitHub's PR ref is available.

GitHub PR metadata and Git branch updates are separate operations: the lease atomically protects the head SHA, but cannot lock PR closure/retargeting. Metadata changes observed before or after synchronization stop the Run; a post-push failure may mean the exact artifact was already pushed. Merge's SHA precondition protects its head; base retargeting is checked before the request, not atomically locked by GitHub's merge API. No automatic rollback overwrites a concurrent actor's changes. Start a new Run to adopt a changed PR. Retry state is in-process; v0 still has no crash-resume command.

The review-fix-merge example graph reviews structured `{approved, findings}` output, passes concrete findings to Fix, synchronizes its checkpoint, and reviews again until approved. Its `max_steps: 30` includes control steps and bounds the loop; exhaustion or invalid output fails without merging. It uses at most one additional retry per invocation. A clean first review merges without a push. Review nodes are instructed to leave files unchanged; the graph is responsible for synchronizing any edits it wants included before merging. The example's approval is a graph decision, distinct from runtime completion and GitHub policy. Comment actions are independent and can be added explicitly when desired.

Codex and Claude Agent Node subprocesses inherit the parent process environment unchanged through normal subprocess inheritance. GitWeave does not supply an environment mapping, maintain an allowlist or denylist, or strip GitHub credentials, SSH agent variables, Git configuration overrides, credential-helper settings, native agent authentication, or ordinary development variables. This supersedes the filtering policy from Issue #16, as required by Issue #23. The default launch environment is the same as invoking the underlying agent CLI directly from the same shell.

GitWeave remains a thin runtime, not a general credential or environment sandbox. The assigned worktree defines official artifact state. Graphs can use explicit System Actions for publication; task-specific agent behavior is defined by the node's instruction. Any future environment restriction should address a concrete observed problem with the smallest necessary change.

Runtime-owned GitHub System Actions retain their existing parent-environment copy and fixed `GH_HOST=github.com` behavior. The runtime Git wrapper's existing environment handling is unchanged. Worktrees share a Git object store; GitWeave does not require Docker or disable native permission checks.

## GitHub Issue input

```sh
gitweave run --graph graph.json --repo owner/repo --issue 123 "Implement this Issue"
```

`--issue NUMBER` accepts a positive integer and uses the same `owner/repo` rules and bare object store as PR mode. GitWeave records the number verbatim as `run_input: {"kind": "issue", "number": 123}` and exposes it in every node's context. It does not verify that the Issue exists, fetch its title/body/state, or interpret it; the Graph decides how nodes read and process the Issue (for example, an Agent instructed to read it with available tools). The same graph can be reused for different Issues.

The Run base is the remote default branch HEAD, fetched once from `https://github.com/OWNER/REPO.git` at initialization and retained as `refs/gitweave/<run-id>/input/base`. Later branch movement does not change the Run. The Run's checkpoint commits, notes and refs are persisted to that same repository unless `--provenance-remote` overrides it.

## Validation and live smoke

```sh
python -m unittest discover -s tests -v
python scripts/smoke.py --provider both
```

Deterministic tests use temporary Git repositories, injected adapters/actions, native event fixtures and mocked GitHub calls. They require no agent login, network, or model allowance. CI runs them on Python 3.11 and 3.14.

The opt-in smoke creates a disposable repository, invokes both installed CLIs with their normal authentication, validates structured results, and checks that both file artifacts reach the terminal commit. It publishes nothing and prints the repository and Run ID for inspection. Use `--provider codex` or `--provider claude` to diagnose an individual adapter. Authenticate using the respective CLI before running; do not change providers, redeem reset tickets or buy allowance when a limit is encountered.

CLI interfaces were checked against [Codex non-interactive documentation](https://developers.openai.com/codex/noninteractive), [Claude programmatic execution documentation](https://code.claude.com/docs/en/headless), and installed CLI help. Smoke validation complements deterministic coverage; it does not prove general task correctness.

## Durable provenance

After recording all attempts and final System Actions (including merge), runtime
finalization pushes `refs/gitweave/RUN_ID/*` and `refs/notes/gitweave/RUN_ID`.
This includes the final Run record, successful and failed attempts, PR input refs,
and notes with retained logs. Only the selected Run's refs are pushed; artifact
branches, tags and other Runs are unchanged. Merging or deleting an artifact branch
does not delete these refs.

Destination selection uses `run --provenance-remote REMOTE_OR_URL` when supplied.
Otherwise, selection happens at finalization using the Run's GitHub repository (`--pr` or `--issue`) and
the repositories of `publish_pr` invocations that actually started, whether they
succeeded or failed. Unexecuted nodes do not contribute. One distinct repository
is used (`https://github.com/OWNER/REPO.git`), falling back to local `origin` when
there is no such repository. Multiple distinct actual repositories require the
explicit override at finalization; the Run records and refs remain local if this
selection is ambiguous. Invoked publication repositories are recorded in
`publication_repositories`. This keeps existing GitHub workflows durable even
when their storage has no origin, while allowing named remotes, SSH URLs and local bare
repositories without requiring canonical URL matching. All destinations, including
direct GitHub HTTPS URLs, use ordinary Git push configuration and credentials.
Keep credentials in Git helpers rather than URLs, since the selected destination
is recorded in the Run.

With no workflow repository, origin or override, the Run remains local/offline
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
