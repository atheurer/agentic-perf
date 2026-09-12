# Outbound HTTP inventory

Ticket-scoped provider/state-store clients use `providers.execution.http`.
`tests/test_httpx_inventory.py` parses the production AST and fails if a new
direct `httpx` constructor or module helper is introduced without an entry
below.  These exclusions are deliberately per-file, not directory-wide:

| File | Exclusion rationale |
| --- | --- |
| `agents/analyze/server.py`, `agents/benchmark/server.py`, `agents/infra/server.py`, `agents/resource/server.py`, `agents/server_utils.py` | MCP subprocess/bootstrap helpers; they have no ticket causal context at construction and are being retained only for backwards-compatible server startup. |
| `agents/chat/agent.py`, `agents/chat/tools.py` | User-session API client; its identity is user/session rather than a ticket invocation. |
| `agents/fleet/agent.py`, `agents/image_builder/agent.py`, `agents/introspection/agent.py`, `agents/jumpstarter_mcp.py`, `agents/stub.py` | Legacy deterministic agents that inject trace headers themselves; conversion is deferred until their lifecycle binding is unified. |
| `agents/mcp_client.py` | Third-party MCP transport factory, where httpx construction is a required library hook. |
| `orchestrator/dispatcher.py`, `orchestrator/main.py`, `orchestrator/poller.py` | Process-control polling/claim setup, including calls made before a ticket claim creates causal context. |
| `providers/image_build/caib.py`, `providers/resource/jumpstarter_images.py`, `providers/resource/jumpstarter_lifecycle.py`, `providers/skills/arcaflow_plugins.py`, `providers/skills/crucible.py` | Provider discovery/bootstrap compatibility paths without a ticket argument. |
| `providers/tracing/client.py` | The trace delivery transport itself; wrapping it would recursively audit its own persistence call. |
| `providers/execution/http.py` | The audited boundary implementation. |

Every listed exclusion must remain narrow and be removed when that caller gets
a ticket-bound lifecycle.  Production files not listed above must not create a
direct `httpx` client.
