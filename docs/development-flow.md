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
