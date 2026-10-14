# Reviewing Changes

This guide describes useful checks for reviewing agentic-perf changes. It is
especially relevant to changes involving agents, prompts, configuration,
state transitions, handoffs, or shared ticket data.

## Review the complete contract

When a change modifies a value or message contract, trace it through every
producer and consumer. Check the related configuration, schema, prompt,
handoff, API, and test code rather than reviewing only the first changed
function.

Pay particular attention to:

- user-authored and agent-specific directives being preserved as intended;
- supplemental context remaining additive and not duplicated;
- system or handoff chatter not leaking into an agent's initial task context;
- supported context keys, schemas, and agent capabilities having one clear
  source of truth;
- errors distinguishing values that were not found, not checked, or not
  supported.

## Check orchestration behavior

For stateful or multi-agent changes, follow the event sequence from input to
completion. Verify that state transitions, cancellation, approval pauses,
restarts, and handoffs behave correctly at their boundaries. Check both the
normal path and partial or repeated execution.

For changes that select resources such as hosts, NICs, roles, or capabilities,
ensure the implementation discovers the complete inventory before selecting
from it. It should not guess from a partial result.

## Check configuration and validation

Compare startup validation with runtime construction. A validation probe should
use the same effective provider, model, region, API mode, and relevant options
as the runtime path. Review deduplication keys to ensure distinct effective
configurations are not collapsed, while equivalent configurations are not
probed repeatedly without reason.

Confirm that documented non-blocking behavior is maintained for malformed or
unsupported configuration. Exceptions from validation should be reported in a
useful context and should not escape an explicitly log-only validation boundary.

## Test the edge cases

Add or check regression coverage for the behavior being changed. Useful cases
include:

- omitted, defaulted, and per-agent values;
- two agents sharing a configuration and two agents differing by one option;
- malformed, unsupported, or partially discovered inputs;
- duplicate or repeated context;
- cancellation, approval pauses, and restart boundaries;
- validation failures, timeouts, and stale-process behavior.

Tests should verify observable behavior, not only that a helper was called. For
configuration changes, assert both the effective runtime settings and the
startup validation settings.

## Optional Codex provider and harness scope review

For a pull request that changes shared prompts, tool descriptions or schemas,
provider dispatch, orchestration, or harness context, ask Codex to review the
complete diff with this checklist:

> Enumerate the registered resource providers and benchmark harnesses affected
> by this change. Check that shared prompts and tool descriptions remain
> provider-neutral and harness-neutral, and that provider- or harness-specific
> instructions appear only in the matching scoped context. Trace each changed
> contract through its producer, validator, consumer, and teardown path. Point
> to tests that cover valid and invalid inputs for every affected provider or
> harness, including a mismatched-provider regression. Report any global change
> that lacks cross-provider or cross-harness coverage.

Treat this as a review aid. CI contract tests remain the automated enforcement
path and should derive coverage from the provider and harness registries where
possible. Harness prompt tests derive distinctive instruction-line markers
from each registered fragment and assert that each marker appears only in its
matching assembled prompt. This catches literal line leakage; CI cannot decide
whether a paraphrase has moved harness-specific meaning into shared guidance,
so the semantic scope review in this checklist still applies.

## Final review checklist

- [ ] All producers, consumers, schemas, prompts, and tests were considered.
- [ ] Normal, repeated, partial, and failure paths were reviewed.
- [ ] Configuration validation matches runtime behavior.
- [ ] Error and timeout messages identify the affected configuration and agent.
- [ ] Regression tests cover the new behavior and relevant edge cases.
- [ ] Documentation and examples reflect the effective contract.
- [ ] The diff does not include credentials, live host identifiers, or
      unrelated changes.
