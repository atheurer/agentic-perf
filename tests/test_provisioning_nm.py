"""Tests for NetworkManager provisioning tools."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from tests.conftest import MockSkillProvider, MockSSHExecutor, SSHResult


def make_handlers(ssh: MockSSHExecutor):
    from agents.provisioning.mcp_server import create_provisioning_tool_handlers

    async def noop(q):
        pass

    with patch("agents.provisioning.mcp_server.SSHExecutor", return_value=ssh):
        handlers, _ = create_provisioning_tool_handlers(
            skill_provider=MockSkillProvider(),
            request_clarification_fn=noop,
        )
    return handlers


# ---------------------------------------------------------------------------
# nm_set_mtu
# ---------------------------------------------------------------------------


class TestNmSetMtu:
    @pytest.mark.asyncio
    async def test_sets_mtu_via_nmcli(self):
        commands = []

        async def tracking_run(host, command, timeout=300):
            commands.append(command)
            if "ip link show" in command and "grep" in command:
                # before: 1500, after: 9000
                call_n = sum(1 for c in commands if "ip link show" in c and "grep" in c)
                return SSHResult(stdout=f"mtu {1500 if call_n == 1 else 9000}")
            if "nmcli connection show --active" in command:
                return SSHResult(stdout="eno16695np0:eno16695np0")
            return SSHResult(stdout="", exit_code=0)

        ssh = MockSSHExecutor()
        ssh.run = tracking_run  # type: ignore[method-assign]
        handlers = make_handlers(ssh)
        result = await handlers["nm_set_mtu"](
            host="10.0.0.1", interface="eno16695np0", mtu=9000
        )
        assert result["status"] == "ok"
        assert result["ok"] is True
        assert any("802-3-ethernet.mtu 9000" in c for c in commands)
        assert any("nmcli connection up" in c for c in commands)

    @pytest.mark.asyncio
    async def test_returns_error_on_nmcli_failure(self):
        ssh = MockSSHExecutor(
            results={
                "nmcli connection show --active": SSHResult(
                    stdout="eno16695np0:eno16695np0"
                ),
                "nmcli connection modify": SSHResult(
                    stdout="Error: unknown connection", exit_code=1
                ),
            }
        )
        handlers = make_handlers(ssh)
        result = await handlers["nm_set_mtu"](
            host="10.0.0.1", interface="eno16695np0", mtu=9000
        )
        assert result["status"] == "error"


# ---------------------------------------------------------------------------
# nm_set_ip
# ---------------------------------------------------------------------------


class TestNmSetIp:
    @pytest.mark.asyncio
    async def test_sets_static_ip(self):
        commands = []

        async def tracking_run(host, command, timeout=300):
            commands.append(command)
            if "ip addr show" in command:
                return SSHResult(
                    stdout="    inet 172.16.0.1/24 brd 172.16.0.255 scope global eno16695np0"
                )
            if "nmcli connection show --active" in command:
                return SSHResult(stdout="eno16695np0:eno16695np0")
            return SSHResult(stdout="", exit_code=0)

        ssh = MockSSHExecutor()
        ssh.run = tracking_run  # type: ignore[method-assign]
        handlers = make_handlers(ssh)
        result = await handlers["nm_set_ip"](
            host="10.0.0.1",
            interface="eno16695np0",
            ip_cidr="172.16.0.1/24",
        )
        assert result["status"] == "ok"
        assert "172.16.0.1" in result["live_addresses"]
        assert any("ipv4.method manual" in c for c in commands)
        assert any("172.16.0.1/24" in c for c in commands)
        assert any("nmcli connection up" in c for c in commands)

    @pytest.mark.asyncio
    async def test_sets_gateway_and_dns_when_provided(self):
        commands = []

        async def tracking_run(host, command, timeout=300):
            commands.append(command)
            if "nmcli connection show --active" in command:
                return SSHResult(stdout="eno16695np0:eno16695np0")
            return SSHResult(stdout="", exit_code=0)

        ssh = MockSSHExecutor()
        ssh.run = tracking_run  # type: ignore[method-assign]
        handlers = make_handlers(ssh)
        await handlers["nm_set_ip"](
            host="10.0.0.1",
            interface="eno16695np0",
            ip_cidr="172.16.0.1/24",
            gateway="172.16.0.254",
            dns="8.8.8.8",
        )
        assert any("ipv4.gateway" in c and "172.16.0.254" in c for c in commands)
        assert any("ipv4.dns" in c and "8.8.8.8" in c for c in commands)


# ---------------------------------------------------------------------------
# nm_set_dhcp
# ---------------------------------------------------------------------------


class TestNmSetDhcp:
    @pytest.mark.asyncio
    async def test_switches_to_dhcp(self):
        commands = []

        async def tracking_run(host, command, timeout=300):
            commands.append(command)
            if "nmcli connection show --active" in command:
                return SSHResult(stdout="eno16695np0:eno16695np0")
            return SSHResult(stdout="", exit_code=0)

        ssh = MockSSHExecutor()
        ssh.run = tracking_run  # type: ignore[method-assign]
        handlers = make_handlers(ssh)
        result = await handlers["nm_set_dhcp"](host="10.0.0.1", interface="eno16695np0")
        assert result["status"] == "ok"
        assert any("ipv4.method auto" in c for c in commands)


# ---------------------------------------------------------------------------
# nm_show_connection
# ---------------------------------------------------------------------------


class TestNmShowConnection:
    @pytest.mark.asyncio
    async def test_returns_profile_and_live_state(self):
        async def tracking_run(host, command, timeout=300):
            if "--active" in command:
                return SSHResult(stdout="eno16695np0:eno16695np0")
            if "nmcli connection show" in command:
                return SSHResult(
                    stdout="ipv4.method: manual\nipv4.addresses: 172.16.0.1/24\n802-3-ethernet.mtu: 9000"
                )
            if "ip link show" in command:
                return SSHResult(stdout="mtu 9000")
            if "ip addr show" in command:
                return SSHResult(stdout="    inet 172.16.0.1/24")
            return SSHResult(stdout="", exit_code=0)

        ssh = MockSSHExecutor()
        ssh.run = tracking_run  # type: ignore[method-assign]
        handlers = make_handlers(ssh)
        result = await handlers["nm_show_connection"](
            host="10.0.0.1", interface="eno16695np0"
        )
        assert "9000" in result["profile"] or "9000" in result["live"]
        assert "eno16695np0" in result["connection"]


# ---------------------------------------------------------------------------
# nm_verify_interface
# ---------------------------------------------------------------------------


class TestNmVerifyInterface:
    @pytest.mark.asyncio
    async def test_correct_mtu_and_ip(self):
        ssh = MockSSHExecutor(
            results={
                "ip link show": SSHResult(
                    stdout="4: eno16695np0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 9000 qdisc fq state UP"
                ),
                "ip addr show": SSHResult(
                    stdout="    inet 172.16.0.1/24 brd 172.16.0.255 scope global eno16695np0\n"
                ),
            }
        )
        handlers = make_handlers(ssh)
        result = await handlers["nm_verify_interface"](
            host="10.0.0.1",
            interface="eno16695np0",
            expected_mtu=9000,
            expected_ip="172.16.0.1",
        )
        assert result["all_ok"] is True
        assert result["checks"]["mtu"]["ok"] is True
        assert result["checks"]["ip"]["ok"] is True
        assert result["checks"]["state"]["ok"] is True

    @pytest.mark.asyncio
    async def test_wrong_mtu_detected(self):
        ssh = MockSSHExecutor(
            results={
                "ip link show": SSHResult(
                    stdout="4: eno16695np0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc fq state UP"
                ),
                "ip addr show": SSHResult(stdout="    inet 172.16.0.1/24\n"),
            }
        )
        handlers = make_handlers(ssh)
        result = await handlers["nm_verify_interface"](
            host="10.0.0.1",
            interface="eno16695np0",
            expected_mtu=9000,
        )
        assert result["all_ok"] is False
        assert result["checks"]["mtu"]["ok"] is False
        assert result["checks"]["mtu"]["actual"] == 1500

    @pytest.mark.asyncio
    async def test_interface_down_detected(self):
        ssh = MockSSHExecutor(
            results={
                "ip link show": SSHResult(
                    stdout="4: eno16695np0: <BROADCAST,MULTICAST> mtu 1500 qdisc fq state DOWN"
                ),
                "ip addr show": SSHResult(stdout=""),
            }
        )
        handlers = make_handlers(ssh)
        result = await handlers["nm_verify_interface"](
            host="10.0.0.1", interface="eno16695np0"
        )
        assert result["checks"]["state"]["ok"] is False
        assert result["all_ok"] is False

    @pytest.mark.asyncio
    async def test_no_expected_values_still_checks_state(self):
        ssh = MockSSHExecutor(
            results={
                "ip link show": SSHResult(
                    stdout="4: eno16695np0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 9000 qdisc fq state UP"
                ),
                "ip addr show": SSHResult(stdout="    inet 172.16.0.1/24\n"),
            }
        )
        handlers = make_handlers(ssh)
        result = await handlers["nm_verify_interface"](
            host="10.0.0.1", interface="eno16695np0"
        )
        assert result["checks"]["state"]["ok"] is True
        assert result["all_ok"] is True
