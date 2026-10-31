# Development Flow

This document defines the default development flow for GitWeave, with particular emphasis on AI-assisted development.

## 1. Start with an Issue

Work should begin with an Issue.

The Issue should clearly state:

- the problem
- the expected outcome
- relevant context

The Issue defines the scope of the work. If the scope is unclear, clarify the Issue before implementation instead of inventing requirements during the change.

By default, completion criteria should be executable and verifiable by an AI agent. Require human checks, such as physical-device testing, subjective evaluation, or external approval, only when there is a necessary reason to do so.

When human work is required, state why it is necessary and what result is expected. Distinguish optional additional validation from mandatory completion criteria.

Choose validation using the [test and CI principles](#test-and-ci-principles).

Mandatory pre-merge acceptance criteria must be achievable and verifiable before merge. Checks possible only after merge must not be prerequisites for pre-merge PR approval. Record required post-merge verification separately from pre-merge acceptance and optional validation. This distinction preserves all implementation requirements and applicable pre-merge tests.

For example, when merging triggers a site deployment, validate code/configuration, local builds, and applicable automated tests before merge. Verify publication and the newly published site after merge, and report that verification as pending until performed.

## 2. Create a Pull Request for the Issue

Implementation should be proposed through a Pull Request associated with the Issue.

The Pull Request should explain:

- what changed
- what outcome the change produces
- how the change was validated
- which required post-merge checks remain pending
- which Issue it addresses

A Pull Request should only claim to close an Issue when it fully addresses that Issue.

If the Pull Request intentionally implements only part of the Issue, it should state that clearly and should not present the Issue as fully resolved.

Report completed pre-merge validation and pending post-merge verification separately. Pending post-merge checks do not make an otherwise complete implementation partial, but do not claim those checks passed or that verification is complete until they have been performed.

## 3. Review Before Merge

Every Pull Request should be reviewed before merge.

A central review question is:

> Does this Pull Request address the Issue completely, without adding changes that are not justified by the Issue?

Review must check both directions:

- **No missing scope:** the Pull Request should not leave required parts of the Issue unresolved while claiming completion.
- **No unnecessary scope:** the Pull Request should not introduce unrelated abstractions, frameworks, policies, or complexity beyond what is needed to solve the Issue.

Apply the [review guidelines](review-guidelines.md#check-pre-merge-acceptance-and-post-merge-verification): require mandatory pre-merge acceptance to pass and required post-merge verification to be recorded as pending, without making those pending checks prerequisites for approval.

This is especially important for AI-generated changes. AI agents may produce broader or more elaborate designs than the task requires. Prefer the smallest change that fully satisfies the Issue.

## 4. Revise Until Review Passes

If review finds missing requirements, unnecessary scope, correctness problems, or insufficient validation, update the Pull Request and review it again.

The Pull Request should be merged only when the reviewed change is an appropriate and complete response to the Issue.

## 5. Merge

After review passes, merge the Pull Request.

## 6. Perform Required Post-Merge Verification

After merge, perform the recorded post-merge checks, such as confirming a merge-triggered deployment and verifying the newly published site. Keep them reported as pending until performed, then record the actual results, including failures and any required follow-up.

The normal flow is therefore:

```text
Issue
  ↓
Implementation
  ↓
Pull Request
  ↓
Review
  ↓
Revision if needed
  ↓
Merge
  ↓
Post-merge verification (if required)
```

## Test and CI Principles

**Detect the required failures at the lightest appropriate layer, without redundant execution.** Preserve required correctness, regressions, and meaningful integration coverage when moving or consolidating checks.

GitWeave's [Tests workflow](../.github/workflows/tests.yml) runs on both push and pull request events. On Python 3.11 and 3.14 it installs the package, runs `python -m unittest discover -s tests -v`, and checks `gitweave --help`. The suite includes unit tests, real local Git/subprocess integration tests, and scripted example workflows; CI does not invoke live models. No deployment/publication workflow is configured.

1. **Prefer the lightest meaningful layer.** Test graph/schema branches and adapter normalization with fast unit tests and fixtures. Use local Git, subprocess, CLI, and scripted workflow tests for boundaries that need them; reserve live Codex/Claude smoke runs for native integration questions. Add assertions that catch a concrete defect rather than mirror implementation. Apply the same rule to browser/renderer tests if a graphical interface is added.

2. **Choose combinations deliberately.** Cover distinct provider, graph/control-flow, input-source, and Git-state paths with representative cases. Multiply combinations only when their interactions require it; do not automatically cross every provider/model, example, input, and Python version. Retain required regressions and integration assertions when redistributing coverage, including the supported-Python CI matrix.

3. **Match validation to the change.** For documentation-only edits, check Markdown structure, links/anchors, consistency, and `git diff --check`; application tests and live runs are unnecessary solely for prose. For CLI/UI changes, check messages, arguments, and relevant examples. For core/runtime changes, use focused tests for fast feedback and the full deterministic suite for regression confidence. For CI/build changes, validate workflow/package changes and installed CLI behavior on affected supported Python versions. Keep applicable mandatory checks before merge; focused feedback does not replace required full regression validation. Treat live smoke or extra experiments as optional unless the Issue requires them. Record genuinely post-merge-only publication checks separately as pending, following the flow above; current CI provides pre-merge regression feedback, not deployment verification.

4. **Reuse work and remove duplicate execution.** In future CI changes, reuse package builds, fixtures, and downloads where valid, with cache keys reflecting inputs and Python/tool versions and isolation preserving fresh-repository tests. The current push/PR triggers can overlap: avoid equivalent checks twice for the same revision when revising them. Cancel superseded validation when safe, preserving any necessary publication ordering. Parallelize independent work only with measured elapsed-time and resource benefits; more workers can add contention and setup cost. These are design principles, not changes to the current workflow.

5. **Review value and cost together.** Ask what additional defect a test detects, whether existing coverage already detects it, and what execution and maintenance cost it adds. Watch repeated package installation, fresh Git repositories/worktrees and subprocess startup, sleep-based session/timeout loops, large fixtures, live model calls, and Cartesian-product growth. Keep assertions and isolation needed to detect real failures.

6. **Maintain an execution-time budget from evidence.** [Issue #114 validation](validation.md#issue-114-explicit-base-branches-for-issue-runs) records 187 deterministic tests taking 60.591 seconds locally on 2026-10-02; this is historical suite evidence, not a normal-CI baseline or limit. Normal-CI elapsed feedback time, total runner/computation time, and queue time remain unmeasured here, with no target established. During actual test/CI performance work, measure these separately on the relevant environment, set a repository-appropriate baseline/target, monitor growth, and investigate overruns through test structure, setup, and duplication while retaining required coverage. This documentation adds no benchmark, hardware purchase, or merge gate.
