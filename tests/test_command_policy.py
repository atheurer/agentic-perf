"""Unit tests for the infra MCP server command policy engine."""

from __future__ import annotations

import pytest

from agents.infra.command_policy import (
    CommandPolicy,
    check_command,
    load_policy,
)


@pytest.fixture
def provisioning_policy():
    return load_policy("provisioning-agent")


@pytest.fixture
def resource_policy():
    return load_policy("resource-agent")


@pytest.fixture
def review_policy():
    return load_policy("review-agent")


@pytest.fixture
def benchmark_policy():
    return load_policy("benchmark-agent")


class TestLoadPolicy:
    def test_load_provisioning(self, provisioning_policy):
        assert "git" in provisioning_policy.allowed_binaries
        assert "crucible" in provisioning_policy.allowed_binaries
        assert "podman" in provisioning_policy.allowed_binaries

    def test_load_resource(self, resource_policy):
        assert "echo" in resource_policy.allowed_binaries
        assert "hostname" in resource_policy.allowed_binaries
        assert "git" not in resource_policy.allowed_binaries
        assert resource_policy.max_timeout == 60

    def test_load_unknown_agent(self):
        policy = load_policy("unknown-agent")
        assert len(policy.allowed_binaries) == 0


class TestCheckCommand:
    def test_allowed_command(self, provisioning_policy):
        allowed, reason = check_command(
            "git clone https://example.com/repo", provisioning_policy
        )
        assert allowed
        assert reason == "OK"

    def test_binary_not_in_allowlist(self, resource_policy):
        allowed, reason = check_command(
            "git clone https://example.com/repo", resource_policy
        )
        assert not allowed
        assert "not in allowlist" in reason

    def test_global_blocked_rm_rf(self, provisioning_policy):
        allowed, reason = check_command("rm -rf /", provisioning_policy)
        assert not allowed
        assert "global safety" in reason

    def test_global_blocked_reboot(self, provisioning_policy):
        allowed, reason = check_command("reboot", provisioning_policy)
        assert not allowed
        assert "global safety" in reason

    def test_global_blocked_shutdown(self, provisioning_policy):
        allowed, reason = check_command("shutdown -h now", provisioning_policy)
        assert not allowed
        assert "global safety" in reason

    def test_global_blocked_mkfs(self, provisioning_policy):
        allowed, reason = check_command("mkfs.ext4 /dev/sda1", provisioning_policy)
        assert not allowed

    def test_global_blocked_dd(self, provisioning_policy):
        allowed, reason = check_command(
            "dd if=/dev/zero of=/dev/sda bs=1M", provisioning_policy
        )
        assert not allowed

    def test_allowed_rm_specific_path(self, provisioning_policy):
        allowed, _ = check_command("rm -rf /var/lib/crucible", provisioning_policy)
        assert allowed

    def test_empty_command(self, provisioning_policy):
        allowed, reason = check_command("", provisioning_policy)
        assert not allowed
        assert "Empty" in reason

    def test_empty_allowlist(self):
        policy = CommandPolicy(agent_name="empty")
        allowed, reason = check_command("echo hello", policy)
        assert not allowed
        assert "No binaries allowed" in reason

    def test_env_var_prefix(self, benchmark_policy):
        allowed, _ = check_command(
            "KUBECONFIG=/root/.kube/config kubectl get pods", benchmark_policy
        )
        assert allowed

    def test_absolute_path_binary(self, provisioning_policy):
        allowed, _ = check_command(
            "/usr/bin/git clone https://example.com", provisioning_policy
        )
        assert allowed

    def test_review_read_only(self, review_policy):
        allowed, _ = check_command("cat /var/log/messages", review_policy)
        assert allowed

        allowed, _ = check_command("rm -f /tmp/foo", review_policy)
        assert not allowed
        assert not allowed

    def test_resource_narrow_scope(self, resource_policy):
        allowed, _ = check_command("hostname -f", resource_policy)
        assert allowed

        allowed, _ = check_command("dnf install -y vim", resource_policy)
        assert not allowed

    def test_benchmark_harness_binaries(self, benchmark_policy):
        allowed, _ = check_command("crucible run /tmp/run.json", benchmark_policy)
        assert allowed

        allowed, _ = check_command("kube-burner init -c config.yaml", benchmark_policy)
        assert allowed

        allowed, _ = check_command("clusterbuster -f job.yaml", benchmark_policy)
        assert allowed


class TestBenchmarkDiscoveryCommands:
    """Verify the benchmark agent can run hardware discovery commands."""

    def test_block_devices(self, benchmark_policy):
        allowed, _ = check_command("lsblk -d -o NAME,SIZE,TYPE --json", benchmark_policy)
        assert allowed

    def test_network_interfaces(self, benchmark_policy):
        allowed, _ = check_command("ip -j link show", benchmark_policy)
        assert allowed

    def test_ethtool(self, benchmark_policy):
        allowed, _ = check_command("ethtool -i eth0", benchmark_policy)
        assert allowed

    def test_sysctl_read(self, benchmark_policy):
        allowed, _ = check_command("sysctl -a", benchmark_policy)
        assert allowed

    def test_numactl(self, benchmark_policy):
        allowed, _ = check_command("numactl --hardware", benchmark_policy)
        assert allowed

    def test_perf_stat(self, benchmark_policy):
        allowed, _ = check_command("perf stat -a sleep 1", benchmark_policy)
        assert allowed

    def test_dd_to_tmpfile(self, benchmark_policy):
        allowed, _ = check_command(
            "dd if=/dev/zero of=/tmp/testfile bs=1M count=100",
            benchmark_policy,
        )
        assert allowed

    def test_mpstat(self, benchmark_policy):
        allowed, _ = check_command("mpstat -P ALL 1 5", benchmark_policy)
        assert allowed

    def test_sar(self, benchmark_policy):
        allowed, _ = check_command("sar -n DEV 1 5", benchmark_policy)
        assert allowed

    def test_dmidecode(self, benchmark_policy):
        allowed, _ = check_command("dmidecode -t memory", benchmark_policy)
        assert allowed

    def test_dmesg(self, benchmark_policy):
        allowed, _ = check_command("dmesg | tail -50", benchmark_policy)
        assert allowed

    def test_journalctl(self, benchmark_policy):
        allowed, _ = check_command("journalctl -u crucible --no-pager -n 100", benchmark_policy)
        assert allowed


class TestBenchmarkBlockedPatterns:
    """Verify destructive commands are caught by blocked_patterns."""

    def test_dd_to_disk(self, benchmark_policy):
        allowed, _ = check_command(
            "dd if=/dev/zero of=/dev/sda bs=1M", benchmark_policy
        )
        assert not allowed

    def test_dd_to_nvme(self, benchmark_policy):
        allowed, _ = check_command(
            "dd if=/dev/zero of=/dev/nvme0n1 bs=4k", benchmark_policy
        )
        assert not allowed

    def test_mkfs(self, benchmark_policy):
        allowed, _ = check_command("mkfs.xfs /dev/sdb1", benchmark_policy)
        assert not allowed

    def test_fdisk(self, benchmark_policy):
        allowed, _ = check_command("fdisk /dev/sda", benchmark_policy)
        assert not allowed

    def test_parted(self, benchmark_policy):
        allowed, _ = check_command("parted /dev/sda mklabel gpt", benchmark_policy)
        assert not allowed

    def test_wipefs(self, benchmark_policy):
        allowed, _ = check_command("wipefs -a /dev/sdb", benchmark_policy)
        assert not allowed

    def test_iptables_flush(self, benchmark_policy):
        allowed, _ = check_command("iptables -F", benchmark_policy)
        assert not allowed

    def test_nft_flush(self, benchmark_policy):
        allowed, _ = check_command("nft flush ruleset", benchmark_policy)
        assert not allowed

    def test_disable_ip_forwarding(self, benchmark_policy):
        allowed, _ = check_command(
            "sysctl -w net.ipv4.ip_forward=0", benchmark_policy
        )
        assert not allowed

    def test_sysctl_write_allowed(self, benchmark_policy):
        """Non-blocked sysctl writes should be allowed (test system tuning)."""
        allowed, _ = check_command(
            "sysctl -w net.core.rmem_max=16777216", benchmark_policy
        )
        assert allowed
