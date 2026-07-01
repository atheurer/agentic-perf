"""Tests for external MCP server connections.

Tests that AgentMCPClient.connect_command() supports arbitrary
commands for non-Python MCP servers (e.g., Jumpstarter).
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

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
