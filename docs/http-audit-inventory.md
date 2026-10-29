# Outbound HTTP inventory

Ticket-scoped provider/state-store clients use `providers.execution.http`.
`tests/test_httpx_inventory.py` parses the production AST and fails if a new
direct `httpx` constructor or module helper is introduced without an entry
below.  These exclusions are deliberately per-file, not directory-wide:

| File | Exclusion rationale |
| --- | --- |
| `agents/chat/agent.py` | User-session API client; its identity is user/session rather than a ticket invocation. |
| `agents/mcp_client.py` | Third-party MCP transport factory, where httpx construction is a required library hook. |
| `orchestrator/dispatcher.py:133` | Initial claim happens before a ticket trace context exists; renew/release are audited. |
| `orchestrator/leader_lease.py` | Process-control infrastructure lease traffic runs before and outside ticket trace lifecycles. |
| `orchestrator/poller.py` | Process-control polling before a ticket claim creates causal context. |
| `providers/skills/arcaflow_plugins.py` | Provider discovery/bootstrap compatibility path without a ticket argument. |
| `providers/skills/crucible.py` | No-ticket catalog compatibility uses bounded direct HTTP reads. Ticket-bound catalog and child-repository reads use the source epoch's exact Git commit captures instead. |
| `providers/tracing/client.py` | The trace delivery transport itself; wrapping it would recursively audit its own persistence call. |
| `providers/execution/http.py` | The audited boundary implementation. |
| `providers/secrets/git_reference.py` | Anonymous, host-pinned GitLab HTTPS fetch for explicitly configured secret-file pointers; no credentials, redirects, proxy environment, or ticket-bound HTTP client are involved. |

Every listed exclusion must remain narrow and be removed when that caller gets
a ticket-bound lifecycle.  Production files not listed above must not create a
direct `httpx` client.
