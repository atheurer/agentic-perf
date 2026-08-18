"""Tests for get_interface_inventory tool in agents/infra/server.py."""

from __future__ import annotations

import json

from tests.conftest import MockSSHExecutor, SSHResult

HOST = "test-host.example.com"

_SAMPLE_IFACES = [
    {
        "name": "eno16695np0",
        "link": "up",
        "speed_mbps": 400000,
        "mtu": 9000,
        "numa_node": 1,
        "mac": "aa:bb:cc:dd:ee:01",
        "ipv4": ["172.21.131.9/16"],
        "ipv6": [],
        "ipv6_link_local": ["fe80::1/64"],
    },
    {
        "name": "eno17105np1",
        "link": "up",
        "speed_mbps": 100000,
        "mtu": 1500,
        "numa_node": 0,
        "mac": "aa:bb:cc:dd:ee:02",
        "ipv4": ["10.0.1.1/24"],
        "ipv6": [],
        "ipv6_link_local": ["fe80::2/64"],
    },
    {
        "name": "eno17415np2",
        "link": "up",
        "speed_mbps": 25000,
        "mtu": 1500,
        "numa_node": 0,
        "mac": "aa:bb:cc:dd:ee:03",
        "ipv4": [],
        "ipv6": [],
        "ipv6_link_local": [],
    },
    {
        "name": "eno17095np0",
        "link": "down",
        "speed_mbps": 100000,
        "mtu": 1500,
        "numa_node": 0,
        "mac": "aa:bb:cc:dd:ee:04",
        "ipv4": [],
        "ipv6": ["2001:db8::1/64"],
        "ipv6_link_local": [],
    },
]


def _make_ssh(ifaces=None, error=False):
    if error:
        return MockSSHExecutor(
            {"python3": SSHResult(exit_code=1, stderr="ssh: connect failed")}
        )
    payload = json.dumps(ifaces if ifaces is not None else _SAMPLE_IFACES)
    return MockSSHExecutor({"python3": SSHResult(exit_code=0, stdout=payload)})


async def _call(ssh, **kwargs):
    import agents.infra.server as srv

    srv._ssh = ssh
    return json.loads(await srv.get_interface_inventory(HOST, **kwargs))


class TestGetInterfaceInventoryNoFilter:
    async def test_returns_all_non_loopback(self):
        result = await _call(_make_ssh())
        assert result["host"] == HOST
        assert result["count"] == 4
        names = {i["name"] for i in result["interfaces"]}
        assert "eno16695np0" in names
        assert "eno17415np2" in names

    async def test_speed_gbps_computed(self):
        result = await _call(_make_ssh())
        iface = next(i for i in result["interfaces"] if i["name"] == "eno16695np0")
        assert iface["speed_gbps"] == 400.0

    async def test_speed_gbps_100g(self):
        result = await _call(_make_ssh())
        iface = next(i for i in result["interfaces"] if i["name"] == "eno17105np1")
        assert iface["speed_gbps"] == 100.0

    async def test_no_filters_applied_key_empty(self):
        result = await _call(_make_ssh())
        assert result["filters_applied"] == {}


class TestLinkFilter:
    async def test_link_up_only(self):
        result = await _call(_make_ssh(), link="up")
        names = {i["name"] for i in result["interfaces"]}
        assert "eno17095np0" not in names  # down
        assert result["count"] == 3

    async def test_link_down_only(self):
        result = await _call(_make_ssh(), link="down")
        assert result["count"] == 1
        assert result["interfaces"][0]["name"] == "eno17095np0"


class TestSpeedFilter:
    async def test_min_speed_100g_excludes_25g(self):
        result = await _call(_make_ssh(), min_speed_gbps=100)
        names = {i["name"] for i in result["interfaces"]}
        assert "eno17415np2" not in names
        assert result["count"] == 3

    async def test_min_speed_400g_keeps_only_400g(self):
        result = await _call(_make_ssh(), min_speed_gbps=400)
        assert result["count"] == 1
        assert result["interfaces"][0]["name"] == "eno16695np0"

    async def test_max_speed_25g(self):
        result = await _call(_make_ssh(), max_speed_gbps=25)
        assert result["count"] == 1
        assert result["interfaces"][0]["name"] == "eno17415np2"


class TestNameRegexFilter:
    async def test_name_regex_eno166(self):
        result = await _call(_make_ssh(), name_regex="eno166")
        assert result["count"] == 1
        assert result["interfaces"][0]["name"] == "eno16695np0"

    async def test_name_regex_np1(self):
        result = await _call(_make_ssh(), name_regex=r"np1$")
        assert result["count"] == 1
        assert result["interfaces"][0]["name"] == "eno17105np1"


class TestIPv4Filter:
    async def test_ipv4_present(self):
        result = await _call(_make_ssh(), ipv4="present")
        names = {i["name"] for i in result["interfaces"]}
        assert "eno16695np0" in names
        assert "eno17105np1" in names
        assert "eno17415np2" not in names

    async def test_ipv4_absent(self):
        result = await _call(_make_ssh(), ipv4="absent")
        names = {i["name"] for i in result["interfaces"]}
        assert "eno17415np2" in names
        assert "eno17105np1" not in names

    async def test_ipv4_regex(self):
        result = await _call(_make_ssh(), ipv4=r"^172\.")
        assert result["count"] == 1
        assert result["interfaces"][0]["name"] == "eno16695np0"


class TestNumaNodeFilter:
    async def test_numa_node_1(self):
        result = await _call(_make_ssh(), numa_node=1)
        assert result["count"] == 1
        assert result["interfaces"][0]["name"] == "eno16695np0"

    async def test_numa_node_0(self):
        result = await _call(_make_ssh(), numa_node=0)
        names = {i["name"] for i in result["interfaces"]}
        assert "eno16695np0" not in names
        assert result["count"] == 3

    async def test_no_numa_filter_sentinel(self):
        # -2 is the sentinel for "no filter"
        result = await _call(_make_ssh(), numa_node=-2)
        assert result["count"] == 4


class TestErrorHandling:
    async def test_ssh_error_returns_error_dict(self):
        result = await _call(_make_ssh(error=True))
        assert "error" in result
        assert result["host"] == HOST

    async def test_filters_applied_recorded(self):
        result = await _call(_make_ssh(), link="up", min_speed_gbps=100, numa_node=0)
        fa = result["filters_applied"]
        assert fa["link"] == "up"
        assert fa["min_speed_gbps"] == 100
        assert fa["numa_node"] == 0
