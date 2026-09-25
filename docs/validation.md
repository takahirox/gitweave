# v0 validation evidence

## Issue #96: shared per-repository store for PR/Issue Runs

On 2026-09-25, `python3 -m unittest discover -s tests -v` passed all 113 deterministic tests; `git diff --check` passed. Real local Git tests (GitHub URLs redirected to a local repository; `gh` guarded) cover the following:

- An Issue Run and a later PR Run, the latter spelled `Owner/Repo`, share `.gitweave/repos/owner/repo.git`. No per-Run store is created.
- Initializing the second Run fetches no objects already present: the object count is unchanged.
- Each Run's `run.json`, refs and notes stay separate in the shared store, only each Run's own refs are pushed, and no worktrees are left behind.
- Two Runs started concurrently against an absent store both complete with correct bases, while both hold worktrees in the store at the same time.

The concurrent test first failed with `could not lock config file … File exists` from simultaneous `git init`. The store is now initialized in a staging directory and renamed into place. Bases are fetched directly into `refs/gitweave/<run-id>/input/base` instead of `FETCH_HEAD`, and worktrees use unique directory names.

## Issue #94: human-readable agent commits in examples

On 2026-09-25, a live Run in a sandbox repository (all nodes on Claude Opus 5.5, `effort: high`) used the new `implement`/`fix` instructions. Issue #5 led to PR #6, with one review rejection and one fix, and was merged with a merge commit. The merged history had agent commits with proper subjects and bodies (`Add power function to calc (#5)` and `Raise ValueError for zero base with negative exponent in power`), each directly followed by a same-tree `GitWeave RUN_ID … attempt 1` checkpoint that carries the note, plus the same-tree publish/review checkpoints. The Run exposed that `fix` lacked an Issue reference, so both example instructions now ask for one. `python3 -m unittest discover -s tests -v` passed; all examples pass `gitweave validate`; `git diff --check` passed.

## Issue #92: per-Run notes refs documented

On 2026-09-25, the Git records section of the runtime guide was updated to explain why notes are stored per Run (`gitweave/git.py`: one notes ref per Run avoids cross-Run read/modify/write races) and that default `git log`/`git notes` do not show them. It also explains how to select a Run's notes by the Run ID in the checkpoint subject (matching `gitweave/runtime.py`), or all Runs by glob. Documentation only; `git diff --check` passed.

## Issue #90: merge commits keep checkpoint provenance

On 2026-09-25, a live `issue-to-merge.json` Run in a sandbox repository, with all nodes on Claude Opus 5.5, merged its PR with a merge commit. From a fresh clone, `git fetch --no-tags origin 'refs/notes/gitweave/*:refs/notes/gitweave/*'` followed by `git log --notes='refs/notes/gitweave/*' main` showed the note for every GitWeave checkpoint commit on `main`, including the same-tree publish and review checkpoints. The runtime guide now recommends merge commits and documents this audit procedure, and both example merge nodes ask for a merge commit. `python3 -m unittest discover -s tests -v` passed; all examples pass `gitweave validate`; `git diff --check` passed.

## Issue #85: optional Run request

On 2026-09-25, `python3 -m unittest discover -s tests -v` passed all 111 deterministic tests; `git diff --check` passed. CLI tests run `--issue`, `--pr` and `--commit` Runs without a request and verify `Runtime` receives `None`; existing tests still pass a request through unchanged. Runtime tests verify that an omitted request is `null` in `run.json`, every node context, and the Run-base input message, and that a supplied request reaches nodes unchanged. The Agent preamble describes `request` as optional operator guidance.

## Issue #82: smaller node execution context

On 2026-09-25, `python3 -m unittest discover -s tests -v` passed all 109 deterministic tests; `git diff --check` passed. Runtime and Command tests verify that Agent and Command Nodes receive only `request`, `github_repository`, `run_input`, `item` and `inputs[]` entries of `node_id`/`commit`/`message`/`data` (Commands also `config`), while attempt notes still record Run ID, instance ID, fan-out origin, the resolved workspace base, and full inputs including instance IDs and validation flags. Control flow still uses the internal validation flag. Each attempt, including a retry, receives a fresh copy of the original context, so node-side mutation cannot leak into retries, downstream inputs or notes. Adapter tests check the updated preamble.

## Issue #84: max_steps counts node invocations

On 2026-09-25, `python3 -m unittest discover -s tests -v` passed all 107 deterministic tests; all example graphs pass `gitweave validate`; `git diff --check` passed. Runtime tests verify that a loop of five node invocations completes with `max_steps: 5` despite repeated `if`/`loop` evaluation and records `steps: 5`, that a sixth invocation fails with `step_limit`, that retry attempts count once, and that the `map` pre-check compares item count with the remaining invocation budget. A loop iteration that invokes no node while its condition still matches (empty `if`, empty `map`, or `parallel` of empty branches) fails immediately with `loop` after only the setup node. Example `max_steps` values were recalibrated to node counts.

## Issue #83: default workspace_base

On 2026-09-25, `python3 -m unittest discover -s tests -v` passed all 105 deterministic tests; all example graphs pass `gitweave validate`; `git diff --check` passed. Graph tests cover omission, explicit indices and `"run"`, and invalid values. A runtime test verifies that an omitted `workspace_base` starts a join's worktree from its first input's checkpoint (and records it as the attempt's `workspace_base`), while explicit `"run"` still selects the Run base. Examples and the runtime guide no longer repeat `"workspace_base": 0`.

## Issue #78: general Agent and Command Nodes

On 2026-09-25, `python3 -m unittest discover -s tests -v` passed all 103 deterministic tests. All example graphs pass `gitweave validate`. `git diff --check` passed.

Command Node tests run real local processes and verify: context JSON on stdin (including `run_input`, `github_repository`, `inputs[]`, `workspace_base`, `instance_id` and `config`), cwd and relative `argv[0]` in the dedicated worktree, Result JSON on stdout handed downstream, stderr retained, same-tree checkpoints, and the same attempt note fields as Agent Nodes plus `argv`/`config`/exit code. Nonzero exit, launch failure, non-UTF-8, empty or non-JSON stdout (including `NaN`), invalid envelopes and schema failures are retryable Runtime Failures. Per-node `retries` and `timeout` override graph values for both node kinds. Adapter tests check the node-contract preamble.

The shipped examples run with scripted agents: `issue-to-merge.json` executes Implement → Publish → Review → Fix → Publish → Review → Merge → Close Issue from `--issue` input, with PR identity forwarded explicitly through Results and a distinct checkpoint commit and note for every invocation (same-tree for Publish/Review/Merge/Close). `review-fix-merge.json` runs from `--pr` input, and `review-comment.json` posts through its Command Node using a fake `gh` executable. Run-input tests verify that PR and Issue Runs fetch their base with Git only and never launch `gh`. GitHub System Actions, `input_pr` metadata, `pr_remote_sha` and publication-based destination selection were removed; earlier entries below describe that historical behavior. No live agents or external publication were invoked.

## Issue #76: GitHub Issue as Run input

On 2026-09-25, `python3 -m unittest discover -s tests -v`: all 143 deterministic tests passed. `git diff --check` passed.

Real local Git tests verify that `--issue` starts from the remote default branch HEAD fetched once at initialization, retains it as the input base ref, exposes only `run_input: {"kind": "issue", "number": 123}` to nodes, makes no GitHub API call, and persists provenance to the Run repository, comparing repository names case-insensitively. Commit-mode tests verify `run_input` of kind `commit` and a null `github_repository` in node context. Contract tests reject nonpositive/non-integer numbers, checkout paths, and combination with `--commit` or `--pr`; CLI tests cover the three mutually exclusive modes. PR and commit Runs expose the same `run_input` shape instead of `input_pr`/`pr_remote_sha` node-context fields. No live agents or external publication were invoked.

## Normal workspace cleanup after storage failures

On 2026-09-20, `python3 -m unittest discover -s tests -v` ran 141 deterministic tests in 23.613 seconds: 140 passed and the installed-entry-point test was skipped because the package was not installed. All 23 focused runtime tests passed. `python3 -m gitweave --help` and `git diff --check` passed.

Focused regressions verify normal worktree removal after attempt-retention or failure-commit storage errors, including removal of the temporary parent directory and Git worktree registration. Cleanup failure now fails the Run and stops dependent execution without rewriting attempt notes. When execution or storage already failed, that failure remains primary and cleanup diagnostics are logged. Tests confirm that failed removal leaves ordinary remnants without fallback deletion and that provenance receives no `cleanup_warning`.

The diff was reviewed against the supplied issue, accepted clarification, and development/review guidelines for completeness and minimal scope. No new recovery protocol or preservation policy was introduced. No live agents or external publication were invoked; final files remain in the assigned worktree for GitWeave checkpointing. This evidence does not constitute PR approval.

## Finalization-only Run records

On 2026-09-20, `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m unittest discover -s tests -v` passed all 141 deterministic tests in 24.508 seconds, including the installed CLI entry point. Module and installed CLI `--help` checks and `git diff --check` passed.

Focused regression coverage verifies that no Run ref exists during execution, earlier successful and failed attempts remain readable through their refs and notes, and successful or exhausted-retry Runs write exactly one final record matching the returned summary. The parallel-failure test verifies that the single final record includes both the failed and completed siblings and their retained notes.

The production change removes only the startup and per-attempt Run-record writes. Finalization and attempt retention remain unchanged. Current runtime documentation explains the finalization-only record and the absence of a final Run record after a hard crash. The diff was reviewed against the supplied issue and development/review guidelines for completeness and minimal scope. No live agents or external publication were invoked; final files remain in the assigned worktree for GitWeave checkpointing. This evidence does not constitute PR approval.

## Final-worktree checkpointing without Git workflow policy

On 2026-09-20, `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m unittest discover -s tests -v` passed all 140 deterministic tests in 29.831 seconds, including the installed CLI entry point. Module and installed CLI `--help` checks and `git diff --check` passed.

New regression tests first reproduced the ancestry and unresolved-index rejections. They now verify checkpointing from an unrelated final HEAD and an unfinished merge, including conflict markers or unstaged resolutions, deleted and untracked files, preserved HEAD and merge parents, byte-for-byte unchanged Agent index and merge state, and retained artifact refs and original-base provenance. The assigned-worktree-root check remains covered.

The production change only removes the ancestry requirement and unresolved-index rejection; the existing private-index capture and provenance mechanisms remain in place. Current runtime documentation describes the resulting behavior. The diff was reviewed against the supplied issue and development/review guidelines for completeness and minimal scope. No live agents or external publication were invoked; deterministic repository tests use disposable local fixtures. Final files remain in the assigned worktree for GitWeave checkpointing. This evidence does not constitute PR approval.

## Node-specific validation at execution

On 2026-09-20, `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m unittest discover -s tests -v` passed all 136 deterministic tests in 25.998 seconds, including the installed CLI entry point. Module and installed CLI `--help` checks and `git diff --check` passed.

Focused branch tests cover an unregistered provider, `sync_pr`, and existing-PR `merge_pr` with both omitted and empty configuration. Unselected nodes do not prevent initialization or completion; selected nodes fail with explicit, nonretryable diagnostics retained in attempt notes and the Run record, and dependent nodes do not execute. No GitHub calls occur for missing PR input. Invalid static configuration in an unselected branch still fails initialization. Existing registered-provider, PR-input, and managed-publication workflows remain covered.

The implementation removes the two whole-Graph runtime preflight scans, checks adapter registration within the agent attempt, and reuses the existing execution-time PR input check. Static validation and Run input resolution are unchanged. The diff was reviewed against the supplied issue and development/review guidelines for completeness and minimal scope. No live agents or external publication were invoked; tests use disposable local repositories and mocks. Final files remain in the assigned worktree for GitWeave checkpointing. This evidence does not constitute PR approval.

## Independent GitHub System Actions

On 2026-09-20, `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m unittest discover -s tests -v` passed all 134 deterministic tests in 22.486 seconds, including the installed CLI entry point. Module and installed CLI `--help` checks and `git diff --check` passed.

Barrier-based tests demonstrate overlapping GitHub calls for independent publishers, input-PR synchronization, and Issue/PR comments, including comments scheduled through the Graph's parallel flow. Thread handshakes verify that repeated publication and managed merges wait for the same publisher's publication to finish, and input-PR sync/merge uses the head recorded by the preceding sync. Existing failure, retry, exact-head/base, and comment behavior remains covered.

The Run-wide action lock is removed. Synchronization is limited to each publisher's branch/PR and recorded head/base, and the input PR's known remote head. A brief mutex protects publisher-lock creation only; no Git/GitHub operation runs under it. Current runtime documentation describes these boundaries.

The diff was reviewed against the supplied issue and development/review guidelines for completeness and scope. No live agents or external publication were invoked; repository tests use disposable local fixtures. Final files remain in the assigned worktree for GitWeave checkpointing. This evidence does not constitute PR approval.

## Single-attempt PR merges

On 2026-09-20, `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m unittest discover -s tests -v` passed all 130 deterministic tests in 22.320 seconds, including the installed CLI entry point. Module and installed CLI `--help` checks and `git diff --check` passed.

Focused managed-PR and input-PR tests verify that closed and already-merged targets fail before a merge request, and transport failures surface unchanged after one exact-SHA merge request without further calls. Existing success, expected-head/base protection, normal merge-commit requests, GitHub policy diagnostics, and unpublished-local-artifact behavior remain covered. The runtime guide now describes single-attempt behavior; historical retry-success evidence below is superseded.

The diff was reviewed against the supplied issue and development/review guidelines for completeness and scope. No replacement reconciliation mechanism, live agent invocation, or external publication was introduced. Final files remain in the assigned worktree for GitWeave checkpointing; this evidence does not constitute PR approval.

## Merge ignores unpublished local artifacts

On 2026-09-20, `python3 -m unittest discover -s tests -v` ran 129 deterministic tests in 22.848 seconds: 128 passed and the installed entry-point test was skipped. After installing the package in the assigned worktree's `.venv`, `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m unittest discover -s tests -p test_cli.py -v` passed all 13 CLI tests, including the installed entry point. Module and installed CLI `--help` checks and `git diff --check` passed.

All 41 focused action and existing-PR tests passed. New regressions verify that both managed and input PRs merge their known remote head despite a different local artifact tree, without local Git inspection or synchronization. Existing coverage retains head/base checks, GitHub rejection diagnostics, exact-SHA merge requests, synchronization, and already-merged retry behavior. The production change only removes the artifact-tree comparison and its two calls; no replacement workflow policy was added.

The diff was reviewed against the supplied issue and development/review guidelines for completeness and scope. No live agents or external publication were invoked. Final files remain in the assigned worktree for GitWeave checkpointing; this evidence does not constitute PR approval.

## Simplified managed PR publication

On 2026-09-20, `python3 -m unittest discover -s tests -v` ran 127 deterministic tests in 23.407 seconds: 126 passed and the installed entry-point test was skipped. After installing the package in the assigned worktree's `.venv`, `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m unittest discover -s tests -p test_cli.py -v` passed all 13 CLI tests, including the installed entry point. Module and installed CLI `--help` checks and `git diff --check` passed.

All 12 focused action tests passed. Publication coverage verifies a single force push to the managed ref before GitHub lookup, PR creation and editing with the configured metadata, native push and GitHub failures, and an ordinary push on retry after a PR operation fails. Closed or retargeted PR metadata no longer triggers a local publication veto. Existing managed merge checks remain covered, and existing-PR synchronization is unchanged.

The diff was reviewed against the supplied issue and development/review guidelines for completeness and scope. The managed namespace permits a single-ref force update without remote-head reconciliation or retry recognition; the retained publication state serves the existing merge checks. No live agents or external publication were invoked. Final files remain in the assigned worktree for GitWeave checkpointing; this evidence does not constitute PR approval.

## Existing-PR mutations use native permission decisions

On 2026-09-20, `python3 -m unittest discover -s tests -v` ran 126 deterministic tests in 22.280 seconds: 125 passed and the installed entry-point test was skipped. After installing the package in the assigned worktree's `.venv`, `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m unittest discover -s tests -p test_cli.py -v` passed all 13 CLI tests, including the installed entry point. Module and installed CLI `--help` checks and `git diff --check` passed.

All 25 focused existing-PR tests passed. Coverage verifies fork synchronization targets the head repository and exact leased ref, merges target the base repository and exact SHA without a push-permission preflight (including fork and deleted-head inputs), synchronization rejects an unavailable push target, and native Git/GitHub permission diagnostics survive. Existing identity, artifact, lease-race, and retry checks remain covered.

The diff was reviewed against the supplied issue and development/review guidelines for completeness and scope. No live agents or external publication were invoked. Final files remain in the assigned worktree for GitWeave checkpointing; this evidence does not constitute PR approval.

## Ordinary GitHub comment actions

On 2026-09-20, `python3 -m unittest discover -s tests -v` ran 124 deterministic tests in 21.242 seconds: 123 passed and the installed entry-point test was skipped. After installing the package in the assigned worktree's `.venv`, `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m unittest discover -s tests -p test_cli.py -v` passed all 13 CLI tests, including the installed entry point. Module and installed CLI `--help` checks and `git diff --check` passed.

Focused mocked tests verify both comment actions post the exact resolved body with one POST and no history lookup, accept calls without instance identity, and post anew on repeated invocations or retries after lost/malformed responses. Existing configuration, body, target, response, and transport validation remain covered. The deterministic runtime loop test verifies retry diagnostics and attempt provenance while each attempt posts a new comment.

The diff was reviewed against the supplied issue and development/review guidelines for completeness and scope. No live agents or external publication were invoked. Final files remain in the assigned worktree for GitWeave checkpointing; this evidence does not constitute PR approval.

## Provenance push failure diagnostics

On 2026-09-20, `python3 -m unittest discover -s tests -v` ran 121 deterministic tests on Python 3.14.6 in 23.496 seconds: 120 passed and the installed entry-point test was skipped. After installing the package in the assigned worktree's `.venv`, `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m unittest discover -s tests -p test_cli.py -v` passed all 13 CLI tests, including that entry point. Module and installed CLI `--help` checks and `git diff --check` passed.

All nine focused persistence tests passed. Local Git tests verify rejected-push and missing-remote diagnostics, CLI exit 2, retained final Run records and notes, and successful native Git retry. Mocked authentication and multiline transport failures verify intact diagnostic text, exception chaining, unchanged persistence classification/retryability, and retained refs. No replacement redaction policy was introduced.

The diff was reviewed against the supplied issue and development/review guidelines for completeness and scope. No live agents or external publication were invoked. Final files remain in the assigned worktree for GitWeave checkpointing; this evidence does not constitute PR approval.

## Native Agent failure diagnostics

On 2026-09-20, `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m unittest discover -s tests -v` passed all 121 deterministic tests on Python 3.14.6 in 23.326 seconds, including the installed entry point. Module and installed CLI `--help` checks and `git diff --check` passed.

Focused tests cover Codex error and failed-turn messages, Claude failed-result errors and result text, terminal diagnostics over earlier errors, blank diagnostics, stderr/stdout fallbacks, native events without text, and the generic fallback when no useful diagnostic exists. Runtime tests verify native messages reach failed Run records and attempt provenance while raw events/stdout/stderr and configured retries remain intact. Existing success, structured-output, timeout, and native rate-limit tests pass.

The diff was reviewed against the supplied issue and development/review guidelines for completeness and scope. No live agents or external publication were invoked. Final files remain in the assigned worktree for GitWeave checkpointing; this evidence does not constitute PR approval.

## Issue #38: exact leases for PR synchronization

`sync_pr` now performs one exact leased push using the known remote head and records the new head only when Git succeeds. Focused tests cover the exact command, stale API head metadata, successive updates, unchanged lease/error propagation on retries (including a response lost after the API head advanced), and the existing identity/state/permission checks. Real local Git tests inject both a branch move and deletion after metadata inspection and verify Git rejects the push without changing the competing state or the recorded remote SHA. Managed publication and merge behavior remain unchanged.

The [PR57 CI failure](https://github.com/takahirox/gitweave/actions/runs/35494567885) used Git 2.55.0 and Python 3.14.7. Its lease assertions passed; `TemporaryDirectory.cleanup` failed with `ENOTEMPTY` at `remote/.git`. The fixture permits detached receive-side maintenance from the final provenance push, even after synchronization fails. In [Git 2.55.0's maintenance implementation](https://github.com/git/git/blob/v2.55.0/builtin/gc.c), the default geometric strategy estimates loose-object counts from the `17` hash directory. Two objects there exceed its rounded threshold, so even this small fixture can launch a background repack depending on its object hashes.

A worktree-local Git 2.55.0 build and controlled blobs in that directory reproduced the background repack: Trace2 recorded a push returning at `06:45:16.315935` UTC and its repack exiting at `06:45:16.325073` UTC. This establishes a writer that can outlive the push and race directory cleanup. The CI log does not identify the leftover entry; the exact `ENOTEMPTY` was not reproduced locally. The narrow fixture fix sets `maintenance.autoDetach=false` only in its disposable remote repository, keeping maintenance enabled and synchronous. Cleanup errors remain visible and runtime Git policy is untouched.

On 2026-09-20, the deterministic suite ran 117 tests on Python 3.14.6 with Git 2.48.0: 116 passed, with only the uninstalled CLI entry-point test skipped. All 23 existing-PR tests also passed with Git 2.55.0. Twenty additional runs of the updated real lease test with the controlled blobs passed both race cases; Trace2 confirmed all 40 receive-side maintenance invocations used `--no-detach`. Module CLI help and `git diff --check` passed.

The diff was reviewed against the supplied issue and development/review guidelines. No live agents or external publication were invoked. Temporary diagnostic builds and traces were removed; final files remain in the assigned worktree for GitWeave checkpointing. This evidence is not PR approval.

## Agent process sessions follow opt-in timeouts

On 2026-09-20, `python3 -m unittest discover -s tests -v` ran 113 tests in 18.193 seconds: 112 passed and the installed-entry-point test was skipped because the package was not installed. `python3 -m gitweave --help` and `git diff --check` passed.

Mocked Codex and Claude launches cover omitted, integer, and fractional timeouts. A local Python subprocess test verifies inherited session and process-group IDs without a timeout and a new session/group with an explicit timeout. Existing timeout tests retain process-group termination, partial output, failure provenance, and retry bounds.

The only production change makes `start_new_session` conditional on a configured timeout. Current runtime documentation describes this behavior. The diff was reviewed against the supplied issue and development/review guidelines for completeness and scope. No live agents or external publication were invoked; persistence tests use disposable local repositories. Files remain in the assigned worktree for GitWeave checkpointing. This evidence does not constitute PR approval.

## Native option values pass through

On 2026-09-20, after installing the package in a worktree-local virtual environment, `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m unittest discover -s tests -v` passed all 112 tests on Python 3.14.6 in 18.606 seconds, including the installed entry point. Module and installed CLI `--help` checks and `git diff --check` passed.

Graph and mocked adapter tests cover unknown future values, empty strings, case variants, and surrounding whitespace for Codex `sandbox` and Claude `permission_mode`. Explicit strings reach the CLI unchanged. Non-string values and wrong-provider/node settings remain rejected; omission retains the Codex default and omits the Claude permission flag. The only production changes remove the two value allowlists, leaving native CLI validation in charge.

The initial suite and an isolated rerun exposed the existing concurrency test's dependence on a 30 ms sleep: it observed only one active worker. That test now synchronizes workers with a barrier while retaining its assertion that peak concurrency equals two. No runtime concurrency behavior changed.

The diff was reviewed against the supplied issue and development/review guidelines for completeness and scope. No live agents or external publication were invoked. Files remain in the assigned worktree for GitWeave checkpointing. Earlier validation entries below describe historical behavior, including the now-removed native option allowlists; they are not the current option contract.

## Common Agent prompt simplification

On 2026-09-20, `python3 -m unittest discover -s tests -v` ran 112 deterministic tests in 18.744 seconds: 111 passed and the installed-entry-point test was skipped because the package was not installed. `python3 -m gitweave --help` and `git diff --check` passed.

Mocked Codex and Claude invocations verify the complete prompt contains only node/worktree context, the declared instruction, and serialized execution inputs. Coverage includes multiline instructions, Unicode input, and an explicit graph-authored publication restriction preserved unchanged. Existing sandbox, permission-mode, environment, schema, and failure-handling tests pass.

The production change only removes behavioral directives from the shared prompt; no replacement policy or configuration was introduced. Current runtime documentation now assigns task-specific instructions to graph authors. The diff was reviewed against the supplied issue and development/review guidelines. No live agents or external publication were invoked. Files remain in the assigned worktree for GitWeave checkpointing; this validation does not constitute PR approval.

## Issue #29: no default Git or GitHub command timeout

On 2026-09-20, `python3 -m unittest discover -s tests -v` ran 111 deterministic tests on Python 3.14.6 in 18.093 seconds: 110 passed and the installed-entry-point test was skipped because the package was not installed. `python3 -m gitweave --help` and `git diff --check` passed.

Four focused mocked invocation tests verify that the Git and `gh` wrappers omit the subprocess timeout argument and preserve command arguments, output handling, launch-error and nonzero-exit diagnostics, failure kinds, and retryability. Injected `TimeoutExpired` exceptions also retain their existing failure conversion. No slow commands or network operations are needed for these tests. Existing Agent Node tests passed for omitted and explicit graph timeouts, process termination, retained diagnostics/provenance, and retry bounds.

The only production changes remove the two fixed 120-second subprocess timeout arguments. No timeout configuration, provider changes, or retry/step policy changes were added. The diff was reviewed against Issue #29 and the development/review guidelines for completeness and scope. No live agents or external publication were invoked; existing persistence tests use disposable local repositories. Files remain in the assigned worktree for GitWeave checkpointing. Validation is implementation evidence, not task or PR approval.

## Issue #28: optional Claude permission mode

On 2026-09-20, installed Claude Code 2.1.272 reported `acceptEdits`, `auto`, `bypassPermissions`, `manual`, `dontAsk`, and `plan` as the supported `--permission-mode` values in `claude --help`. Only help/version inspection was used; no live agent was invoked.

`python3 -m unittest discover -s tests -v` ran 107 deterministic tests on Python 3.14.6: 106 passed and the installed-entry-point test was skipped before package installation. After installing the package in a worktree-local virtual environment, all 13 CLI tests passed, including the installed entry point. Module and installed CLI `--help` checks and `git diff --check` passed.

Graph tests cover omission without inserting a default, every supported explicit mode, invalid types/values, unsupported providers, and all System Action types. Mocked adapter commands verify omission of the permission flag, unchanged pass-through of every explicit mode alongside model/effort/schema options, and rejection before launch. Existing tests retain Codex sandbox commands, native environment inheritance, and worktree instructions. Native permission settings and prompt controls are untouched; no mode substitution or fallback was added.

The diff was reviewed against Issue #28 and the development/review guidelines for completeness and scope. Files are left for GitWeave checkpointing; no GitHub publication, review, or merge was performed. Existing persistence tests use disposable local repositories. Deterministic validation establishes implementation evidence, not task or PR approval.

## Issue #27: normal Git configuration and environment

On 2026-09-20, `python3 -m unittest discover -s tests -v` ran 103 deterministic tests on Python 3.14.6: 102 passed and the installed-entry-point test was skipped before package installation. After installing the package in a worktree-local virtual environment, all 13 CLI tests passed, including the installed entry point. Module and installed CLI `--help` checks and `git diff --check` also passed.

Three focused Git tests verify inherited fake environment values with only the existing provenance identity overrides, actual Git reads of inherited global and environment-injected configuration, execution of a harmless repository-configured `post-checkout` hook, and an unchanged parent environment. Checkpoint coverage verifies final unstaged and untracked files, agent commit ancestry, GitWeave author/committer identity, retained refs and notes, unchanged HEAD, and byte-for-byte preservation of both the worktree index and an inherited `GIT_INDEX_FILE`.

The Git wrapper no longer forces `core.hooksPath=/dev/null` or strips `GIT_*` variables. No replacement restrictions, GH_HOST changes, or credential-helper changes were added. No live agent tests or external publication were performed; existing deterministic persistence tests use disposable local repositories. These results establish implementation validation, not task or PR approval.

## Issue #23: unchanged Agent Node environment

On 2026-09-20, `python3 -m unittest discover -s tests -v` passed all 92 deterministic tests on Python 3.14.6. Adapter tests launch `/usr/bin/env` with fake parent values and compare the complete inherited environment, covering development settings, native authentication, GitHub credentials, SSH variables, Git repository/configuration overrides, credential helpers, and transport settings. Mocked Codex and Claude launches verify normal inheritance (no `env` argument), unchanged CLI arguments, sandbox modes, and assigned worktree instructions. System Action credential and fixed-host assertions remain intact.

`python3 -m gitweave --help` and `git diff --check` passed. No live agent tests or remote publication were performed. Runtime-owned Git internals are unchanged. The historical filtering coverage below records the earlier policy, which Issue #23 supersedes along with Issue #16.

## Issue #11: optional native sandboxing

On 2026-09-20, installed `codex-cli 0.155.1` reported `read-only`, `workspace-write`, and `danger-full-access` as the supported `--sandbox` values in `codex exec --help`. The help lists the combined approval/sandbox bypass as a separate option; GitWeave does not use it.

`python3 -m unittest discover -s tests -v`: all 80 deterministic tests passed. New coverage checks the explicit nonrestrictive Codex default, all three explicit modes, optional configuration for other providers, rejection of unsupported values/types/providers and action-node settings, rejection before CLI launch, unchanged Claude arguments, assigned worktree arguments and instructions, native authentication inheritance, and publication-credential exclusion for every mode. Existing System Action tests verify that runtime-owned credentials remain available separately. `git diff --check` passed. No live agent smoke or remote publication was performed for this change.

## Earlier validation

Validation for Issue #5 / PR #6 on 2026-09-20.

## Deterministic validation

`python3 -m unittest discover -s tests -v`: 34 tests passed on local Python 3.14.6. CI exercises Python 3.11 and 3.14, including package installation and the installed command entry point.

Coverage includes sequential artifact/result handoff, empty commits, Git-addressable Run records, conditional routing, convergent/bounded loops, dynamic fan-out, empty maps, deterministic multi-input joins, nested parallel concurrency bounds, worktree isolation, retry ancestry, retained failure refs/notes, sibling completion on failure, storage/cleanup diagnostics, schema/graph validation, native adapter normalization, CLI timeout handling, publication credential filtering, usage-limit handling, and mocked PR publication/update/approval/merge/retry behavior.

## Native CLI smoke

The smoke test used existing native subscription authentication and provider defaults. It did not purchase allowance, reset limits, change providers to bypass limits, or publish artifacts remotely.

| Invocation | Versions | Result |
| --- | --- | --- |
| `python3 scripts/smoke.py --provider codex` | codex-cli 0.155.1 | Passed: file artifact, structured result, attempt note and Run record. |
| `python3 scripts/smoke.py --provider claude` | Claude Code 2.1.272 | Passed: file artifact, structured result, attempt note and Run record. |
| `python3 scripts/smoke.py --provider both` | Both versions above | Passed: Codex → Claude in one Run, both artifacts present in the terminal commit. |

Mixed Run ID: `f7d38fb4f0564dababc71459807cca4a`. Its original temporary local repository retains the full Git-native records. No personal authentication details or raw agent logs are copied into this document.

## Review corrections

The PR review found and corrected diagnostic worktree cleanup, malformed input validation, missing native usage-limit variants, publication-before-closed-PR-check ordering, and private-remote credential-helper selection. A further pass corrected missing structured envelopes being accepted as null data, nested boolean/number equality, and invalid JSON pointer array indices. Focused regression tests cover these cases.

The runtime guide records the v0 boundaries: structured graphs, a documented schema subset, trusted native CLI execution, no automatic crash resume, and explicit GitHub actions. Live smoke complements deterministic coverage and does not establish semantic correctness of arbitrary agent tasks.
