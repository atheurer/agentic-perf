"""Tests for shared MCP server utilities."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agents.crucible_mcp import CrucibleMCPAdapter
from agents.mcp_client import AgentMCPClient, _ServerConnection
from agents.server_utils import (
    _list_controller_directory_mcp,
    ticket_controller_host,
)


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        (
            {
                "crucible_controller_context": {
                    "host": " context-host ",
                    "controller": "context-controller",
                },
                "ssh_hardware_ips": {"controller": "ssh-controller"},
                "assigned_hardware_ips": {"controller": "assigned-controller"},
            },
            "context-host",
        ),
        (
            {
                "crucible_controller_context": {
                    "host": " ",
                    "controller": " context-controller ",
                },
                "ssh_hardware_ips": {"controller": "ssh-controller"},
                "assigned_hardware_ips": {"controller": "assigned-controller"},
            },
            "context-controller",
        ),
        (
            {
                "ssh_hardware_ips": {"controller": " ssh-controller "},
                "assigned_hardware_ips": {"controller": "assigned-controller"},
            },
            "ssh-controller",
        ),
        (
            {"assigned_hardware_ips": {"controller": " assigned-controller "}},
            "assigned-controller",
        ),
        ({}, None),
    ],
)
def test_ticket_controller_host_precedence(
    fields: dict[str, object], expected: str | None
) -> None:
    """Prefer explicit controller context, SSH address, then assigned address."""
    assert ticket_controller_host({"custom_fields": fields}) == expected


@pytest.mark.asyncio
async def test_controller_context_uses_all_mcp_resource_pages():
    first_page = SimpleNamespace(
        resources=[{"uri": "crucible://docs/first"}],
        nextCursor="cursor-1",
    )
    second_page = SimpleNamespace(
        resources=[{"uri": "crucible://docs/second"}],
        nextCursor=None,
    )
    session = AsyncMock()
    session.list_resources = AsyncMock(side_effect=[first_page, second_page])
    client = AgentMCPClient()
    client._servers[CrucibleMCPAdapter.SERVER_NAME] = _ServerConnection(
        name=CrucibleMCPAdapter.SERVER_NAME,
        session=session,
        transport="stdio",
        session_id="session-1",
        ticket_id="PERF-1",
        agent_id="benchmark",
        connected=True,
    )

    adapter = CrucibleMCPAdapter(
        controller="controller.example",
        ticket_id="PERF-1",
        agent_id="benchmark",
        client=client,
    )
    adapter._connected = True

    directory = await _list_controller_directory_mcp(adapter, "docs")

    assert directory["complete"] is True
    assert [entry["path"] for entry in directory["entries"]] == [
        "docs/first",
        "docs/second",
    ]
