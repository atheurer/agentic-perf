# Crucible userenv discovery

The infrastructure MCP server exposes `list_controller_userenvs` as a
read-only compatibility wrapper around the controller-scoped Crucible MCP
adapter.

- The adapter connects to the controller’s MCP contract v3 endpoint.
- It calls the Crucible MCP `list_userenvs` tool.
- It returns the controller’s `official`, `external`, and `deprecated`
  userenv arrays.
- It does not expose raw Crucible MCP tools to the model.
- It does not fall back to SSH or the Crucible CLI.

The response is structured JSON containing the controller, source
(`crucible_mcp`), and the three userenv arrays. Benchmark execution and all
mutating operations remain behind their existing validation, approval, and
durable-operation guardrails.
