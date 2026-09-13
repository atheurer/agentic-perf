from __future__ import annotations

import ast
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastmcp.server.middleware import MiddlewareContext
from mcp import McpError
from mcp.types import CallToolRequestParams, RequestParams

from agents.mcp_audit import MCPAuditMiddleware, assert_fastmcp_audit_compatibility
from agents.mcp_client import AgentMCPClient, _ServerConnection
from providers.tracing import LifecycleState, TraceContext


def _request(correlation_id: str, *, ticket: str = "PERF-1") -> MiddlewareContext:
    meta = RequestParams.Meta(
        **{
            "agentic-perf": {
                "ticket_id": ticket,
                "agent_id": "benchmark",
                "invocation_id": str(uuid.uuid4()),
                "trace_id": "a" * 32,
                "action_id": "b" * 16,
                "correlation_request_id": correlation_id,
            }
        }
    )
    return MiddlewareContext(
        message=CallToolRequestParams(name="read_only", _meta=meta),
        method="tools/call",
        fastmcp_context=SimpleNamespace(request_id="rpc-1", session_id="session-1"),
    )


@pytest.mark.asyncio
async def test_server_records_metadata_and_detects_same_session_replay():
    events = []
    middleware = MCPAuditMiddleware(
        "benchmark-agent",
        ticket_id="PERF-1",
        agent_id="benchmark",
        record=events.append,
    )
    calls = 0

    async def next_handler(_):
        nonlocal calls
        calls += 1
        return "ok"

    await middleware.on_call_tool(_request("correlation-1"), next_handler)
    with pytest.raises(McpError, match="duplicate MCP delivery"):
        await middleware.on_call_tool(_request("correlation-1"), next_handler)

    assert calls == 1
    assert [event.lifecycle.state for event in events] == [
        LifecycleState.REQUEST_RECEIVED,
        LifecycleState.RESPONSE_SENT,
        LifecycleState.REQUEST_RECEIVED,
        LifecycleState.DUPLICATE_DETECTED,
    ]
    assert events[0].mcp.protocol_request_id == "rpc-1"
    assert events[0].mcp.correlation_request_id == "correlation-1"


@pytest.mark.asyncio
async def test_server_rejects_mismatched_ticket_before_handler():
    events = []
    middleware = MCPAuditMiddleware(
        "benchmark-agent",
        ticket_id="PERF-1",
        agent_id="benchmark",
        record=events.append,
    )
    handler = AsyncMock()

    with pytest.raises(McpError, match="ticket identity"):
        await middleware.on_call_tool(
            _request("correlation-2", ticket="PERF-2"), handler
        )

    handler.assert_not_awaited()
    assert [event.lifecycle.state for event in events] == [
        LifecycleState.REQUEST_RECEIVED,
        LifecycleState.REJECTED,
    ]


@pytest.mark.asyncio
async def test_client_sends_trace_metadata_without_changing_tool_arguments():
    session = AsyncMock()
    session.call_tool = AsyncMock(
        return_value=SimpleNamespace(
            content=[SimpleNamespace(text="ok")], isError=False
        )
    )
    client = AgentMCPClient()
    client._tool_routing["tool"] = "local"
    client._servers["local"] = _ServerConnection(
        name="local",
        session=session,
        transport="stdio",
        session_id="session-1",
        reconnect_generation=0,
        ticket_id="PERF-1",
        agent_id="benchmark",
    )
    trace = TraceContext(
        ticket_id="PERF-1",
        agent_id="benchmark",
        mcp_correlation_request_id="stable-correlation",
    )

    assert await client.call_tool("tool", {"value": 3}, trace) == "ok"
    _, arguments = session.call_tool.call_args.args
    assert arguments == {"value": 3}
    meta = session.call_tool.call_args.kwargs["meta"]
    assert meta["agentic-perf"]["correlation_request_id"] == "stable-correlation"
    assert meta["traceparent"].startswith("00-")
    assert [event.lifecycle.state for event in client.audit_events] == [
        LifecycleState.REQUEST_SENT,
        LifecycleState.RESPONSE_RECEIVED,
    ]


def test_every_local_fastmcp_server_uses_the_shared_factory():
    agents_root = Path(__file__).parents[1] / "agents"
    violations = []
    for path in sorted(agents_root.glob("*/server.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "FastMCP":
                    violations.append(str(path.relative_to(agents_root)))
    assert violations == []


def test_pinned_sdk_exposes_the_audited_mcp_contract():
    assert_fastmcp_audit_compatibility()
