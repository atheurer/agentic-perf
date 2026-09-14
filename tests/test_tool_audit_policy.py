"""CI guardrail for auditable MCP and native agent tool registrations."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.tools.base import ToolResult
from mcp.types import CallToolRequestParams, RequestParams

from agents.mcp_audit import MCPAuditMiddleware
from agents.tool_audit_policy import (
    AUDIT_BYPASS_ALLOWLIST,
    OPERATION_OWNER_CONTRACTS,
    POLICY_BY_REGISTRATION,
    TOOL_AUDIT_POLICY,
)
from providers.tracing import LifecycleState

ROOT = Path(__file__).parents[1]
SERVER_ROOT = ROOT / "agents"

# Calls in a handler that always mutate local state.  This deliberately avoids
# vague names such as ``run``: a fixed remote read command is still read-only.
_PROTECTED_MUTATORS = {
    "open",
    "os.open",
    "os.mkdir",
    "os.makedirs",
    "os.remove",
    "os.unlink",
    "os.rename",
    "os.replace",
    "os.rmdir",
    "os.chmod",
    "os.chown",
    "subprocess.run",
    "subprocess.Popen",
    "asyncio.create_subprocess_exec",
    "asyncio.create_subprocess_shell",
}
_PROTECTED_METHODS = {
    "chmod",
    "chown",
    "mkdir",
    "rename",
    "rmdir",
    "touch",
    "unlink",
    "write_bytes",
    "write_text",
}


@dataclass(frozen=True, order=True)
class Registration:
    key: str
    path: str
    function: str
    line: int
    kind: str


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    """Resolve imports before deciding whether a tool reaches a protected API."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                aliases[item.asname or item.name] = item.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for item in node.names:
                aliases[item.asname or item.name] = f"{node.module}.{item.name}"
    return aliases


def _resolved_call_name(node: ast.expr, aliases: dict[str, str]) -> str:
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Call):
        constructor = _resolved_call_name(node.value.func, aliases)
        if constructor == "pathlib.Path" and node.attr == "open":
            return f"{constructor}.{node.attr}"
    raw = _call_name(node)
    root, dot, rest = raw.partition(".")
    return f"{aliases.get(root, root)}{dot}{rest}" if raw else raw


def _tool_name(node: ast.Call, default: str) -> str:
    for keyword in node.keywords:
        if (
            keyword.arg == "name"
            and isinstance(keyword.value, ast.Constant)
            and isinstance(keyword.value.value, str)
        ):
            return keyword.value.value
    return default


def _mcp_registrations() -> tuple[set[Registration], list[str]]:
    registrations: set[Registration] = set()
    violations: list[str] = []
    for path in sorted(SERVER_ROOT.glob("*/server.py")):
        relative = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        audited_instances = {
            target.id
            for node in tree.body
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and _call_name(node.value.func) == "create_ticket_mcp"
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not (
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and decorator.func.attr == "tool"
                ):
                    continue
                receiver = _call_name(decorator.func.value)
                if receiver not in audited_instances:
                    violations.append(
                        f"{relative}:{node.lineno}:{node.name} decorates "
                        f"{receiver or '<dynamic>'}.tool outside create_ticket_mcp"
                    )
                name = _tool_name(decorator, node.name)
                registrations.add(
                    Registration(
                        key=f"{relative}:{name}",
                        path=relative,
                        function=node.name,
                        line=node.lineno,
                        kind="mcp",
                    )
                )
    return registrations, violations


def _native_registrations() -> set[Registration]:
    """Discover every native LLM surface, including the chat-only dispatcher."""
    base_path = ROOT / "agents/base.py"
    tree = ast.parse(base_path.read_text(encoding="utf-8"), filename="agents/base.py")
    registrations: set[Registration] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = [key.value for key in node.keys if isinstance(key, ast.Constant)]
        if not keys or not all(isinstance(key, str) for key in keys):
            continue
        # The native workspace registration map is the only literal dict in
        # AgentBase containing the canonical workspace tool names.
        if "jq_file_from_workspace" not in keys:
            continue
        for key in keys:
            registrations.add(
                Registration(
                    key=f"agents/base.py:{key}",
                    path="agents/base.py",
                    function="_register_workspace_tools",
                    line=node.lineno,
                    kind="native",
                )
            )

    for path in sorted(SERVER_ROOT.glob("*/agent.py")):
        relative = path.relative_to(ROOT).as_posix()
        module = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        for node in ast.walk(module):
            if not (
                isinstance(node, ast.Call)
                and _call_name(node.func).endswith("ToolDefinition")
            ):
                continue
            for keyword in node.keywords:
                if (
                    keyword.arg == "name"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ):
                    registrations.add(
                        Registration(
                            key=f"{relative}:{keyword.value.value}",
                            path=relative,
                            function="ToolDefinition",
                            line=node.lineno,
                            kind="native",
                        )
                    )
        for node in ast.walk(module):
            if not (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == "local_handlers"
                    for target in node.targets
                )
                and isinstance(node.value, ast.Dict)
            ):
                continue
            for key in node.value.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    registrations.add(
                        Registration(
                            key=f"{relative}:{key.value}",
                            path=relative,
                            function="local_handlers",
                            line=node.lineno,
                            kind="native",
                        )
                    )
    return registrations


def _chat_registrations() -> set[Registration]:
    """Discover CHAT_TOOLS instead of treating the web chat as out of scope."""
    relative = "agents/chat/tools.py"
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"), filename=relative)
    registrations: set[Registration] = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and _call_name(node.func).endswith("ToolDefinition")
        ):
            continue
        name = _tool_name(node, "")
        if name:
            registrations.add(
                Registration(
                    key=f"{relative}:{name}",
                    path=relative,
                    function="execute_tool",
                    line=node.lineno,
                    kind="chat",
                )
            )
    return registrations


def _chat_handler_name(tree: ast.AST, tool_name: str) -> str | None:
    """Find the concrete _dispatch_tool branch for one advertised chat name."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "tool_name"
            and len(test.ops) == len(test.comparators) == 1
            and isinstance(test.ops[0], ast.Eq)
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value == tool_name
        ):
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                name = _call_name(child.func)
                if name.startswith("_") and name != "_dispatch_tool":
                    return name
    return None


def _read_only_direct_mutations(registration: Registration) -> list[str]:
    """Return definite protected calls in an actual read-only entry handler.

    The checker resolves aliased imports (``from os import open as fd_open``
    and ``import os as operating_system``), not just their spelling at the
    call site.  Chat registrations resolve their real dispatch branch rather
    than inspecting the whole shared dispatcher.
    """
    if registration.kind not in {"mcp", "chat"}:
        return []
    tree = ast.parse(
        (ROOT / registration.path).read_text(encoding="utf-8"),
        filename=registration.path,
    )
    function = registration.function
    if registration.kind == "chat":
        function = _chat_handler_name(tree, registration.key.rsplit(":", 1)[1]) or ""
    aliases = _import_aliases(tree)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != function:
            continue
        calls = []
        for descendant in ast.walk(node):
            if not isinstance(descendant, ast.Call):
                continue
            call = _resolved_call_name(descendant.func, aliases)
            if (
                call in _PROTECTED_MUTATORS
                or call.rsplit(".", 1)[-1] in _PROTECTED_METHODS
            ):
                calls.append(f"{registration.path}:{descendant.lineno}:{call}")
        return calls
    raise AssertionError(f"could not resolve declared handler {registration!r}")


def test_tool_registration_inventory_is_complete_and_classified() -> None:
    """A new MCP/native action must add an explicit reviewed policy entry."""
    mcp, violations = _mcp_registrations()
    actual = mcp | _native_registrations() | _chat_registrations()
    assert not violations, "\n".join(violations)
    expected = set(POLICY_BY_REGISTRATION)
    actual_keys = {registration.key for registration in actual}
    assert expected == actual_keys, (
        "tool audit inventory changed; add explicit classification, audited owner, "
        "and a bounded fixture exemption. "
        f"missing={sorted(actual_keys - expected)!r}; "
        f"stale={sorted(expected - actual_keys)!r}"
    )
    assert len(POLICY_BY_REGISTRATION) == len(TOOL_AUDIT_POLICY)


def test_tool_audit_exemptions_and_side_effect_owners_are_reviewable() -> None:
    """Exemptions may be necessary, but cannot become silent permanent bypasses."""
    for policy in TOOL_AUDIT_POLICY:
        exemption = policy.fixture_exemption
        assert exemption.owner and exemption.reason
        assert date.fromisoformat(exemption.expires_on) >= date.today()
        if policy.classification == "side_effecting":
            assert policy.operation_owner, policy.registration
            contract = OPERATION_OWNER_CONTRACTS.get(policy.operation_owner)
            assert contract, f"no concrete contract for {policy.operation_owner}"
            path, symbol = contract.split(":", 1)
            if path.endswith(".py"):
                source = (ROOT / path).read_text(encoding="utf-8")
            else:
                source = "\n".join(
                    candidate.read_text(encoding="utf-8")
                    for candidate in (ROOT / path).rglob("*.py")
                )
            assert symbol in source, (
                f"stale operation owner {policy.operation_owner}: {contract}"
            )
        else:
            assert policy.operation_owner is None, policy.registration

    used_owners = {
        policy.operation_owner
        for policy in TOOL_AUDIT_POLICY
        if policy.operation_owner is not None
    }
    assert used_owners == set(OPERATION_OWNER_CONTRACTS), (
        "operation owner contracts must not become stale documentation; "
        f"unused={set(OPERATION_OWNER_CONTRACTS) - used_owners!r}"
    )


def test_read_only_tool_declarations_do_not_call_protected_mutators() -> None:
    """Prevent a local write/subprocess from being labelled read-only."""
    mcp, _ = _mcp_registrations()
    checked = mcp | _chat_registrations()
    mismatches = []
    for registration in checked:
        policy = POLICY_BY_REGISTRATION[registration.key]
        if policy.classification != "read_only":
            continue
        mismatches.extend(_read_only_direct_mutations(registration))
    assert not mismatches, (
        "read-only tool calls a protected API; reclassify it and name its audited "
        f"operation/idempotency owner: {sorted(mismatches)!r}"
    )


def test_production_fastmcp_and_native_dispatch_have_one_audit_boundary() -> None:
    """Direct FastMCP construction or native handler dispatch is a CI failure."""
    allowlisted = {f"{entry.path}:{entry.symbol}" for entry in AUDIT_BYPASS_ALLOWLIST}
    for entry in AUDIT_BYPASS_ALLOWLIST:
        assert entry.owner and entry.scope and entry.reason
        assert date.fromisoformat(entry.expires_on) >= date.today()
    bypasses = []
    consumed_allowlist: set[str] = set()
    for path in sorted((ROOT / "agents").rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        if relative == "agents/mcp_audit.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        fastmcp_constructors = {"FastMCP"}
        for import_node in ast.walk(tree):
            if (
                isinstance(import_node, ast.ImportFrom)
                and import_node.module == "fastmcp"
            ):
                fastmcp_constructors.update(
                    alias.asname or alias.name
                    for alias in import_node.names
                    if alias.name == "FastMCP"
                )
            if isinstance(import_node, ast.Import):
                fastmcp_constructors.update(
                    f"{alias.asname or alias.name}.FastMCP"
                    for alias in import_node.names
                    if alias.name == "fastmcp"
                )
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and _call_name(node.func) in fastmcp_constructors
            ):
                key = f"{relative}:FastMCP"
                if key not in allowlisted:
                    bypasses.append(f"{relative}:{node.lineno}:FastMCP")
                else:
                    consumed_allowlist.add(key)
            if (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Attribute)
                    and target.attr == "_tool_handlers"
                    for target in node.targets
                )
                and relative != "agents/base.py"
            ):
                key = f"{relative}:native handler assignment"
                if key not in allowlisted:
                    bypasses.append(
                        f"{relative}:{node.lineno}:native handler assignment"
                    )
                else:
                    consumed_allowlist.add(key)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"update", "setdefault"}
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "_tool_handlers"
                and relative != "agents/base.py"
            ):
                key = f"{relative}:native handler {node.func.attr}"
                if key not in allowlisted:
                    bypasses.append(f"{relative}:{node.lineno}:native handler mutation")
                else:
                    consumed_allowlist.add(key)
    assert allowlisted == consumed_allowlist, (
        "audit bypass allowlist has stale or unmatched entries: "
        f"{sorted(allowlisted - consumed_allowlist)!r}"
    )
    assert not bypasses, (
        "production registrations must use create_ticket_mcp or "
        f"AgentBase._execute_tool: {bypasses!r}"
    )


def _synthetic_mcp_request(name: str) -> MiddlewareContext:
    meta = RequestParams.Meta(
        **{
            "agentic-perf": {
                "ticket_id": "PERF-policy",
                "agent_id": "audit-policy",
                "invocation_id": str(uuid4()),
                "trace_id": "a" * 32,
                "action_id": "b" * 16,
                "correlation_request_id": uuid4().hex,
                "idempotency_key": "policy-operation",
                "idempotency_request_hash": "policy-request-hash",
            }
        }
    )
    return MiddlewareContext(
        message=CallToolRequestParams(name=name, _meta=meta),
        method="tools/call",
        fastmcp_context=SimpleNamespace(
            request_id="policy-rpc", session_id="policy-session"
        ),
    )


@pytest.mark.asyncio
async def test_each_registered_mcp_name_gets_a_correlated_audit_pair() -> None:
    """Exercise the audited factory contract for every discovered MCP name."""
    registrations, _ = _mcp_registrations()
    for registration in registrations:
        events = []
        middleware = MCPAuditMiddleware(
            "policy-fixture",
            ticket_id="PERF-policy",
            agent_id="audit-policy",
            record=events.append,
        )

        async def handler(_: MiddlewareContext) -> ToolResult:
            return ToolResult(content="ok")

        await middleware.on_call_tool(
            _synthetic_mcp_request(registration.key.rsplit(":", 1)[1]), handler
        )
        assert events[0].lifecycle.state == LifecycleState.REQUEST_RECEIVED, (
            registration.key
        )
        assert events[-1].lifecycle.state in {
            LifecycleState.RESPONSE_SENT,
            LifecycleState.FAILED,
            LifecycleState.REJECTED,
            LifecycleState.DUPLICATE_DETECTED,
            LifecycleState.CANCELLED,
        }, registration.key
        assert len(events) == 2, registration.key
        assert {event.action.phase for event in events} == {
            registration.key.rsplit(":", 1)[1]
        }
        assert len({event.action_id for event in events}) == 1
        assert all(event.mcp.correlation_request_id for event in events)


@pytest.mark.asyncio
async def test_each_registered_chat_name_gets_a_correlated_audit_pair() -> None:
    """Run every real CHAT_TOOLS dispatcher branch through ChatToolAudit."""
    from unittest.mock import AsyncMock, MagicMock

    from agents.chat.tools import ChatToolAudit, execute_tool

    registrations = _chat_registrations()
    for registration in registrations:
        events = []
        client = AsyncMock()
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {"id": "PERF-policy", "approvals": []}
        client.get = AsyncMock(return_value=response)
        client.post = AsyncMock(return_value=response)
        client.patch = AsyncMock(return_value=response)
        tool_name = registration.key.rsplit(":", 1)[1]
        await execute_tool(
            tool_name,
            {},
            client,
            "http://state-store.invalid",
            "policy-token",
            audit=ChatToolAudit(
                client,
                "http://state-store.invalid",
                "policy-token",
                record=events.append,
            ),
        )
        assert [event.lifecycle.state for event in events] == [
            LifecycleState.STARTED,
            LifecycleState.COMPLETED,
        ] or [event.lifecycle.state for event in events] == [
            LifecycleState.STARTED,
            LifecycleState.FAILED,
        ], registration.key
        assert len({event.action_id for event in events}) == 1
        assert {event.action.phase for event in events} == {tool_name}


@pytest.mark.asyncio
async def test_chat_audit_uses_the_service_credential_for_trace_ingestion() -> None:
    """Trace ingestion rejects user credentials, so this must be a service call."""
    from unittest.mock import AsyncMock, MagicMock

    from agents.chat.tools import ChatToolAudit, execute_tool

    client = AsyncMock()
    response = MagicMock()
    response.raise_for_status = MagicMock()
    client.post = AsyncMock(return_value=response)
    await execute_tool(
        "start_ticket",
        {},
        client,
        "http://state-store.invalid",
        "user-token",
        audit=ChatToolAudit(client, "http://state-store.invalid", "service-token"),
        tool_call_id="toolu-verified",
    )
    assert client.post.await_count == 2
    for call in client.post.await_args_list:
        assert call.args[0].endswith("/api/v1/traces/events")
        assert call.kwargs["headers"] == {"Authorization": "Bearer service-token"}
    assert [
        call.kwargs["json"]["lifecycle"]["state"]
        for call in client.post.await_args_list
    ] == [
        "started",
        "failed",
    ]
    assert {
        call.kwargs["json"]["tool_call_id"] for call in client.post.await_args_list
    } == {"toolu-verified"}


def test_chat_tool_dispatch_has_no_production_audit_bypass() -> None:
    """Any production call to execute_tool must explicitly construct its boundary."""
    calls: list[tuple[str, ast.Call]] = []
    for path in (ROOT / "agents").rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        if relative == "agents/chat/tools.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        calls.extend(
            (relative, node)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _call_name(node.func) == "execute_tool"
        )
    assert calls, "CHAT_TOOLS has no production dispatcher"
    for relative, call in calls:
        audit = next(
            (item.value for item in call.keywords if item.arg == "audit"), None
        )
        assert (
            isinstance(audit, ast.Call) and _call_name(audit.func) == "ChatToolAudit"
        ), f"{relative}:{call.lineno} bypasses ChatToolAudit"
        assert any(item.arg == "tool_call_id" for item in call.keywords), (
            f"{relative}:{call.lineno} drops the LLM tool-call correlation id"
        )

    store = ast.parse(
        (ROOT / "state_store/main.py").read_text(encoding="utf-8"),
        filename="state_store/main.py",
    )
    constructors = [
        node
        for node in ast.walk(store)
        if isinstance(node, ast.Call) and _call_name(node.func) == "ChatAgent"
    ]
    assert len(constructors) == 1
    assert any(item.arg == "audit_token" for item in constructors[0].keywords), (
        "embedded ChatAgent must use the service credential required by trace ingestion"
    )


def test_protected_call_aliases_resolve_to_the_canonical_api() -> None:
    """Regression coverage for import aliases that could otherwise evade CI."""
    tree = ast.parse(
        "from os import open as fd_open\nimport os as operating_system\n"
        "from pathlib import Path as LocalPath\n"
        "fd_open('x', operating_system.O_CREAT)\n"
        "operating_system.open('x', operating_system.O_WRONLY)\n"
        "LocalPath('x').open('w')\n"
    )
    aliases = _import_aliases(tree)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    assert [_resolved_call_name(call.func, aliases) for call in calls] == [
        "os.open",
        "os.open",
        "pathlib.Path.open",
        "pathlib.Path",
    ]
