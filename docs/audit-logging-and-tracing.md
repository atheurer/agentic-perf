# Audit logging and tracing design

Status: implementation guide and rationale

This document explains how agentic-perf records work, why the system has both
trace events and state-mutation audit records, and how operators reconstruct a
ticket's history. It is intentionally an internal design note: it describes
the guarantees made by the current implementation rather than promising a
general-purpose observability platform.

## Executive summary

Agentic-perf is an asynchronous, multi-agent system. A ticket can cross
processes, agents, MCP servers, remote hosts, and external benchmark systems;
the process that starts an operation is not necessarily the process that
finishes it. The audit design therefore makes the ticket the correlation
boundary and records causal relationships explicitly.

The design has four rules:

1. Every ticket-scoped action gets a trace context before it can mutate state
   or contact infrastructure.
2. Every state mutation has a durable, actor-aware audit record.
3. Every tool, LLM call, and ticket-scoped outbound request has a lifecycle
   record with a terminal outcome, when the transport is available.
4. Safety decisions—claims, fences, retries, approvals, and run-file
   validation—are enforced by code and recorded as data, not inferred from
   log text.

The result is a causal record that answers both “what happened?” and “why was
this action allowed?” without storing secrets or relying on process-local
memory.

## Scope and non-goals

The system records ticket work, control-plane mutations, agent/tool activity,
LLM usage, and the durable operation state needed for replay and diagnosis. It
does not attempt to capture private keys, bearer tokens, full HTTP bodies, or
an immutable copy of every external system's logs. External benchmark artifacts
remain in the harness or workspace storage and are referenced by metadata and
digests.

Trace delivery is best-effort for agent progress, but a failed state-store
mutation or a missing required audit boundary must be visible and fail closed.
The audit record is not encryption, a retention policy, or a substitute for
filesystem permissions and backups.

## Components and responsibilities

```text
                               ticket_id + trace context
   CLI / dashboard / chat ─────────────────────────────────────┐
                                                               │
                         ┌─────────────────────────────────────▼──┐
                         │ State store (FastAPI)                    │
                         │ ticket state, claims, approvals, audit  │
                         │ TraceStore (SQLite) + compatibility log  │
                         └───────────────┬─────────────────────────┘
                                         │ REST / causal headers
                         ┌───────────────▼─────────────────────────┐
                         │ Orchestrator and agents                  │
                         │ EventBus, LLM loop, MCP, fenced actions  │
                         └───────────────┬─────────────────────────┘
                                         │ audited HTTP / subprocess
                         ┌───────────────▼─────────────────────────┐
                         │ Providers and benchmark harnesses         │
                         │ SSH, Crucible, resource providers, APIs  │
                         └───────────────────────────────────────────┘
```

### Trace context

`providers.tracing` owns the canonical `TraceContext` and `TraceEventV1`
contracts. A root context contains the ticket, agent, invocation, and trace
identity. `child_context()` creates a new action identity while preserving the
trace and parent-action relationship. Context is task-local and is propagated
through HTTP headers and MCP environment variables.

The identifiers have deliberately bounded formats: trace IDs are 32 lowercase
hexadecimal characters, action IDs are 16 hexadecimal characters, and
invocation/event identities are UUIDs. This makes correlation stable across
Python tasks, subprocesses, and service restarts.

### State store and audit log

The state store is the authority for ticket status, comments, claims,
validation records, approvals, and operation state. Mutations call
`AuditLog.log()` while holding the store's mutation lock. `AuditLog` applies
the shared redactor and writes the canonical event to `TraceStore` (SQLite).
The legacy JSONL projection remains available for compatibility and transcript
rendering; it is not a second independent source of truth.

The state store also records authentication attempts, denied mutations, trace
queries, trace exports, and operation conflicts. This makes the audit trail
cover both successful actions and rejected attempts.

### EventBus

The EventBus is the agent activity stream. It records agent lifecycle, LLM
requests/responses and usage, tool calls/results/skips, progress, comments,
interjections, transitions, errors, and circuit-breaker decisions. It keeps a
real-time in-memory view and persists ticket events under
`~/.agentic-perf/logs/`; it also projects durable trace events for restart
recovery and cumulative usage accounting.

`status_change` is emitted by the state store when state actually changes.
`transition` is an agent/orchestrator activity breadcrumb. They are both
useful, but they are not interchangeable: the former is authoritative for
state mutation, while the latter explains the agent's intent and context.

### Audited outbound HTTP

Ticket-scoped provider and state-store calls use `providers.execution.http`.
The wrapper records method, sanitized target, safe headers, payload metadata
(size and digest), retry/attempt information, status, duration, and terminal
outcome. It deliberately excludes request/response bodies and credentials.
Retries are only automatic for read-only requests or operations explicitly
declared idempotent; an ambiguous mutating request is surfaced for
reconciliation rather than replayed blindly.

The HTTP inventory in [http-audit-inventory.md](http-audit-inventory.md) lists
the narrow process-control and transport exceptions that run before a ticket
context exists or would recursively audit the trace transport itself.

## Lifecycle of a ticket action

1. **Context creation.** The orchestrator creates a root/control context before
   claiming or dispatching ticket work. A dispatched agent binds its ticket
   context before LLM or tool execution.
2. **Admission and fencing.** The state store validates the leader lease,
   ticket claim, session, epoch, and claim ID on mutating requests. A stale
   worker receives a rejection instead of changing the ticket.
3. **Start record.** The agent/tool/API boundary emits a `STARTED` event with
   action and operation descriptors. The event contains metadata and digests,
   not secret payloads.
4. **Work and children.** LLM calls, MCP tools, subprocesses, SSH operations,
   and outbound HTTP calls create child actions. Progress can be streamed, but
   the parent action remains open until a terminal event is recorded.
5. **Terminal record.** Successful completion, failure, cancellation, timeout,
   or indeterminate delivery is recorded with duration and outcome. If trace
   delivery fails, the failure is logged for operators; an operation that needs
   a durable authorization does not proceed without its required record.
6. **State mutation.** The state store commits status, comments, claims, and
   operation changes under its lock and emits the authoritative mutation audit
   record.

This ordering matters. A state mutation must not be justified only by a later
log line, and a tool result must not appear complete without a correlated
terminal outcome.

## Central trace readiness

The state store owns the durable trace endpoint. The orchestrator exports the
resolved store URL so audited execution clients can initialize their recorder
against the same instance. Startup establishes the store and its trace
recorder before ticket agents are dispatched.

Requests that mutate state require a usable ticket trace context. If a caller
attempts a mutating HTTP operation before that context is ready, the operation
is rejected with a clear readiness error instead of producing an uncorrelated
Python traceback. Process-control actions that necessarily happen before a
ticket exists (leader lease acquisition, polling, and initial claim admission)
are explicit, narrow exceptions documented in the HTTP inventory.

## Approvals and immutable run-files

Benchmark approval is a capability, not a prose convention. Validation stores
an immutable run-file, its fingerprint, and an execution-intent digest. The
approval record binds those values to the ticket, claim, invocation, and
waiter. Approval resolution is compare-and-swap guarded and consumption is
single-use.

The user-facing approval reply is still natural language. The benchmark agent
receives the full reply, interprets it with its LLM, and must call the
structured approval resolver before execution. Exact slash/CLI commands may
resolve the record directly, but they use the same durable API and resume the
waiting agent. A pending approval is not canceled merely because the normal
resume transition delivered a reply; aborts, intent changes, lease takeover,
and other invalidating transitions still retire it.

This separation is deliberate: the LLM handles intent interpretation, while
code enforces the immutable run-file, identity, and single-use invariants.

## Redaction and data handling

The shared redactor runs before audit persistence. Payload descriptors contain
size, media type, and a digest rather than raw credentials or bodies. Secret
values registered for a ticket and known sensitive patterns are recursively
redacted. Redaction failures are fail-closed and visible to operators.

Operators should still treat ticket text, hostnames, IP addresses, commands,
results, artifacts, and transcripts as potentially sensitive. Use filesystem
permissions, encrypted backups, and the configured secret providers. Historical
logs require an explicit scrub operation; redaction cannot retroactively clean
data that was already stored elsewhere.

## Querying and diagnosis

For a ticket, start with the ticket status and `status_trail`, then correlate:

| Question | Evidence |
|---|---|
| What state changes occurred? | State-store `status_change` events and audit entries |
| Which agent or tool acted? | `agent_*`, `tool_*`, and `transition` events |
| Which LLM/model and cost? | `llm_usage`, cumulative usage, and provider metadata |
| Did an external request reach its peer? | Audited HTTP outcome, status, request ID, and retry kind |
| Why was a mutation rejected? | Fencing, authorization, operation-conflict, or audit-denied record |
| Which run-file was authorized? | Validation fingerprint, execution-intent digest, approval ID, and consume record |
| What survived a restart? | TraceStore events, operation history, ticket state, and JSONL compatibility projection |

The trace query and export APIs are themselves audited. Use the CLI transcript
for the human-readable event stream, and use trace queries/exports when exact
causal fields, operation history, or machine processing are required.

## Failure and recovery policy

- **Trace recorder unavailable:** readiness-sensitive mutations fail before
  side effects; non-critical progress failures remain visible in process logs.
- **State-store restart:** TraceStore and ticket persistence restore event
  sequence and cumulative usage; process-local agent tasks are redispatched
  from ticket state and claim fencing prevents stale work.
- **Duplicate request or retry:** operation keys, request hashes, leases, and
  fencing tokens distinguish safe replay from an ambiguous mutating outcome.
- **Stale worker:** claim/session/epoch validation rejects the mutation and
  records the reason; the worker must not retry with a new identity silently.
- **Approval invalidation:** superseded validation, abort, lease takeover, or
  changed execution intent cancels the pending capability while retaining its
  historical record.
- **Redaction or persistence failure:** the failure is logged and surfaced;
  callers must not infer success from a missing audit record.

## Design decisions at a glance

| Decision | Rationale |
|---|---|
| Ticket-scoped causal context | Correlates work across agents, processes, and services |
| SQLite TraceStore plus legacy projection | Serialized durable writes with compatibility for existing consumers |
| Descriptors/digests instead of bodies | Debuggability without putting credentials or large payloads in logs |
| Code-enforced fences and approvals | Prevents stale workers and mutable run-file substitution |
| LLM intent, structured mutation | Preserves natural language while keeping side effects deterministic |
| Explicit pre-ticket exceptions | Makes startup/control-plane boundaries reviewable instead of silently unaudited |

## Related references

- [Trace event contract](tracing-contract.md)
- [Architecture and event system](architecture.md#event-system)
- [Data flow, retention, and redaction](dataflow.md)
- [Outbound HTTP audit inventory](http-audit-inventory.md)
- [Filesystem audit inventory](filesystem-audit-inventory.md)
- [Internal tool audit policy](internal/tool-audit-policy.md)
- [REST API reference](rest-api-reference.md)
