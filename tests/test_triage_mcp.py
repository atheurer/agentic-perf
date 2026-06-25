"""Tests for the MCP-based triage agent dispatch.

Tests that AgentMCPClient can connect to a FastMCP server over stdio,
list tools, and call them — proving the MCP pattern works end-to-end.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from agents.mcp_client import AgentMCPClient


@pytest.fixture
def mock_triage_server(tmp_path: Path) -> Path:
    """Write a minimal FastMCP triage server that uses inline mock data."""
    script = tmp_path / "mock_server.py"
    script.write_text(
        textwrap.dedent("""\
        import json
        from fastmcp import FastMCP

        mcp = FastMCP("triage-agent-test")

        BENCHMARKS = [
            {
                "name": "uperf",
                "description": "Network throughput/latency via uperf",
                "roles": ["client", "server"],
                "min_hosts": 2,
                "harness": "crucible",
            },
            {
                "name": "fio",
                "description": "Storage I/O benchmark",
                "roles": ["client"],
                "min_hosts": 1,
                "harness": "crucible",
            },
        ]

        @mcp.tool()
        async def list_benchmarks() -> str:
            \"\"\"List all available benchmark suites.\"\"\"
            return json.dumps(BENCHMARKS, indent=2)

        @mcp.tool()
        async def get_benchmark_details(name: str) -> str:
            \"\"\"Get detailed info about a benchmark suite.\"\"\"
            for b in BENCHMARKS:
                if b["name"] == name:
                    return json.dumps(b, indent=2)
            return json.dumps({"error": f"Benchmark '{name}' not found"})

        @mcp.tool()
        async def resolve_benchmark(
            description: str,
            workload_type: str = "",
            harness: str = "",
        ) -> str:
            \"\"\"Resolve a description to a benchmark suite.\"\"\"
            desc_lower = description.lower()
            if "network" in desc_lower or "throughput" in desc_lower:
                return json.dumps({"matched_suite": "uperf", "harnesses": ["crucible"]})
            if "storage" in desc_lower or "disk" in desc_lower:
                return json.dumps({"matched_suite": "fio", "harnesses": ["crucible"]})
            return json.dumps({"matched_suite": None})

        if __name__ == "__main__":
            mcp.run()
    """)
    )
    return script


@pytest.mark.asyncio
async def test_mcp_client_list_tools(mock_triage_server: Path):
    client = AgentMCPClient()
    await client.connect(str(mock_triage_server))
    try:
        tools = await client.list_tools()
        names = {t.name for t in tools}
        assert "list_benchmarks" in names
        assert "get_benchmark_details" in names
        assert "resolve_benchmark" in names
        assert len(tools) == 3

        for t in tools:
            assert t.description
            assert isinstance(t.input_schema, dict)
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_mcp_client_call_list_benchmarks(mock_triage_server: Path):
    client = AgentMCPClient()
    await client.connect(str(mock_triage_server))
    try:
        result = await client.call_tool("list_benchmarks", {})
        benchmarks = json.loads(result)
        assert len(benchmarks) == 2
        assert benchmarks[0]["name"] == "uperf"
        assert benchmarks[1]["name"] == "fio"
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_mcp_client_call_get_benchmark_details(mock_triage_server: Path):
    client = AgentMCPClient()
    await client.connect(str(mock_triage_server))
    try:
        result = await client.call_tool("get_benchmark_details", {"name": "uperf"})
        details = json.loads(result)
        assert details["name"] == "uperf"
        assert details["min_hosts"] == 2
        assert "client" in details["roles"]
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_mcp_client_call_get_benchmark_not_found(mock_triage_server: Path):
    client = AgentMCPClient()
    await client.connect(str(mock_triage_server))
    try:
        result = await client.call_tool(
            "get_benchmark_details", {"name": "nonexistent"}
        )
        details = json.loads(result)
        assert "error" in details
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_mcp_client_call_resolve_benchmark(mock_triage_server: Path):
    client = AgentMCPClient()
    await client.connect(str(mock_triage_server))
    try:
        result = await client.call_tool(
            "resolve_benchmark",
            {"description": "I want to test network throughput"},
        )
        resolved = json.loads(result)
        assert resolved["matched_suite"] == "uperf"

        result = await client.call_tool(
            "resolve_benchmark",
            {"description": "test disk I/O performance"},
        )
        resolved = json.loads(result)
        assert resolved["matched_suite"] == "fio"

        result = await client.call_tool(
            "resolve_benchmark",
            {"description": "something completely unrelated"},
        )
        resolved = json.loads(result)
        assert resolved["matched_suite"] is None
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_mcp_tool_schemas_have_correct_structure(mock_triage_server: Path):
    """Verify that MCP-provided tool schemas are compatible with our ToolDefinition format."""
    client = AgentMCPClient()
    await client.connect(str(mock_triage_server))
    try:
        tools = await client.list_tools()
        resolve_tool = next(t for t in tools if t.name == "resolve_benchmark")
        schema = resolve_tool.input_schema
        assert schema.get("type") == "object"
        assert "description" in schema.get("properties", {})
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_base_agent_mcp_dispatch():
    """Verify AgentBase._execute_tool routes to MCP when no local handler matches."""
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

    mock_llm = MagicMock()
    agent = _TestAgent(
        agent_name="test",
        llm_provider=mock_llm,
        state_store_url="http://localhost:9999",
        tools=[],
        tool_handlers={"local_tool": AsyncMock(return_value="local result")},
    )

    mock_mcp = AsyncMock()
    mock_mcp.call_tool = AsyncMock(return_value='{"mcp": "result"}')
    agent._mcp = mock_mcp

    local_call = ToolCall(id="1", name="local_tool", input={"x": 1})
    result = await agent._execute_tool(local_call)
    assert result.content == "local result"
    assert not result.is_error
    mock_mcp.call_tool.assert_not_called()

    mcp_call = ToolCall(id="2", name="mcp_tool", input={"y": 2})
    result = await agent._execute_tool(mcp_call)
    assert result.content == '{"mcp": "result"}'
    assert not result.is_error
    mock_mcp.call_tool.assert_called_once_with("mcp_tool", {"y": 2})

    agent._mcp = None
    unknown_call = ToolCall(id="3", name="unknown", input={})
    result = await agent._execute_tool(unknown_call)
    assert result.is_error
    assert "Unknown tool" in result.content


@pytest.mark.asyncio
async def test_mcp_client_list_tools_with_filter(mock_triage_server: Path):
    """list_tools(include=...) only returns matching tools."""
    client = AgentMCPClient()
    await client.connect(str(mock_triage_server))
    try:
        # Filter to just one tool
        tools = await client.list_tools(
            include={"list_benchmarks"},
        )
        names = {t.name for t in tools}
        assert names == {"list_benchmarks"}

        # Filtered-out tools are still callable
        result = await client.call_tool(
            "resolve_benchmark",
            {"description": "network test"},
        )
        assert result  # didn't raise
    finally:
        await client.disconnect()
