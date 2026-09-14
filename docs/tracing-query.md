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

Pagination uses an opaque continuation cursor. Its immutable ordering key is
`(global_seq, event_id)` for persisted events; legacy events without a global
sequence use `(occurred_at UTC, event_id)` in a separate final ordering tier.
The same cursor semantics apply to query and export, so inserts cannot cause a
continuation page to duplicate or skip an already returned event. `next_cursor`
is null when the page is complete.

Every export contains a manifest. `event_content_digest` is SHA-256 over the
UTF-8 canonical event body produced for the selected format, explicitly
excluding the manifest wrapper. Consumers verify it by removing the manifest
(`manifest` JSON member, final `_manifest` JSONL record, or CSV manifest
comment), reproducing that canonical body, and hashing its UTF-8 bytes. Blob
digests are included only for canonical `sha256:<64 lowercase hex>` references
whose content-addressed blob exists and passes integrity verification.
