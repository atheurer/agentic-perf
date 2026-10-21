---
name: agentic-perf-orchestration
description: Develop, debug, and review agentic-perf agent context, prompts, handoffs, dispatch, ticket state, and iterative benchmark workflows.
---

# Agentic-perf orchestration development

Use this skill when changing prompt assembly, agent context, handoffs,
dispatch, ticket state, cancellation, or iterative benchmark behavior. Follow
the repository's [AGENTS.md](../../../AGENTS.md) for general development
standards and [docs/reviewing.md](../../../docs/reviewing.md) for project
review checks. Use the [PR-cycle skill](../agentic-perf-pr-cycle/SKILL.md)
for review and landing of the resulting pull request.

## Context and handoffs

- Preserve user-authored and agent-specific directives verbatim when the
  contract requires it.
- Keep supplemental context additive. Avoid repeating directives or parsed
  specifications that are already present.
- Filter system and agent handoff chatter from an agent's initial task context
  unless it contains a user-relevant decision.
- Keep supported context keys, schemas, and agent capabilities in one source
  of truth where practical; derive secondary lists from it.
- When changing a message contract, trace every producer, consumer, prompt,
  schema, and test.

## Discovery and state transitions

- Enumerate the complete resource set before selecting a host, NIC, role, or
  capability. If a required resource cannot be verified, report the gap or ask
  for clarification instead of guessing from a partial inventory.
- Distinguish “not found,” “not checked,” and “not supported” in status and
  error messages.
- Verify the running service version when stale processes could explain the
  observed behavior.
- Treat cancellation, approval pauses, retries, and restart boundaries as
  state-machine behavior. Test the normal, partial, repeated, and failure
  paths.

## Develop in an isolated instance

1. Search for an existing GitHub issue covering the work. If none exists,
   create a focused issue before implementation and include clear acceptance
   criteria.
2. From the selected repository root, prepare a managed instance based on the
   intended base (normally `origin/main`):

   ```bash
   ./scripts/dev-instance.sh prepare --issue NUMBER --base origin/main
   ```

   Keep unrelated primary-checkout changes out of the instance. Use the
   generated issue-specific worktree and branch. `prepare` refreshes the
   configured capability source (private skills, skill cache, and secrets)
   through the script's controlled sync. Verify the configured source exists
   and contains the expected capabilities before running it; the sync removes
   destination entries that are absent from its source. Follow
   [docs/multi-instance.md](../../../docs/multi-instance.md); do not manually
   copy a live runtime home, credentials, claims, leases, or process state.
3. Implement the change, add regression coverage for observable behavior, and
   update relevant user or developer documentation. Inspect the complete diff
   and run the applicable repository validation commands.
4. For authenticated CLI operations, enter the intended instance shell so its
   `AGENTIC_PERF_HOME`, `STATE_STORE_URL`, and token are selected together.
   Keep related commands in that same shell session.
5. Commit through the managed-instance workflow, push the issue branch, and
   open a PR that links the issue. Report the issue, worktree, validation,
   commit, and PR together.

Run GitHub, managed-instance lifecycle, SSH, live-ticket, and credential-
dependent commands only in an execution context with the intended identity,
configuration, and access. Never expose secret values in prompts, logs,
issues, or review artifacts. Run the live-ticket gate only when the change
requires it and the user or workflow authorizes it.

## Iterative experiments and review

- Establish a baseline/control run before attributing a measured improvement
  to a directive or code change. Preserve enough metadata to compare
  configuration, received context, actions, and results.
- For hypothesis sweeps, state the variables and stopping criteria instead of
  relying on unbounded manual reruns.
- Prefer fixes in shared message-building or state-transition code when the
  same defect affects several agents.
- Review orchestration changes using the complete producer-consumer path and
  the cases in [docs/reviewing.md](../../../docs/reviewing.md). Add regression
  tests for context omission or duplication, stale-process behavior, partial
  resource discovery, and state-transition boundaries as relevant.
- Classify review findings as PR-introduced or pre-existing follow-ups. Keep
  credentials, live host identifiers, and unverified assumptions out of this
  skill and public review artifacts.
