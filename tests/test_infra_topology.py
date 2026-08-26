"""Unit tests for agents/infra/topology.py (hardware cache and CCD discovery)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from agents.infra.topology import (
    discover_cache_topology,
    format_cpu_list,
    parse_cpu_list,
    parse_cpu_mask,
    parse_topology_data,
)
from tests.conftest import MockSSHExecutor, SSHResult

# ---------------------------------------------------------------------------
# Helpers to build mock topology data
# ---------------------------------------------------------------------------


def make_turin_mock_data() -> dict[str, Any]:
    """Generate mock sysfs topology data for a dual-socket AMD EPYC 9965 (Turin).

    Socket 0: CPUs 0-191 (primary), 384-575 (SMT) -> 24 CCDs of 8 cores (16 threads).
    Socket 1: CPUs 192-383 (primary), 576-767 (SMT) -> 24 CCDs of 8 cores (16 threads).
    CCD 0 on Socket 1: CPUs 192-207, 576-591 (16 cores, 32 threads).
    """
    cpus: dict[int, Any] = {}
    nodes = {0: "0-191,384-575", 1: "192-383,576-767"}

    for ccd in range(24):
        # Socket 0
        s0_primary = list(range(ccd * 8, (ccd + 1) * 8))
        s0_smt = list(range(384 + ccd * 8, 384 + (ccd + 1) * 8))
        s0_all = sorted(s0_primary + s0_smt)
        s0_list = f"{s0_primary[0]}-{s0_primary[-1]},{s0_smt[0]}-{s0_smt[-1]}"
        for cpu in s0_all:
            cpus[cpu] = {
                "cpu_id": cpu,
                "online": True,
                "physical_package_id": 0,
                "core_id": cpu % 192,
                "die_id": ccd,
                "cluster_id": ccd,
                "caches": [
                    {
                        "level": 1,
                        "type": "Data",
                        "id": cpu,
                        "size": "32K",
                        "shared_cpu_list": str(cpu),
                    },
                    {
                        "level": 2,
                        "type": "Unified",
                        "id": cpu,
                        "size": "1024K",
                        "shared_cpu_list": str(cpu),
                    },
                    {
                        "level": 3,
                        "type": "Unified",
                        "id": ccd,
                        "size": "32M",
                        "shared_cpu_list": s0_list,
                    },
                ],
            }

        # Socket 1 (CCD 0 is 192-207, 576-591 if 16-core or 192-199, 576-583 if 8-core)
        # Using 16 cores (32 threads) per CCD as in the issue description
        s1_primary = list(range(192 + ccd * 16, 192 + (ccd + 1) * 16))
        s1_smt = list(range(576 + ccd * 16, 576 + (ccd + 1) * 16))
        s1_all = sorted(s1_primary + s1_smt)
        s1_list = f"{s1_primary[0]}-{s1_primary[-1]},{s1_smt[0]}-{s1_smt[-1]}"
        for cpu in s1_all:
            cpus[cpu] = {
                "cpu_id": cpu,
                "online": True,
                "physical_package_id": 1,
                "core_id": (cpu - 192) % 384,
                "die_id": ccd,
                "cluster_id": ccd,
                "caches": [
                    {
                        "level": 1,
                        "type": "Data",
                        "id": cpu,
                        "size": "32K",
                        "shared_cpu_list": str(cpu),
                    },
                    {
                        "level": 2,
                        "type": "Unified",
                        "id": cpu,
                        "size": "1024K",
                        "shared_cpu_list": str(cpu),
                    },
                    {
                        "level": 3,
                        "type": "Unified",
                        "id": 24 + ccd,
                        "size": "32M",
                        "shared_cpu_list": s1_list,
                    },
                ],
            }

    return {
        "cpus": cpus,
        "nodes": nodes,
        "system": {
            "arch": "x86_64",
            "vendor": "AuthenticAMD",
            "model": "AMD EPYC 9965 384-Core Processor",
            "kernel": "6.8.0",
        },
    }


def make_intel_xeon_mock_data() -> dict[str, Any]:
    """Generate mock sysfs topology data for a 2-socket Intel Xeon with LLC per socket."""
    cpus: dict[int, Any] = {}
    nodes = {0: "0-31,64-95", 1: "32-63,96-127"}

    for socket_id in (0, 1):
        for core in range(32):
            cpu0 = socket_id * 32 + core  # Socket 0: 0-31; Socket 1: 32-63
            cpu1 = 64 + socket_id * 32 + core  # Socket 0: 64-95; Socket 1: 96-127
            shared_str = "0-31,64-95" if socket_id == 0 else "32-63,96-127"

            for cpu in (cpu0, cpu1):
                cpus[cpu] = {
                    "cpu_id": cpu,
                    "online": True,
                    "physical_package_id": socket_id,
                    "core_id": core,
                    "die_id": 0,
                    "caches": [
                        {
                            "level": 1,
                            "type": "Data",
                            "id": cpu,
                            "size": "48K",
                            "shared_cpu_list": str(cpu),
                        },
                        {
                            "level": 2,
                            "type": "Unified",
                            "id": core,
                            "size": "2048K",
                            "shared_cpu_list": f"{cpu0},{cpu1}",
                        },
                        {
                            "level": 3,
                            "type": "Unified",
                            "id": socket_id,
                            "size": "105M",
                            "shared_cpu_list": shared_str,
                        },
                    ],
                }

    return {
        "cpus": cpus,
        "nodes": nodes,
        "system": {
            "arch": "x86_64",
            "vendor": "GenuineIntel",
            "model": "Intel(R) Xeon(R) Platinum 8480+",
        },
    }


# ---------------------------------------------------------------------------
# Test parsing helper functions
# ---------------------------------------------------------------------------


class TestCpuListParsing:
    def test_empty_and_none(self):
        assert parse_cpu_list("") == []
        assert parse_cpu_list(None) == []
        assert parse_cpu_list("   ") == []

    def test_single_and_comma_separated(self):
        assert parse_cpu_list("0") == [0]
        assert parse_cpu_list("0,1,2,5") == [0, 1, 2, 5]
        assert parse_cpu_list(" 5, 2, 0 ") == [0, 2, 5]

    def test_ranges(self):
        assert parse_cpu_list("0-3") == [0, 1, 2, 3]
        assert parse_cpu_list("0-3,8,10-12") == [0, 1, 2, 3, 8, 10, 11, 12]
        assert parse_cpu_list("192-207,576-591") == list(range(192, 208)) + list(
            range(576, 592)
        )

    def test_overlapping_ranges(self):
        assert parse_cpu_list("0-4,2-6") == [0, 1, 2, 3, 4, 5, 6]

    def test_invalid_tokens_tolerated(self):
        assert parse_cpu_list("0-3,foo,5-invalid,8") == [0, 1, 2, 3, 8]


class TestCpuListFormatting:
    def test_empty(self):
        assert format_cpu_list([]) == ""

    def test_single(self):
        assert format_cpu_list([4]) == "4"

    def test_contiguous_range(self):
        assert format_cpu_list([0, 1, 2, 3]) == "0-3"

    def test_mixed_ranges(self):
        assert format_cpu_list([0, 1, 2, 3, 8, 10, 11, 12]) == "0-3,8,10-12"

    def test_turin_ccd_formatting(self):
        cpus = list(range(192, 208)) + list(range(576, 592))
        assert format_cpu_list(cpus) == "192-207,576-591"


class TestCpuMaskParsing:
    def test_empty_and_zero(self):
        assert parse_cpu_mask("") == []
        assert parse_cpu_mask("0") == []
        assert parse_cpu_mask(None) == []

    def test_single_hex_word(self):
        assert parse_cpu_mask("1") == [0]
        assert parse_cpu_mask("f") == [0, 1, 2, 3]
        assert parse_cpu_mask("0f") == [0, 1, 2, 3]
        assert parse_cpu_mask("ff") == list(range(8))

    def test_multi_word_mask(self):
        # "00000001,00000003": Word 1 (lower 32) has bits 0,1 (CPUs 0, 1); Word 0 (upper 32) has bit 0 (CPU 32)
        assert parse_cpu_mask("00000001,00000003") == [0, 1, 32]
        # "00000000,0000000f" -> [0, 1, 2, 3]
        assert parse_cpu_mask("00000000,0000000f") == [0, 1, 2, 3]


# ---------------------------------------------------------------------------
# Test Topology Parsing on Simulated Hardware
# ---------------------------------------------------------------------------


class TestAmdTurinTopology:
    def test_turin_all_sockets(self):
        data = make_turin_mock_data()
        result = parse_topology_data(data, host="turin-node.lab")

        assert result.host == "turin-node.lab"
        assert result.socket is None
        assert result.cache_level == 3
        assert result.source == "sysfs"
        assert result.vendor == "AuthenticAMD"
        assert "EPYC 9965" in (result.model or "")
        # 24 CCDs on Socket 0 + 24 CCDs on Socket 1 = 48 CCDs total
        assert result.total_ccds == 48
        assert len(result.domains) == 48

    def test_turin_socket_1_filtering(self):
        data = make_turin_mock_data()
        result = parse_topology_data(data, host="turin-node.lab", socket=1)

        assert result.socket == 1
        assert result.total_ccds == 24
        assert len(result.domains) == 24
        # CCD 0 on Socket 1 should have CPUs 192-207 and 576-591 (32 threads, 16 cores)
        ccd0_cpus = result.ccds[0]
        expected_ccd0 = list(range(192, 208)) + list(range(576, 592))
        assert ccd0_cpus == expected_ccd0

        domain0 = result.domains[0]
        assert domain0.ccd_id == 0
        assert domain0.socket_id == 1
        assert domain0.numa_node == 1
        assert domain0.cpu_list == "192-207,576-591"
        assert domain0.core_count == 16
        assert domain0.thread_count == 32
        assert domain0.size == "32M"

        # CCD 1 on Socket 1 should have CPUs 208-223 and 592-607
        ccd1_cpus = result.ccds[1]
        expected_ccd1 = list(range(208, 224)) + list(range(592, 608))
        assert ccd1_cpus == expected_ccd1

        # Dict serialization test
        d = result.to_dict()
        assert d["socket"] == 1
        assert "0" in d["ccds"]
        assert d["ccds"]["0"] == expected_ccd0
        assert len(d["domains"]) == 24

    def test_turin_socket_0_filtering(self):
        data = make_turin_mock_data()
        result = parse_topology_data(data, host="turin-node.lab", socket=0)

        assert result.socket == 0
        assert result.total_ccds == 24
        assert result.domains[0].socket_id == 0
        assert result.domains[0].numa_node == 0


class TestIntelXeonTopology:
    def test_xeon_llc_domains(self):
        data = make_intel_xeon_mock_data()
        result = parse_topology_data(data, host="xeon-node.lab")

        assert result.total_ccds == 2
        assert result.cache_level == 3
        assert result.domains[0].socket_id == 0
        assert result.domains[0].cpu_list == "0-31,64-95"
        assert result.domains[1].socket_id == 1
        assert result.domains[1].cpu_list == "32-63,96-127"

    def test_xeon_socket_filter(self):
        data = make_intel_xeon_mock_data()
        result = parse_topology_data(data, host="xeon-node.lab", socket=1)

        assert result.total_ccds == 1
        assert result.domains[0].socket_id == 1
        assert result.domains[0].cpu_list == "32-63,96-127"


# ---------------------------------------------------------------------------
# Test Fallback Chains
# ---------------------------------------------------------------------------


class TestFallbackChains:
    def test_lscpu_json_fallback(self):
        """When sysfs caches are missing, fallback to lscpu -e -J output."""
        lscpu_data = [
            {"cpu": 0, "socket": 0, "core": 0, "node": 0, "l3": "0"},
            {"cpu": 1, "socket": 0, "core": 1, "node": 0, "l3": "0"},
            {"cpu": 2, "socket": 0, "core": 2, "node": 0, "l3": "1"},
            {"cpu": 3, "socket": 0, "core": 3, "node": 0, "l3": "1"},
            {"cpu": 4, "socket": 1, "core": 0, "node": 1, "l3": "2"},
            {"cpu": 5, "socket": 1, "core": 1, "node": 1, "l3": "2"},
        ]
        raw = {
            "cpus": {},
            "lscpu": lscpu_data,
            "system": {"vendor": "AuthenticAMD", "arch": "x86_64"},
        }
        result = parse_topology_data(raw, host="fallback-host")

        assert result.source == "lscpu"
        assert result.total_ccds == 3
        assert result.ccds[0] == [0, 1]
        assert result.ccds[1] == [2, 3]
        assert result.ccds[2] == [4, 5]

    def test_lscpu_with_socket_filter(self):
        lscpu_data = [
            {"cpu": 0, "socket": 0, "core": 0, "node": 0, "l3": "0"},
            {"cpu": 1, "socket": 0, "core": 1, "node": 0, "l3": "0"},
            {"cpu": 4, "socket": 1, "core": 0, "node": 1, "l3": "1"},
            {"cpu": 5, "socket": 1, "core": 1, "node": 1, "l3": "1"},
        ]
        raw = {
            "cpus": {},
            "lscpu": lscpu_data,
            "system": {"vendor": "AuthenticAMD", "arch": "x86_64"},
        }
        result = parse_topology_data(raw, host="fallback-host", socket=1)

        assert result.source == "lscpu"
        assert result.total_ccds == 1
        assert result.ccds[0] == [4, 5]

    def test_cpuinfo_fallback(self):
        """When sysfs and lscpu are absent, fallback to /proc/cpuinfo."""
        cpuinfo = [
            {
                "processor": "0",
                "physical id": "0",
                "core id": "0",
                "apicid": "0",
                "vendor_id": "AuthenticAMD",
            },
            {
                "processor": "1",
                "physical id": "0",
                "core id": "1",
                "apicid": "1",
                "vendor_id": "AuthenticAMD",
            },
            {
                "processor": "2",
                "physical id": "1",
                "core id": "0",
                "apicid": "32",
                "vendor_id": "AuthenticAMD",
            },
            {
                "processor": "3",
                "physical id": "1",
                "core id": "1",
                "apicid": "33",
                "vendor_id": "AuthenticAMD",
            },
        ]
        raw = {
            "cpus": {},
            "cpuinfo": cpuinfo,
            "system": {"vendor": "AuthenticAMD", "arch": "x86_64"},
        }
        result = parse_topology_data(raw, host="cpuinfo-host")

        assert result.source == "cpuinfo_apic"
        assert result.total_ccds == 2

    def test_l2_cache_selected_if_no_l3(self):
        """If machine has only Level 2 cache, select Level 2 as LLC."""
        cpus = {
            0: {
                "cpu_id": 0,
                "online": True,
                "physical_package_id": 0,
                "core_id": 0,
                "caches": [
                    {
                        "level": 1,
                        "type": "Data",
                        "id": 0,
                        "shared_cpu_list": "0",
                    },
                    {
                        "level": 2,
                        "type": "Unified",
                        "id": 0,
                        "shared_cpu_list": "0,1",
                    },
                ],
            },
            1: {
                "cpu_id": 1,
                "online": True,
                "physical_package_id": 0,
                "core_id": 1,
                "caches": [
                    {
                        "level": 1,
                        "type": "Data",
                        "id": 1,
                        "shared_cpu_list": "1",
                    },
                    {
                        "level": 2,
                        "type": "Unified",
                        "id": 0,
                        "shared_cpu_list": "0,1",
                    },
                ],
            },
        }
        raw = {"cpus": cpus, "system": {"arch": "x86_64"}}
        result = parse_topology_data(raw, host="l2-host")

        assert result.cache_level == 2
        assert result.total_ccds == 1
        assert result.ccds[0] == [0, 1]


# ---------------------------------------------------------------------------
# Test SSH Execution with MockSSHExecutor
# ---------------------------------------------------------------------------


class TestDiscoverCacheTopologySSH:
    @pytest.mark.asyncio
    async def test_ssh_success(self):
        mock_data = make_turin_mock_data()
        mock_ssh = MockSSHExecutor(
            results={"python3 -c": SSHResult(exit_code=0, stdout=json.dumps(mock_data))}
        )

        res = await discover_cache_topology(mock_ssh, "10.0.0.1", socket=1)
        assert res["host"] == "10.0.0.1"
        assert res["socket"] == 1
        assert res["total_ccds"] == 24
        assert "0" in res["ccds"]
        assert res["ccds"]["0"] == list(range(192, 208)) + list(range(576, 592))

    @pytest.mark.asyncio
    async def test_ssh_script_fail_lscpu_fallback(self):
        lscpu_json = json.dumps(
            {
                "cpus": [
                    {
                        "cpu": 0,
                        "socket": 0,
                        "core": 0,
                        "node": 0,
                        "l3": "0",
                    },
                    {
                        "cpu": 1,
                        "socket": 0,
                        "core": 1,
                        "node": 0,
                        "l3": "0",
                    },
                ]
            }
        )
        mock_ssh = MockSSHExecutor(
            results={
                "python3 -c": SSHResult(exit_code=1, stderr="python3 not found"),
                "lscpu -e -J": SSHResult(exit_code=0, stdout=lscpu_json),
            }
        )

        res = await discover_cache_topology(mock_ssh, "10.0.0.1")
        assert res["source"] == "lscpu"
        assert res["total_ccds"] == 1
        assert res["ccds"]["0"] == [0, 1]

    @pytest.mark.asyncio
    async def test_ssh_all_fail(self):
        mock_ssh = MockSSHExecutor(
            results={
                "python3 -c": SSHResult(exit_code=1, stderr="SSH connection failed"),
                "lscpu -e -J": SSHResult(exit_code=1, stderr="Command not found"),
            }
        )

        res = await discover_cache_topology(mock_ssh, "10.0.0.1")
        assert "error" in res
        assert "Topology discovery failed" in res["error"]
        assert res["total_ccds"] == 0
