# Outbound HTTP inventory

Ticket-scoped provider/state-store clients use `providers.execution.http`.
`tests/test_httpx_inventory.py` parses the production AST and fails if a new
direct `httpx` constructor or module helper is introduced without an entry
below.  These exclusions are deliberately per-file, not directory-wide:

| File | Exclusion rationale |
| --- | --- |
| `agents/chat/agent.py` | User-session API client; its identity is user/session rather than a ticket invocation. |
| `agents/mcp_client.py` | Third-party MCP transport factory, where httpx construction is a required library hook. |
| `orchestrator/dispatcher.py:135` | Initial claim happens before a ticket trace context exists; renew/release are audited. |
| `orchestrator/poller.py` | Process-control polling before a ticket claim creates causal context. |
| `providers/skills/arcaflow_plugins.py`, `providers/skills/crucible.py` | Provider discovery/bootstrap compatibility paths without a ticket argument. |
| `providers/tracing/client.py` | The trace delivery transport itself; wrapping it would recursively audit its own persistence call. |
| `providers/execution/http.py` | The audited boundary implementation. |

Every listed exclusion must remain narrow and be removed when that caller gets
a ticket-bound lifecycle.  Production files not listed above must not create a
direct `httpx` client.
