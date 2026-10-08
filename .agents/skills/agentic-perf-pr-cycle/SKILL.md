---
name: agentic-perf-pr-cycle
description: Review, update, validate, and land agentic-perf pull requests, including stale branches, provider or tool changes, audit-sensitive behavior, and runtime validation.
---

# Agentic-perf PR cycle

Use this skill to work through an existing pull request from initial review to
final CI checks. For the one-at-a-time backlog workflow, read
[references/backlog-pr-cycle.md](references/backlog-pr-cycle.md). For a new
implementation, use the issue-first development workflow in
[agentic-perf-orchestration](../agentic-perf-orchestration/SKILL.md), then
return here for the PR review and landing steps.

Repository guidance is authoritative for current commands and policy. Read
[AGENTS.md](../../../AGENTS.md),
[CONTRIBUTING.md](../../../CONTRIBUTING.md),
[docs/reviewing.md](../../../docs/reviewing.md), and the relevant audit or
testing docs for the paths under review. Do not copy mutable project details
into this skill when a repository document already owns them.

## Review, fix, and revalidate

1. Verify the clean base checkout, PR base and head SHAs, author, changed
   files, commits, and check state. Record the expected remote head. Use an
   isolated worktree for source review; do not bring unrelated checkout
   changes into it.
2. If the PR is based on stale `main`, bring the exact PR head onto the current
   `origin/main` in a fresh worktree. Resolve conflicts deliberately and
   inspect the complete resulting diff. Stop before pushing if the remote PR
   head has changed.
3. Review the full diff independently from its implementation. Trace changed
   contracts through their producers, consumers, schemas, prompts, and tests.
   Apply [docs/reviewing.md](../../../docs/reviewing.md) and the relevant
   audit policies.
4. Classify each finding as **PR-introduced** or **review-discovered
   follow-up**. Fix material PR-introduced correctness, security, or behavior
   findings before push. Track pre-existing follow-ups separately; create a
   focused issue when that is appropriate for the workflow and link it to the
   PR. Do not describe a pre-existing defect as a regression.
5. After every implementation or generated-file change, format the changed
   files before testing and inspect the complete diff. Repeat the independent
   review and every affected validation step after review-driven fixes.

For changes to MCP actions, providers, dispatch, or audited side effects,
build a focused path matrix. Where applicable, cover success, rejection,
transport failure, ambiguous failure after send, timeout, cancellation,
retry classification, redaction, context identity, and exactly one terminal
event. Use the existing audit policy and inventories rather than weakening
them to make a change pass.

## Validate the final tree

- Follow the current project commands in `AGENTS.md` and
  `CONTRIBUTING.md`. Run `scripts/lint.sh` and, for code-bearing changes,
  focused tests plus the full suite with `scripts/test-parallel.sh` before
  push. Documentation-only changes may skip local tests; the PR's required CI
  checks remain the final gate. Record what ran and its result.
- The pre-commit hook runs scoped validation. Follow `AGENTS.md` about the
  hook for routine commits. `scripts/validate.sh` runs full serial validation;
  current CI uses `scripts/lint.sh` and `scripts/test-parallel.sh`. Use the
  commands documented by the repository for the check you need.
- If formatting or linting changes files, inspect the new diff and rerun
  checks affected by those changes. Treat unexplained test failures and new
  resource, descriptor, or database warnings as unresolved until understood.
- Establish a clean-main comparison when needed to diagnose a failing or
  flaky check. Do not repair a main-side failure as part of the PR.
- Run live or runtime validation only when the change needs it and the user
  or workflow authorizes it. Use the managed-instance procedure in
  [docs/multi-instance.md](../../../docs/multi-instance.md) and
  [docs/live-ticket-gate.md](../../../docs/live-ticket-gate.md). Confirm the
  services remain healthy after startup and use the intended instance for
  authenticated commands. A started process or partial test is not evidence
  of a green result.

If the execution environment separates a restricted sandbox from a host
context, run GitHub, managed-instance lifecycle, SSH, live-ticket, and
credential-dependent operations only in the context that has the intended
identity and configuration. Follow the environment's security boundary; do
not test these operations first in a context known to lack them.

## Push and hand off

- Commit the complete validated change, including tests, inventories, and
  formatting updates. Before pushing, confirm the destination branch and that
  its current remote head is still the reviewed SHA. Use a lease-protected
  update when rewriting history; never use an unconditional force push.
- Monitor every required check to completion and confirm it belongs to the
  final pushed SHA. Missing, stale, cancelled, pending, or absent required
  checks are not green. Inspect the exact failing job and logs, then restart
  the review/fix/revalidate cycle after any fix.
- Merge only after independent review is clear, required local and runtime
  validation is complete, and required checks pass. Follow the user's merge
  authorization and repository policy.
- Report the base and final commits, worktree or instance, findings and
  follow-up issues, validation commands and results, final-SHA CI state, and
  whether the change was pushed or merged.
