# v0 validation evidence

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
