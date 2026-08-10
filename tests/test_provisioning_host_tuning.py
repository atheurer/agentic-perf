"""Tests for host tuning MCP tools: tune_nic, tune_tcp, pin_irq, verify_host_tuning."""

from __future__ import annotations

import pytest

from tests.conftest import MockSSHExecutor, SSHResult, make_provisioning_handlers

PROC_INTERRUPTS_ONE_QUEUE = """\
  0:         19   IO-APIC    2-edge      timer
 42:      12345   PCI-MSI    0-edge      eno16695np0-TxRx-0
 43:       9999   PCI-MSI    0-edge      eth0-TxRx-0
"""

PROC_INTERRUPTS_TWO_QUEUES = """\
 42:      12345   PCI-MSI    0-edge      eno16695np0-TxRx-0
 43:      12346   PCI-MSI    0-edge      eno16695np0-TxRx-1
"""

# mlx5-style /proc/interrupts: interrupts are named by PCI address, not
# interface name — a plain interface-name substring match finds nothing.
PROC_INTERRUPTS_MLX5 = """\
407:      12345   PCI-MSI-edge      mlx5_comp0@pci:0000:21:00.0
408:      12346   PCI-MSI-edge      mlx5_comp1@pci:0000:21:00.0
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
    return make_provisioning_handlers(ssh)


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
    async def test_sets_bbr_and_fq_with_tc(self):
        commands_run = []

        async def smart_run(host, command, timeout=300):
            commands_run.append(command)
            if "sysctl -n net.ipv4.tcp_congestion_control" in command:
                return SSHResult(stdout="bbr")
            if "sysctl -n net.core.default_qdisc" in command:
                return SSHResult(stdout="fq")
            if "tc qdisc show" in command:
                return SSHResult(stdout="qdisc fq 8001: root refcnt 2")
            return SSHResult(stdout="ok")

        ssh = MockSSHExecutor()
        ssh.run = smart_run  # type: ignore[method-assign]
        handlers = make_handlers(ssh)
        result = await handlers["tune_tcp"](
            host="10.0.0.1",
            interface="eno16695np0",
            congestion_control="bbr",
            qdisc="fq",
        )
        assert result["status"] == "ok"
        assert "net.ipv4.tcp_congestion_control" in result["sysctls"]
        # tc qdisc replace must have been called on the interface
        assert any("tc qdisc replace" in c and "eno16695np0" in c for c in commands_run)
        assert result["tc_qdisc"]["ok"] is True

    @pytest.mark.asyncio
    async def test_qdisc_without_interface_skips_tc(self):
        commands_run = []

        async def tracking_run(host, command, timeout=300):
            commands_run.append(command)
            return SSHResult(stdout="ok")

        ssh = MockSSHExecutor()
        ssh.run = tracking_run  # type: ignore[method-assign]
        handlers = make_handlers(ssh)
        result = await handlers["tune_tcp"](host="10.0.0.1", qdisc="fq")
        assert not any("tc qdisc" in c for c in commands_run)
        assert result["tc_qdisc"] == {}

    @pytest.mark.asyncio
    async def test_sets_buffer_sizes(self):
        applied = []

        async def tracking_run(host, command, timeout=300):
            if "sysctl -w" in command:
                applied.append(command)
            return SSHResult(stdout="ok")

        ssh = MockSSHExecutor()
        ssh.run = tracking_run  # type: ignore[method-assign]
        handlers = make_handlers(ssh)
        await handlers["tune_tcp"](
            host="10.0.0.1", rmem_max=134217728, wmem_max=134217728
        )
        assert any("rmem_max=134217728" in c for c in applied)
        assert any("wmem_max=134217728" in c for c in applied)

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
        result = await handlers["tune_tcp"](host="10.0.0.1", congestion_control="bbr")
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
                "msi_irqs": SSHResult(stdout="42\n"),
                "smp_affinity": SSHResult(stdout="", exit_code=0),
                "IRQBALANCE_BANNED_INTERRUPTS": SSHResult(stdout=""),
                "systemctl restart irqbalance": SSHResult(stdout="", exit_code=0),
            }
        )
        handlers = make_handlers(ssh)
        result = await handlers["pin_irq"](
            host="10.0.0.1", interface="eno16695np0", cpus=[194]
        )
        assert result["status"] == "ok"
        assert result["irq_numbers"] == [42]
        assert result["assignments"] == [{"irq": 42, "cpu": 194}]
        assert result["irqbalance"]["mode"] == "ban_irq"

    @pytest.mark.asyncio
    async def test_smp_affinity_write_has_no_0x_prefix(self):
        """Regression test: /proc/irq/N/smp_affinity rejects a "0x"-prefixed
        mask on systems with enough CPUs to need the comma-grouped 32-bit-word
        format (confirmed live on a 128-CPU host — `echo 0x4 > smp_affinity`
        fails, `echo 4 > smp_affinity` succeeds)."""
        commands_run = []

        async def tracking_run(host, command, timeout=300):
            commands_run.append(command)
            if "msi_irqs" in command:
                return SSHResult(stdout="408\n")
            return SSHResult(stdout="", exit_code=0)

        ssh = MockSSHExecutor()
        ssh.run = tracking_run  # type: ignore[method-assign]
        handlers = make_handlers(ssh)
        result = await handlers["pin_irq"](
            host="10.0.0.1", interface="ens1f0np0", cpus=[2]
        )
        assert result["status"] == "ok"
        affinity_writes = [c for c in commands_run if "smp_affinity" in c]
        assert len(affinity_writes) == 1
        assert "echo 4 >" in affinity_writes[0]
        assert "0x" not in affinity_writes[0]

    @pytest.mark.asyncio
    async def test_msi_irqs_discovery_ignores_proc_interrupts_naming(self):
        """Primary regression test for #496/#499: msi_irqs discovery must
        succeed even when /proc/interrupts uses PCI-address naming (mlx5)
        with no interface name present at all."""
        ssh = MockSSHExecutor(
            results={
                "msi_irqs": SSHResult(stdout="407\n408\n"),
                "/proc/interrupts": SSHResult(stdout=PROC_INTERRUPTS_MLX5),
                "smp_affinity": SSHResult(stdout="", exit_code=0),
                "IRQBALANCE_BANNED_INTERRUPTS": SSHResult(stdout=""),
                "systemctl restart irqbalance": SSHResult(stdout="", exit_code=0),
            }
        )
        handlers = make_handlers(ssh)
        result = await handlers["pin_irq"](
            host="10.0.0.1", interface="ens1f0np0", cpus=[2]
        )
        assert result["status"] == "ok"
        assert result["irq_numbers"] == [407, 408]

    @pytest.mark.asyncio
    async def test_falls_back_to_proc_interrupts_by_pci_when_msi_irqs_missing(self):
        ssh = MockSSHExecutor(
            results={
                "msi_irqs": SSHResult(
                    stdout="ls: cannot access: No such file or directory",
                    exit_code=2,
                ),
                "basename": SSHResult(stdout="0000:21:00.0"),
                "/proc/interrupts": SSHResult(stdout=PROC_INTERRUPTS_MLX5),
                "smp_affinity": SSHResult(stdout="", exit_code=0),
                "IRQBALANCE_BANNED_INTERRUPTS": SSHResult(stdout=""),
                "systemctl restart irqbalance": SSHResult(stdout="", exit_code=0),
            }
        )
        handlers = make_handlers(ssh)
        result = await handlers["pin_irq"](
            host="10.0.0.1", interface="ens1f0np0", cpus=[2]
        )
        assert result["status"] == "ok"
        assert result["irq_numbers"] == [407, 408]
        assert result["pci"] == "0000:21:00.0"

    @pytest.mark.asyncio
    async def test_explicit_irqs_skips_discovery(self):
        commands_run = []

        async def tracking_run(host, command, timeout=300):
            commands_run.append(command)
            return SSHResult(stdout="", exit_code=0)

        ssh = MockSSHExecutor()
        ssh.run = tracking_run  # type: ignore[method-assign]
        handlers = make_handlers(ssh)
        result = await handlers["pin_irq"](host="10.0.0.1", irqs=[407], cpus=[2])
        assert result["status"] == "ok"
        assert result["irq_numbers"] == [407]
        assert not any("msi_irqs" in c or "/proc/interrupts" in c for c in commands_run)

    @pytest.mark.asyncio
    async def test_requires_device_identifier(self):
        ssh = MockSSHExecutor()
        handlers = make_handlers(ssh)
        result = await handlers["pin_irq"](host="10.0.0.1", cpus=[2])
        assert result["status"] == "error"
        assert "interface, pci, or irqs" in result["errors"][0]

    @pytest.mark.asyncio
    async def test_no_irqs_found_returns_error(self):
        ssh = MockSSHExecutor(
            results={
                "msi_irqs": SSHResult(
                    stdout="ls: cannot access: No such file or directory",
                    exit_code=2,
                ),
                "/proc/interrupts": SSHResult(stdout="  0:  19  timer\n"),
            }
        )
        handlers = make_handlers(ssh)
        result = await handlers["pin_irq"](
            host="10.0.0.1", interface="eno16695np0", cpus=[194]
        )
        assert result["status"] == "error"
        assert "No IRQs found" in result["errors"][0]

    @pytest.mark.asyncio
    async def test_explicit_cpu_list_round_robins(self):
        ssh = MockSSHExecutor(
            results={
                "msi_irqs": SSHResult(stdout="407\n408\n409\n"),
                "smp_affinity": SSHResult(stdout="", exit_code=0),
                "IRQBALANCE_BANNED_INTERRUPTS": SSHResult(stdout=""),
                "systemctl restart irqbalance": SSHResult(stdout="", exit_code=0),
            }
        )
        handlers = make_handlers(ssh)
        result = await handlers["pin_irq"](
            host="10.0.0.1", interface="ens1f0np0", cpus=[2, 3]
        )
        assert result["status"] == "ok"
        assert result["assignments"] == [
            {"irq": 407, "cpu": 2},
            {"irq": 408, "cpu": 3},
            {"irq": 409, "cpu": 2},
        ]

    @pytest.mark.asyncio
    async def test_auto_detects_local_numa_node(self):
        ssh = MockSSHExecutor(
            results={
                "msi_irqs": SSHResult(stdout="407\n"),
                "numa_node": SSHResult(stdout="1"),
                "cpulist": SSHResult(stdout="192-199"),
                "smp_affinity": SSHResult(stdout="", exit_code=0),
                "IRQBALANCE_BANNED_INTERRUPTS": SSHResult(stdout=""),
                "systemctl restart irqbalance": SSHResult(stdout="", exit_code=0),
            }
        )
        handlers = make_handlers(ssh)
        result = await handlers["pin_irq"](host="10.0.0.1", interface="ens1f0np0")
        assert result["status"] == "ok"
        assert result["target_mode"].startswith("numa_node:1")
        assert result["assignments"] == [{"irq": 407, "cpu": 192}]

    @pytest.mark.asyncio
    async def test_explicit_numa_node_skips_local_detection(self):
        commands_run = []

        async def tracking_run(host, command, timeout=300):
            commands_run.append(command)
            if "msi_irqs" in command:
                return SSHResult(stdout="407\n408\n")
            if "cpulist" in command:
                return SSHResult(stdout="64-71")
            return SSHResult(stdout="", exit_code=0)

        ssh = MockSSHExecutor()
        ssh.run = tracking_run  # type: ignore[method-assign]
        handlers = make_handlers(ssh)
        result = await handlers["pin_irq"](
            host="10.0.0.1", interface="ens1f0np0", numa_node=2
        )
        assert result["status"] == "ok"
        # Explicit numa_node must skip the local-node auto-detection read.
        assert not any("cat" in c and "/device/numa_node" in c for c in commands_run)
        assert result["target_mode"] == "numa_node:2"
        assert result["assignments"] == [
            {"irq": 407, "cpu": 64},
            {"irq": 408, "cpu": 65},
        ]

    @pytest.mark.asyncio
    async def test_disable_mode_masks_irqbalance(self):
        commands_run = []

        async def tracking_run(host, command, timeout=300):
            commands_run.append(command)
            if "msi_irqs" in command:
                return SSHResult(stdout="42\n")
            return SSHResult(stdout="", exit_code=0)

        ssh = MockSSHExecutor()
        ssh.run = tracking_run  # type: ignore[method-assign]
        handlers = make_handlers(ssh)
        result = await handlers["pin_irq"](
            host="10.0.0.1",
            interface="eno16695np0",
            cpus=[194],
            irqbalance_mode="disable",
        )
        assert result["status"] == "ok"
        assert result["irqbalance"]["mode"] == "disable"
        assert any("mask irqbalance" in c for c in commands_run)

    @pytest.mark.asyncio
    async def test_ban_cpu_bans_union_of_used_cpus(self):
        commands_run = []

        async def tracking_run(host, command, timeout=300):
            commands_run.append(command)
            if "msi_irqs" in command:
                return SSHResult(stdout="407\n408\n")
            if "IRQBALANCE_BANNED_CPUS" in command:
                return SSHResult(stdout="")
            return SSHResult(stdout="", exit_code=0)

        ssh = MockSSHExecutor()
        ssh.run = tracking_run  # type: ignore[method-assign]
        handlers = make_handlers(ssh)
        result = await handlers["pin_irq"](
            host="10.0.0.1",
            interface="ens1f0np0",
            cpus=[2, 3],
            irqbalance_mode="ban_cpu",
        )
        assert result["status"] == "ok"
        assert result["irqbalance"]["mode"] == "ban_cpu"
        mask = int(result["irqbalance"]["banned_cpus_mask"], 16)
        assert mask == (1 << 2) | (1 << 3)


# ---------------------------------------------------------------------------
# reset_irq_pinning
# ---------------------------------------------------------------------------


class TestResetIrqPinning:
    @pytest.mark.asyncio
    async def test_reset_restores_default_affinity_and_clears_matching_ban(self):
        commands_run = []

        async def tracking_run(host, command, timeout=300):
            commands_run.append(command)
            if "msi_irqs" in command:
                return SSHResult(stdout="407\n408\n")
            if "default_smp_affinity" in command:
                return SSHResult(stdout="ffffffff")
            if "IRQBALANCE_BANNED_INTERRUPTS" in command:
                return SSHResult(stdout='IRQBALANCE_BANNED_INTERRUPTS="407 408 999"')
            return SSHResult(stdout="", exit_code=0)

        ssh = MockSSHExecutor()
        ssh.run = tracking_run  # type: ignore[method-assign]
        handlers = make_handlers(ssh)
        result = await handlers["reset_irq_pinning"](
            host="10.0.0.1", interface="ens1f0np0"
        )
        assert result["status"] == "ok"
        assert any("restored to default" in a for a in result["applied"])
        assert any("unmasked and restarted" in a for a in result["applied"])
        # Only this device's IRQs (407, 408) are stripped — the unrelated
        # IRQ 999 banned by something else must survive.
        assert any('IRQBALANCE_BANNED_INTERRUPTS="999"' in c for c in commands_run)
        assert not any('IRQBALANCE_BANNED_INTERRUPTS="407' in c for c in commands_run)

    @pytest.mark.asyncio
    async def test_reset_clears_banned_cpus_when_given(self):
        async def tracking_run(host, command, timeout=300):
            if "msi_irqs" in command:
                return SSHResult(stdout="407\n")
            if "IRQBALANCE_BANNED_CPUS" in command:
                return SSHResult(stdout='IRQBALANCE_BANNED_CPUS="0xc"')
            return SSHResult(stdout="", exit_code=0)

        ssh = MockSSHExecutor()
        ssh.run = tracking_run  # type: ignore[method-assign]
        handlers = make_handlers(ssh)
        result = await handlers["reset_irq_pinning"](
            host="10.0.0.1", interface="ens1f0np0", cpus=[2, 3]
        )
        assert result["status"] == "ok"
        assert any("IRQBALANCE_BANNED_CPUS cleared" in a for a in result["applied"])

    @pytest.mark.asyncio
    async def test_reset_requires_device_identifier(self):
        ssh = MockSSHExecutor()
        handlers = make_handlers(ssh)
        result = await handlers["reset_irq_pinning"](host="10.0.0.1")
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_reset_no_irqs_found_returns_error(self):
        ssh = MockSSHExecutor(
            results={
                "msi_irqs": SSHResult(
                    stdout="ls: cannot access: No such file or directory",
                    exit_code=2,
                ),
                "/proc/interrupts": SSHResult(stdout="  0:  19  timer\n"),
            }
        )
        handlers = make_handlers(ssh)
        result = await handlers["reset_irq_pinning"](
            host="10.0.0.1", interface="eno16695np0"
        )
        assert result["status"] == "error"


# ---------------------------------------------------------------------------
# verify_host_tuning
# ---------------------------------------------------------------------------


class TestVerifyHostTuning:
    @pytest.mark.asyncio
    async def test_all_correct(self):
        ssh = MockSSHExecutor(
            results={
                "net.ipv4.tcp_congestion_control": SSHResult(stdout="bbr"),
                "tc qdisc show": SSHResult(stdout="qdisc fq 8001: root refcnt 2"),
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
                "irq_assignments": [{"irq": 42, "cpu": 194}],
                "irqbalance_mode": "disable",
            },
        )
        assert result["all_ok"] is True
        assert result["checks"]["net.ipv4.tcp_congestion_control"]["ok"] is True
        assert result["checks"]["qdisc"]["ok"] is True
        assert result["checks"]["channels"]["ok"] is True
        assert result["checks"]["irq"]["ok"] is True

    @pytest.mark.asyncio
    async def test_irq_mismatch_detected(self):
        ssh = MockSSHExecutor(
            results={
                "net.ipv4.tcp_congestion_control": SSHResult(stdout="bbr"),
                "tc qdisc show": SSHResult(stdout="qdisc fq 8001: root refcnt 2"),
                "ethtool -l": SSHResult(stdout=ETHTOOL_L_ONE_QUEUE),
                "/proc/interrupts": SSHResult(stdout=PROC_INTERRUPTS_TWO_QUEUES),
                "smp_affinity_list": SSHResult(stdout="3"),
                "is-active irqbalance": SSHResult(stdout="inactive"),
            }
        )
        handlers = make_handlers(ssh)
        result = await handlers["verify_host_tuning"](
            host="10.0.0.1",
            interface="eno16695np0",
            expected={
                "irq_assignments": [
                    {"irq": 42, "cpu": 194},
                    {"irq": 43, "cpu": 195},
                ]
            },
        )
        assert result["all_ok"] is False
        assert len(result["checks"]["irq"]["mismatches"]) == 2

    @pytest.mark.asyncio
    async def test_wrong_qdisc_detected(self):
        async def mock_run(host, command, timeout=300):
            if "tc qdisc show" in command:
                return SSHResult(stdout="qdisc fq_codel 0: root refcnt 2")
            if "tcp_congestion_control" in command:
                return SSHResult(stdout="bbr")
            if "ethtool -l" in command:
                return SSHResult(stdout=ETHTOOL_L_ONE_QUEUE)
            if "/proc/interrupts" in command:
                return SSHResult(stdout=PROC_INTERRUPTS_ONE_QUEUE)
            if "smp_affinity_list" in command:
                return SSHResult(stdout="194")
            if "is-active" in command:
                return SSHResult(stdout="inactive")
            return SSHResult(stdout="ok")

        ssh = MockSSHExecutor()
        ssh.run = mock_run  # type: ignore[method-assign]
        handlers = make_handlers(ssh)
        result = await handlers["verify_host_tuning"](
            host="10.0.0.1",
            interface="eno16695np0",
            expected={"congestion_control": "bbr", "qdisc": "fq"},
        )
        assert result["all_ok"] is False
        assert result["checks"]["qdisc"]["ok"] is False
        assert result["checks"]["qdisc"]["actual"] == "fq_codel"

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
