# Task Project automation

[Issue #100](https://github.com/takahirox/gitweave/issues/100) adds
[Sync task Project](../.github/workflows/sync-task-project.yml) for the shared
[user Project #4](https://github.com/users/takahirox/projects/4).
The workflow must be on the default branch to receive Issue events. It handles
opened, reopened, closed, labeled, and unlabeled Issues, and ignores pull requests.
It does not backfill existing Issues.

An open Issue with `task` and without `draft` is added once and receives
`AI execution = Ready`. A new or unset Status is initialized to `Todo`;
assigned Status and other fields, including Priority, are preserved.
Removing `task`, adding `draft`, or closing the Issue sets an existing item's
permission to `Not ready`, retaining membership and Status. Ineligible Issues
without an item are not added. Re-adding `task` or removing `draft` restores Ready
when the full condition matches; reopening preserves even `Done`.

Runs are serialized per Issue without cancelling an active run. Because Actions
can coalesce pending runs and deliver events out of order, each run reads current
state and all label pages, not the event's eligibility snapshot. Fields and item
membership are also paginated. Eligibility is rechecked after writes to reconcile
changes during a run. GitHub has no atomic Issue-state/Project-field transaction:
a brief intermediate value is possible, and subsequent events reconcile changes
after the final read. Concurrent manual Status edits can also race initialization;
this workflow only writes Todo when its latest Status read is unset.

## Owner setup

Automation is **inactive until `ADD_TO_PROJECT_PAT` is configured**; a missing
secret produces an actionable failed-run message. Credential provisioning is a
separate owner action. Do not reuse unrelated credentials or put tokens in files,
Issues, PRs, or logs.

1. As the Project owner, open GitHub **Settings → Developer settings → Personal
   access tokens → Tokens (classic) → Generate new token (classic)**. Choose an
   expiration and the `project` scope (user-Project read/write). For these public
   repositories, no private-repository `repo` scope is needed. Arrange rotation
   before expiration. The ordinary repository `GITHUB_TOKEN` cannot access Projects.
2. In **both** `takahirox/gitweave` and `takahirox/projectweave`, open **Settings →
   Secrets and variables → Actions → New repository secret** and save it as
   `ADD_TO_PROJECT_PAT`. Each repository needs its own workflow; this change only
   installs GitWeave's. No credential is created or retrieved by this workflow.
3. Ensure Project #4 has single-select `Status` with `Todo`, `In Progress`, `Done`,
   and `AI execution` with `Ready`, `Not ready` (exact spelling).
4. In the Project's **Workflows**, enable the standard **Item closed** workflow
   with Status `Done` and save it. Verify an Issue closure actually sets Done.
   This Actions workflow only sets Not ready on closure. Keep the Issue's
   `Completed` versus `Not planned` reason when interpreting outcomes: Done
   alone does not mean work was implemented. Review any other built-in workflow
   that changes Status on addition/reopening so it does not reset assigned work.

Only Project state is updated. ProjectWeave's continuously running `coordinate`
process admits open, Todo, Ready Tasks under its resource policy. Actions does
not launch AI or introduce a `Pending` Status. Not ready blocks future admission;
it does not cancel running work. Running-work cancellation, eligibility checks
before merge, and passing permission to GitWeave remain out of scope. GitWeave's
runtime does not read Project state.

## Live verification after provisioning

Use disposable Issues and inspect the Actions run and Project after each step.
Coordinate with the Project owner before making an Issue Ready: a running
ProjectWeave coordinator may immediately admit it.

| Current Issue / change | Expected result |
| --- | --- |
| Open + task | One item, Ready, Todo if Status unset |
| Open + task + draft | No addition; registered item becomes Not ready |
| Open without task | No addition; registered item becomes Not ready |
| Closed + task | No addition; registered item becomes Not ready |
| Remove draft from open + task | One item, Ready; initialize only unset Status |
| Remove then re-add task | Not ready then Ready; same item |
| Add then remove draft | Not ready then Ready; same item |
| Repeat matching label events | Still exactly one item; same Status and Priority |
| Close a registered Issue | Same item, Not ready; built-in workflow sets Done |
| Reopen with task and no draft | Ready; preserve existing Done |

Repeat label transitions with registered `Todo`, `In Progress`, and `Done` items,
and with a chosen Priority; verify those values are preserved. Clear Status on
an eligible item and trigger a label event to verify Todo initialization. Close
one Issue as Completed and another as Not planned; verify Done and their distinct
closure reasons. Rapidly toggle task/draft, then rerun an older Actions run: the
final permission must match current eligibility, with one Project item. Confirm
unregistered closed/draft/non-task Issues remain outside the Project.

For the eventual PR, record syntax checks, mocked reconciliation results, and
live results separately. Live mutation and built-in closure checks require owner
secret provisioning and Project setup; do not report them as passed from local
checks alone.

References: [GitHub Actions authentication and Project automation](https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/automating-projects-using-actions),
[Projects GraphQL API](https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/using-the-api-to-manage-projects).

## Implementation validation (2026-09-30)

- `actionlint` 1.7.7, YAML parsing, embedded Python compilation, and
  `git diff --check` passed.
- A temporary mocked GraphQL harness executed the embedded workflow script:
  48 scenarios covered the state/label/membership/Status matrix, repeated runs,
  task/draft restoration, reopening, permission changes during writes, forced
  second pages for fields/items/labels, and preserved Priority. The missing-secret
  shell path also failed with the expected setup message. No live writes were made.
- `python3 -m unittest discover -s tests`: 125 tests passed.
- A read-only repository secret-name check found no `ADD_TO_PROJECT_PAT` in
  `takahirox/gitweave`. Live Actions, Project membership/field updates, and the
  built-in closed-to-Done behavior remain unverified pending owner setup above.
  Carry these results and limitations into the eventual Issue #100 PR and review.
