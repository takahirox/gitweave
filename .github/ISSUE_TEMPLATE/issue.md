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

Record required checks possible only after merge separately, or state that none are required. For a merge-triggered deployment, verify publication and the newly published site after merge. Report these checks as pending until performed; do not claim they passed based on pre-merge validation. See the [review guidance](https://github.com/takahirox/gitweave/blob/main/docs/review-guidelines.md#check-pre-merge-acceptance-and-post-merge-verification).

## Context

Add any relevant context, examples, logs, or related issues.
