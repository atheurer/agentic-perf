# Trace event contract v1

`providers.tracing` is the canonical, dependency-free contract for trace
producers and consumers. `TraceEventV1` is immutable and closed to unknown
fields. Callers serialize with `model_dump(mode="json")` or `model_dump_json()`
without `exclude_none`, so every contract field is retained with `null` when it
does not apply.

`TraceContext` is immutable and task-local. Create a root with
`new_trace_context()`, bind it with `bind_trace_context()`, and make child work
with `child_context()`. A child keeps trace and invocation ancestry, receives a
new 16-hex action ID, and records its parent's action ID.

Trace IDs are 32 lowercase hexadecimal characters and action IDs are 16, making
them compatible with W3C Trace Context width. Invocation and event identities
are UUIDs. Use `MonotonicTimer` for duration calculation; event timestamps are
UTC wall-clock values only. This package does not persist, export, or emit
events; those concerns belong to later trace issues.
