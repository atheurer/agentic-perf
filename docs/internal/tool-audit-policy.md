# Tool audit policy

`agents/tool_audit_policy.py` is the reviewed inventory of every MCP tool,
native agent handler, and chat tool exposed to an LLM. `tests/test_tool_audit_policy.py` AST
discovers the registrations and fails if the inventory is incomplete, contains
stale entries, or a production FastMCP/native registration bypasses the shared
audit boundary.

Every entry declares `read_only` or `side_effecting`. Side-effecting entries
name the audited operation/idempotency owner; a tool may not rely on an
implicit server-wide classification. The checker also rejects a read-only tool
whose handler directly calls a protected mutating API.

Concrete production handlers are intentionally not run by the unit suite: many
require a real ticket, provider credentials, or remote hosts. Each policy entry
therefore carries a reviewed fixture exemption with owner and expiry. The test
suite resolves every discovered MCP name from its actual FastMCP server,
generates a schema-valid safe fixture from that registration, and exercises the
canonical middleware contract. It also runs every `CHAT_TOOLS` dispatcher
branch using a schema-valid fixture. Chat writes a correlated
`started`/terminal trace pair using only the embedded state-store deployment
credential; trace-entry delivery is fail-closed, so a handler is not invoked
when ingestion cannot acknowledge the entry. Side-effecting chat events carry
their concrete handler owner, stable idempotency key/hash, and redacted API
target. The AST checks prove every production registration uses one of those
already-tested boundaries. Exemptions are review debt, not a permanent
allowlist: an expired or unexplained exemption fails CI.

`AUDIT_BYPASS_ALLOWLIST` is empty by design. If an SDK compatibility bridge
temporarily cannot use a canonical boundary, its exception must identify the
exact file and symbol plus its owner, scope, reason, and expiry. Broad file or
directory exemptions are rejected.

The filesystem, HTTP, SSH, and subprocess inventories remain complementary.
They inventory execution primitives; this policy ensures a new agent action
cannot become visible to an LLM without the correlated tool audit contract.
