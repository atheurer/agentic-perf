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
        assert data["data"]["rx-checksumming"]["active"] is True
        assert any("ethtool -k" in c["command"] for c in patch_ssh.calls)

    @pytest.mark.asyncio
    async def test_native_json_mode(self, patch_ssh):
        patch_ssh._results["ethtool --json -S"] = SSHResult(
            exit_code=0,
            stdout=json.dumps([{"rx_packets": 100000, "tx_packets": 200000}]),
        )
        result = await srv.get_ethtool_info("10.0.0.1", "eth0", mode="stats")
        data = json.loads(result)
        assert data["exit_code"] == 0
        assert data["data"]["rx_packets"] == 100000
        assert data["data"]["tx_packets"] == 200000

    @pytest.mark.asyncio
    async def test_stats_mode_fallback_parsing(self, patch_ssh):
        patch_ssh._results["ethtool -S"] = SSHResult(
            exit_code=0,
            stdout="NIC statistics:\n     rx_packets: 12345\n     gro_packets: 678\n",
        )
        result = await srv.get_ethtool_info("10.0.0.1", "eth0", mode="stats")
        data = json.loads(result)
        assert data["exit_code"] == 0
        assert "gro_packets" in data["stdout"]
        assert data["data"]["rx_packets"] == 12345
        assert data["data"]["gro_packets"] == 678
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
# run_crucible_command
# ---------------------------------------------------------------------------


class TestCrucibleCommandBroker:
    @pytest.mark.asyncio
    async def test_dispatches_allowlisted_read_command(self, patch_ssh):
        patch_ssh._results["crucible benchmark list"] = SSHResult(
            exit_code=0,
            stdout="uperf\nfio\n",
        )

        result = await srv.run_crucible_command(
            "controller.example", "benchmark_list", {}
        )
        data = json.loads(result)

        assert data["exit_code"] == 0
        assert data["command"] == "benchmark_list"
        assert data["controller"] == "controller.example"
        assert data["stdout"] == "uperf\nfio\n"
        assert patch_ssh.calls[-1]["command"] == "crucible benchmark list"

    @pytest.mark.asyncio
    async def test_schema_enumerates_operations_and_empty_arguments(self):
        tools = await srv.mcp.list_tools()
        tool = next(t for t in tools if t.name == "run_crucible_command")
        schema = tool.parameters

        assert schema["properties"]["command"]["enum"] == [
            "benchmark_list",
            "tools_list",
            "userenvs_list",
        ]
        assert schema["properties"]["arguments"]["anyOf"][0][
            "additionalProperties"
        ] is False

    @pytest.mark.asyncio
    async def test_rejects_unknown_or_mutating_operation_without_ssh(self, patch_ssh):
        for command in ("shell", "run", "benchmark_install"):
            result = await srv.run_crucible_command("controller", command, {})
            data = json.loads(result)
            assert data["success"] is False

        assert patch_ssh.calls == []

    @pytest.mark.asyncio
    async def test_rejects_arguments_before_ssh(self, patch_ssh):
        result = await srv.run_crucible_command(
            "controller", "tools_list", {"extra": "--json"}
        )
        data = json.loads(result)

        assert data["success"] is False
        assert "does not accept arguments" in data["error"]
        assert patch_ssh.calls == []

    @pytest.mark.asyncio
    async def test_userenv_compatibility_wrapper_uses_broker(self, patch_ssh):
        patch_ssh._results["crucible userenvs list"] = SSHResult(
            exit_code=0, stdout="alma8\n"
        )

        result = await srv.list_controller_userenvs("controller")
        data = json.loads(result)

        assert data["stdout"] == "alma8\n"
        assert data["command"] == "userenvs_list"
        assert patch_ssh.calls[-1]["command"] == "crucible userenvs list"


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


# ---------------------------------------------------------------------------
# get_cache_topology
# ---------------------------------------------------------------------------


class TestGetCacheTopology:
    @pytest.mark.asyncio
    async def test_success(self, patch_ssh):
        topology_payload = {
            "cpus": {
                0: {
                    "cpu_id": 0,
                    "online": True,
                    "physical_package_id": 0,
                    "core_id": 0,
                    "caches": [
                        {
                            "level": 3,
                            "type": "Unified",
                            "id": 0,
                            "size": "32M",
                            "shared_cpu_list": "0-3",
                        }
                    ],
                },
                1: {
                    "cpu_id": 1,
                    "online": True,
                    "physical_package_id": 0,
                    "core_id": 1,
                    "caches": [
                        {
                            "level": 3,
                            "type": "Unified",
                            "id": 0,
                            "size": "32M",
                            "shared_cpu_list": "0-3",
                        }
                    ],
                },
                2: {
                    "cpu_id": 2,
                    "online": True,
                    "physical_package_id": 0,
                    "core_id": 2,
                    "caches": [
                        {
                            "level": 3,
                            "type": "Unified",
                            "id": 0,
                            "size": "32M",
                            "shared_cpu_list": "0-3",
                        }
                    ],
                },
                3: {
                    "cpu_id": 3,
                    "online": True,
                    "physical_package_id": 0,
                    "core_id": 3,
                    "caches": [
                        {
                            "level": 3,
                            "type": "Unified",
                            "id": 0,
                            "size": "32M",
                            "shared_cpu_list": "0-3",
                        }
                    ],
                },
            },
            "nodes": {0: "0-3"},
            "system": {
                "arch": "x86_64",
                "vendor": "AuthenticAMD",
                "model": "AMD EPYC",
            },
        }
        patch_ssh._results["python3 -c"] = SSHResult(
            exit_code=0,
            stdout=json.dumps(topology_payload),
        )
        result = await srv.get_cache_topology("10.0.0.1", socket=0)
        data = json.loads(result)
        assert data["host"] == "10.0.0.1"
        assert data["socket"] == 0
        assert data["total_ccds"] == 1
        assert data["ccds"]["0"] == [0, 1, 2, 3]
        assert data["domains"][0]["cpu_list"] == "0-3"
        assert data["domains"][0]["size"] == "32M"


# ---------------------------------------------------------------------------
# get_hardware_topology
# ---------------------------------------------------------------------------


class TestGetHardwareTopology:
    @pytest.mark.asyncio
    async def test_hardware_topology_with_nic(self, patch_ssh):
        topology_payload = {
            "cpus": {
                0: {
                    "cpu_id": 0,
                    "online": True,
                    "physical_package_id": 0,
                    "core_id": 0,
                    "thread_siblings_list": "0,4",
                    "caches": [
                        {
                            "level": 3,
                            "type": "Unified",
                            "id": 0,
                            "size": "32M",
                            "shared_cpu_list": "0-3,4-7",
                        }
                    ],
                },
                4: {
                    "cpu_id": 4,
                    "online": True,
                    "physical_package_id": 0,
                    "core_id": 0,
                    "thread_siblings_list": "0,4",
                    "caches": [
                        {
                            "level": 3,
                            "type": "Unified",
                            "id": 0,
                            "size": "32M",
                            "shared_cpu_list": "0-3,4-7",
                        }
                    ],
                },
            },
            "nodes": {0: "0-7"},
            "system": {
                "arch": "x86_64",
                "vendor": "AuthenticAMD",
                "model": "AMD EPYC",
            },
            "netdevs": {
                "eno1": {
                    "iface": "eno1",
                    "operstate": "up",
                    "mac": "52:54:00:12:34:56",
                    "speed_mbps": 10000,
                    "numa_node": 0,
                    "pci_address": "0000:01:00.0",
                    "driver": "ixgbe",
                    "ip_addresses": ["192.168.1.100"],
                }
            },
            "block_devices": {
                "nvme0n1": {
                    "device": "nvme0n1",
                    "size_gb": 1000.0,
                    "type": "SSD/NVMe",
                    "rotational": False,
                    "numa_node": 0,
                    "pci_address": "0000:04:00.0",
                    "model": "Samsung SSD 980 PRO",
                }
            },
        }
        patch_ssh._results["python3 -c"] = SSHResult(
            exit_code=0,
            stdout=json.dumps(topology_payload),
        )
        patch_ssh._results["cat /sys/class/net/eth0/device/numa_node"] = SSHResult(
            exit_code=0,
            stdout="0\n",
        )
        patch_ssh._results["readlink -f /sys/class/net/eth0/device"] = SSHResult(
            exit_code=0,
            stdout="/sys/devices/pci0000:c0/0000:c0:01.1/0000:c1:00.0\n",
        )

        result = await srv.get_hardware_topology("10.0.0.1", iface="eth0")
        data = json.loads(result)
        assert data["host"] == "10.0.0.1"
        assert data["thread_siblings"]["0"] == [0, 4]
        assert data["thread_siblings_list"]["0"] == "0,4"
        assert data["netdevs"]["eno1"]["operstate"] == "up"
        assert data["netdevs"]["eno1"]["driver"] == "ixgbe"
        assert data["netdevs"]["eno1"]["speed_mbps"] == 10000
        assert data["block_devices"]["nvme0n1"]["type"] == "SSD/NVMe"
        assert data["block_devices"]["nvme0n1"]["size_gb"] == 1000.0
        assert data["nic"]["iface"] == "eth0"
        assert data["nic"]["numa_node_int"] == 0
        assert data["nic"]["pci_address"] == "0000:c1:00.0"
