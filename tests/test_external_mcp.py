"""Tests for external MCP server connections.

Tests that AgentMCPClient.connect_command() supports arbitrary
commands for non-Python MCP servers (e.g., Jumpstarter).
"""

from __future__ import annotations

import asyncio
import os
import sys
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agents.mcp_client import AgentMCPClient


@pytest.fixture
def mock_mcp_server(tmp_path: Path) -> Path:
    """Create a minimal MCP server script for testing."""
    script = tmp_path / "mock_server.py"
    script.write_text(
        textwrap.dedent("""\
        from fastmcp import FastMCP
        mcp = FastMCP("mock-external")

        @mcp.tool()
        async def mock_tool(message: str = "hello") -> str:
            \"\"\"A mock tool for testing.\"\"\"
            return f"mock response: {message}"

        if __name__ == "__main__":
            mcp.run()
    """)
    )
    return script


@pytest.mark.asyncio
async def test_connect_command_basic(mock_mcp_server: Path):
    """connect_command() connects to a server via arbitrary command."""
    client = AgentMCPClient()
    await client.connect_command(
        command=sys.executable,
        args=[str(mock_mcp_server)],
        name="mock-ext",
    )
    try:
        tools = await client.list_tools()
        assert len(tools) == 1
        assert tools[0].name == "mock_tool"
    finally:
        await client.disconnect()


@pytest.mark.skipif(
    sys.version_info >= (3, 14),
    reason="FastMCP stdio hangs on local Python 3.14; covered in CI 3.12/3.13",
)
@pytest.mark.asyncio
async def test_concurrent_stdio_servers_keep_process_ownership_and_reconnect(
    mock_mcp_server: Path,
):
    """Concurrent owned transports never share a PID or SDK launch state."""
    first = AgentMCPClient()
    second = AgentMCPClient()
    first_pid = second_pid = reconnect_pid = None
    try:
        await asyncio.wait_for(
            asyncio.gather(
                first.connect_command(sys.executable, [str(mock_mcp_server)], "first"),
                second.connect_command(
                    sys.executable, [str(mock_mcp_server)], "second"
                ),
            ),
            timeout=15,
        )
        first_pid = first._servers["first"].subprocess_pid
        second_pid = second._servers["second"].subprocess_pid
        assert first_pid is not None
        assert second_pid is not None
        assert first_pid != second_pid

        await asyncio.wait_for(first.disconnect(), timeout=10)
        for pid in (first_pid,):
            for _ in range(50):
                if not os.path.exists(f"/proc/{pid}"):
                    break
                await asyncio.sleep(0.05)
            assert not os.path.exists(f"/proc/{pid}")
        await asyncio.wait_for(
            first.connect_command(
                sys.executable, [str(mock_mcp_server)], "reconnected"
            ),
            timeout=10,
        )
        reconnect_pid = first._servers["reconnected"].subprocess_pid
        assert reconnect_pid is not None
        assert reconnect_pid not in {first_pid, second_pid}
        assert "mock response: later" in await first.call_tool(
            "mock_tool", {"message": "later"}
        )
    finally:
        await asyncio.wait_for(first.disconnect(), timeout=10)
        await asyncio.wait_for(second.disconnect(), timeout=10)
        for pid in (second_pid, reconnect_pid):
            if pid is None:
                continue
            for _ in range(50):
                if not os.path.exists(f"/proc/{pid}"):
                    break
                await asyncio.sleep(0.05)
            assert not os.path.exists(f"/proc/{pid}")


@pytest.mark.asyncio
async def test_connect_command_tool_routing(mock_mcp_server: Path):
    """Tools from connect_command() are routable via call_tool()."""
    client = AgentMCPClient()
    await client.connect_command(
        command=sys.executable,
        args=[str(mock_mcp_server)],
        name="mock-ext",
    )
    try:
        result = await client.call_tool(
            "mock_tool",
            {"message": "test"},
        )
        assert "mock response: test" in result
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_connect_command_default_name(mock_mcp_server: Path):
    """connect_command() uses command as default name."""
    client = AgentMCPClient()
    await client.connect_command(
        command=sys.executable,
        args=[str(mock_mcp_server)],
    )
    try:
        assert sys.executable in client._servers
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_connect_command_with_env(mock_mcp_server: Path):
    """connect_command() passes environment variables."""
    client = AgentMCPClient()
    await client.connect_command(
        command=sys.executable,
        args=[str(mock_mcp_server)],
        name="mock-env",
        env={"TEST_VAR": "test_value"},
    )
    try:
        tools = await client.list_tools()
        assert len(tools) > 0
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_connect_and_connect_command_coexist(
    mock_mcp_server: Path,
    tmp_path: Path,
):
    """Can mix connect() and connect_command() on same client."""
    # Second server with a different tool name
    server2 = tmp_path / "server2.py"
    server2.write_text(
        textwrap.dedent("""\
        from fastmcp import FastMCP
        mcp = FastMCP("mock-second")

        @mcp.tool()
        async def second_tool() -> str:
            \"\"\"Another mock tool.\"\"\"
            return "second"

        if __name__ == "__main__":
            mcp.run()
    """)
    )

    client = AgentMCPClient()
    # connect() for Python script
    await client.connect(str(mock_mcp_server), name="first")
    # connect_command() for same Python but different server
    await client.connect_command(
        command=sys.executable,
        args=[str(server2)],
        name="second",
    )
    try:
        tools = await client.list_tools()
        tool_names = {t.name for t in tools}
        assert "mock_tool" in tool_names
        assert "second_tool" in tool_names
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_connect_command_invalid_command():
    """connect_command() raises on invalid command."""
    client = AgentMCPClient()
    with pytest.raises(Exception):
        await client.connect_command(
            command="/nonexistent/binary",
            args=["serve"],
            name="bad",
        )


def test_connect_delegates_to_connect_command():
    """connect() is a thin wrapper around connect_command()."""
    import inspect

    source = inspect.getsource(AgentMCPClient.connect)
    assert "connect_command" in source


@pytest.mark.asyncio
async def test_ticket_server_connection_injects_required_context():
    client = AgentMCPClient()
    client.connect = AsyncMock()

    await client.connect_ticket_server(
        "/project/agents/triage/server.py",
        name="triage",
        ticket_id="PERF-12345678",
        state_store_url="http://state-store:8090",
        agent_name="triage-agent",
    )

    client.connect.assert_awaited_once_with(
        "/project/agents/triage/server.py",
        name="triage",
        env={
            "TICKET_ID": "PERF-12345678",
            "STATE_STORE_URL": "http://state-store:8090",
            "AGENT_NAME": "triage-agent",
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ticket_id", "state_store_url", "agent_name", "missing"),
    [
        ("", "http://state-store:8090", "triage-agent", "TICKET_ID"),
        ("PERF-12345678", "", "triage-agent", "STATE_STORE_URL"),
        ("PERF-12345678", "http://state-store:8090", "", "AGENT_NAME"),
    ],
)
async def test_ticket_server_connection_rejects_missing_context(
    ticket_id, state_store_url, agent_name, missing
):
    client = AgentMCPClient()
    client.connect = AsyncMock()

    with pytest.raises(ValueError, match=missing):
        await client.connect_ticket_server(
            "/project/agents/triage/server.py",
            name="triage",
            ticket_id=ticket_id,
            state_store_url=state_store_url,
            agent_name=agent_name,
        )

    client.connect.assert_not_awaited()
