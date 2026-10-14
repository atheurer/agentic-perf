# Backlog PR cycle

Use this reference to review and land one existing, non-self-authored backlog
PR at a time. Keep source review, implementation, runtime validation, and
cleanup scoped to the selected PR.

## 1. Keep the workspace safe

- Keep source-review worktrees separate from managed runtime instances.
- Before starting another backlog item, inspect the instances created for this
  workflow. Preserve the default instance and any instance with active work,
  pending guidance, a related open PR or issue, local changes, or uncertain
  status.
- Clean up only an instance that is confirmed inactive, clean, and no longer
  needed. Verify the instance and worktree paths first; remove only its
  runtime state and worktree. Handle its Git branch separately. If the
  cleanup helper reports an unregistered worktree, stop and verify the paths
  rather than forcing removal.

## 2. Choose one PR

- Refresh the open PR backlog and inspect author, size, changed paths, base
  age, current head, and check state.
- Start with a PR whose behavior and validation can be understood in
  isolation. Changed-line count helps with triage, but generated files,
  security-sensitive paths, migrations, and runtime behavior can make a small
  diff higher risk.
- Complete review, fixes, validation, CI, and the authorized landing step for
  one PR before selecting another.

## 3. Establish the base and isolate review

- Confirm the primary checkout is clean, synchronize `origin/main`, and record
  its immutable SHA. Inspect the PR base/head SHAs, author, files, full diff,
  commits, and current checks.
- Record the expected remote PR head and stop before push if it changes during
  the review.
- Establish a clean-main baseline for relevant validation when needed to
  identify pre-existing failures. A baseline failure must be understood before
  attributing the same failure to the PR; do not silently fix main-side
  failures in the PR.
- Fetch the exact PR head into a dedicated review worktree. Keep it separate
  from managed development-instance worktrees and unrelated checkout changes.
- For an old PR, rebase or replay its exact head onto the recorded current-main
  base, resolve conflicts deliberately, and review the complete result rather
  than only the original patch.
- Do not create a managed runtime instance solely for source review or unit
  tests. Use one only when runtime validation is relevant and authorized.

## 4. Review independently and fix

- Ask for an independent review of the complete diff, with actionable findings
  and severity. The reviewer should not implement its own findings.
- For implementation fixes, keep changes within the PR's intent. After each
  fix, review the complete updated diff and repeat affected checks.
- For filesystem changes, check the audited wrapper rules and the exact
  side-effect inventory. Read-only access and mutations have different policy
  requirements; do not weaken the inventory to hide an unapproved capability.
- Track pre-existing issues separately from PR-introduced findings. File a
  focused follow-up issue when appropriate, and link it to the PR.

## 5. Validate the final tree

After each change, format the affected files before running tests, inspect the
complete diff and `git diff --check`, then use the validation commands in the
repository's current `AGENTS.md` and `CONTRIBUTING.md`. For code-bearing PRs,
run focused tests and the full suite with `scripts/test-parallel.sh` before
push. Documentation-only PRs may skip local tests, but their required CI
checks remain a gate. Re-run affected validation after the final edit;
previous green results do not validate a changed tree.

If a test fails, identify the exact failure. Compare against clean current main
when needed to distinguish a PR regression from a baseline or environment
problem. Do not dismiss unexplained failures or push around them.

## 6. Push, monitor, and land

- Commit validated source, tests, inventories, and mechanical formatting
  together. Confirm the destination branch and remote head before pushing.
- After push, record the new commit SHA and verify every required check belongs
  to that SHA and reaches a successful conclusion. Missing, stale, pending,
  cancelled, or failed required checks are not green.
- On CI failure, inspect the exact job and logs, fix the cause, validate again,
  request another independent review, then push and monitor the new SHA.
- Land only after review, required local and runtime validation, and CI are
  clear, and the user or repository policy authorizes the merge.

## Handoff

Report the PR and author, selection rationale, base and final SHAs, review
findings and follow-ups, files changed, focused and full validation results,
check state for the final SHA, and merge result or remaining blocker.
