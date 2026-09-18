from __future__ import annotations

import ast
import asyncio
import os
import socket
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.tools.base import ToolResult
from mcp import McpError
from mcp.types import CallToolRequestParams, RequestParams

import agents.jumpstarter_mcp as jumpstarter_mcp
import agents.mcp_audit as mcp_audit
import agents.mcp_client as mcp_client_module
from agents.jumpstarter_mcp import _JmpCallHook
from agents.mcp_audit import MCPAuditMiddleware, assert_fastmcp_audit_compatibility
from agents.mcp_client import (
    AgentMCPClient,
    MCPHookResult,
    MCPToolCallError,
    _ServerConnection,
)
from providers.redaction import get_shared_redactor
from providers.tracing import (
    LifecycleState,
    OperationOutcome,
    RetryKind,
    TraceContext,
    bind_trace_context,
    new_trace_context,
    reset_trace_context,
)
from providers.tracing.client import TraceClient
from state_store.trace_store import TraceStore


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _terminate_process(process: subprocess.Popen[str], timeout: float = 5) -> str:
    """Stop a test subprocess and collect diagnostics without blocking forever."""
    if process.poll() is None:
        process.terminate()
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as expired:
        process.kill()
        try:
            output, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            output = expired.output or ""
    return output or ""


def test_client_connection_audit_generates_correlation_from_ticket_context():
    """Connection boundaries are valid before any tool-call context exists."""
    events = []
    client = AgentMCPClient(audit_hook=events.append)
    connection = _ServerConnection(
        name="ticket-server",
        session=None,
        transport="stdio",
        ticket_id="PERF-connection",
        agent_id="triage",
    )
    token = bind_trace_context(
        new_trace_context(ticket_id="PERF-connection", agent_id="triage")
    )
    try:
        client._record_boundary(connection, LifecycleState.CONNECTING)
    finally:
        reset_trace_context(token)

    assert events[0].mcp.session_id == connection.session_id
    assert events[0].mcp.correlation_request_id


@pytest.mark.skipif(
    sys.version_info >= (3, 14),
    reason="FastMCP stdio hangs on local Python 3.14; covered in CI 3.12/3.13",
)
@pytest.mark.asyncio
async def test_ticket_stdio_protected_replay_is_durable_and_exact(
    tmp_path, monkeypatch
):
    """A real ticket server replays a large protected result after restart."""
    # Keep the spawned state store's persistence root separate from the test
    # process.  In particular, importing/initializing a parent app must never
    # hold the lock needed by the child process.
    store_home = tmp_path / "state-store"
    store_home.mkdir()
    port = _free_port()
    store_env = os.environ | {
        "AGENTIC_PERF_HOME": str(store_home),
        "AGENTIC_PERF_API_TOKEN": "service",
        "STORE_PORT": str(port),
    }
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "state_store.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=Path(__file__).parents[1],
        env=store_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        if server.poll() is not None:
            output = _terminate_process(server)
            pytest.fail(f"temporary state-store exited during startup: {output}")
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
        output = _terminate_process(server)
        pytest.fail(f"temporary state-store did not start: {output}")
    counter = tmp_path / "handler-count"
    sentinel = "cross-process-secret-sentinel-786"
    monkeypatch.setenv("MCP_TEST_TOKEN", sentinel)
    script = tmp_path / "ticket_server.py"
    script.write_text(
        textwrap.dedent(f"""\
        from pathlib import Path
        from agents.mcp_audit import create_ticket_mcp
        from agents.server_utils import _emit_tool_progress_event
        import os
        mcp = create_ticket_mcp("ticket-audit")
        @mcp.tool()
        async def execute_benchmark() -> str:
            path = Path({str(counter)!r})
            path.write_text(str(int(path.read_text() if path.exists() else "0") + 1))
            _emit_tool_progress_event(
                "PERF-786", "benchmark", os.environ["MCP_TEST_TOKEN"]
            )
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
    monkeypatch.setenv("AGENTIC_PERF_HOME", str(store_home))
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
        assert sentinel not in result
        assert result == "[REDACTED:env/MCP_TEST_TOKEN]" + ("X" * 5000)
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
        with TraceStore(store_home / "trace.db") as store:
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
        with pytest.raises(Exception) as error:
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
        assert sentinel not in str(error.value)
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
        _terminate_process(server, timeout=10)


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
    assert events[0].attributes["audit_boundary"] == "MCPAuditMiddleware.on_call_tool"
    assert events[0].attributes["audit_transport"] == "local"


@pytest.mark.asyncio
async def test_server_reads_metadata_from_fastmcp_request_context():
    events = []
    middleware = MCPAuditMiddleware(
        "benchmark-agent",
        ticket_id="PERF-1",
        agent_id="benchmark",
        record=events.append,
    )
    meta = RequestParams.Meta(
        **{
            "agentic-perf": {
                "ticket_id": "PERF-1",
                "agent_id": "benchmark",
                "trace_id": "a" * 32,
                "action_id": "b" * 16,
                "correlation_request_id": "request-context",
            }
        }
    )
    context = MiddlewareContext(
        message=CallToolRequestParams(
            name="read_only", _meta={"fastmcp": {"version": "3.4.4"}}
        ),
        method="tools/call",
        fastmcp_context=SimpleNamespace(
            request_id="rpc-1",
            session_id="session-1",
            request_context=SimpleNamespace(meta=meta),
        ),
    )

    await middleware.on_call_tool(context, AsyncMock(return_value="ok"))
    assert events[0].lifecycle.state == LifecycleState.REQUEST_RECEIVED
    assert events[0].mcp.correlation_request_id == "request-context"


@pytest.mark.asyncio
async def test_server_reads_metadata_from_fastmcp_request_params():
    events = []
    middleware = MCPAuditMiddleware(
        "benchmark-agent",
        ticket_id="PERF-1",
        agent_id="benchmark",
        record=events.append,
    )
    params = CallToolRequestParams(
        name="read_only",
        _meta={
            "agentic-perf": {
                "ticket_id": "PERF-1",
                "agent_id": "benchmark",
                "trace_id": "a" * 32,
                "action_id": "b" * 16,
                "correlation_request_id": "request-params",
            }
        },
    )
    context = MiddlewareContext(
        message=CallToolRequestParams(name="read_only"),
        method="tools/call",
        fastmcp_context=SimpleNamespace(
            request_id="rpc-1",
            session_id="session-1",
            request_context=SimpleNamespace(
                meta=RequestParams.Meta(**{"fastmcp": {"version": "3.4.4"}}),
                request=SimpleNamespace(params=params),
            ),
        ),
    )

    await middleware.on_call_tool(context, AsyncMock(return_value="ok"))
    assert events[0].mcp.correlation_request_id == "request-params"


@pytest.mark.asyncio
async def test_result_redaction_sanitizes_structured_keys_without_collision_loss():
    sentinel = "structured-key-secret-786"
    ticket = "PERF-structured-keys"
    get_shared_redactor().register(ticket, "secret/key", sentinel)
    middleware = MCPAuditMiddleware(
        "benchmark-agent", ticket_id=ticket, agent_id="benchmark", record=lambda _: None
    )

    async def handler(_):
        return ToolResult(
            content="safe",
            structured_content={sentinel: "first", "secret/key": "second"},
            meta={sentinel: "metadata"},
        )

    result = await middleware.on_call_tool(
        _request("structured-keys", ticket=ticket), handler
    )
    encoded = result.model_dump_json()
    assert sentinel not in encoded
    assert len(result.structured_content) == 2
    assert len(result.meta) == 1


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
async def test_ticket_mcp_fails_closed_without_audit_transport():
    """A real ticket server cannot use a no-op audit configuration."""
    middleware = MCPAuditMiddleware(
        "benchmark-agent", ticket_id="PERF-1", agent_id="benchmark"
    )
    handler = AsyncMock(return_value="unexpected")
    with pytest.raises(McpError, match="audit transport is unavailable"):
        await middleware.on_call_tool(_request("no-audit"), handler)
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_protected_terminal_audit_failure_marks_real_operation_indeterminate(
    tmp_path,
):
    """A lost terminal trace cannot leave a real operation terminal-success."""

    class StoreBackedRegistry:
        """Minimal service-client facade backed by the production TraceStore."""

        def __init__(self, store: TraceStore) -> None:
            self.store = store
            self.owner = "test-service"

        def operation_acquire(self, key: str, request_hash: str, ttl: float) -> dict:
            operation, status = self.store.acquire_operation_result(
                key, request_hash, self.owner, ttl
            )
            return {"status": status, "operation": operation.__dict__}

        def operation_transition(
            self, key: str, action: str, token: int, **kwargs
        ) -> dict:
            descriptor = kwargs.get("descriptor") or {}
            operation = {
                "prepared": self.store.mark_prepared,
                "side-effect-started": self.store.mark_side_effect_started,
                "complete": lambda *args: self.store.complete(*args, descriptor),
                "fail": lambda *args: self.store.fail(*args, descriptor),
                "indeterminate": lambda *args: self.store.mark_indeterminate(
                    *args, descriptor
                ),
            }[action](key, self.owner, token)
            return {"operation": operation.__dict__}

    event_count = 0

    def record(_event):
        nonlocal event_count
        event_count += 1
        if event_count == 2:
            raise OSError("trace service unavailable")

    with TraceStore(tmp_path / "trace.db") as store:
        middleware = MCPAuditMiddleware(
            "benchmark-agent",
            ticket_id="PERF-1",
            agent_id="benchmark",
            record=record,
        )
        middleware._client = StoreBackedRegistry(store)
        request = _request("terminal-loss")
        request.message.meta.model_extra["agentic-perf"].update(
            {"idempotency_key": "terminal-loss", "idempotency_request_hash": "hash"}
        )
        with pytest.raises(McpError, match="outcome is indeterminate"):
            await middleware.on_call_tool(
                request.copy(
                    message=request.message.model_copy(
                        update={"name": "execute_benchmark"}
                    )
                ),
                AsyncMock(return_value=ToolResult(content="effect happened")),
            )
        operation = store.get_operation("terminal-loss")
        assert operation is not None
        assert operation.state == "terminal"
        assert operation.terminal_outcome == "indeterminate"
        assert [
            entry["reason"] for entry in store.operation_history("terminal-loss")
        ] == [
            "registered",
            "lease_acquired",
            "prepared",
            "side_effect_started",
            "terminal",
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
async def test_client_audits_pre_call_hook_boundaries():
    client = AgentMCPClient()
    client._tool_routing["tool"] = "local"
    client._servers["local"] = _ServerConnection(
        name="local",
        session=AsyncMock(),
        transport="stdio",
        session_id="session-1",
        ticket_id="PERF-1",
    )
    client.pre_call_hook = AsyncMock(return_value="short-circuited")

    assert await client.call_tool("tool", {}, TraceContext(ticket_id="PERF-1")) == (
        "short-circuited"
    )
    assert [event.lifecycle.state for event in client.audit_events] == [
        LifecycleState.SHORT_CIRCUITED,
    ]
    assert client.audit_events[0].outcome == OperationOutcome.SUCCESS
    client._servers["local"].session.call_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_jumpstarter_internal_dispatch_preserves_trace_audit_metadata():
    session = AsyncMock()
    session.call_tool = AsyncMock(
        return_value=SimpleNamespace(
            content=[SimpleNamespace(text="connected")], isError=False
        )
    )
    client = AgentMCPClient()
    client._tool_routing["jmp_connect"] = "jumpstarter"
    client._servers["jumpstarter"] = _ServerConnection(
        name="jumpstarter",
        session=session,
        transport="stdio",
        session_id="session-1",
        ticket_id="PERF-1",
    )
    client.pre_call_hook = _JmpCallHook(client).pre_call
    trace = TraceContext(
        ticket_id="PERF-1",
        agent_id="benchmark",
        mcp_correlation_request_id="corr-jumpstarter",
    )

    assert await client.call_tool("jmp_connect", {"lease_id": "lease-1"}, trace) == (
        "connected"
    )
    metadata = session.call_tool.call_args.kwargs["meta"]
    assert metadata["agentic-perf"]["correlation_request_id"] == "corr-jumpstarter"
    assert [event.lifecycle.state for event in client.audit_events] == [
        LifecycleState.REQUEST_SENT,
        LifecycleState.RESPONSE_RECEIVED,
    ]
    assert all(
        event.mcp.correlation_request_id == "corr-jumpstarter"
        for event in client.audit_events
    )


@pytest.mark.asyncio
async def test_jumpstarter_internal_mcp_error_is_not_a_success_short_circuit():
    session = AsyncMock()
    session.call_tool = AsyncMock(
        return_value=SimpleNamespace(
            content=[SimpleNamespace(text="lease rejected")], isError=True
        )
    )
    client = AgentMCPClient()
    client._tool_routing["jmp_connect"] = "jumpstarter"
    client._servers["jumpstarter"] = _ServerConnection(
        name="jumpstarter",
        session=session,
        transport="stdio",
        session_id="session-1",
        ticket_id="PERF-1",
    )
    client.pre_call_hook = _JmpCallHook(client).pre_call

    with pytest.raises(MCPToolCallError) as exc_info:
        await client.call_tool(
            "jmp_connect",
            {"lease_id": "lease-1"},
            TraceContext(
                ticket_id="PERF-1",
                mcp_correlation_request_id="corr-jumpstarter",
            ),
        )

    assert exc_info.value.retry_classification == "intentional_agent_retry"
    assert [event.lifecycle.state for event in client.audit_events] == [
        LifecycleState.REQUEST_SENT,
        LifecycleState.FAILED,
    ]
    assert len(client.audit_events) == 2
    assert (
        client.audit_events[-1].lifecycle.retry_kind
        == RetryKind.INTENTIONAL_AGENT_RETRY
    )
    assert session.call_tool.await_count == 1


@pytest.mark.asyncio
async def test_jumpstarter_internal_dispatch_cancellation_has_one_terminal_boundary():
    started = asyncio.Event()

    async def block_call(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    session = AsyncMock()
    session.call_tool = block_call
    client = AgentMCPClient()
    client._tool_routing["jmp_connect"] = "jumpstarter"
    client._servers["jumpstarter"] = _ServerConnection(
        name="jumpstarter",
        session=session,
        transport="stdio",
        session_id="session-1",
        ticket_id="PERF-1",
    )
    client.pre_call_hook = _JmpCallHook(client).pre_call

    task = asyncio.create_task(
        client.call_tool(
            "jmp_connect",
            {"lease_id": "lease-1"},
            TraceContext(ticket_id="PERF-1"),
        )
    )
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError) as exc_info:
        await task

    assert getattr(exc_info.value, "mcp_audit_recorded", False) is True
    assert [event.lifecycle.state for event in client.audit_events] == [
        LifecycleState.REQUEST_SENT,
        LifecycleState.CANCELLED,
    ]


@pytest.mark.asyncio
async def test_jumpstarter_immediate_dispatch_cancellation_gets_fallback_boundary():
    async def cancel_before_dispatch(*args, **kwargs):
        raise asyncio.CancelledError(mcp_client_module._MCP_PROVIDER_CANCELLATION)

    client = AgentMCPClient()
    client._tool_routing["jmp_connect"] = "jumpstarter"
    client._servers["jumpstarter"] = _ServerConnection(
        name="jumpstarter",
        session=AsyncMock(),
        transport="stdio",
        session_id="session-1",
        ticket_id="PERF-1",
    )
    client.dispatch_internal_tool = cancel_before_dispatch
    client.pre_call_hook = _JmpCallHook(client).pre_call

    with pytest.raises(asyncio.CancelledError):
        await client.call_tool(
            "jmp_connect",
            {"lease_id": "lease-1"},
            TraceContext(ticket_id="PERF-1"),
        )

    assert [event.lifecycle.state for event in client.audit_events] == [
        LifecycleState.CANCELLED,
    ]
    assert client.audit_events[0].outcome == OperationOutcome.CANCELLED


@pytest.mark.asyncio
async def test_internal_dispatch_timeout_has_one_timed_out_boundary():
    started = asyncio.Event()

    async def block_call(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    session = AsyncMock()
    session.call_tool = block_call
    client = AgentMCPClient()
    client._tool_routing["jmp_connect"] = "jumpstarter"
    client._servers["jumpstarter"] = _ServerConnection(
        name="jumpstarter",
        session=session,
        transport="stdio",
        session_id="session-1",
        ticket_id="PERF-1",
    )

    task = asyncio.create_task(
        client.dispatch_internal_tool(
            "jmp_connect",
            {"lease_id": "lease-1"},
            TraceContext(ticket_id="PERF-1"),
        )
    )
    await started.wait()
    task.cancel(mcp_client_module._MCP_TIMEOUT_CANCELLATION)

    with pytest.raises(asyncio.CancelledError):
        await task

    assert [event.lifecycle.state for event in client.audit_events] == [
        LifecycleState.REQUEST_SENT,
        LifecycleState.TIMED_OUT,
    ]
    assert client.audit_events[-1].outcome == OperationOutcome.TIMED_OUT


@pytest.mark.asyncio
async def test_jumpstarter_connect_timeout_has_one_timed_out_boundary(monkeypatch):
    started = asyncio.Event()

    async def block_call(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    session = AsyncMock()
    session.call_tool = block_call
    client = AgentMCPClient()
    client._tool_routing["jmp_connect"] = "jumpstarter"
    client._servers["jumpstarter"] = _ServerConnection(
        name="jumpstarter",
        session=session,
        transport="stdio",
        session_id="session-1",
        ticket_id="PERF-1",
    )
    client.pre_call_hook = _JmpCallHook(client).pre_call
    monkeypatch.setattr(jumpstarter_mcp, "_JMP_CONNECT_TIMEOUT", 0.01)

    with pytest.raises(MCPToolCallError) as exc_info:
        await client.call_tool(
            "jmp_connect",
            {"lease_id": "lease-1"},
            TraceContext(ticket_id="PERF-1"),
        )

    assert exc_info.value.retry_classification == "ambiguous_after_send"
    assert [event.lifecycle.state for event in client.audit_events] == [
        LifecycleState.REQUEST_SENT,
        LifecycleState.TIMED_OUT,
    ]
    assert client.audit_events[-1].outcome == OperationOutcome.TIMED_OUT
    assert (
        client.audit_events[-1].lifecycle.retry_kind == RetryKind.AMBIGUOUS_AFTER_SEND
    )


@pytest.mark.asyncio
async def test_client_audits_hook_rejection_without_request():
    client = AgentMCPClient()
    client._tool_routing["tool"] = "local"
    client._servers["local"] = _ServerConnection(
        name="local",
        session=AsyncMock(),
        transport="stdio",
        session_id="session-1",
        ticket_id="PERF-1",
    )
    client.pre_call_hook = AsyncMock(
        side_effect=MCPToolCallError("invalid request", "validation")
    )

    with pytest.raises(MCPToolCallError):
        await client.call_tool("tool", {}, TraceContext(ticket_id="PERF-1"))

    assert [event.lifecycle.state for event in client.audit_events] == [
        LifecycleState.REJECTED,
    ]
    assert client.audit_events[0].outcome == OperationOutcome.REJECTED
    client._servers["local"].session.call_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_client_preserves_provider_hook_transport_failure_classification():
    client = AgentMCPClient()
    client._tool_routing["tool"] = "local"
    client._servers["local"] = _ServerConnection(
        name="local",
        session=AsyncMock(),
        transport="stdio",
        session_id="session-1",
        ticket_id="PERF-1",
    )
    client.pre_call_hook = AsyncMock(
        return_value=MCPHookResult(
            content="transport unavailable",
            is_error=True,
            retry_classification="transport_before_send",
        )
    )

    with pytest.raises(MCPToolCallError) as exc_info:
        await client.call_tool("tool", {}, TraceContext(ticket_id="PERF-1"))

    assert exc_info.value.retry_classification == "transport_before_send"
    assert [event.lifecycle.state for event in client.audit_events] == [
        LifecycleState.FAILED,
    ]
    assert client.audit_events[0].outcome == OperationOutcome.FAILURE
    assert (
        client.audit_events[0].lifecycle.retry_kind == RetryKind.TRANSPORT_BEFORE_SEND
    )


@pytest.mark.asyncio
async def test_client_audits_missing_connection_as_transport_failure():
    client = AgentMCPClient()
    client._tool_routing["tool"] = "local"

    with pytest.raises(MCPToolCallError) as exc_info:
        await client.call_tool("tool", {}, TraceContext(ticket_id="PERF-1"))

    assert exc_info.value.retry_classification == "transport_before_send"
    assert [event.lifecycle.state for event in client.audit_events] == [
        LifecycleState.FAILED,
    ]
    assert client.audit_events[0].outcome == OperationOutcome.FAILURE
    assert (
        client.audit_events[0].lifecycle.retry_kind == RetryKind.TRANSPORT_BEFORE_SEND
    )


class _BlockingTransport:
    def __init__(self, entered: asyncio.Event, release: asyncio.Event):
        self.entered = entered
        self.release = release

    async def __aenter__(self):
        self.entered.set()
        await self.release.wait()
        return (SimpleNamespace(), SimpleNamespace())

    async def __aexit__(self, *_):
        return False


class _ReadyTransport:
    async def __aenter__(self):
        return (SimpleNamespace(), SimpleNamespace())

    async def __aexit__(self, *_):
        return False


class _TestClientSession:
    list_tools_started: asyncio.Event | None = None
    list_tools_release: asyncio.Event | None = None
    list_tools_error: BaseException | None = None
    list_tools_tools: list[Any] = []

    def __init__(self, *_):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def initialize(self):
        return None

    async def list_tools(self):
        if self.list_tools_started is not None:
            self.list_tools_started.set()
        if self.list_tools_release is not None:
            await self.list_tools_release.wait()
        if self.list_tools_error is not None:
            raise self.list_tools_error
        return SimpleNamespace(tools=self.list_tools_tools)


@pytest.mark.asyncio
async def test_client_cancellation_during_connect_cleans_up_task():
    entered = asyncio.Event()
    release = asyncio.Event()
    client = AgentMCPClient()
    task = asyncio.create_task(
        client._connect_transport(
            "local",
            _BlockingTransport(entered, release),
            transport="stdio",
            endpoint="server.py",
            ticket_id="PERF-1",
        )
    )
    await entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert client._servers == {}
    assert client._tool_routing == {}
    assert [event.lifecycle.state for event in client.audit_events] == [
        LifecycleState.CONNECTING,
        LifecycleState.CANCELLED,
    ]
    assert not any(
        child.get_name() == "mcp:local" and not child.done()
        for child in asyncio.all_tasks()
    )


@pytest.mark.asyncio
async def test_client_timeout_during_connect_is_audited_as_timed_out():
    entered = asyncio.Event()
    release = asyncio.Event()
    client = AgentMCPClient()
    task = asyncio.create_task(
        client._connect_transport(
            "local",
            _BlockingTransport(entered, release),
            transport="stdio",
            endpoint="server.py",
            ticket_id="PERF-1",
        )
    )
    await entered.wait()
    task.cancel(mcp_client_module._MCP_TIMEOUT_CANCELLATION)

    with pytest.raises(asyncio.CancelledError):
        await task

    assert client._servers == {}
    assert client._tool_routing == {}
    assert [event.lifecycle.state for event in client.audit_events] == [
        LifecycleState.CONNECTING,
        LifecycleState.TIMED_OUT,
    ]
    assert client.audit_events[-1].outcome == OperationOutcome.TIMED_OUT


@pytest.mark.asyncio
async def test_client_cancellation_during_initial_list_tools_cleans_up(
    monkeypatch,
):
    started = asyncio.Event()
    release = asyncio.Event()
    _TestClientSession.list_tools_started = started
    _TestClientSession.list_tools_release = release
    _TestClientSession.list_tools_error = None
    _TestClientSession.list_tools_tools = []
    monkeypatch.setattr(mcp_client_module, "ClientSession", _TestClientSession)
    client = AgentMCPClient()

    task = asyncio.create_task(
        client._connect_transport(
            "local",
            _ReadyTransport(),
            transport="stdio",
            endpoint="server.py",
            ticket_id="PERF-1",
        )
    )
    # The transport enters immediately; initialization then reaches list_tools.
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert client._servers == {}
    assert client._tool_routing == {}
    assert [
        (event.action.phase, event.lifecycle.state) for event in client.audit_events
    ] == [
        (None, LifecycleState.CONNECTING),
        ("initialize", LifecycleState.REQUEST_SENT),
        ("initialize", LifecycleState.RESPONSE_RECEIVED),
        (None, LifecycleState.CONNECTED),
        ("list_tools", LifecycleState.REQUEST_SENT),
        ("list_tools", LifecycleState.CANCELLED),
        (None, LifecycleState.DISCONNECTED),
    ]


@pytest.mark.asyncio
async def test_client_failed_initial_list_tools_cleans_up_connection(monkeypatch):
    _TestClientSession.list_tools_started = None
    _TestClientSession.list_tools_release = None
    _TestClientSession.list_tools_error = RuntimeError("tool listing failed")
    _TestClientSession.list_tools_tools = []
    monkeypatch.setattr(mcp_client_module, "ClientSession", _TestClientSession)
    client = AgentMCPClient()

    with pytest.raises(RuntimeError, match="tool listing failed"):
        await client._connect_transport(
            "local",
            _ReadyTransport(),
            transport="stdio",
            endpoint="server.py",
            ticket_id="PERF-1",
        )

    assert client._servers == {}
    assert client._tool_routing == {}
    assert client.audit_events[-2].lifecycle.state == LifecycleState.FAILED
    assert client.audit_events[-2].action.phase == "list_tools"
    assert client.audit_events[-1].lifecycle.state == LifecycleState.DISCONNECTED


@pytest.mark.asyncio
async def test_client_conflicting_initial_list_tools_cleans_up_connected_state(
    monkeypatch,
):
    _TestClientSession.list_tools_started = None
    _TestClientSession.list_tools_release = None
    _TestClientSession.list_tools_error = None
    _TestClientSession.list_tools_tools = [SimpleNamespace(name="existing")]
    monkeypatch.setattr(mcp_client_module, "ClientSession", _TestClientSession)
    client = AgentMCPClient()
    client._tool_routing["existing"] = "already-connected"

    with pytest.raises(ValueError, match="conflicts"):
        await client._connect_transport(
            "local",
            _ReadyTransport(),
            transport="stdio",
            endpoint="server.py",
            ticket_id="PERF-1",
        )

    assert client._servers == {}
    assert client._tool_routing == {"existing": "already-connected"}
    assert [event.lifecycle.state for event in client.audit_events] == [
        LifecycleState.CONNECTING,
        LifecycleState.REQUEST_SENT,
        LifecycleState.RESPONSE_RECEIVED,
        LifecycleState.CONNECTED,
        LifecycleState.REQUEST_SENT,
        LifecycleState.RESPONSE_RECEIVED,
        LifecycleState.DISCONNECTED,
    ]


@pytest.mark.asyncio
async def test_client_audits_connection_initialization_failure():
    class FailingTransport:
        async def __aenter__(self):
            raise RuntimeError("transport unavailable")

        async def __aexit__(self, *_):
            return False

    client = AgentMCPClient()

    with pytest.raises(RuntimeError, match="transport unavailable"):
        await client._connect_transport(
            "local",
            FailingTransport(),
            transport="stdio",
            endpoint="server.py",
            ticket_id="PERF-1",
        )

    assert [event.lifecycle.state for event in client.audit_events] == [
        LifecycleState.CONNECTING,
        LifecycleState.FAILED,
    ]
    assert client.audit_events[-1].action.phase == "initialize"


def test_client_missing_correlation_is_generated_for_boundary_event():
    client = AgentMCPClient()
    connection = _ServerConnection(
        name="local",
        session=None,
        transport="stdio",
        session_id="session-1",
        ticket_id="PERF-1",
    )

    client._record_boundary(
        connection,
        LifecycleState.CONNECTING,
        context=TraceContext(ticket_id="PERF-1"),
    )

    assert len(client.audit_events) == 1
    assert client.audit_events[0].mcp.correlation_request_id


def test_client_invalid_mcp_audit_context_is_fail_open_and_visible(caplog):
    client = AgentMCPClient()
    connection = _ServerConnection(
        name="local",
        session=None,
        transport="stdio",
        session_id="session-1",
        ticket_id="PERF-1",
    )
    secret = "fallback-validation-secret-844"
    malformed = TraceContext.model_construct(
        ticket_id="PERF-1",
        trace_id=secret,
        action_id=secret,
    )

    with caplog.at_level("WARNING", logger="agents.mcp_client"):
        client._record_boundary(
            connection,
            LifecycleState.CONNECTING,
            context=malformed,
        )

    assert len(client.audit_events) == 1
    assert client.audit_events[0].attributes["audit_context_fallback"] is True
    assert client.audit_events[0].lifecycle.state == LifecycleState.CONNECTING
    assert "MCP audit event validation failed" in caplog.text
    assert "dispatch continues" in caplog.text
    assert "error_type=ValidationError" in caplog.text
    assert secret not in caplog.text


def test_client_missing_session_is_fail_open_and_visible(caplog):
    client = AgentMCPClient()
    connection = _ServerConnection(
        name="local",
        session=None,
        transport="stdio",
        session_id=None,
        ticket_id="PERF-1",
    )

    with caplog.at_level("WARNING", logger="agents.mcp_client"):
        client._record_boundary(
            connection,
            LifecycleState.CONNECTING,
            context=TraceContext(ticket_id="PERF-1"),
        )

    assert len(client.audit_events) == 1
    assert client.audit_events[0].attributes["audit_context_fallback"] is True
    assert client.audit_events[0].mcp.session_id
    assert "MCP audit event validation failed" in caplog.text


@pytest.mark.asyncio
async def test_client_rejects_malformed_call_context_before_dispatch():
    session = AsyncMock()
    client = AgentMCPClient()
    client._tool_routing["tool"] = "local"
    client._servers["local"] = _ServerConnection(
        name="local",
        session=session,
        transport="stdio",
        session_id="session-1",
        ticket_id="PERF-1",
    )
    malformed = TraceContext.model_construct(
        ticket_id="PERF-1",
        trace_id="malformed",
        action_id="malformed",
    )

    with pytest.raises(MCPToolCallError) as exc_info:
        await client.call_tool("tool", {}, malformed)

    assert exc_info.value.retry_classification == "validation"
    assert session.call_tool.await_count == 0
    assert [event.lifecycle.state for event in client.audit_events] == [
        LifecycleState.REJECTED,
    ]
    assert client.audit_events[0].attributes["audit_context_fallback"] is True


@pytest.mark.asyncio
async def test_client_redacts_hook_validation_errors_in_result_and_audit():
    secret = "mcp-hook-validation-secret-844"
    get_shared_redactor().register("hook-error", "secret", secret)
    client = AgentMCPClient()
    client._tool_routing["tool"] = "local"
    client._servers["local"] = _ServerConnection(
        name="local",
        session=AsyncMock(),
        transport="stdio",
        session_id="session-1",
        ticket_id="hook-error",
    )
    client.pre_call_hook = AsyncMock(side_effect=MCPToolCallError(secret, "validation"))

    with pytest.raises(MCPToolCallError) as exc_info:
        await client.call_tool("tool", {}, TraceContext(ticket_id="hook-error"))

    assert secret not in str(exc_info.value)
    assert secret not in (client.audit_events[0].error.message or "")
    assert client.audit_events[0].lifecycle.state == LifecycleState.REJECTED


@pytest.mark.asyncio
async def test_client_audits_ticket_scoped_unrouted_validation_failure():
    client = AgentMCPClient()

    with pytest.raises(MCPToolCallError) as exc_info:
        await client.call_tool(
            "missing-tool",
            {},
            TraceContext(ticket_id="PERF-unrouted"),
        )

    assert exc_info.value.retry_classification == "validation"
    assert [event.lifecycle.state for event in client.audit_events] == [
        LifecycleState.REJECTED,
    ]
    assert client.audit_events[0].mcp.server == "unrouted"


@pytest.mark.asyncio
async def test_client_records_retry_failure_and_disconnect_boundaries():
    session = AsyncMock()
    session.call_tool = AsyncMock(
        return_value=SimpleNamespace(
            content=[SimpleNamespace(text="rejected")], isError=True
        )
    )
    client = AgentMCPClient()
    client._tool_routing["tool"] = "local"
    connection = _ServerConnection(
        name="local",
        session=session,
        transport="stdio",
        session_id="session-1",
        ticket_id="PERF-1",
    )
    client._servers["local"] = connection

    with pytest.raises(MCPToolCallError) as exc_info:
        await client.call_tool("tool", {}, TraceContext(ticket_id="PERF-1"))
    assert exc_info.value.retry_classification == "intentional_agent_retry"
    assert [event.lifecycle.state for event in client.audit_events] == [
        LifecycleState.REQUEST_SENT,
        LifecycleState.FAILED,
    ]

    await client.disconnect()
    assert client.audit_events[-1].lifecycle.state == LifecycleState.DISCONNECTED
    assert (
        sum(
            event.lifecycle.state == LifecycleState.DISCONNECTED
            for event in client.audit_events
        )
        == 1
    )


@pytest.mark.asyncio
async def test_client_records_exception_failure_boundary():
    session = AsyncMock()
    session.call_tool = AsyncMock(side_effect=RuntimeError("connection lost"))
    client = AgentMCPClient()
    client._tool_routing["tool"] = "local"
    client._servers["local"] = _ServerConnection(
        name="local",
        session=session,
        transport="stdio",
        session_id="session-1",
        ticket_id="PERF-1",
    )

    with pytest.raises(MCPToolCallError) as exc_info:
        await client.call_tool("tool", {}, TraceContext(ticket_id="PERF-1"))

    assert exc_info.value.retry_classification == "ambiguous_after_send"
    assert [event.lifecycle.state for event in client.audit_events] == [
        LifecycleState.REQUEST_SENT,
        LifecycleState.FAILED,
    ]
    assert (
        client.audit_events[-1].lifecycle.retry_kind == RetryKind.AMBIGUOUS_AFTER_SEND
    )


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


@pytest.mark.asyncio
async def test_protected_call_renews_lease_until_unbounded_handler_returns(
    tmp_path, monkeypatch
):
    """A call longer than its initial lease remains terminally acknowledgeable."""

    # Leave enough margin for the first asyncio.to_thread heartbeat to start
    # on a busy CI runner while still making the handler outlive its lease.
    monkeypatch.setattr(mcp_audit, "_OPERATION_LEASE_TTL_SECONDS", 2.0)
    monkeypatch.setattr(mcp_audit, "_OPERATION_LEASE_RENEW_INTERVAL_SECONDS", 0.05)

    class StoreBackedRegistry:
        def __init__(self, store: TraceStore) -> None:
            self.store = store
            self.owner = "test-service"

        def operation_acquire(self, key: str, request_hash: str, ttl: float) -> dict:
            operation, status = self.store.acquire_operation_result(
                key, request_hash, self.owner, ttl
            )
            return {"status": status, "operation": operation.__dict__}

        def operation_transition(
            self, key: str, action: str, token: int, **kwargs
        ) -> dict:
            if action == "renew":
                operation = self.store.renew_operation(
                    key, self.owner, token, kwargs["ttl_seconds"]
                )
            else:
                operation = {
                    "prepared": self.store.mark_prepared,
                    "side-effect-started": self.store.mark_side_effect_started,
                    "complete": lambda *args: self.store.complete(
                        *args, kwargs.get("descriptor") or {}
                    ),
                    "fail": lambda *args: self.store.fail(
                        *args, kwargs.get("descriptor") or {}
                    ),
                    "indeterminate": lambda *args: self.store.mark_indeterminate(
                        *args, kwargs.get("descriptor") or {}
                    ),
                }[action](key, self.owner, token)
            return {"operation": operation.__dict__}

    with TraceStore(tmp_path / "trace.db") as store:
        middleware = MCPAuditMiddleware(
            "benchmark-agent",
            ticket_id="PERF-1",
            agent_id="benchmark",
            record=lambda _: None,
        )
        middleware._client = StoreBackedRegistry(store)
        request = _request("long-running")
        request.message.meta.model_extra["agentic-perf"].update(
            {"idempotency_key": "long-running", "idempotency_request_hash": "hash"}
        )

        async def slow_handler(_):
            await asyncio.sleep(2.5)
            return ToolResult(content="completed")

        result = await middleware.on_call_tool(
            request.copy(
                message=request.message.model_copy(update={"name": "execute_benchmark"})
            ),
            slow_handler,
        )

        assert result.content[0].text == "completed"
        operation = store.get_operation("long-running")
        assert operation is not None
        assert operation.terminal_outcome == "success"
        reasons = [entry["reason"] for entry in store.operation_history("long-running")]
        assert "renewed" in reasons
        assert reasons[-1] == "terminal"
        assert "rejected:expired_lease" not in reasons
        renewal_events = [
            event
            for event in store.list_events("long-running")
            if event.attributes.get("reason") == "renewed"
        ]
        assert renewal_events
        assert renewal_events[-1].attributes["operation_owner"] == "test-service"
        assert renewal_events[-1].idempotency.fencing_token == 1


@pytest.mark.asyncio
async def test_lost_lease_renewal_acknowledgement_is_indeterminate(
    tmp_path, monkeypatch
):
    """A failed heartbeat cannot be returned as a protected success."""

    monkeypatch.setattr(mcp_audit, "_OPERATION_LEASE_TTL_SECONDS", 0.5)
    monkeypatch.setattr(mcp_audit, "_OPERATION_LEASE_RENEW_INTERVAL_SECONDS", 0.05)
    events = []

    class Registry:
        def __init__(self, store: TraceStore) -> None:
            self.store = store
            self.owner = "test-service"

        def operation_acquire(self, key: str, request_hash: str, ttl: float) -> dict:
            operation, status = self.store.acquire_operation_result(
                key, request_hash, self.owner, ttl
            )
            return {"status": status, "operation": operation.__dict__}

        def operation_transition(
            self, key: str, action: str, token: int, **kwargs
        ) -> dict:
            if action == "renew":
                raise OSError("renewal acknowledgement lost")
            method = {
                "prepared": self.store.mark_prepared,
                "side-effect-started": self.store.mark_side_effect_started,
                "indeterminate": self.store.mark_indeterminate,
            }[action]
            descriptor = kwargs.get("descriptor") or {}
            operation = (
                method(key, self.owner, token)
                if action in {"prepared", "side-effect-started"}
                else method(key, self.owner, token, descriptor)
            )
            return {"operation": operation.__dict__}

    with TraceStore(tmp_path / "trace.db") as store:
        middleware = MCPAuditMiddleware(
            "benchmark-agent",
            ticket_id="PERF-1",
            agent_id="benchmark",
            record=events.append,
        )
        middleware._client = Registry(store)
        request = _request("renewal-loss")
        request.message.meta.model_extra["agentic-perf"].update(
            {"idempotency_key": "renewal-loss", "idempotency_request_hash": "hash"}
        )

        async def slow_handler(_):
            await asyncio.sleep(0.2)
            return ToolResult(content="completed")

        with pytest.raises(McpError, match="lease renewal"):
            await middleware.on_call_tool(
                request.copy(
                    message=request.message.model_copy(
                        update={"name": "execute_benchmark"}
                    )
                ),
                slow_handler,
            )

        operation = store.get_operation("renewal-loss")
        assert operation is not None
        assert operation.terminal_outcome == "indeterminate"
        assert store.operation_history("renewal-loss")[-1]["reason"] == "terminal"
        assert events[-1].lifecycle.state == LifecycleState.INDETERMINATE


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
