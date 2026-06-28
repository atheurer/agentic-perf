"""Integration tests for the infra MCP server and multi-server MCP client."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from agents.mcp_client import AgentMCPClient


@pytest.fixture
def mock_infra_server(tmp_path: Path) -> Path:
    """Minimal infra MCP server that doesn't require real SSH or secrets."""
    script = tmp_path / "mock_infra.py"
    script.write_text(
        textwrap.dedent("""\
        import json
        from fastmcp import FastMCP

        mcp = FastMCP("infra-test")

        _context = {"ready": False, "user": None}

        @mcp.tool()
        async def set_ssh_context(ticket_id: str, agent_name: str = "") -> str:
            \"\"\"Set SSH context from ticket.\"\"\"
            _context["ready"] = True
            _context["user"] = "root"
            return json.dumps({
                "status": "ok",
                "ssh_user": "root",
                "has_key": True,
                "agent_policy": agent_name or "none",
            })

        @mcp.tool()
        async def check_host(host: str) -> str:
            \"\"\"Check host connectivity.\"\"\"
            if not _context["ready"]:
                return json.dumps({"error": "SSH context not set"})
            return json.dumps({
                "reachable": True,
                "host": host,
                "system_info": "mockhost\\nNAME=MockOS\\n4\\n16",
            })

        @mcp.tool()
        async def execute_command(host: str, command: str, timeout: int = 300, background: bool = False) -> str:
            \"\"\"Execute a command.\"\"\"
            if not _context["ready"]:
                return json.dumps({"error": "SSH context not set"})
            if background or command.rstrip().endswith("&"):
                return json.dumps({
                    "status": "backgrounded",
                    "bg_id": "bg-mock1234",
                    "pid": 12345,
                    "host": host,
                })
            return json.dumps({
                "exit_code": 0,
                "stdout": f"mock output for: {command}",
            })

        @mcp.tool()
        async def stop_background_command(bg_id: str) -> str:
            \"\"\"Stop a background command.\"\"\"
            return json.dumps({"status": "stopped", "bg_id": bg_id, "pid": 12345})

        @mcp.tool()
        async def check_background_command(bg_id: str) -> str:
            \"\"\"Check a background command.\"\"\"
            return json.dumps({"bg_id": bg_id, "running": True, "pid": 12345, "output": "mock"})

        @mcp.tool()
        async def write_remote_file(host: str, remote_path: str, content: str) -> str:
            \"\"\"Write file to remote host.\"\"\"
            return json.dumps({
                "success": True,
                "remote_path": remote_path,
                "bytes_written": len(content),
            })

        @mcp.tool()
        async def read_remote_file(host: str, remote_path: str, max_bytes: int = 10000) -> str:
            \"\"\"Read file from remote host.\"\"\"
            return json.dumps({
                "exit_code": 0,
                "stdout": f"mock content of {remote_path}",
            })

        @mcp.tool()
        async def deploy_secret(host: str, secret_path: str, remote_path: str) -> str:
            \"\"\"Deploy secret to remote host.\"\"\"
            return json.dumps({
                "success": True,
                "secret_path": secret_path,
                "remote_path": remote_path,
            })

        @mcp.tool()
        async def transfer_file(host: str, local_path: str, remote_path: str, direction: str = "push") -> str:
            \"\"\"Transfer file via SCP.\"\"\"
            return json.dumps({
                "success": True,
                "direction": direction,
            })

        @mcp.tool()
        async def install_packages(host: str, packages: list, manager: str = "dnf") -> str:
            \"\"\"Install packages.\"\"\"
            return json.dumps({
                "exit_code": 0,
                "stdout": f"Installed: {', '.join(packages)}",
            })

        if __name__ == "__main__":
            mcp.run()
    """)
    )
    return script


@pytest.fixture
def mock_triage_server(tmp_path: Path) -> Path:
    """Minimal triage server for multi-server testing."""
    script = tmp_path / "mock_triage.py"
    script.write_text(
        textwrap.dedent("""\
        import json
        from fastmcp import FastMCP

        mcp = FastMCP("triage-test")

        @mcp.tool()
        async def list_benchmarks() -> str:
            \"\"\"List benchmarks.\"\"\"
            return json.dumps([{"name": "uperf", "description": "network test"}])

        @mcp.tool()
        async def resolve_benchmark(description: str) -> str:
            \"\"\"Resolve a benchmark.\"\"\"
            return json.dumps({"matched_suite": "uperf"})

        if __name__ == "__main__":
            mcp.run()
    """)
    )
    return script


@pytest.mark.asyncio
async def test_infra_server_tools(mock_infra_server: Path):
    """Verify infra server exposes all expected tools."""
    client = AgentMCPClient()
    await client.connect(str(mock_infra_server), name="infra")
    try:
        tools = await client.list_tools()
        names = {t.name for t in tools}
        expected = {
            "set_ssh_context",
            "check_host",
            "execute_command",
            "stop_background_command",
            "check_background_command",
            "write_remote_file",
            "read_remote_file",
            "deploy_secret",
            "transfer_file",
            "install_packages",
        }
        assert expected == names
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_infra_set_context_then_check_host(mock_infra_server: Path):
    """Verify set_ssh_context enables subsequent SSH operations."""
    client = AgentMCPClient()
    await client.connect(str(mock_infra_server), name="infra")
    try:
        result = await client.call_tool(
            "set_ssh_context",
            {"ticket_id": "PERF-TEST", "agent_name": "provisioning-agent"},
        )
        ctx = json.loads(result)
        assert ctx["status"] == "ok"
        assert ctx["ssh_user"] == "root"

        result = await client.call_tool("check_host", {"host": "10.0.0.1"})
        host_info = json.loads(result)
        assert host_info["reachable"] is True
        assert host_info["host"] == "10.0.0.1"
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_infra_execute_command(mock_infra_server: Path):
    client = AgentMCPClient()
    await client.connect(str(mock_infra_server), name="infra")
    try:
        await client.call_tool("set_ssh_context", {"ticket_id": "PERF-TEST"})
        result = await client.call_tool(
            "execute_command",
            {"host": "10.0.0.1", "command": "hostname -f"},
        )
        data = json.loads(result)
        assert data["exit_code"] == 0
        assert "hostname" in data["stdout"]
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_infra_write_read_file(mock_infra_server: Path):
    client = AgentMCPClient()
    await client.connect(str(mock_infra_server), name="infra")
    try:
        result = await client.call_tool(
            "write_remote_file",
            {
                "host": "10.0.0.1",
                "remote_path": "/tmp/test.yaml",
                "content": "key: value\n",
            },
        )
        data = json.loads(result)
        assert data["success"] is True
        assert data["bytes_written"] == 11

        result = await client.call_tool(
            "read_remote_file",
            {"host": "10.0.0.1", "remote_path": "/tmp/test.yaml"},
        )
        data = json.loads(result)
        assert data["exit_code"] == 0
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_infra_deploy_secret(mock_infra_server: Path):
    client = AgentMCPClient()
    await client.connect(str(mock_infra_server), name="infra")
    try:
        result = await client.call_tool(
            "deploy_secret",
            {
                "host": "10.0.0.1",
                "secret_path": "crucible/token.json",
                "remote_path": "/root/token.json",
            },
        )
        data = json.loads(result)
        assert data["success"] is True
        assert data["secret_path"] == "crucible/token.json"
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_multi_server_routing(mock_triage_server: Path, mock_infra_server: Path):
    """Verify multi-server client merges tools and routes calls correctly."""
    client = AgentMCPClient()
    await client.connect(str(mock_triage_server), name="triage")
    await client.connect(str(mock_infra_server), name="infra")
    try:
        tools = await client.list_tools()
        names = {t.name for t in tools}
        assert "list_benchmarks" in names
        assert "resolve_benchmark" in names
        assert "set_ssh_context" in names
        assert "execute_command" in names
        assert len(names) == 12

        result = await client.call_tool("list_benchmarks", {})
        benchmarks = json.loads(result)
        assert benchmarks[0]["name"] == "uperf"

        result = await client.call_tool("set_ssh_context", {"ticket_id": "PERF-TEST"})
        ctx = json.loads(result)
        assert ctx["status"] == "ok"
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_multi_server_tool_conflict(tmp_path: Path):
    """Verify that duplicate tool names across servers raise ValueError."""
    server_a = tmp_path / "server_a.py"
    server_b = tmp_path / "server_b.py"

    for script in (server_a, server_b):
        script.write_text(
            textwrap.dedent("""\
            from fastmcp import FastMCP
            mcp = FastMCP("dup-test")

            @mcp.tool()
            async def duplicate_tool() -> str:
                \"\"\"Duplicate.\"\"\"
                return "ok"

            if __name__ == "__main__":
                mcp.run()
        """)
        )

    client = AgentMCPClient()
    await client.connect(str(server_a), name="a")
    with pytest.raises(ValueError, match="conflicts"):
        await client.connect(str(server_b), name="b")
    await client.disconnect()


@pytest.mark.asyncio
async def test_multi_server_disconnect_all(
    mock_triage_server: Path, mock_infra_server: Path
):
    """Verify disconnect clears all servers and routing."""
    client = AgentMCPClient()
    await client.connect(str(mock_triage_server), name="triage")
    await client.connect(str(mock_infra_server), name="infra")
    assert len(client._servers) == 2
    assert len(client._tool_routing) == 12

    await client.disconnect()
    assert len(client._servers) == 0
    assert len(client._tool_routing) == 0


@pytest.mark.asyncio
async def test_base_agent_mcp_dispatch_multi_server():
    """Verify AgentBase._execute_tool works with multi-server MCP client."""
    from unittest.mock import AsyncMock, MagicMock

    from agents.base import AgentBase
    from providers.llm.base import ToolCall

    class _TestAgent(AgentBase):
        def _system_prompt(self):
            return "test"

        def _build_messages(self, ticket):
            return []

        async def _handle_completion(self, ticket_id, response):
            pass

    agent = _TestAgent(
        agent_name="test",
        llm_provider=MagicMock(),
        state_store_url="http://localhost:9999",
        tools=[],
        tool_handlers={"local_tool": AsyncMock(return_value="local ok")},
    )

    mock_mcp = AsyncMock()
    mock_mcp.call_tool = AsyncMock(return_value='{"remote": "ok"}')
    agent._mcp = mock_mcp

    local_result = await agent._execute_tool(
        ToolCall(id="1", name="local_tool", input={})
    )
    assert local_result.content == "local ok"
    mock_mcp.call_tool.assert_not_called()

    remote_result = await agent._execute_tool(
        ToolCall(id="2", name="check_host", input={"host": "10.0.0.1"})
    )
    assert remote_result.content == '{"remote": "ok"}'
    mock_mcp.call_tool.assert_called_once()
