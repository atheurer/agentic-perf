"""Tests for host tuning MCP tools: tune_nic, tune_tcp, pin_irq, verify_host_tuning."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from tests.conftest import MockSSHExecutor, MockSkillProvider, SSHResult


PROC_INTERRUPTS_ONE_QUEUE = """\
  0:         19   IO-APIC    2-edge      timer
 42:      12345   PCI-MSI    0-edge      eno16695np0-TxRx-0
 43:       9999   PCI-MSI    0-edge      eth0-TxRx-0
"""

PROC_INTERRUPTS_TWO_QUEUES = """\
 42:      12345   PCI-MSI    0-edge      eno16695np0-TxRx-0
 43:      12346   PCI-MSI    0-edge      eno16695np0-TxRx-1
"""

ETHTOOL_L_ONE_QUEUE = """\
Channel parameters for eno16695np0:
Pre-set maximums:
Combined:    16
Current hardware settings:
Combined:    1
"""

ETHTOOL_L_MULTI_QUEUE = """\
Channel parameters for eno16695np0:
Pre-set maximums:
Combined:    16
Current hardware settings:
Combined:    4
"""


def make_handlers(ssh: MockSSHExecutor):
    from agents.provisioning.mcp_server import create_provisioning_tool_handlers

    async def noop_clarification(q):
        pass

    with patch("agents.provisioning.mcp_server.SSHExecutor", return_value=ssh):
        handlers, _ = create_provisioning_tool_handlers(
            skill_provider=MockSkillProvider(),
            request_clarification_fn=noop_clarification,
        )
    return handlers


# ---------------------------------------------------------------------------
# tune_nic
# ---------------------------------------------------------------------------


class TestTuneNic:
    @pytest.mark.asyncio
    async def test_already_correct_channel_count(self):
        ssh = MockSSHExecutor(
            results={"ethtool -l": SSHResult(stdout=ETHTOOL_L_ONE_QUEUE)}
        )
        handlers = make_handlers(ssh)
        result = await handlers["tune_nic"](
            host="10.0.0.1", interface="eno16695np0", channels=1
        )
        assert result["status"] == "ok"
        assert any("already" in a for a in result["applied"])
        # Should NOT have issued ethtool -L
        assert not any("ethtool -L" in c["command"] for c in ssh.calls)

    @pytest.mark.asyncio
    async def test_reduces_channel_count(self):
        ssh = MockSSHExecutor(
            results={
                "ethtool -l": SSHResult(stdout=ETHTOOL_L_MULTI_QUEUE),
                "ethtool -L": SSHResult(stdout="", exit_code=0),
            }
        )
        handlers = make_handlers(ssh)
        result = await handlers["tune_nic"](
            host="10.0.0.1", interface="eno16695np0", channels=1
        )
        assert result["status"] == "ok"
        assert any("4 → 1" in a for a in result["applied"])

    @pytest.mark.asyncio
    async def test_sets_ring_buffers(self):
        ssh = MockSSHExecutor(
            results={
                "ethtool -l": SSHResult(stdout=ETHTOOL_L_ONE_QUEUE),
                "ethtool -G": SSHResult(stdout="", exit_code=0),
            }
        )
        handlers = make_handlers(ssh)
        result = await handlers["tune_nic"](
            host="10.0.0.1",
            interface="eno16695np0",
            channels=1,
            ring_rx=4096,
            ring_tx=4096,
        )
        assert result["status"] == "ok"
        assert any("ring" in a for a in result["applied"])

    @pytest.mark.asyncio
    async def test_sets_offloads(self):
        ssh = MockSSHExecutor(
            results={
                "ethtool -l": SSHResult(stdout=ETHTOOL_L_ONE_QUEUE),
                "ethtool -K": SSHResult(stdout="", exit_code=0),
            }
        )
        handlers = make_handlers(ssh)
        result = await handlers["tune_nic"](
            host="10.0.0.1",
            interface="eno16695np0",
            offloads={"gro": "on", "lro": "off"},
        )
        assert result["status"] == "ok"
        assert any("gro=on" in a for a in result["applied"])

    @pytest.mark.asyncio
    async def test_ethtool_l_failure_returns_error(self):
        ssh = MockSSHExecutor(
            results={
                "ethtool -l": SSHResult(stdout=ETHTOOL_L_MULTI_QUEUE),
                "ethtool -L": SSHResult(stdout="Operation not supported", exit_code=1),
            }
        )
        handlers = make_handlers(ssh)
        result = await handlers["tune_nic"](
            host="10.0.0.1", interface="eno16695np0", channels=1
        )
        assert result["status"] == "error"
        assert result["errors"]


# ---------------------------------------------------------------------------
# tune_tcp
# ---------------------------------------------------------------------------


class TestTuneTcp:
    @pytest.mark.asyncio
    async def test_sets_bbr_and_fq(self):
        ssh = MockSSHExecutor(
            results={
                "sysctl -n net.ipv4.tcp_congestion_control": SSHResult(stdout="cubic"),
                "sysctl -n net.core.default_qdisc": SSHResult(stdout="fq_codel"),
                "sysctl -w net.ipv4.tcp_congestion_control": SSHResult(stdout="ok"),
                "sysctl -w net.core.default_qdisc": SSHResult(stdout="ok"),
            }
        )
        # After write, verify reads return the new value
        call_counts: dict[str, int] = {}

        async def smart_run(host, command, timeout=300):
            call_counts[command] = call_counts.get(command, 0) + 1
            if "sysctl -n net.ipv4.tcp_congestion_control" in command:
                return SSHResult(
                    stdout="cubic" if call_counts.get(command, 1) == 1 else "bbr"
                )
            if "sysctl -n net.core.default_qdisc" in command:
                return SSHResult(
                    stdout="fq_codel" if call_counts.get(command, 1) == 1 else "fq"
                )
            return SSHResult(stdout="ok")

        ssh.run = smart_run  # type: ignore[method-assign]
        handlers = make_handlers(ssh)
        result = await handlers["tune_tcp"](
            host="10.0.0.1", congestion_control="bbr", qdisc="fq"
        )
        assert result["status"] == "ok"
        assert "net.ipv4.tcp_congestion_control" in result["sysctls"]
        assert "net.core.default_qdisc" in result["sysctls"]

    @pytest.mark.asyncio
    async def test_sysctl_write_failure_reported(self):
        ssh = MockSSHExecutor(
            results={
                "sysctl -w net.ipv4.tcp_congestion_control": SSHResult(
                    stdout="Permission denied", exit_code=1
                ),
            }
        )
        handlers = make_handlers(ssh)
        result = await handlers["tune_tcp"](
            host="10.0.0.1", congestion_control="bbr"
        )
        assert result["status"] == "error"
        assert result["errors"]

    @pytest.mark.asyncio
    async def test_extra_sysctls_applied(self):
        applied = []

        async def tracking_run(host, command, timeout=300):
            if "sysctl -w" in command:
                applied.append(command)
            return SSHResult(stdout="ok")

        ssh = MockSSHExecutor()
        ssh.run = tracking_run  # type: ignore[method-assign]
        handlers = make_handlers(ssh)
        await handlers["tune_tcp"](
            host="10.0.0.1",
            extra_sysctls={"net.core.rmem_max": "134217728"},
        )
        assert any("rmem_max" in c for c in applied)


# ---------------------------------------------------------------------------
# pin_irq
# ---------------------------------------------------------------------------


class TestPinIrq:
    @pytest.mark.asyncio
    async def test_happy_path_ban_irq(self):
        ssh = MockSSHExecutor(
            results={
                "/proc/interrupts": SSHResult(stdout=PROC_INTERRUPTS_ONE_QUEUE),
                "smp_affinity": SSHResult(stdout="", exit_code=0),
                "IRQBALANCE_BANNED_INTERRUPTS": SSHResult(stdout=""),
                "systemctl restart irqbalance": SSHResult(stdout="", exit_code=0),
            }
        )
        handlers = make_handlers(ssh)
        result = await handlers["pin_irq"](
            host="10.0.0.1", interface="eno16695np0", cpu=194
        )
        assert result["status"] == "ok"
        assert result["irq_numbers"] == [42]
        assert result["cpu"] == 194
        assert result["irqbalance"]["mode"] == "ban_irq"

    @pytest.mark.asyncio
    async def test_irq_not_found_returns_error(self):
        ssh = MockSSHExecutor(
            results={
                "/proc/interrupts": SSHResult(stdout="  0:  19  timer\n"),
            }
        )
        handlers = make_handlers(ssh)
        result = await handlers["pin_irq"](
            host="10.0.0.1", interface="eno16695np0", cpu=194
        )
        assert result["status"] == "error"
        assert "No IRQ found" in result["errors"][0]

    @pytest.mark.asyncio
    async def test_multiple_irqs_pinned(self):
        ssh = MockSSHExecutor(
            results={
                "/proc/interrupts": SSHResult(stdout=PROC_INTERRUPTS_TWO_QUEUES),
                "smp_affinity": SSHResult(stdout="", exit_code=0),
                "IRQBALANCE_BANNED_INTERRUPTS": SSHResult(stdout=""),
                "systemctl restart irqbalance": SSHResult(stdout="", exit_code=0),
            }
        )
        handlers = make_handlers(ssh)
        result = await handlers["pin_irq"](
            host="10.0.0.1", interface="eno16695np0", cpu=194
        )
        assert result["status"] == "ok"
        assert sorted(result["irq_numbers"]) == [42, 43]
        assert len(result["applied"]) == 2

    @pytest.mark.asyncio
    async def test_disable_mode_masks_irqbalance(self):
        commands_run = []

        async def tracking_run(host, command, timeout=300):
            commands_run.append(command)
            if "/proc/interrupts" in command:
                return SSHResult(stdout=PROC_INTERRUPTS_ONE_QUEUE)
            return SSHResult(stdout="", exit_code=0)

        ssh = MockSSHExecutor()
        ssh.run = tracking_run  # type: ignore[method-assign]
        handlers = make_handlers(ssh)
        result = await handlers["pin_irq"](
            host="10.0.0.1",
            interface="eno16695np0",
            cpu=194,
            irqbalance_mode="disable",
        )
        assert result["status"] == "ok"
        assert result["irqbalance"]["mode"] == "disable"
        assert any("mask irqbalance" in c for c in commands_run)

    @pytest.mark.asyncio
    async def test_ban_cpu_mode(self):
        commands_run = []

        async def tracking_run(host, command, timeout=300):
            commands_run.append(command)
            if "/proc/interrupts" in command:
                return SSHResult(stdout=PROC_INTERRUPTS_ONE_QUEUE)
            if "IRQBALANCE_BANNED_CPUS" in command:
                return SSHResult(stdout="")
            return SSHResult(stdout="", exit_code=0)

        ssh = MockSSHExecutor()
        ssh.run = tracking_run  # type: ignore[method-assign]
        handlers = make_handlers(ssh)
        result = await handlers["pin_irq"](
            host="10.0.0.1",
            interface="eno16695np0",
            cpu=194,
            irqbalance_mode="ban_cpu",
        )
        assert result["status"] == "ok"
        assert result["irqbalance"]["mode"] == "ban_cpu"
        assert any("IRQBALANCE_BANNED_CPUS" in c for c in commands_run)


# ---------------------------------------------------------------------------
# verify_host_tuning
# ---------------------------------------------------------------------------


class TestVerifyHostTuning:
    @pytest.mark.asyncio
    async def test_all_correct(self):
        ssh = MockSSHExecutor(
            results={
                "net.ipv4.tcp_congestion_control": SSHResult(stdout="bbr"),
                "net.core.default_qdisc": SSHResult(stdout="fq"),
                "ethtool -l": SSHResult(stdout=ETHTOOL_L_ONE_QUEUE),
                "/proc/interrupts": SSHResult(stdout=PROC_INTERRUPTS_ONE_QUEUE),
                "smp_affinity_list": SSHResult(stdout="194"),
                "is-active irqbalance": SSHResult(stdout="inactive"),
            }
        )
        handlers = make_handlers(ssh)
        result = await handlers["verify_host_tuning"](
            host="10.0.0.1",
            interface="eno16695np0",
            expected={
                "congestion_control": "bbr",
                "qdisc": "fq",
                "channels": 1,
                "irq_cpu": 194,
                "irqbalance_mode": "disable",
            },
        )
        assert result["all_ok"] is True
        assert result["checks"]["net.ipv4.tcp_congestion_control"]["ok"] is True
        assert result["checks"]["net.core.default_qdisc"]["ok"] is True
        assert result["checks"]["channels"]["ok"] is True

    @pytest.mark.asyncio
    async def test_wrong_qdisc_detected(self):
        ssh = MockSSHExecutor(
            results={
                "net.ipv4.tcp_congestion_control": SSHResult(stdout="bbr"),
                "net.core.default_qdisc": SSHResult(stdout="fq_codel"),
                "ethtool -l": SSHResult(stdout=ETHTOOL_L_ONE_QUEUE),
                "/proc/interrupts": SSHResult(stdout=PROC_INTERRUPTS_ONE_QUEUE),
                "smp_affinity_list": SSHResult(stdout="194"),
                "is-active irqbalance": SSHResult(stdout="inactive"),
            }
        )
        handlers = make_handlers(ssh)
        result = await handlers["verify_host_tuning"](
            host="10.0.0.1",
            interface="eno16695np0",
            expected={"congestion_control": "bbr", "qdisc": "fq"},
        )
        assert result["all_ok"] is False
        assert result["checks"]["net.core.default_qdisc"]["ok"] is False
        assert result["checks"]["net.core.default_qdisc"]["actual"] == "fq_codel"

    @pytest.mark.asyncio
    async def test_irqbalance_drift_detected(self):
        ssh = MockSSHExecutor(
            results={
                "net.ipv4.tcp_congestion_control": SSHResult(stdout="bbr"),
                "net.core.default_qdisc": SSHResult(stdout="fq"),
                "ethtool -l": SSHResult(stdout=ETHTOOL_L_ONE_QUEUE),
                "/proc/interrupts": SSHResult(stdout=PROC_INTERRUPTS_ONE_QUEUE),
                "smp_affinity_list": SSHResult(stdout="194"),
                "is-active irqbalance": SSHResult(stdout="active"),
            }
        )
        handlers = make_handlers(ssh)
        result = await handlers["verify_host_tuning"](
            host="10.0.0.1",
            interface="eno16695np0",
            expected={"irqbalance_mode": "disable"},
        )
        assert result["all_ok"] is False
        assert result["checks"]["irqbalance"]["active"] is True

    @pytest.mark.asyncio
    async def test_no_expected_values_runs_without_error(self):
        ssh = MockSSHExecutor(
            results={
                "net.ipv4.tcp_congestion_control": SSHResult(stdout="bbr"),
                "net.core.default_qdisc": SSHResult(stdout="fq"),
                "ethtool -l": SSHResult(stdout=ETHTOOL_L_ONE_QUEUE),
                "/proc/interrupts": SSHResult(stdout=PROC_INTERRUPTS_ONE_QUEUE),
                "smp_affinity_list": SSHResult(stdout="194"),
                "is-active irqbalance": SSHResult(stdout="inactive"),
            }
        )
        handlers = make_handlers(ssh)
        result = await handlers["verify_host_tuning"](
            host="10.0.0.1", interface="eno16695np0"
        )
        # With no expected values, all checks are unconstrained — all_ok stays True
        assert result["all_ok"] is True
        assert "checks" in result
