# v0 validation evidence

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
