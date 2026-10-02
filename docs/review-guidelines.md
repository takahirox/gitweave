# Review Guidelines

The purpose of review is not only to check whether a change works. It is also to verify that the change is the right response to the Issue that motivated it.

These guidelines are particularly important when reviewing AI-generated changes.

## Review Against the Issue

Start by reading the source Issue.

Treat the Issue as the reference for the intended problem and expected outcome.

Ask:

> Is the Pull Request a complete and appropriately scoped solution to this Issue?

## Check for Missing Work

Verify that the Pull Request addresses all parts of the Issue that it claims to resolve.

Do not approve a Pull Request as closing an Issue when important requirements remain unimplemented.

If the change is intentionally partial, the Pull Request should say so and the Issue should remain open.

## Check for Unnecessary Work

Verify that the Pull Request does not go beyond what the Issue requires without a clear reason.

Watch for:

- unnecessary abstractions
- speculative extensibility
- unrelated refactoring
- new frameworks or subsystems that are not required
- additional policies or configuration with no demonstrated need

AI agents can over-engineer solutions. Do not treat additional complexity as automatically beneficial.

Prefer the smallest design that completely solves the stated problem.

## Check the Result

Also verify the ordinary quality of the change:

- behavior matches the expected outcome
- implementation is coherent with the existing architecture
- validation is sufficient for the change
- documentation is updated when the change affects documented behavior

## Check Pre-Merge Acceptance and Post-Merge Verification

Following the [development flow](development-flow.md#1-start-with-an-issue), require mandatory pre-merge acceptance criteria to be achievable and verifiable before merge. Checks possible only after merge must not be prerequisites for pre-merge PR approval.

For example, for a merge-triggered site deployment, review code/configuration, local builds, and applicable automated test results before merge. Verification of publication and the newly published site belongs after merge.

Ensure required post-merge verification is recorded separately and reported as pending until performed. Check that validation claims describe the checks actually performed and their results; pre-merge evidence does not prove post-merge publication succeeded. This distinction does not waive implementation requirements or applicable pre-merge tests, and required post-merge checks remain required.

## Review Outcome

A Pull Request is ready to merge when:

- it fully implements the requirements of the Issue it claims to resolve
- it does not introduce unjustified scope or complexity
- the implementation is correct and all mandatory pre-merge acceptance criteria pass
- required post-merge verification is recorded separately and reported as pending until performed

If any of these conditions are not met, request changes and review again after revision.

Pending checks possible only after merge do not prevent approval when these conditions are met. Do not report post-merge verification as complete until it has been performed and its results recorded.
