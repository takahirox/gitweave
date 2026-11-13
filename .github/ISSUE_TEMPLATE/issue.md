---
name: Issue
about: Report a problem or propose a change
title: ""
labels: ""
assignees: ""
---

## Problem

Describe the problem.

## Expected outcome

Describe what should be true when the issue is resolved.

Default to completion criteria an AI agent can execute and verify. Require human checks only when necessary; explain why and the expected result, and distinguish optional validation from mandatory criteria. See the [development guidance](https://github.com/takahirox/gitweave/blob/main/docs/development-flow.md#1-start-with-an-issue).

## Pre-merge acceptance

List mandatory acceptance criteria that can be achieved and verified before merge. Checks possible only after merge must not be prerequisites for pre-merge PR approval. Keep all implementation requirements and applicable pre-merge tests.

For a merge-triggered site deployment, validate code/configuration, local builds, and applicable automated tests before merge.

## Post-merge verification

Record checks possible only after merge when the actual change requires them, or state that none are required. Documentation-only edits need no routine manual, device, model-download, or deployment gates. Report pending or environment-blocked checks honestly; closure does not claim they passed. Follow the [merge and post-merge lifecycle](https://github.com/takahirox/gitweave/blob/main/docs/development-flow.md#5-merge): merge and close the fully implemented Issue after mandatory pre-merge acceptance, then record verification evidence and track actual defects in an existing equivalent or new linked Issue.

## Context

Add any relevant context, examples, logs, or related issues.
