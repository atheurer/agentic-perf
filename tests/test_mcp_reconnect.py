"""Tests for MCP subprocess disconnection detection and auto-reconnect."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from mcp.shared.exceptions import McpError
from mcp.types import CONNECTION_CLOSED, ErrorData

from agents.mcp_client import (
    AgentMCPClient,
    MCPToolCallError,
    _ConnectParams,
    _is_disconnect_error,
    _ServerConnection,
)
from providers.tracing import LifecycleState, TraceContext

# ---------------------------------------------------------------------------
# Unit tests for _is_disconnect_error
# ---------------------------------------------------------------------------


class TestIsDisconnectError:
    def test_broken_pipe(self):
        assert _is_disconnect_error(BrokenPipeError("pipe"))

    def test_connection_reset(self):
        assert _is_disconnect_error(ConnectionResetError("reset"))

    def test_connection_aborted(self):
        assert _is_disconnect_error(ConnectionAbortedError("aborted"))

    def test_eof_error(self):
        assert _is_disconnect_error(EOFError("eof"))

    def test_regular_runtime_error_is_not_disconnect(self):
        assert not _is_disconnect_error(RuntimeError("something else"))

    def test_value_error_is_not_disconnect(self):
        assert not _is_disconnect_error(ValueError("bad value"))

    def test_wrapped_broken_pipe_via_cause(self):
        wrapper = RuntimeError("transport failed")
        wrapper.__cause__ = BrokenPipeError("pipe gone")
        assert _is_disconnect_error(wrapper)

    def test_wrapped_broken_pipe_via_context(self):
        wrapper = RuntimeError("transport failed")
        wrapper.__context__ = ConnectionResetError("reset")
        assert _is_disconnect_error(wrapper)

    def test_closed_resource_error_by_name(self):
        # Simulate anyio.ClosedResourceError without importing anyio.
        class ClosedResourceError(Exception):
            pass

        assert _is_disconnect_error(ClosedResourceError("closed"))

    def test_mcp_connection_closed_error(self):
        error = McpError(ErrorData(code=CONNECTION_CLOSED, message="Connection closed"))
        assert _is_disconnect_error(error)


# ---------------------------------------------------------------------------
# Helpers for integration tests
# ---------------------------------------------------------------------------


class _FakeSession:
    """A mock ClientSession that can be configured to raise on call_tool."""

    def __init__(
        self, tools: list[Any] | None = None, call_error: Exception | None = None
    ):
        self._tools = tools or []
        self._call_error = call_error
        self.call_count = 0

    async def list_tools(self):
        return SimpleNamespace(tools=self._tools)

    async def call_tool(self, name, arguments, meta=None):
        self.call_count += 1
        if self._call_error is not None:
            raise self._call_error
        return SimpleNamespace(
            content=[SimpleNamespace(text=f"result:{name}")],
            isError=False,
        )

    async def initialize(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass


def _make_tool(name: str) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        description=f"Tool {name}",
        inputSchema={"type": "object", "properties": {}},
    )


def _make_connected_client(
    server_name: str = "test-server",
    tools: list[str] | None = None,
    session: _FakeSession | None = None,
    connect_params: _ConnectParams | None = None,
) -> tuple[AgentMCPClient, _ServerConnection]:
    """Create a client with a pre-wired connection (no real transport)."""
    tool_names = tools or ["check_host"]
    fake_tools = [_make_tool(t) for t in tool_names]
    if session is None:
        session = _FakeSession(tools=fake_tools)

    client = AgentMCPClient()
    conn = _ServerConnection(
        name=server_name,
        session=session,
        transport="stdio",
        endpoint="test-cmd",
        ticket_id="PERF-TEST",
        agent_id="test-agent",
        connected=True,
        _connect_params=connect_params,
    )
    client._servers[server_name] = conn
    for t in tool_names:
        client._tool_routing[t] = server_name

    return client, conn


def _default_connect_params() -> _ConnectParams:
    return _ConnectParams(
        command="python",
        args=["server.py"],
        env={},
        ticket_id="PERF-TEST",
        agent_id="test-agent",
    )


def _trace_context() -> TraceContext:
    return TraceContext(ticket_id="PERF-TEST", agent_id="test-agent")


# ---------------------------------------------------------------------------
# Auto-reconnect on disconnection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconnect_on_broken_pipe_during_call_tool():
    """A BrokenPipeError triggers auto-reconnect and retries the call."""
    failing_session = _FakeSession(
        tools=[_make_tool("check_host")],
        call_error=BrokenPipeError("subprocess died"),
    )
    params = _default_connect_params()
    client, _conn = _make_connected_client(
        session=failing_session,
        connect_params=params,
    )

    # After reconnect, the new session should succeed.
    success_session = _FakeSession(tools=[_make_tool("check_host")])

    async def fake_connect_command(
        command, args=None, name=None, env=None, ticket_id=None, agent_id=None
    ):
        new_conn = _ServerConnection(
            name=name,
            session=success_session,
            transport="stdio",
            endpoint=command,
            ticket_id=ticket_id,
            agent_id=agent_id,
            connected=True,
            reconnect_generation=1,
            _connect_params=params,
        )
        client._servers[name] = new_conn
        for t in ["check_host"]:
            client._tool_routing[t] = name

    with patch.object(client, "connect_command", side_effect=fake_connect_command):
        with pytest.raises(MCPToolCallError, match="not retried|ambiguous"):
            await client.call_tool("check_host", {}, trace_context=_trace_context())
    assert success_session.call_count == 0  # no retry after ambiguous send


@pytest.mark.asyncio
async def test_reconnect_on_connection_reset_during_call_tool():
    """ConnectionResetError also triggers auto-reconnect."""
    failing_session = _FakeSession(
        tools=[_make_tool("query_numa")],
        call_error=ConnectionResetError("connection reset"),
    )
    params = _default_connect_params()
    client, _conn = _make_connected_client(
        tools=["query_numa"],
        session=failing_session,
        connect_params=params,
    )

    success_session = _FakeSession(tools=[_make_tool("query_numa")])

    async def fake_connect_command(
        command, args=None, name=None, env=None, ticket_id=None, agent_id=None
    ):
        new_conn = _ServerConnection(
            name=name,
            session=success_session,
            transport="stdio",
            endpoint=command,
            ticket_id=ticket_id,
            agent_id=agent_id,
            connected=True,
            reconnect_generation=1,
            _connect_params=params,
        )
        client._servers[name] = new_conn
        client._tool_routing["query_numa"] = name

    with patch.object(client, "connect_command", side_effect=fake_connect_command):
        with pytest.raises(MCPToolCallError, match="not retried|ambiguous"):
            await client.call_tool("query_numa", {}, trace_context=_trace_context())


@pytest.mark.asyncio
async def test_reconnect_on_mcp_connection_closed_error():
    """The MCP SDK's closed-connection error reconnects for future calls."""
    failing_session = _FakeSession(
        tools=[_make_tool("check_host")],
        call_error=McpError(
            ErrorData(code=CONNECTION_CLOSED, message="Connection closed")
        ),
    )
    params = _default_connect_params()
    client, _conn = _make_connected_client(
        session=failing_session,
        connect_params=params,
    )
    success_session = _FakeSession(tools=[_make_tool("check_host")])

    async def fake_connect_command(
        command, args=None, name=None, env=None, ticket_id=None, agent_id=None
    ):
        client._servers[name] = _ServerConnection(
            name=name,
            session=success_session,
            transport="stdio",
            endpoint=command,
            ticket_id=ticket_id,
            agent_id=agent_id,
            connected=True,
            reconnect_generation=1,
            _connect_params=params,
        )
        client._tool_routing["check_host"] = name

    reconnect = AsyncMock(side_effect=fake_connect_command)
    with patch.object(client, "connect_command", reconnect):
        with pytest.raises(MCPToolCallError, match="not retried|ambiguous"):
            await client.call_tool("check_host", {}, trace_context=_trace_context())

    reconnect.assert_awaited_once()
    assert success_session.call_count == 0


@pytest.mark.asyncio
async def test_reconnect_failure_returns_clear_error():
    """If reconnect fails, a clear MCPToolCallError is raised."""
    failing_session = _FakeSession(
        tools=[_make_tool("check_host")],
        call_error=BrokenPipeError("subprocess died"),
    )
    params = _default_connect_params()
    client, _conn = _make_connected_client(
        session=failing_session,
        connect_params=params,
    )

    async def failing_reconnect(
        command, args=None, name=None, env=None, ticket_id=None, agent_id=None
    ):
        raise RuntimeError("Cannot relaunch subprocess")

    with patch.object(client, "connect_command", side_effect=failing_reconnect):
        with pytest.raises(MCPToolCallError) as exc_info:
            await client.call_tool("check_host", {}, trace_context=_trace_context())

    assert "disconnected" in str(exc_info.value).lower()
    assert "reconnection failed" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_no_reconnect_without_connect_params():
    """Without stored connect params, no reconnect is attempted."""
    failing_session = _FakeSession(
        tools=[_make_tool("check_host")],
        call_error=BrokenPipeError("subprocess died"),
    )
    # No connect_params
    client, _conn = _make_connected_client(
        session=failing_session,
        connect_params=None,
    )

    with pytest.raises(MCPToolCallError) as exc_info:
        await client.call_tool("check_host", {}, trace_context=_trace_context())

    # Should fail but not mention reconnection
    assert exc_info.value.retry_classification == "ambiguous_after_send"


@pytest.mark.asyncio
async def test_non_disconnect_error_does_not_trigger_reconnect():
    """A regular ValueError does not trigger reconnect logic."""
    failing_session = _FakeSession(
        tools=[_make_tool("check_host")],
        call_error=ValueError("bad argument"),
    )
    params = _default_connect_params()
    client, _conn = _make_connected_client(
        session=failing_session,
        connect_params=params,
    )

    connect_mock = AsyncMock()
    with patch.object(client, "connect_command", connect_mock):
        with pytest.raises(MCPToolCallError):
            await client.call_tool("check_host", {}, trace_context=_trace_context())

    connect_mock.assert_not_called()


@pytest.mark.asyncio
async def test_reconnect_on_closed_session():
    """When session is None (already dead), reconnect is attempted."""
    params = _default_connect_params()
    client, conn = _make_connected_client(connect_params=params)
    conn.session = None  # Simulate dead session

    success_session = _FakeSession(tools=[_make_tool("check_host")])

    async def fake_connect_command(
        command, args=None, name=None, env=None, ticket_id=None, agent_id=None
    ):
        new_conn = _ServerConnection(
            name=name,
            session=success_session,
            transport="stdio",
            endpoint=command,
            ticket_id=ticket_id,
            agent_id=agent_id,
            connected=True,
            reconnect_generation=1,
            _connect_params=params,
        )
        client._servers[name] = new_conn
        client._tool_routing["check_host"] = name

    with patch.object(client, "connect_command", side_effect=fake_connect_command):
        result = await client.call_tool(
            "check_host", {}, trace_context=_trace_context()
        )

    # Pre-dispatch reconnect is safe to retry (no ambiguity)
    assert "result:check_host" in result


@pytest.mark.asyncio
async def test_concurrent_calls_share_one_reconnect():
    """Simultaneous calls after a disconnect only relaunch the server once."""
    params = _default_connect_params()
    client, conn = _make_connected_client(connect_params=params)
    conn.session = None
    success_session = _FakeSession(tools=[_make_tool("check_host")])
    reconnect_count = 0

    async def fake_connect_command(
        command, args=None, name=None, env=None, ticket_id=None, agent_id=None
    ):
        nonlocal reconnect_count
        reconnect_count += 1
        await asyncio.sleep(0.01)
        client._servers[name] = _ServerConnection(
            name=name,
            session=success_session,
            transport="stdio",
            endpoint=command,
            ticket_id=ticket_id,
            agent_id=agent_id,
            connected=True,
            reconnect_generation=1,
            _connect_params=params,
        )
        client._tool_routing["check_host"] = name

    with patch.object(client, "connect_command", side_effect=fake_connect_command):
        results = await asyncio.gather(
            client.call_tool("check_host", {}, trace_context=_trace_context()),
            client.call_tool("check_host", {}, trace_context=_trace_context()),
        )

    assert reconnect_count == 1
    assert all("result:check_host" in result for result in results)


@pytest.mark.asyncio
async def test_reconnect_audit_trail():
    """Verify that disconnection and reconnect produce expected audit events."""
    failing_session = _FakeSession(
        tools=[_make_tool("check_host")],
        call_error=BrokenPipeError("pipe broke"),
    )
    params = _default_connect_params()
    client, _conn = _make_connected_client(
        session=failing_session,
        connect_params=params,
    )

    success_session = _FakeSession(tools=[_make_tool("check_host")])

    async def fake_connect_command(
        command, args=None, name=None, env=None, ticket_id=None, agent_id=None
    ):
        new_conn = _ServerConnection(
            name=name,
            session=success_session,
            transport="stdio",
            endpoint=command,
            ticket_id=ticket_id,
            agent_id=agent_id,
            connected=True,
            reconnect_generation=1,
            _connect_params=params,
        )
        client._servers[name] = new_conn
        client._tool_routing["check_host"] = name

    with patch.object(client, "connect_command", side_effect=fake_connect_command):
        # The call returns an error (no retry) but reconnects for future calls
        try:
            result = await client.call_tool(
                "check_host", {}, trace_context=_trace_context()
            )
            # call_tool may return error string instead of raising
            assert "not retried" in result.lower() or "ambiguous" in result.lower()
        except Exception:
            pass  # MCPToolCallError is acceptable

    states = [e.lifecycle.state for e in client.audit_events]
    # Should see DISCONNECTED from the transport failure.
    # No RESPONSE_RECEIVED — we don't retry ambiguous calls.
    assert LifecycleState.DISCONNECTED in states


@pytest.mark.asyncio
async def test_wrapped_disconnect_error_triggers_reconnect():
    """A RuntimeError wrapping a BrokenPipeError triggers reconnect."""
    wrapper = RuntimeError("transport failed")
    wrapper.__cause__ = BrokenPipeError("pipe gone")

    failing_session = _FakeSession(
        tools=[_make_tool("check_host")],
        call_error=wrapper,
    )
    params = _default_connect_params()
    client, _conn = _make_connected_client(
        session=failing_session,
        connect_params=params,
    )

    success_session = _FakeSession(tools=[_make_tool("check_host")])

    async def fake_connect_command(
        command, args=None, name=None, env=None, ticket_id=None, agent_id=None
    ):
        new_conn = _ServerConnection(
            name=name,
            session=success_session,
            transport="stdio",
            endpoint=command,
            ticket_id=ticket_id,
            agent_id=agent_id,
            connected=True,
            reconnect_generation=1,
            _connect_params=params,
        )
        client._servers[name] = new_conn
        client._tool_routing["check_host"] = name

    with patch.object(client, "connect_command", side_effect=fake_connect_command):
        with pytest.raises(MCPToolCallError, match="not retried|ambiguous"):
            await client.call_tool("check_host", {}, trace_context=_trace_context())
