"""Tests for lazy SSH-context initialization in the infra MCP server."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from fastmcp.tools.base import ToolResult

import agents.infra.server as srv
from tests.conftest import MockSSHExecutor


@pytest.fixture(autouse=True)
def clear_ssh_context(monkeypatch):
    monkeypatch.setattr(srv, "_ssh", None)
    monkeypatch.setattr(srv, "_ticket_id", None)
    monkeypatch.setattr(srv, "_ssh_has_key", False)
    monkeypatch.delenv("TICKET_ID", raising=False)


@pytest.mark.asyncio
async def test_ssh_tool_lazily_initializes_from_ticket_identity(monkeypatch):
    ssh = MockSSHExecutor()
    monkeypatch.setenv("TICKET_ID", "PERF-LAZY-SSH")
    monkeypatch.setenv("STATE_STORE_URL", "http://state-store.test")
    build_ssh = AsyncMock(
        return_value=(
            ssh,
            {"custom_fields": {"ssh_key_path": "~/.ssh/id_ed25519"}},
        )
    )
    monkeypatch.setattr(srv, "build_ssh_from_ticket", build_ssh)

    result = json.loads(await srv.check_host("sut.example"))

    assert result["reachable"] is True
    build_ssh.assert_awaited_once_with("PERF-LAZY-SSH", "http://state-store.test")
    assert len(ssh.calls) == 2
    assert {call["host"] for call in ssh.calls} == {"sut.example"}


@pytest.mark.asyncio
async def test_set_ssh_context_is_idempotent(monkeypatch):
    ssh = MockSSHExecutor()
    ssh.user = "root"
    build_ssh = AsyncMock(
        return_value=(
            ssh,
            {"custom_fields": {"ssh_key_path": "~/.ssh/id_ed25519"}},
        )
    )
    monkeypatch.setattr(srv, "build_ssh_from_ticket", build_ssh)

    first = json.loads(await srv.set_ssh_context("PERF-LAZY-SSH"))
    second = json.loads(await srv.set_ssh_context("PERF-LAZY-SSH"))

    assert first == second == {"status": "ok", "ssh_user": "root", "has_key": True}
    build_ssh.assert_awaited_once_with("PERF-LAZY-SSH", "http://localhost:8090")


@pytest.mark.asyncio
async def test_missing_ticket_identity_returns_recoverable_action_result():
    result = await srv.check_host("sut.example")

    assert isinstance(result, ToolResult)
    assert result.is_error is True
    assert result.structured_content == {
        "status": "precondition_required",
        "error": "ssh_context_required",
        "required_action": "set_ssh_context",
        "retryable": True,
        "message": (
            "SSH context is unavailable because no ticket ID is configured. "
            "Call set_ssh_context with the ticket ID, then retry this tool."
        ),
    }


@pytest.mark.asyncio
async def test_ssh_context_initialization_failure_remains_an_error(monkeypatch):
    monkeypatch.setenv("TICKET_ID", "PERF-LAZY-SSH")
    build_ssh = AsyncMock(side_effect=RuntimeError("ticket lookup failed"))
    monkeypatch.setattr(srv, "build_ssh_from_ticket", build_ssh)

    with pytest.raises(RuntimeError, match="ticket lookup failed"):
        await srv.check_host("sut.example")


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("check_host", {"host": "sut.example"}),
        (
            "write_remote_file",
            {"host": "sut.example", "remote_path": "/tmp/test", "content": ""},
        ),
        ("read_remote_file", {"host": "sut.example", "remote_path": "/tmp/test"}),
        ("list_controller_userenvs", {"controller": "controller.example"}),
        (
            "run_crucible_command",
            {
                "controller": "controller.example",
                "command": "benchmark_list",
                "arguments": {},
            },
        ),
        ("read_remote_dir", {"host": "sut.example", "remote_path": "/tmp"}),
        ("get_ethtool_info", {"host": "sut.example", "iface": "eth0"}),
        ("get_sysctl_values", {"host": "sut.example", "params": []}),
        ("get_hardware_topology", {"host": "sut.example"}),
        ("get_cache_topology", {"host": "sut.example"}),
        (
            "verify_ssh_path",
            {"host": "controller.example", "target_host": "sut.example"},
        ),
        ("list_interfaces", {"host": "sut.example"}),
        ("get_interface_inventory", {"host": "sut.example"}),
        (
            "deploy_secret",
            {
                "host": "sut.example",
                "secret_path": "secret",
                "remote_path": "/tmp/secret",
            },
        ),
        (
            "transfer_file",
            {
                "host": "sut.example",
                "local_path": "/tmp/source",
                "remote_path": "/tmp/destination",
            },
        ),
        ("check_hosts", {"hosts": []}),
        (
            "test_port_connectivity",
            {
                "server_ssh_host": "server.example",
                "client_ssh_host": "client.example",
                "server_test_ip": "192.0.2.1",
                "port": 12345,
            },
        ),
    ],
)
@pytest.mark.asyncio
async def test_every_ssh_dependent_tool_returns_actionable_precondition(
    tool_name, arguments
):
    result = await getattr(srv, tool_name)(**arguments)

    assert isinstance(result, ToolResult), tool_name
    assert result.is_error is True, tool_name
    assert result.structured_content["error"] == "ssh_context_required"
    assert result.structured_content["required_action"] == "set_ssh_context"
