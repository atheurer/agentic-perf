"""Tests for Jumpstarter MCP attachment.

Tests conditional attachment based on ticket resource_provider
and tool filtering for agent scope control.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.jumpstarter_mcp import (
    AGENT_DEVICE_TOOLS,
    _JmpCallHook,
    attach_jumpstarter_mcp,
)
from agents.mcp_client import AgentMCPClient, MCPHookResult
from providers.tracing import TraceContext, bind_trace_context, reset_trace_context


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
        # jmp_disconnect intentionally excluded — MCP
        # connection must stay alive through benchmark
        assert "jmp_disconnect" not in AGENT_DEVICE_TOOLS
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
        context = TraceContext(ticket_id="PERF-TEST", agent_id="platform-agent")
        token = bind_trace_context(context)

        try:
            with patch("providers.execution.AuditedAsyncHTTPClient") as MockClient:
                MockClient.return_value = _make_mock_httpx(
                    {"resource_provider": "jumpstarter"}
                )

                result = await attach_jumpstarter_mcp(
                    mcp, "PERF-TEST", "http://localhost:8090"
                )
        finally:
            reset_trace_context(token)

        assert result is True
        mcp.connect_command.assert_called_once()
        call_kwargs = mcp.connect_command.call_args[1]
        assert call_kwargs["command"] == "jmp"
        assert call_kwargs["args"] == ["mcp", "serve"]
        assert call_kwargs["name"] == "jumpstarter"
        assert call_kwargs["ticket_id"] == "PERF-TEST"
        assert call_kwargs["agent_id"] == "platform-agent"

    @pytest.mark.asyncio
    async def test_skips_when_not_jumpstarter(self):
        """Does not attach when resource_provider is not jumpstarter."""
        mcp = AsyncMock(spec=AgentMCPClient)
        mcp.connect_command = AsyncMock()

        with patch("providers.execution.AuditedAsyncHTTPClient") as MockClient:
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

        with patch("providers.execution.AuditedAsyncHTTPClient") as MockClient:
            MockClient.return_value = _make_mock_httpx({})

            result = await attach_jumpstarter_mcp(
                mcp, "PERF-TEST", "http://localhost:8090"
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_handles_ticket_not_found(self):
        """Returns False when ticket lookup fails."""
        mcp = AsyncMock(spec=AgentMCPClient)

        with patch("providers.execution.AuditedAsyncHTTPClient") as MockClient:
            MockClient.return_value = _make_mock_httpx({}, status_code=404)

            result = await attach_jumpstarter_mcp(
                mcp, "PERF-BAD", "http://localhost:8090"
            )

        assert result is False


class TestInternalDispatch:
    @pytest.mark.asyncio
    async def test_jmp_connect_uses_explicit_internal_dispatch_contract(self):
        mcp = MagicMock(spec=AgentMCPClient)
        mcp.dispatch_internal_tool = AsyncMock(
            return_value=MCPHookResult(content="connected", request_sent=True)
        )
        hook = _JmpCallHook(mcp)
        context = TraceContext(
            ticket_id="PERF-TEST",
            agent_id="benchmark",
            mcp_correlation_request_id="corr-1",
        )
        token = bind_trace_context(context)
        try:
            result = await hook.pre_call("jmp_connect", {"lease_id": "lease-1"})
        finally:
            reset_trace_context(token)

        assert result == MCPHookResult(content="connected", request_sent=True)
        mcp.dispatch_internal_tool.assert_awaited_once_with(
            "jmp_connect",
            {"lease_id": "lease-1"},
            context,
        )

    @pytest.mark.asyncio
    async def test_jmp_connect_error_does_not_mark_hook_connected(self):
        mcp = MagicMock(spec=AgentMCPClient)
        mcp.dispatch_internal_tool = AsyncMock(
            return_value=MCPHookResult(
                content="MCP rejected",
                is_error=True,
                request_sent=True,
                retry_classification="intentional_agent_retry",
            )
        )
        hook = _JmpCallHook(mcp)
        token = bind_trace_context(
            TraceContext(
                ticket_id="PERF-TEST",
                agent_id="benchmark",
                mcp_correlation_request_id="corr-1",
            )
        )
        try:
            result = await hook.pre_call("jmp_connect", {})
        finally:
            reset_trace_context(token)

        assert result.is_error is True
        assert hook._connected is False
