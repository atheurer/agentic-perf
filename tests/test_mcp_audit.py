from __future__ import annotations

import ast
import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.tools.base import ToolResult
from mcp import McpError
from mcp.types import CallToolRequestParams, RequestParams

from agents.mcp_audit import MCPAuditMiddleware, assert_fastmcp_audit_compatibility
from agents.mcp_client import AgentMCPClient, _ServerConnection
from providers.tracing import LifecycleState, TraceContext


def _request(
    correlation_id: str, *, ticket: str = "PERF-1", session_id: str = "session-1"
) -> MiddlewareContext:
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
        fastmcp_context=SimpleNamespace(request_id="rpc-1", session_id=session_id),
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
async def test_server_labels_stable_correlation_after_reconnect_as_replay():
    events = []
    middleware = MCPAuditMiddleware(
        "benchmark-agent",
        ticket_id="PERF-1",
        agent_id="benchmark",
        record=events.append,
    )
    handler = AsyncMock(return_value="ok")
    await middleware.on_call_tool(_request("stable", session_id="before"), handler)
    with pytest.raises(McpError, match="duplicate MCP delivery"):
        await middleware.on_call_tool(_request("stable", session_id="after"), handler)
    handler.assert_awaited_once()
    assert events[-1].lifecycle.state == LifecycleState.DUPLICATE_DETECTED


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
    assert meta["agentic-perf"]["idempotency_key"].startswith("mcp-delivery:")
    assert len(meta["agentic-perf"]["idempotency_request_hash"]) == 64
    assert [event.lifecycle.state for event in client.audit_events] == [
        LifecycleState.REQUEST_SENT,
        LifecycleState.RESPONSE_RECEIVED,
    ]


@pytest.mark.asyncio
async def test_client_spools_boundary_events_and_closes_cancelled_call():
    recorder = SimpleNamespace(record=MagicMock())
    session = AsyncMock()
    session.call_tool = AsyncMock(side_effect=asyncio.CancelledError())
    client = AgentMCPClient(trace_client=recorder)
    client._tool_routing["tool"] = "local"
    client._servers["local"] = _ServerConnection(
        name="local",
        session=session,
        transport="sse",
        session_id="session-1",
        reconnect_generation=0,
        ticket_id="PERF-1",
    )
    with pytest.raises(asyncio.CancelledError):
        await client.call_tool("tool", {}, TraceContext(ticket_id="PERF-1"))
    assert recorder.record.call_count == 2
    assert client.audit_events[-1].lifecycle.state == LifecycleState.CANCELLED


@pytest.mark.asyncio
async def test_protected_tool_uses_registry_and_returns_terminal_result():
    events = []
    registry = SimpleNamespace(
        operation_acquire=lambda *_: {
            "status": "terminal",
            "operation": {
                "result_descriptor": {
                    "tool_result": ToolResult(content="existing").model_dump(
                        mode="json"
                    )
                }
            },
        }
    )
    middleware = MCPAuditMiddleware(
        "benchmark-agent",
        ticket_id="PERF-1",
        agent_id="benchmark",
        record=events.append,
    )
    middleware._client = registry
    handler = AsyncMock()
    request = _request("operation-replay")
    request.message.meta.model_extra["agentic-perf"]["idempotency_key"] = "operation-1"
    request.message.meta.model_extra["agentic-perf"]["idempotency_request_hash"] = (
        "hash"
    )

    result = await middleware.on_call_tool(
        request.copy(
            message=request.message.model_copy(update={"name": "execute_benchmark"})
        ),
        handler,
    )
    handler.assert_not_awaited()
    assert result.is_error is False
    assert result.content[0].text == "existing"
    assert events[-1].lifecycle.state == LifecycleState.DUPLICATE_DETECTED


@pytest.mark.asyncio
async def test_acquired_protected_tool_transitions_legally_and_invokes_once():
    transitions = []
    registry = SimpleNamespace(
        operation_acquire=lambda *_: {
            "status": "acquired",
            "operation": {"fencing_generation": 1},
        },
        operation_transition=lambda key, action, token, **kwargs: transitions.append(
            (key, action, token, kwargs)
        ),
    )
    middleware = MCPAuditMiddleware(
        "benchmark-agent",
        ticket_id="PERF-1",
        agent_id="benchmark",
        record=lambda _: None,
    )
    middleware._client = registry
    request = _request("delivery")
    request.message.meta.model_extra["agentic-perf"].update(
        {"idempotency_key": "delivery-key", "idempotency_request_hash": "hash"}
    )
    result = await middleware.on_call_tool(
        request.copy(
            message=request.message.model_copy(update={"name": "execute_benchmark"})
        ),
        AsyncMock(return_value=ToolResult(content="actual response")),
    )
    assert result.content[0].text == "actual response"
    assert [item[1] for item in transitions] == [
        "prepared",
        "side-effect-started",
        "complete",
    ]
    assert (
        transitions[-1][3]["descriptor"]["tool_result"]["content"][0]["text"]
        == "actual response"
    )


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
