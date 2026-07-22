"""Tests for SSE and StreamableHTTP MCP transport."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.mcp_client import AgentMCPClient


def _make_mock_session(tool_names: list[str] | None = None):
    """Create a mock that works as both ClientSession constructor
    result and async context manager.

    ClientSession usage in _connect_transport:
        session_cm = ClientSession(read, write)  # constructor
        session = await session_cm.__aenter__()   # enter CM
        await session.initialize()                # init
        result = await session.list_tools()       # list tools
    """
    if tool_names is None:
        tool_names = ["test_tool"]

    tools = []
    for name in tool_names:
        t = MagicMock()
        t.name = name
        t.description = f"Tool {name}"
        t.inputSchema = {"type": "object"}
        tools.append(t)

    # The actual session object (returned by __aenter__)
    session = AsyncMock()
    session.initialize = AsyncMock()
    session.list_tools = AsyncMock(return_value=MagicMock(tools=tools))

    # The CM wrapper (returned by ClientSession constructor)
    session_cm = AsyncMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock()

    # ClientSession(read, write) -> session_cm
    constructor = MagicMock(return_value=session_cm)

    return constructor


def _make_mock_transport():
    """Create a mock transport context manager."""
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))
    cm.__aexit__ = AsyncMock()
    return cm


class TestConnectSSE:
    @pytest.mark.asyncio
    async def test_connects_and_registers_tools(self):
        transport = _make_mock_transport()
        cs = _make_mock_session(["sse_tool"])

        with (
            patch("agents.mcp_client.ClientSession", cs),
            patch(
                "mcp.client.sse.sse_client",
                return_value=transport,
            ),
        ):
            client = AgentMCPClient()
            await client.connect_sse(
                url="http://test:8080/sse",
                name="my-sse",
            )

        assert "my-sse" in client._servers
        assert "sse_tool" in client._tool_routing
        assert client._tool_routing["sse_tool"] == "my-sse"

    @pytest.mark.asyncio
    async def test_passes_headers_and_timeouts(self):
        transport = _make_mock_transport()
        cs = _make_mock_session()

        with (
            patch("agents.mcp_client.ClientSession", cs),
            patch(
                "mcp.client.sse.sse_client",
                return_value=transport,
            ) as mock_sse,
        ):
            client = AgentMCPClient()
            await client.connect_sse(
                url="http://test:8080/sse",
                name="s",
                headers={"Authorization": "Bearer t"},
                timeout=15,
                sse_read_timeout=600,
            )

        mock_sse.assert_called_once_with(
            url="http://test:8080/sse",
            headers={"Authorization": "Bearer t"},
            timeout=15,
            sse_read_timeout=600,
        )

    @pytest.mark.asyncio
    async def test_default_name_from_url(self):
        transport = _make_mock_transport()
        cs = _make_mock_session()

        with (
            patch("agents.mcp_client.ClientSession", cs),
            patch(
                "mcp.client.sse.sse_client",
                return_value=transport,
            ),
        ):
            client = AgentMCPClient()
            await client.connect_sse(
                url="http://domain-mcp.lab:8080/sse",
            )

        assert "http://domain-mcp.lab:8080/sse" in client._servers


class TestConnectStreamableHTTP:
    @pytest.mark.asyncio
    async def test_connects_and_registers_tools(self):
        transport = _make_mock_transport()
        cs = _make_mock_session(["http_tool"])

        with (
            patch("agents.mcp_client.ClientSession", cs),
            patch(
                "mcp.client.streamable_http.streamablehttp_client",
                return_value=transport,
            ),
        ):
            client = AgentMCPClient()
            await client.connect_streamable_http(
                url="http://test:8080/mcp",
                name="my-http",
            )

        assert "my-http" in client._servers
        assert "http_tool" in client._tool_routing


class TestMixedTransports:
    @pytest.mark.asyncio
    async def test_stdio_and_sse_together(self):
        stdio_transport = _make_mock_transport()
        sse_transport = _make_mock_transport()
        cs1 = _make_mock_session(["stdio_tool"])
        cs2 = _make_mock_session(["sse_tool"])

        with (
            patch(
                "agents.mcp_client.ClientSession",
                side_effect=[cs1.return_value, cs2.return_value],
            ),
            patch(
                "agents.mcp_client.stdio_client",
                return_value=stdio_transport,
            ),
            patch(
                "mcp.client.sse.sse_client",
                return_value=sse_transport,
            ),
        ):
            client = AgentMCPClient()
            await client.connect_command(command="echo", name="stdio-srv")
            await client.connect_sse(url="http://test/sse", name="sse-srv")

        assert len(client._servers) == 2
        assert client._tool_routing["stdio_tool"] == "stdio-srv"
        assert client._tool_routing["sse_tool"] == "sse-srv"

    @pytest.mark.asyncio
    async def test_tool_conflict_across_transports(self):
        stdio_transport = _make_mock_transport()
        sse_transport = _make_mock_transport()
        # Both expose "conflict_tool"
        cs1 = _make_mock_session(["conflict_tool"])
        cs2 = _make_mock_session(["conflict_tool"])

        with (
            patch(
                "agents.mcp_client.ClientSession",
                side_effect=[cs1.return_value, cs2.return_value],
            ),
            patch(
                "agents.mcp_client.stdio_client",
                return_value=stdio_transport,
            ),
            patch(
                "mcp.client.sse.sse_client",
                return_value=sse_transport,
            ),
        ):
            client = AgentMCPClient()
            await client.connect_command(command="echo", name="srv-1")
            with pytest.raises(ValueError, match="conflicts"):
                await client.connect_sse(url="http://test/sse", name="srv-2")


class TestConnectExternalServers:
    @pytest.mark.asyncio
    async def test_connects_matching_agent(self):
        """Connects servers configured for the agent type."""
        transport = _make_mock_transport()
        cs = _make_mock_session(["domain_tool"])

        config = {
            "external_mcp_servers": [
                {
                    "name": "domain-mcp",
                    "url": "http://test:8080/sse",
                    "transport": "sse",
                    "agents": {
                        "gathering_context": {
                            "enabled_tools": "all"
                        }
                    },
                }
            ]
        }

        with (
            patch("agents.mcp_client.ClientSession", cs),
            patch(
                "mcp.client.sse.sse_client",
                return_value=transport,
            ),
        ):
            from agents.mcp_client import connect_external_servers

            client = AgentMCPClient()
            connected, enabled = await connect_external_servers(
                client, "gathering_context", config=config
            )

        assert connected == ["domain-mcp"]
        assert "domain_tool" in client._tool_routing
        assert enabled is None  # "all" means no filtering

    @pytest.mark.asyncio
    async def test_skips_non_matching_agent(self):
        """Skips servers not configured for the agent type."""
        config = {
            "external_mcp_servers": [
                {
                    "name": "domain-mcp",
                    "url": "http://test:8080/sse",
                    "transport": "sse",
                    "agents": {
                        "gathering_context": {
                            "enabled_tools": "all"
                        }
                    },
                }
            ]
        }

        from agents.mcp_client import connect_external_servers

        client = AgentMCPClient()
        connected, enabled = await connect_external_servers(
            client, "benchmark", config=config
        )

        assert connected == []
        assert len(client._servers) == 0

    @pytest.mark.asyncio
    async def test_skips_server_with_no_agents_field(self):
        """Skips servers with no agents field."""
        config = {
            "external_mcp_servers": [
                {
                    "name": "global-mcp",
                    "url": "http://test:8080/mcp",
                    "transport": "streamable_http",
                }
            ]
        }

        from agents.mcp_client import connect_external_servers

        client = AgentMCPClient()
        connected, enabled = await connect_external_servers(
            client, "any_agent", config=config
        )

        assert connected == []
        assert enabled is None

    @pytest.mark.asyncio
    async def test_reads_auth_from_secrets(self, tmp_path):
        """Reads bearer token from secrets file."""
        transport = _make_mock_transport()
        cs = _make_mock_session(["tool1"])
        token_file = tmp_path / "domain-mcp" / "token"
        token_file.parent.mkdir()
        token_file.write_text("secret123")

        config = {
            "external_mcp_servers": [
                {
                    "name": "authed",
                    "url": "http://test:8080/sse",
                    "transport": "sse",
                    "agents": {"any_agent": {"enabled_tools": "all"}},
                    "secret": "domain-mcp/token",
                }
            ]
        }

        with (
            patch("agents.mcp_client.ClientSession", cs),
            patch(
                "mcp.client.sse.sse_client",
                return_value=transport,
            ) as mock_sse,
        ):
            from agents.mcp_client import connect_external_servers

            client = AgentMCPClient()
            await connect_external_servers(
                client,
                "any_agent",
                config=config,
                secrets_dir=str(tmp_path),
            )

        mock_sse.assert_called_once()
        call_headers = mock_sse.call_args.kwargs.get("headers")
        assert call_headers == {"Authorization": "Bearer secret123"}

    @pytest.mark.asyncio
    async def test_handles_connection_failure(self):
        """Failed connections are logged, not raised."""
        config = {
            "external_mcp_servers": [
                {
                    "name": "broken",
                    "url": "http://unreachable:9999/sse",
                    "transport": "sse",
                }
            ]
        }

        with patch(
            "mcp.client.sse.sse_client",
            side_effect=ConnectionError("refused"),
        ):
            from agents.mcp_client import connect_external_servers

            client = AgentMCPClient()
            connected, _ = await connect_external_servers(
                client, "any_agent", config=config
            )

        assert connected == []

    @pytest.mark.asyncio
    async def test_tool_scoping_returns_enabled_set(self):
        """Returns enabled tool set when configured."""
        transport = _make_mock_transport()
        cs = _make_mock_session(["get_baseline_stats", "get_key_metrics", "compare"])

        config = {
            "external_mcp_servers": [
                {
                    "name": "domain-mcp",
                    "url": "http://test:8080/mcp",
                    "transport": "streamable_http",
                    "agents": {
                        "review": {
                            "enabled_tools": [
                                "get_baseline_stats",
                                "compare",
                            ]
                        }
                    },
                }
            ]
        }

        with (
            patch("agents.mcp_client.ClientSession", cs),
            patch(
                "mcp.client.streamable_http.streamablehttp_client",
                return_value=transport,
            ),
        ):
            from agents.mcp_client import connect_external_servers

            client = AgentMCPClient()
            connected, enabled = await connect_external_servers(
                client, "review", config=config
            )

        assert connected == ["domain-mcp"]
        assert enabled == {"get_baseline_stats", "compare"}
        # get_key_metrics is connected but not in enabled set
        assert "get_key_metrics" in client._tool_routing
