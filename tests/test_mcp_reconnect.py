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
from providers.tracing import LifecycleState, OperationOutcome, RetryKind, TraceContext

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


def test_connect_params_repr_hides_argument_values():
    params = _ConnectParams(
        command="mcp-server",
        args=["--api-token", "credential-value"],
        env={"TOKEN": "environment-secret"},
        ticket_id="PERF-TEST",
        agent_id="test-agent",
    )

    rendered = repr(params)

    assert "args=<2 args>" in rendered
    assert "--api-token" not in rendered
    assert "credential-value" not in rendered
    assert "environment-secret" not in rendered


@pytest.mark.asyncio
async def test_connect_command_snapshots_mutable_args(monkeypatch):
    client = AgentMCPClient()
    caller_args = ["--mode", "original"]
    transport_started = asyncio.Event()
    release_transport = asyncio.Event()
    launched_args = []

    class _ArgsTransport:
        def __init__(self, args):
            self.args = args

        async def __aenter__(self):
            transport_started.set()
            await release_transport.wait()
            launched_args.extend(self.args)
            return (SimpleNamespace(), SimpleNamespace())

        async def __aexit__(self, *_):
            return False

    class _ArgsSession:
        def __init__(self, *_args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def initialize(self):
            return None

        async def list_tools(self):
            return SimpleNamespace(tools=[])

    monkeypatch.setattr(
        "agents.mcp_client.audited_stdio_client",
        lambda params, *_args, **_kwargs: _ArgsTransport(params.args),
    )
    monkeypatch.setattr("agents.mcp_client.ClientSession", _ArgsSession)

    connect_task = asyncio.create_task(
        client.connect_command(
            command="mcp-server",
            args=caller_args,
            name="args-server",
            env={},
        )
    )
    try:
        await asyncio.wait_for(transport_started.wait(), timeout=1)
        caller_args[:] = ["--mode", "mutated", "--new-flag"]
        release_transport.set()
        await connect_task

        assert launched_args == ["--mode", "original"]
        assert client._servers["args-server"]._connect_params.args == [
            "--mode",
            "original",
        ]
    finally:
        release_transport.set()
        await asyncio.gather(connect_task, return_exceptions=True)
        await client.disconnect()


@pytest.mark.asyncio
async def test_client_can_connect_again_after_disconnect(monkeypatch):
    class _ReadyTransport:
        async def __aenter__(self):
            return (SimpleNamespace(), SimpleNamespace())

        async def __aexit__(self, *_):
            return False

    class _ReadySession:
        def __init__(self, *_args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def initialize(self):
            return None

        async def list_tools(self):
            return SimpleNamespace(tools=[])

    monkeypatch.setattr(
        "agents.mcp_client.audited_stdio_client",
        lambda *_args, **_kwargs: _ReadyTransport(),
    )
    monkeypatch.setattr("agents.mcp_client.ClientSession", _ReadySession)
    client = AgentMCPClient()

    await client.connect_command("first-server", name="first", env={})
    await client.disconnect()
    assert client._closing is False

    await client.connect_command("second-server", name="second", env={})
    assert client._servers["second"].connected is True
    assert client._closing is False
    await client.disconnect()


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
        command,
        args=None,
        name=None,
        env=None,
        ticket_id=None,
        agent_id=None,
        **_kwargs,
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
            _reconnect_origin_session_id=client._servers[name].session_id,
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
        command,
        args=None,
        name=None,
        env=None,
        ticket_id=None,
        agent_id=None,
        **_kwargs,
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
            _reconnect_origin_session_id=client._servers[name].session_id,
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
        command,
        args=None,
        name=None,
        env=None,
        ticket_id=None,
        agent_id=None,
        **_kwargs,
    ):
        old_conn = client._servers[name]
        client._servers[name] = _ServerConnection(
            name=name,
            session=success_session,
            transport="stdio",
            endpoint=command,
            ticket_id=ticket_id,
            agent_id=agent_id,
            connected=True,
            reconnect_generation=1,
            _reconnect_origin_session_id=old_conn.session_id,
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
async def test_reconnect_failure_returns_clear_error_without_logging_details(caplog):
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
        command,
        args=None,
        name=None,
        env=None,
        ticket_id=None,
        agent_id=None,
        **_kwargs,
    ):
        raise RuntimeError("server echoed --password=secret-value")

    with caplog.at_level("WARNING", logger="agents.mcp_client"):
        with patch.object(client, "connect_command", side_effect=failing_reconnect):
            with pytest.raises(MCPToolCallError) as exc_info:
                await client.call_tool("check_host", {}, trace_context=_trace_context())

    assert "disconnected" in str(exc_info.value).lower()
    assert "reconnection failed" in str(exc_info.value).lower()
    assert "error_type=RuntimeError" in caplog.text
    assert "server echoed" not in caplog.text
    assert "--password" not in caplog.text
    assert "secret-value" not in caplog.text


@pytest.mark.asyncio
async def test_failed_reconnect_keeps_route_for_a_later_retry(monkeypatch):
    params = _default_connect_params()
    client, old_conn = _make_connected_client(connect_params=params)
    old_conn.session = None
    transport_enter_count = 0
    session_count = 0

    class _RetryTransport:
        async def __aenter__(self):
            nonlocal transport_enter_count
            transport_enter_count += 1
            if transport_enter_count == 1:
                raise RuntimeError("temporary reconnect startup failure")
            return (SimpleNamespace(), SimpleNamespace())

        async def __aexit__(self, *_):
            return False

    class _RetrySession:
        def __init__(self, *_args):
            nonlocal session_count
            session_count += 1

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def initialize(self):
            return None

        async def list_tools(self):
            return SimpleNamespace(tools=[_make_tool("check_host")])

        async def call_tool(self, name, arguments, meta=None):
            return SimpleNamespace(
                content=[SimpleNamespace(text="recovered result")],
                isError=False,
            )

    monkeypatch.setattr(
        "agents.mcp_client.audited_stdio_client",
        lambda *_args, **_kwargs: _RetryTransport(),
    )
    monkeypatch.setattr("agents.mcp_client.ClientSession", _RetrySession)

    try:
        with pytest.raises(
            MCPToolCallError, match="session closed before tool dispatch"
        ):
            await client.call_tool("check_host", {}, trace_context=_trace_context())

        assert client._servers[old_conn.name] is old_conn
        assert old_conn.session is None
        assert old_conn.connected is False
        assert old_conn._connect_params is params
        assert client._tool_routing == {"check_host": old_conn.name}
        assert await client.list_tools() == []

        result = await client.call_tool(
            "check_host", {}, trace_context=_trace_context()
        )

        assert result == "recovered result"
        replacement = client._servers[old_conn.name]
        assert replacement is not old_conn
        assert replacement.connected is True
        assert client._tool_routing == {"check_host": old_conn.name}
        assert transport_enter_count == 2
        assert session_count == 1
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_reconnect_stops_old_transport_holder_before_relaunch(monkeypatch):
    client = AgentMCPClient()
    params = _default_connect_params()
    old_session_exited = asyncio.Event()

    class _HolderTransport:
        async def __aenter__(self):
            return (SimpleNamespace(), SimpleNamespace())

        async def __aexit__(self, *_):
            return False

    class _HolderSession:
        def __init__(self, *_args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            old_session_exited.set()
            return False

        async def initialize(self):
            return None

        async def list_tools(self):
            return SimpleNamespace(tools=[_make_tool("check_host")])

    monkeypatch.setattr(
        "agents.mcp_client.audited_stdio_client",
        lambda *_args, **_kwargs: _HolderTransport(),
    )
    monkeypatch.setattr("agents.mcp_client.ClientSession", _HolderSession)

    await client._connect_transport(
        "test-server",
        _HolderTransport(),
        transport="stdio",
        endpoint="old-server.py",
        connect_params=params,
    )
    old_conn = client._servers["test-server"]
    old_task = old_conn._task
    assert old_task is not None and not old_task.done()
    old_conn.session = None

    async def failing_relaunch(*_args, **_kwargs):
        assert old_task.done()
        assert old_session_exited.is_set()
        assert client._servers["test-server"] is old_conn
        assert client._tool_routing["check_host"] == "test-server"
        raise RuntimeError("relaunch failed")

    try:
        with patch.object(client, "connect_command", side_effect=failing_relaunch):
            assert await client._reconnect_server(old_conn) is False

        assert old_task.done()
        assert old_conn.session is None
        assert client._servers["test-server"] is old_conn
        assert client._tool_routing["check_host"] == "test-server"
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_concurrent_call_waits_for_reconnect_with_route_reserved(monkeypatch):
    params = _default_connect_params()
    client, old_conn = _make_connected_client(connect_params=params)
    old_conn.session = None
    list_tools_started = asyncio.Event()
    finish_list_tools = asyncio.Event()

    class _ReconnectTransport:
        async def __aenter__(self):
            return (SimpleNamespace(), SimpleNamespace())

        async def __aexit__(self, *_):
            return False

    class _ReconnectSession:
        def __init__(self, *_args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def initialize(self):
            return None

        async def list_tools(self):
            list_tools_started.set()
            await finish_list_tools.wait()
            return SimpleNamespace(tools=[_make_tool("check_host")])

        async def call_tool(self, name, arguments, meta=None):
            return SimpleNamespace(
                content=[SimpleNamespace(text=f"recovered:{name}")],
                isError=False,
            )

    monkeypatch.setattr(
        "agents.mcp_client.audited_stdio_client",
        lambda *_args, **_kwargs: _ReconnectTransport(),
    )
    monkeypatch.setattr("agents.mcp_client.ClientSession", _ReconnectSession)

    first_call = asyncio.create_task(
        client.call_tool("check_host", {}, trace_context=_trace_context())
    )
    second_call = None
    try:
        await asyncio.wait_for(list_tools_started.wait(), timeout=1)
        assert client._servers[old_conn.name] is old_conn
        assert client._tool_routing["check_host"] == old_conn.name
        assert await client.list_tools() == []

        second_call = asyncio.create_task(
            client.call_tool("check_host", {}, trace_context=_trace_context())
        )
        await asyncio.sleep(0)
        assert not second_call.done()
        assert client._servers[old_conn.name] is old_conn
        assert client._tool_routing["check_host"] == old_conn.name

        finish_list_tools.set()
        results = await asyncio.gather(first_call, second_call)
        assert results == ["recovered:check_host", "recovered:check_host"]
        assert client._servers[old_conn.name] is not old_conn
        assert client._tool_routing["check_host"] == old_conn.name
    finally:
        finish_list_tools.set()
        await asyncio.gather(
            first_call,
            *([second_call] if second_call is not None else []),
            return_exceptions=True,
        )
        await client.disconnect()


@pytest.mark.asyncio
async def test_reconnect_reserves_tool_route_until_atomic_replacement(monkeypatch):
    params = _default_connect_params()
    client, old_conn = _make_connected_client(
        server_name="server-a",
        tools=["shared_tool"],
        connect_params=params,
    )
    old_conn.session = None
    reconnect_list_tools_started = asyncio.Event()
    finish_reconnect_list_tools = asyncio.Event()

    class _KeyedTransport:
        def __init__(self, server_key):
            self.server_key = server_key

        async def __aenter__(self):
            stream = SimpleNamespace(server_key=self.server_key)
            return (stream, SimpleNamespace())

        async def __aexit__(self, *_):
            return False

    class _KeyedSession:
        def __init__(self, read_stream, *_args):
            self.server_key = read_stream.server_key

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def initialize(self):
            return None

        async def list_tools(self):
            if self.server_key == params.command:
                reconnect_list_tools_started.set()
                await finish_reconnect_list_tools.wait()
            return SimpleNamespace(tools=[_make_tool("shared_tool")])

    monkeypatch.setattr(
        "agents.mcp_client.audited_stdio_client",
        lambda server_params, *_args, **_kwargs: _KeyedTransport(server_params.command),
    )
    monkeypatch.setattr("agents.mcp_client.ClientSession", _KeyedSession)

    reconnect_task = asyncio.create_task(client._reconnect_server(old_conn))
    try:
        await asyncio.wait_for(reconnect_list_tools_started.wait(), timeout=1)
        assert client._servers[old_conn.name] is old_conn
        assert client._tool_routing["shared_tool"] == old_conn.name

        with pytest.raises(ValueError, match="conflicts"):
            await client.connect_command(
                command="server-b-command",
                args=["server.py"],
                name="server-b",
                env={},
            )

        assert client._servers[old_conn.name] is old_conn
        assert "server-b" not in client._servers
        assert client._tool_routing["shared_tool"] == old_conn.name

        finish_reconnect_list_tools.set()
        assert await reconnect_task is True
        assert client._servers[old_conn.name] is not old_conn
        assert client._tool_routing == {"shared_tool": old_conn.name}
    finally:
        finish_reconnect_list_tools.set()
        await asyncio.gather(reconnect_task, return_exceptions=True)
        await client.disconnect()


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
        command,
        args=None,
        name=None,
        env=None,
        ticket_id=None,
        agent_id=None,
        **_kwargs,
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
            _reconnect_origin_session_id=conn.session_id,
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
async def test_pre_send_reconnect_does_not_dispatch_a_removed_tool(monkeypatch):
    params = _default_connect_params()
    client, old_conn = _make_connected_client(connect_params=params)
    old_conn.session = None
    replacement_call_count = 0

    class _ReadyTransport:
        async def __aenter__(self):
            return (SimpleNamespace(), SimpleNamespace())

        async def __aexit__(self, *_):
            return False

    class _ReplacementSession:
        def __init__(self, *_args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def initialize(self):
            return None

        async def list_tools(self):
            return SimpleNamespace(tools=[_make_tool("replacement_tool")])

        async def call_tool(self, name, arguments, meta=None):
            nonlocal replacement_call_count
            replacement_call_count += 1
            return SimpleNamespace(
                content=[SimpleNamespace(text=f"unexpected:{name}")],
                isError=False,
            )

    monkeypatch.setattr(
        "agents.mcp_client.audited_stdio_client",
        lambda *_args, **_kwargs: _ReadyTransport(),
    )
    monkeypatch.setattr("agents.mcp_client.ClientSession", _ReplacementSession)

    try:
        with pytest.raises(MCPToolCallError) as exc_info:
            await client.call_tool("check_host", {}, trace_context=_trace_context())

        replacement = client._servers[old_conn.name]
        assert replacement._reconnect_origin_session_id == old_conn.session_id
        assert client._tool_routing == {"replacement_tool": old_conn.name}
        assert exc_info.value.retry_classification == "transport_before_send"
        assert replacement_call_count == 0
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_direct_call_cancellation_during_reconnect_is_pre_send():
    reconnect_started = asyncio.Event()
    params = _default_connect_params()
    client, conn = _make_connected_client(connect_params=params)
    conn.session = None

    async def block_reconnect(*args, **kwargs):
        reconnect_started.set()
        await asyncio.Event().wait()

    with patch.object(client, "connect_command", side_effect=block_reconnect):
        task = asyncio.create_task(
            client.call_tool("check_host", {}, trace_context=_trace_context())
        )
        await reconnect_started.wait()
        task.cancel()

        with pytest.raises(asyncio.CancelledError) as exc_info:
            await task

    assert getattr(exc_info.value, "mcp_audit_recorded", False) is True
    assert [event.lifecycle.state for event in client.audit_events] == [
        LifecycleState.CANCELLED,
    ]
    assert (
        client.audit_events[-1].lifecycle.retry_kind == RetryKind.TRANSPORT_BEFORE_SEND
    )


@pytest.mark.asyncio
async def test_concurrent_calls_share_one_reconnect():
    """Simultaneous calls after a disconnect only relaunch the server once."""
    params = _default_connect_params()
    client, conn = _make_connected_client(connect_params=params)
    conn.session = None
    success_session = _FakeSession(tools=[_make_tool("check_host")])
    reconnect_count = 0

    async def fake_connect_command(
        command,
        args=None,
        name=None,
        env=None,
        ticket_id=None,
        agent_id=None,
        **_kwargs,
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
            _reconnect_origin_session_id=conn.session_id,
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
        command,
        args=None,
        name=None,
        env=None,
        ticket_id=None,
        agent_id=None,
        **_kwargs,
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
            _reconnect_origin_session_id=client._servers[name].session_id,
            _connect_params=params,
        )
        client._servers[name] = new_conn
        client._tool_routing["check_host"] = name

    with patch.object(client, "connect_command", side_effect=fake_connect_command):
        with pytest.raises(MCPToolCallError) as exc_info:
            await client.call_tool("check_host", {}, trace_context=_trace_context())

    assert exc_info.value.retry_classification == "ambiguous_after_send"
    states = [event.lifecycle.state for event in client.audit_events]
    assert states == [
        LifecycleState.REQUEST_SENT,
        LifecycleState.DISCONNECTED,
        LifecycleState.FAILED,
    ]
    terminal_events = [
        event
        for event in client.audit_events
        if event.lifecycle.state
        in {
            LifecycleState.FAILED,
            LifecycleState.CANCELLED,
            LifecycleState.TIMED_OUT,
            LifecycleState.REJECTED,
            LifecycleState.SHORT_CIRCUITED,
            LifecycleState.RESPONSE_RECEIVED,
        }
    ]
    assert len(terminal_events) == 1
    assert terminal_events[0].outcome == OperationOutcome.FAILURE
    assert terminal_events[0].lifecycle.retry_kind == RetryKind.AMBIGUOUS_AFTER_SEND


@pytest.mark.asyncio
async def test_disconnect_closes_connection_installed_by_inflight_reconnect():
    reconnect_started = asyncio.Event()
    reconnect_cancelled = asyncio.Event()
    finish_reconnect = asyncio.Event()
    params = _default_connect_params()
    client, conn = _make_connected_client(connect_params=params)
    conn.session = None
    installed_connections = []

    async def install_reconnected_server(
        command,
        args=None,
        name=None,
        env=None,
        ticket_id=None,
        agent_id=None,
        **_kwargs,
    ):
        reconnect_started.set()
        try:
            await finish_reconnect.wait()
        except asyncio.CancelledError:
            # Model startup completing at the same time shutdown cancels it.
            reconnect_cancelled.set()
            await finish_reconnect.wait()
        new_conn = _ServerConnection(
            name=name,
            session=_FakeSession(tools=[_make_tool("check_host")]),
            transport="stdio",
            endpoint=command,
            ticket_id=ticket_id,
            agent_id=agent_id,
            connected=True,
            reconnect_generation=1,
            _reconnect_origin_session_id=client._servers[name].session_id,
            _connect_params=params,
        )
        installed_connections.append(new_conn)
        client._servers[name] = new_conn
        client._tool_routing["check_host"] = name

    with patch.object(
        client, "connect_command", side_effect=install_reconnected_server
    ):
        reconnect_task = asyncio.create_task(client._reconnect_server(conn))
        await asyncio.wait_for(reconnect_started.wait(), timeout=1)
        disconnect_task = asyncio.create_task(client.disconnect())
        await asyncio.wait_for(reconnect_cancelled.wait(), timeout=1)
        closing_started = client._closing
        disconnect_waiting = not disconnect_task.done()

        finish_reconnect.set()
        reconnect_succeeded = await reconnect_task
        await disconnect_task

    assert closing_started is True
    assert disconnect_waiting is True
    assert reconnect_succeeded is False
    assert len(installed_connections) == 1
    new_conn = installed_connections[0]
    assert new_conn.connected is False
    assert new_conn._shutdown.is_set()
    assert client._servers == {}
    assert client._tool_routing == {}


@pytest.mark.asyncio
async def test_reconnect_waiting_when_disconnect_starts_does_not_relaunch():
    params = _default_connect_params()
    client, conn = _make_connected_client(connect_params=params)
    conn.session = None
    connect_command = AsyncMock()
    client.connect_command = connect_command

    await conn._reconnect_lock.acquire()
    reconnect_task = asyncio.create_task(client._reconnect_server(conn))
    await asyncio.sleep(0)
    try:
        await client.disconnect()
    finally:
        conn._reconnect_lock.release()

    assert await reconnect_task is False
    connect_command.assert_not_awaited()
    assert client._servers == {}
    assert conn._shutdown.is_set()


@pytest.mark.asyncio
async def test_queued_reconnect_skips_connection_replaced_before_lock_acquisition(
    monkeypatch,
):
    params = _default_connect_params()
    client, old_conn = _make_connected_client(connect_params=params)
    old_conn.session = None
    startup_lock = asyncio.Lock()
    await startup_lock.acquire()
    client._connection_startup_locks[old_conn.name] = startup_lock
    session_count = 0
    transport_enter_count = 0

    class _CountedReadyTransport:
        async def __aenter__(self):
            nonlocal transport_enter_count
            transport_enter_count += 1
            return (SimpleNamespace(), SimpleNamespace())

        async def __aexit__(self, *_):
            return False

    class _ReplacementSession:
        def __init__(self, *_args):
            nonlocal session_count
            session_count += 1

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def initialize(self):
            return None

        async def list_tools(self):
            return SimpleNamespace(tools=[_make_tool("replacement_tool")])

    monkeypatch.setattr(
        "agents.mcp_client.audited_stdio_client",
        lambda *_args, **_kwargs: _CountedReadyTransport(),
    )
    monkeypatch.setattr("agents.mcp_client.ClientSession", _ReplacementSession)

    public_startup = None
    reconnect_task = None
    try:
        public_startup = asyncio.create_task(
            client.connect_command(
                command="public-replacement",
                args=["server.py"],
                name=old_conn.name,
                env={},
            )
        )
        await asyncio.sleep(0)
        assert public_startup in client._connection_startup_tasks

        reconnect_task = asyncio.create_task(client._reconnect_server(old_conn))

        async def _wait_for_queued_reconnect_child():
            while len(client._connection_startup_tasks) < 2:
                await asyncio.sleep(0)

        await asyncio.wait_for(_wait_for_queued_reconnect_child(), timeout=1)
        startup_lock.release()

        await public_startup
        assert await reconnect_task is False

        replacement = client._servers[old_conn.name]
        assert replacement is not old_conn
        assert replacement.endpoint == "public-replacement"
        assert replacement.reconnect_generation == old_conn.reconnect_generation + 1
        assert client._tool_routing == {"replacement_tool": old_conn.name}
        assert session_count == 1
        assert transport_enter_count == 1
    finally:
        if startup_lock.locked():
            startup_lock.release()
        startup_tasks = [
            task for task in (public_startup, reconnect_task) if task is not None
        ]
        if startup_tasks:
            await asyncio.gather(*startup_tasks, return_exceptions=True)
        await client.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("public_tools", "expected_route"),
    [
        pytest.param(["check_host"], {"check_host": "test-server"}, id="same-tool"),
        pytest.param(
            ["replacement_tool"],
            {"replacement_tool": "test-server"},
            id="removed-tool",
        ),
    ],
)
async def test_queued_call_rejects_public_same_name_replacement(
    monkeypatch,
    public_tools,
    expected_route,
):
    params = _default_connect_params()
    client, old_conn = _make_connected_client(connect_params=params)
    old_conn.session = None
    startup_lock = asyncio.Lock()
    await startup_lock.acquire()
    client._connection_startup_locks[old_conn.name] = startup_lock
    replacement_sessions = []

    class _KeyedTransport:
        def __init__(self, command):
            self.command = command

        async def __aenter__(self):
            return (SimpleNamespace(command=self.command), SimpleNamespace())

        async def __aexit__(self, *_):
            return False

    class _ReplacementSession:
        def __init__(self, read_stream, *_args):
            self.command = read_stream.command
            self.call_count = 0
            if self.command == "public-replacement":
                replacement_sessions.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def initialize(self):
            return None

        async def list_tools(self):
            return SimpleNamespace(tools=[_make_tool(name) for name in public_tools])

        async def call_tool(self, name, arguments, meta=None):
            self.call_count += 1
            return SimpleNamespace(
                content=[SimpleNamespace(text=f"unexpected:{name}")],
                isError=False,
            )

    monkeypatch.setattr(
        "agents.mcp_client.audited_stdio_client",
        lambda server_params, *_args, **_kwargs: _KeyedTransport(server_params.command),
    )
    monkeypatch.setattr("agents.mcp_client.ClientSession", _ReplacementSession)

    public_startup = None
    queued_call = None
    try:
        public_startup = asyncio.create_task(
            client.connect_command(
                command="public-replacement",
                args=["server.py"],
                name=old_conn.name,
                env={},
            )
        )
        await asyncio.sleep(0)
        assert public_startup in client._connection_startup_tasks

        queued_call = asyncio.create_task(
            client.call_tool("check_host", {}, trace_context=_trace_context())
        )

        async def _wait_for_queued_reconnect_child():
            while len(client._connection_startup_tasks) < 2:
                await asyncio.sleep(0)

        await asyncio.wait_for(_wait_for_queued_reconnect_child(), timeout=1)
        startup_lock.release()
        await public_startup

        with pytest.raises(MCPToolCallError) as exc_info:
            await queued_call

        replacement = client._servers[old_conn.name]
        assert replacement.endpoint == "public-replacement"
        assert replacement._reconnect_origin_session_id is None
        assert client._tool_routing == expected_route
        assert exc_info.value.retry_classification == "transport_before_send"
        assert len(replacement_sessions) == 1
        assert replacement_sessions[0].call_count == 0
    finally:
        if startup_lock.locked():
            startup_lock.release()
        tasks = [task for task in (public_startup, queued_call) if task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await client.disconnect()


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
        command,
        args=None,
        name=None,
        env=None,
        ticket_id=None,
        agent_id=None,
        **_kwargs,
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
            _reconnect_origin_session_id=client._servers[name].session_id,
            _connect_params=params,
        )
        client._servers[name] = new_conn
        client._tool_routing["check_host"] = name

    with patch.object(client, "connect_command", side_effect=fake_connect_command):
        with pytest.raises(MCPToolCallError, match="not retried|ambiguous"):
            await client.call_tool("check_host", {}, trace_context=_trace_context())
