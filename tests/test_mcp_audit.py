from __future__ import annotations

import ast
import asyncio
import socket
import sys
import textwrap
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import uvicorn
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.tools.base import ToolResult
from mcp import McpError
from mcp.types import CallToolRequestParams, RequestParams

from agents.mcp_audit import MCPAuditMiddleware, assert_fastmcp_audit_compatibility
from agents.mcp_client import AgentMCPClient, _ServerConnection
from providers.tracing import LifecycleState, TraceContext
from providers.tracing.client import TraceClient
from state_store.trace_store import TraceStore
from tests.test_trace_ingestion import make_app


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.skipif(
    sys.version_info >= (3, 14),
    reason="FastMCP stdio hangs on local Python 3.14; covered in CI 3.12/3.13",
)
@pytest.mark.asyncio
async def test_ticket_stdio_protected_replay_is_durable_and_exact(
    tmp_path, monkeypatch
):
    """A real ticket server replays a large protected result after restart."""
    app = make_app(tmp_path)
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    serve_task = asyncio.create_task(server.serve())
    base_url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            import httpx

            async with httpx.AsyncClient() as health_client:
                if (
                    await health_client.get(base_url + "/api/v1/health")
                ).status_code == 200:
                    break
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.03)
    else:
        server.should_exit = True
        await asyncio.wait_for(serve_task, 5)
        pytest.fail("temporary state-store did not start")
    counter = tmp_path / "handler-count"
    sentinel = "cross-process-secret-sentinel-786"
    monkeypatch.setenv("MCP_TEST_TOKEN", sentinel)
    script = tmp_path / "ticket_server.py"
    script.write_text(
        textwrap.dedent(f"""\
        from pathlib import Path
        from agents.mcp_audit import create_ticket_mcp
        from agents.server_utils import _emit_tool_progress
        import os
        mcp = create_ticket_mcp("ticket-audit")
        @mcp.tool()
        async def execute_benchmark() -> str:
            path = Path({str(counter)!r})
            path.write_text(str(int(path.read_text() if path.exists() else "0") + 1))
            _emit_tool_progress("PERF-786", "benchmark", os.environ["MCP_TEST_TOKEN"])
            return os.environ["MCP_TEST_TOKEN"] + ("X" * 5000)
        @mcp.tool()
        async def fail_with_secret() -> str:
            raise RuntimeError(os.environ["MCP_TEST_TOKEN"])
        if __name__ == "__main__":
            mcp.run()
    """)
    )
    monkeypatch.setenv("AGENTIC_PERF_API_TOKEN", "service")
    monkeypatch.setenv("STATE_STORE_URL", base_url)
    monkeypatch.setenv("AGENTIC_PERF_HOME", str(tmp_path))
    recorder = TraceClient(base_url, "service", spool_dir=tmp_path / "client-spool")
    trace = TraceContext(
        ticket_id="PERF-786",
        agent_id="benchmark",
        mcp_correlation_request_id="stable",
        idempotency_key="mcp-786",
        idempotency_request_hash="hash-786",
    )
    first = AgentMCPClient(trace_client=recorder)
    second = AgentMCPClient(trace_client=recorder)
    try:
        await asyncio.wait_for(
            first.connect_ticket_server(
                str(script),
                name="ticket",
                ticket_id="PERF-786",
                state_store_url=base_url,
                agent_name="benchmark",
            ),
            15,
        )
        first_pid = first._servers["ticket"].subprocess_pid
        result = await asyncio.wait_for(
            first.call_tool("execute_benchmark", {}, trace), 15
        )
        assert result == sentinel + ("X" * 5000)
        await asyncio.wait_for(first.disconnect(), 10)
        await asyncio.wait_for(
            second.connect_ticket_server(
                str(script),
                name="ticket",
                ticket_id="PERF-786",
                state_store_url=base_url,
                agent_name="benchmark",
            ),
            15,
        )
        second_pid = second._servers["ticket"].subprocess_pid
        replay = await asyncio.wait_for(
            second.call_tool("execute_benchmark", {}, trace), 15
        )
        assert sentinel not in replay
        assert replay == "[REDACTED:env/MCP_TEST_TOKEN]" + ("X" * 5000)
        assert counter.read_text() == "1"
        assert first_pid and second_pid and first_pid != second_pid
        await asyncio.to_thread(recorder.flush)
        with TraceStore(tmp_path / "trace.db") as store:
            operation = store.get_operation("mcp-786")
            assert operation and operation.state == "terminal"
            descriptor = operation.result_descriptor["operation_result"]
            assert descriptor["blob_ref"]
            assert descriptor["original_size_bytes"] > 4096
            events = store.list_events("PERF-786")
        assert any(
            e.producer.component == "mcp_client"
            and e.mcp.server_pid in {first_pid, second_pid}
            for e in events
        )
        assert any(
            e.producer.component == "mcp_server"
            and e.mcp.server_pid in {first_pid, second_pid}
            for e in events
        )
        with pytest.raises(Exception):
            await asyncio.wait_for(
                second.call_tool(
                    "fail_with_secret",
                    {},
                    trace.model_copy(
                        update={
                            "mcp_correlation_request_id": "exception-secret",
                            "idempotency_key": None,
                            "idempotency_request_hash": None,
                        }
                    ),
                ),
                15,
            )
        await asyncio.to_thread(recorder.flush)
        surfaces = []
        for path in tmp_path.rglob("*"):
            if path.is_file():
                try:
                    surfaces.append(path.read_bytes())
                except OSError:
                    pass
        assert all(sentinel.encode() not in content for content in surfaces)
    finally:
        await asyncio.wait_for(first.disconnect(), 10)
        await asyncio.wait_for(second.disconnect(), 10)
        await asyncio.to_thread(recorder.close)
        server.should_exit = True
        await asyncio.wait_for(serve_task, 10)
        app.state.trace_store.close()


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
