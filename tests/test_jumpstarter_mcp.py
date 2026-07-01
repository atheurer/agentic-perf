"""Tests for Jumpstarter MCP attachment.

Tests conditional attachment based on ticket resource_provider
and tool filtering for agent scope control.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.jumpstarter_mcp import (
    AGENT_DEVICE_TOOLS,
    attach_jumpstarter_mcp,
)
from agents.mcp_client import AgentMCPClient


class TestToolSets:
    """Verify tool filtering sets are correct."""

    def test_device_tools_exclude_lease_management(self):
        assert "jmp_create_lease" not in AGENT_DEVICE_TOOLS
        assert "jmp_delete_lease" not in AGENT_DEVICE_TOOLS
        assert "jmp_list_leases" not in AGENT_DEVICE_TOOLS
        assert "jmp_list_exporters" not in AGENT_DEVICE_TOOLS

    def test_device_tools_include_interaction(self):
        assert "jmp_run" in AGENT_DEVICE_TOOLS
        assert "jmp_connect" in AGENT_DEVICE_TOOLS
        assert "jmp_disconnect" in AGENT_DEVICE_TOOLS
        assert "jmp_explore" in AGENT_DEVICE_TOOLS
        assert "jmp_drivers" in AGENT_DEVICE_TOOLS


def _make_mock_httpx(custom_fields, status_code=200):
    """Create a mock httpx context for attach_jumpstarter_mcp."""
    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_response.json.return_value = {
        "custom_fields": custom_fields,
    }

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)

    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_client)
    mock_cm.__aexit__ = AsyncMock(return_value=False)
    return mock_cm


class TestAttachment:
    @pytest.mark.asyncio
    async def test_attaches_when_jumpstarter(self):
        """Attaches MCP when resource_provider is jumpstarter."""
        mcp = AsyncMock(spec=AgentMCPClient)
        mcp.connect_command = AsyncMock()

        with patch("agents.jumpstarter_mcp.httpx.AsyncClient") as MockClient:
            MockClient.return_value = _make_mock_httpx(
                {"resource_provider": "jumpstarter"}
            )

            result = await attach_jumpstarter_mcp(
                mcp, "PERF-TEST", "http://localhost:8090"
            )

        assert result is True
        mcp.connect_command.assert_called_once_with(
            command="jmp",
            args=["mcp", "serve"],
            name="jumpstarter",
        )

    @pytest.mark.asyncio
    async def test_skips_when_not_jumpstarter(self):
        """Does not attach when resource_provider is not jumpstarter."""
        mcp = AsyncMock(spec=AgentMCPClient)
        mcp.connect_command = AsyncMock()

        with patch("agents.jumpstarter_mcp.httpx.AsyncClient") as MockClient:
            MockClient.return_value = _make_mock_httpx({"resource_provider": "aws"})

            result = await attach_jumpstarter_mcp(
                mcp, "PERF-TEST", "http://localhost:8090"
            )

        assert result is False
        mcp.connect_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_no_provider(self):
        """Does not attach when no resource_provider is set."""
        mcp = AsyncMock(spec=AgentMCPClient)

        with patch("agents.jumpstarter_mcp.httpx.AsyncClient") as MockClient:
            MockClient.return_value = _make_mock_httpx({})

            result = await attach_jumpstarter_mcp(
                mcp, "PERF-TEST", "http://localhost:8090"
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_handles_ticket_not_found(self):
        """Returns False when ticket lookup fails."""
        mcp = AsyncMock(spec=AgentMCPClient)

        with patch("agents.jumpstarter_mcp.httpx.AsyncClient") as MockClient:
            MockClient.return_value = _make_mock_httpx({}, status_code=404)

            result = await attach_jumpstarter_mcp(
                mcp, "PERF-BAD", "http://localhost:8090"
            )

        assert result is False


class TestAsyncWait:
    @pytest.mark.asyncio
    async def test_suspends_for_long_provisioning(self):
        """Suspends when expected duration exceeds threshold."""
        mock_agent = AsyncMock()
        mock_agent._suspend_for_async = AsyncMock()

        with patch("agents.jumpstarter_mcp.httpx.AsyncClient") as MockClient:
            MockClient.return_value = _make_mock_httpx(
                {
                    "resource_provider": "jumpstarter",
                    "resource_provider_metadata": {
                        "lease_id": "lease-abc",
                    },
                    "jumpstarter_flash": {
                        "expected_duration_mins": 30,
                    },
                }
            )

            from agents.jumpstarter_mcp import suspend_for_device_ready

            result = await suspend_for_device_ready(
                mock_agent, "PERF-TEST", "http://localhost:8090"
            )

        assert result is True
        mock_agent._suspend_for_async.assert_called_once()
        call_kwargs = mock_agent._suspend_for_async.call_args.kwargs
        assert call_kwargs["wait_type"] == "jumpstarter_device_ready"
        assert call_kwargs["operation_id"] == "lease-abc"
        assert call_kwargs["expected_duration_mins"] == 30

    @pytest.mark.asyncio
    async def test_skips_for_short_provisioning(self):
        """Does not suspend when provisioning is short."""
        mock_agent = AsyncMock()

        with patch("agents.jumpstarter_mcp.httpx.AsyncClient") as MockClient:
            MockClient.return_value = _make_mock_httpx(
                {
                    "resource_provider": "jumpstarter",
                    "resource_provider_metadata": {
                        "lease_id": "lease-abc",
                    },
                    "jumpstarter_flash": {
                        "expected_duration_mins": 5,
                    },
                }
            )

            from agents.jumpstarter_mcp import suspend_for_device_ready

            result = await suspend_for_device_ready(
                mock_agent, "PERF-TEST", "http://localhost:8090"
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_skips_when_not_jumpstarter(self):
        """Does not suspend for non-Jumpstarter tickets."""
        mock_agent = AsyncMock()

        with patch("agents.jumpstarter_mcp.httpx.AsyncClient") as MockClient:
            MockClient.return_value = _make_mock_httpx({"resource_provider": "aws"})

            from agents.jumpstarter_mcp import suspend_for_device_ready

            result = await suspend_for_device_ready(
                mock_agent, "PERF-TEST", "http://localhost:8090"
            )

        assert result is False
        mock_agent._suspend_for_async.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_already_signaled(self):
        """Does not re-suspend when device already signaled ready."""
        mock_agent = AsyncMock()

        with patch("agents.jumpstarter_mcp.httpx.AsyncClient") as MockClient:
            MockClient.return_value = _make_mock_httpx(
                {
                    "resource_provider": "jumpstarter",
                    "async_context": {
                        "signal_received": {"id": "lease-abc"},
                    },
                }
            )

            from agents.jumpstarter_mcp import suspend_for_device_ready

            result = await suspend_for_device_ready(
                mock_agent, "PERF-TEST", "http://localhost:8090"
            )

        assert result is False
        mock_agent._suspend_for_async.assert_not_called()
