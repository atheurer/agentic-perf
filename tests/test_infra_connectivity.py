"""Tests for test_port_connectivity multi-port support."""

from __future__ import annotations

import asyncio
import json

import pytest

import agents.infra.server as srv
from tests.conftest import MockSSHExecutor, SSHResult

_PID_REPLY = "__PID:12345"


@pytest.fixture(autouse=True)
def patch_ssh(monkeypatch):
    """Wire a MockSSHExecutor into the infra server module."""
    mock = MockSSHExecutor(
        results={
            "nc -l": SSHResult(stdout=_PID_REPLY),
            "nc -z": SSHResult(exit_code=0),
            "kill": SSHResult(exit_code=0),
        }
    )
    monkeypatch.setattr(srv, "_ssh", mock)
    return mock


class TestPortConnectivitySinglePort:
    """Backward-compatible single-port calls."""

    @pytest.mark.asyncio
    async def test_single_port_param(self, patch_ssh):
        result = json.loads(
            await srv.test_port_connectivity(
                server_ssh_host="10.0.0.1",
                client_ssh_host="10.0.0.2",
                server_test_ip="192.168.1.1",
                port=30002,
            )
        )
        assert result["all_reachable"] is True
        assert len(result["tests"]) == 1
        assert result["tests"][0]["port"] == 30002
        assert result["tests"][0]["reachable"] is True

    @pytest.mark.asyncio
    async def test_existing_positional_arguments_remain_compatible(self, patch_ssh):
        result = json.loads(
            await srv.test_port_connectivity(
                "10.0.0.1",
                "10.0.0.2",
                "192.168.1.1",
                30002,
                "192.168.1.2",
                10,
            )
        )
        assert result["all_reachable"] is True
        assert len(result["tests"]) == 2

    @pytest.mark.asyncio
    async def test_single_port_in_ports_list_returns_multi_shape(self, patch_ssh):
        result = json.loads(
            await srv.test_port_connectivity(
                server_ssh_host="10.0.0.1",
                client_ssh_host="10.0.0.2",
                server_test_ip="192.168.1.1",
                ports=[30002],
            )
        )
        assert result["all_ports_ok"] is True
        assert result["failed_ports"] == []
        assert "30002" in result["results"]

    @pytest.mark.asyncio
    async def test_single_port_with_reverse(self, patch_ssh):
        result = json.loads(
            await srv.test_port_connectivity(
                server_ssh_host="10.0.0.1",
                client_ssh_host="10.0.0.2",
                server_test_ip="192.168.1.1",
                port=30002,
                client_test_ip="192.168.1.2",
            )
        )
        assert result["all_reachable"] is True
        assert len(result["tests"]) == 2


class TestPortConnectivityMultiPort:
    """Multi-port concurrent testing."""

    @pytest.mark.asyncio
    async def test_all_ports_ok(self, patch_ssh):
        result = json.loads(
            await srv.test_port_connectivity(
                server_ssh_host="10.0.0.1",
                client_ssh_host="10.0.0.2",
                server_test_ip="192.168.1.1",
                ports=[30002, 30003, 30004],
            )
        )
        assert result["all_ports_ok"] is True
        assert result["failed_ports"] == []
        assert "30002" in result["results"]
        assert "30003" in result["results"]
        assert "30004" in result["results"]

    @pytest.mark.asyncio
    async def test_one_port_fails(self, patch_ssh):
        original_run = patch_ssh.run

        async def fail_on_port(host, cmd, **kwargs):
            if "nc -z" in cmd and "30003" in cmd:
                return SSHResult(
                    exit_code=1,
                    stderr="Connection refused",
                )
            return await original_run(host, cmd, **kwargs)

        patch_ssh.run = fail_on_port

        result = json.loads(
            await srv.test_port_connectivity(
                server_ssh_host="10.0.0.1",
                client_ssh_host="10.0.0.2",
                server_test_ip="192.168.1.1",
                ports=[30002, 30003, 30004],
            )
        )
        assert result["all_ports_ok"] is False
        assert 30003 in result["failed_ports"]
        assert 30002 not in result["failed_ports"]
        assert 30004 not in result["failed_ports"]


class TestPortConnectivityErrors:
    """Argument validation."""

    @pytest.mark.asyncio
    async def test_neither_port_nor_ports(self, patch_ssh):
        result = json.loads(
            await srv.test_port_connectivity(
                server_ssh_host="10.0.0.1",
                client_ssh_host="10.0.0.2",
                server_test_ip="192.168.1.1",
            )
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_both_port_and_ports(self, patch_ssh):
        result = json.loads(
            await srv.test_port_connectivity(
                server_ssh_host="10.0.0.1",
                client_ssh_host="10.0.0.2",
                server_test_ip="192.168.1.1",
                port=30002,
                ports=[30003],
            )
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_empty_ports_list(self, patch_ssh):
        result = json.loads(
            await srv.test_port_connectivity(
                server_ssh_host="10.0.0.1",
                client_ssh_host="10.0.0.2",
                server_test_ip="192.168.1.1",
                ports=[],
            )
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_duplicate_ports_deduplicated(self, patch_ssh):
        result = json.loads(
            await srv.test_port_connectivity(
                server_ssh_host="10.0.0.1",
                client_ssh_host="10.0.0.2",
                server_test_ip="192.168.1.1",
                ports=[30002, 30002, 30003],
            )
        )
        assert result["all_ports_ok"] is True
        assert len(result["results"]) == 2

    @pytest.mark.asyncio
    async def test_cancellation_still_kills_listener(self, patch_ssh):
        started = asyncio.Event()

        async def block_probe(host, command, **kwargs):
            if "nc -z" in command:
                started.set()
                await asyncio.Event().wait()
            return await MockSSHExecutor.run(patch_ssh, host, command, **kwargs)

        patch_ssh.run = block_probe
        task = asyncio.create_task(
            srv.test_port_connectivity(
                server_ssh_host="10.0.0.1",
                client_ssh_host="10.0.0.2",
                server_test_ip="192.168.1.1",
                port=30002,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert any(call["command"].startswith("kill 12345") for call in patch_ssh.calls)
