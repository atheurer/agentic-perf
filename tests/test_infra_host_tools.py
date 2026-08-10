"""Unit tests for the targeted host-query tools in agents/infra/server.py."""

from __future__ import annotations

import json

import pytest

import agents.infra.server as srv
from tests.conftest import MockSSHExecutor, SSHResult


@pytest.fixture(autouse=True)
def patch_ssh(monkeypatch):
    """Wire a MockSSHExecutor into the infra server module for each test."""
    mock = MockSSHExecutor()
    monkeypatch.setattr(srv, "_ssh", mock)
    return mock


# ---------------------------------------------------------------------------
# get_ethtool_info
# ---------------------------------------------------------------------------


class TestGetEthtoolInfo:
    @pytest.mark.asyncio
    async def test_features_mode(self, patch_ssh):
        patch_ssh._results["ethtool -k"] = SSHResult(
            exit_code=0,
            stdout="rx-checksumming: on\ntx-checksumming: on\n",
        )
        result = await srv.get_ethtool_info("10.0.0.1", "eth0", mode="features")
        data = json.loads(result)
        assert data["exit_code"] == 0
        assert "rx-checksumming" in data["stdout"]
        assert any("ethtool -k" in c["command"] for c in patch_ssh.calls)

    @pytest.mark.asyncio
    async def test_stats_mode(self, patch_ssh):
        patch_ssh._results["ethtool -S"] = SSHResult(
            exit_code=0,
            stdout="rx_packets: 12345\ngro_packets: 678\n",
        )
        result = await srv.get_ethtool_info("10.0.0.1", "eth0", mode="stats")
        data = json.loads(result)
        assert data["exit_code"] == 0
        assert "gro_packets" in data["stdout"]
        assert any("ethtool -S" in c["command"] for c in patch_ssh.calls)

    @pytest.mark.asyncio
    async def test_invalid_mode(self, patch_ssh):
        result = await srv.get_ethtool_info("10.0.0.1", "eth0", mode="badmode")
        data = json.loads(result)
        assert data["success"] is False
        assert "Unknown mode" in data["error"]
        # No SSH calls should have been made.
        assert patch_ssh.calls == []


# ---------------------------------------------------------------------------
# get_sysctl_values
# ---------------------------------------------------------------------------


class TestGetSysctlValues:
    @pytest.mark.asyncio
    async def test_valid_keys(self, patch_ssh):
        patch_ssh._results["sysctl"] = SSHResult(
            exit_code=0,
            stdout="net.core.rmem_max = 4194304\nnet.ipv4.tcp_wmem = 4096 16384 4194304\n",
        )
        result = await srv.get_sysctl_values(
            "10.0.0.1",
            ["net.core.rmem_max", "net.ipv4.tcp_wmem"],
        )
        data = json.loads(result)
        assert data["exit_code"] == 0
        assert "rmem_max" in data["stdout"]

    @pytest.mark.asyncio
    async def test_invalid_key_rejected(self, patch_ssh):
        # Semicolon in key name should fail the regex guard.
        result = await srv.get_sysctl_values(
            "10.0.0.1", ["net.core.rmem_max; rm -rf /"]
        )
        data = json.loads(result)
        assert data["success"] is False
        assert "Invalid sysctl key" in data["error"]
        assert patch_ssh.calls == []

    @pytest.mark.asyncio
    async def test_space_in_key_rejected(self, patch_ssh):
        result = await srv.get_sysctl_values("10.0.0.1", ["net.core rmem_max"])
        data = json.loads(result)
        assert data["success"] is False
        assert patch_ssh.calls == []


# ---------------------------------------------------------------------------
# query_numa_topology
# ---------------------------------------------------------------------------


class TestQueryNumaTopology:
    @pytest.mark.asyncio
    async def test_success(self, patch_ssh):
        patch_ssh._results["numa_node"] = SSHResult(exit_code=0, stdout="1\n")
        patch_ssh._results["cpulist"] = SSHResult(
            exit_code=0,
            stdout=(
                "/sys/devices/system/node/node0/cpulist: 0-383\n"
                "/sys/devices/system/node/node1/cpulist: 384-767\n"
            ),
        )
        result = await srv.query_numa_topology("10.0.0.1", "eno16695np0")
        data = json.loads(result)
        assert data["nic_numa_node"] == "1"
        assert "node0" in data["node_cpu_lists"]
        assert data["iface"] == "eno16695np0"

    @pytest.mark.asyncio
    async def test_nic_node_failure(self, patch_ssh):
        patch_ssh._results["numa_node"] = SSHResult(
            exit_code=1,
            stdout="",
            stderr="No such file or directory",
        )
        result = await srv.query_numa_topology("10.0.0.1", "eth99")
        data = json.loads(result)
        assert "error" in data
        assert "NIC NUMA node" in data["error"]


# ---------------------------------------------------------------------------
# list_interfaces
# ---------------------------------------------------------------------------


class TestListInterfaces:
    @pytest.mark.asyncio
    async def test_parses_up_interfaces(self, patch_ssh):
        patch_ssh._results["ip -br addr show"] = SSHResult(
            exit_code=0,
            stdout=(
                "lo               UNKNOWN        127.0.0.1/8 ::1/128\n"
                "eth0             UP             10.0.0.1/24 fe80::1/64\n"
                "eth1             DOWN           \n"
                "eno1             UP             192.168.1.5/24\n"
            ),
        )
        result = await srv.list_interfaces("10.0.0.1")
        ifaces = json.loads(result)
        names = [i["iface"] for i in ifaces]
        assert "eth0" in names
        assert "eno1" in names
        # lo is UNKNOWN and eth1 is DOWN — both should be excluded.
        assert "lo" not in names
        assert "eth1" not in names

    @pytest.mark.asyncio
    async def test_ssh_failure_returns_format_result(self, patch_ssh):
        patch_ssh._results["ip -br addr show"] = SSHResult(
            exit_code=1,
            stdout="",
            stderr="permission denied",
        )
        result = await srv.list_interfaces("10.0.0.1")
        data = json.loads(result)
        # _format_result is returned on SSH failure.
        assert data["exit_code"] == 1


# ---------------------------------------------------------------------------
# verify_ssh_path
# ---------------------------------------------------------------------------


class TestVerifySshPath:
    @pytest.mark.asyncio
    async def test_reachable(self, patch_ssh):
        patch_ssh._results["StrictHostKeyChecking=accept-new"] = SSHResult(
            exit_code=0,
            stdout="endpoint-node\n",
        )
        result = await srv.verify_ssh_path("10.0.0.1", "10.0.0.2")
        data = json.loads(result)
        assert data["reachable"] is True
        assert data["from"] == "10.0.0.1"
        assert data["to"] == "10.0.0.2"
        assert data["hostname"] == "endpoint-node"

    @pytest.mark.asyncio
    async def test_unreachable(self, patch_ssh):
        patch_ssh._results["StrictHostKeyChecking=accept-new"] = SSHResult(
            exit_code=255,
            stdout="",
            stderr="Connection refused",
        )
        result = await srv.verify_ssh_path("10.0.0.1", "10.0.0.99")
        data = json.loads(result)
        assert data["reachable"] is False
        assert "hostname" not in data


# ---------------------------------------------------------------------------
# read_remote_dir
# ---------------------------------------------------------------------------


class TestReadRemoteDir:
    @pytest.mark.asyncio
    async def test_success(self, patch_ssh, monkeypatch):
        # copy_from is on the SSHExecutor, not the module — patch it via
        # the mock already installed on srv._ssh.
        async def _fake_copy_from(host, remote_path, local_path, timeout=120):
            return SSHResult(exit_code=0, stdout="", stderr="")

        monkeypatch.setattr(srv._ssh, "copy_from", _fake_copy_from)

        result = await srv.read_remote_dir(
            "10.0.0.1", "/var/lib/crucible/run/abc/tool-data"
        )
        data = json.loads(result)
        assert data["success"] is True
        assert "local_path" in data

    @pytest.mark.asyncio
    async def test_copy_failure(self, patch_ssh, monkeypatch):
        async def _bad_copy_from(host, remote_path, local_path, timeout=120):
            return SSHResult(exit_code=1, stdout="", stderr="no such file")

        monkeypatch.setattr(srv._ssh, "copy_from", _bad_copy_from)

        result = await srv.read_remote_dir("10.0.0.1", "/nonexistent/path")
        data = json.loads(result)
        assert data["success"] is False
        assert "no such file" in data["error"]
