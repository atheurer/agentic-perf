# Trace query and export

Authenticated callers can query the durable causal trace projection:

```text
GET /api/v1/traces/query?ticket_id=PERF-123&causal=true
GET /api/v1/traces/export?ticket_id=PERF-123&format=jsonl
```

Supported query selectors include ticket, trace, invocation, action and parent
action IDs, action type, lifecycle state, outcome, producer, and UTC time
bounds. `causal=true` expands a matching action to its known ancestors and
descendants. Service principals and administrators may query any ticket;
multi-user accounts may query only unowned tickets or tickets they own.

The CLI provides the same operations:

```text
agentic-perf trace --ticket-id PERF-123 --causal --json
agentic-perf trace --ticket-id PERF-123 --export --format csv --output trace.csv
```

Exports contain immutable event envelopes. CSV is a compact summary for
spreadsheets; JSON and JSONL preserve the complete envelope.
