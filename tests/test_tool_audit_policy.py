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
    """Discover handlers registered through AgentBase's sole native dispatcher."""
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


def _read_only_direct_mutations(registration: Registration) -> list[str]:
    """Return definite protected API calls directly inside one MCP handler."""
    if registration.kind != "mcp":
        return []
    tree = ast.parse(
        (ROOT / registration.path).read_text(encoding="utf-8"),
        filename=registration.path,
    )
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != registration.function or node.lineno != registration.line:
            continue
        calls = []
        for descendant in ast.walk(node):
            if not isinstance(descendant, ast.Call):
                continue
            call = _call_name(descendant.func)
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
    actual = mcp | _native_registrations()
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
        else:
            assert policy.operation_owner is None, policy.registration


def test_read_only_tool_declarations_do_not_call_protected_mutators() -> None:
    """Prevent a local write/subprocess from being labelled read-only."""
    mcp, _ = _mcp_registrations()
    mismatches = []
    for registration in mcp:
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
async def test_shared_boundaries_record_a_tool_entry_and_terminal_pair() -> None:
    """Exercise the common fixture used by every reviewed exemption."""
    events = []
    middleware = MCPAuditMiddleware(
        "policy-fixture",
        ticket_id="PERF-policy",
        agent_id="audit-policy",
        record=events.append,
    )

    async def read_only_handler(_: MiddlewareContext) -> ToolResult:
        return ToolResult(content="ok")

    await middleware.on_call_tool(
        _synthetic_mcp_request("read_only_fixture"), read_only_handler
    )
    assert [event.lifecycle.state for event in events] == [
        LifecycleState.REQUEST_RECEIVED,
        LifecycleState.RESPONSE_SENT,
    ]
    assert all(event.invocation_id for event in events)
    assert all(event.mcp.correlation_request_id for event in events)

    transitions = []
    middleware._client = SimpleNamespace(
        operation_acquire=lambda *_: {
            "status": "acquired",
            "operation": {"fencing_generation": 1},
        },
        operation_transition=lambda *args, **kwargs: transitions.append((args, kwargs)),
    )

    async def side_effect_handler(_: MiddlewareContext) -> ToolResult:
        return ToolResult(content="complete")

    await middleware.on_call_tool(
        _synthetic_mcp_request("execute_benchmark"), side_effect_handler
    )
    assert [transition[0][1] for transition in transitions] == [
        "prepared",
        "side-effect-started",
        "complete",
    ]

    # AgentBase is the sole native dispatch path and creates its paired TOOL
    # STARTED/terminal records around _execute_tool (the source avoids a second
    # slow LLM loop fixture while remaining a hard CI contract).
    base = (ROOT / "agents/base.py").read_text(encoding="utf-8")
    assert "ActionType.TOOL,\n                        LifecycleState.STARTED" in base
    assert "LifecycleState.FAILED\n                        if result.is_error" in base
