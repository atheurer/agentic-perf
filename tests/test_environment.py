"""Tests for providers/environment.py — parsing, fingerprinting, diffing."""

from __future__ import annotations

from providers.environment import (
    diff_snapshots,
    environment_fingerprint,
    parse_environment,
)

ENV_FIXTURE = """\
@@uname
5.14.0-503.14.1.el9_5.x86_64
@@arch
x86_64
@@cmdline
BOOT_IMAGE=/boot/vmlinuz-5.14.0-503.14.1.el9_5.x86_64 root=UUID=abc-123 ro
@@os
NAME="Red Hat Enterprise Linux"
ID="rhel"
VERSION_ID="9.5"
@@cpu
Architecture:          x86_64
CPU(s):                128
Socket(s):             2
Model name:            AMD EPYC 9654 96-Core Processor
@@numa
/sys/devices/system/node/node0
/sys/devices/system/node/node1
@@tuned
Current active profile: throughput-performance
@@thp
always [madvise] never
@@sysctl
4194304
4194304
cubic
fq_codel
1
@@nics
eth0 mlx5_core 100000 9000
eth1 mlx5_core 100000 1500
"""


class TestParseEnvironment:
    def test_full_output(self):
        env = parse_environment(ENV_FIXTURE)
        assert env["kernel_release"] == "5.14.0-503.14.1.el9_5.x86_64"
        assert env["arch"] == "x86_64"
        assert env["os_id"] == "rhel"
        assert env["os_version_id"] == "9.5"
        assert env["cpu"]["model"] == "AMD EPYC 9654 96-Core Processor"
        assert env["cpu"]["count"] == "128"
        assert env["cpu"]["sockets"] == "2"
        assert env["numa_nodes"] == 2
        assert env["tuned_profile"] == "throughput-performance"
        assert env["thp"] == "madvise"
        assert env["sysctls"]["net.core.rmem_max"] == "4194304"
        assert env["sysctls"]["net.ipv4.tcp_congestion_control"] == "cubic"
        assert len(env["nics"]) == 2
        assert env["nics"][0]["name"] == "eth0"
        assert env["nics"][0]["driver"] == "mlx5_core"
        assert env["nics"][0]["speed"] == "100000"
        assert env["nics"][0]["mtu"] == "9000"

    def test_partial_output(self):
        partial = "@@uname\n5.14.0-503.14.1.el9_5.x86_64\n@@arch\nx86_64\n"
        env = parse_environment(partial)
        assert env["kernel_release"] == "5.14.0-503.14.1.el9_5.x86_64"
        assert env["tuned_profile"] == ""
        assert env["nics"] == []

    def test_missing_tuned(self):
        no_tuned = ENV_FIXTURE.replace(
            "Current active profile: throughput-performance",
            "No current active profile.",
        )
        env = parse_environment(no_tuned)
        assert env["tuned_profile"] == "No current active profile."


class TestFingerprint:
    def test_stable(self):
        env = parse_environment(ENV_FIXTURE)
        fp1 = environment_fingerprint(env)
        fp2 = environment_fingerprint(env)
        assert fp1 == fp2
        assert len(fp1) == 64

    def test_invariant_to_boot_specific_fields(self):
        env1 = parse_environment(ENV_FIXTURE)
        modified = ENV_FIXTURE.replace(
            "root=UUID=abc-123",
            "root=UUID=def-456",
        )
        env2 = parse_environment(modified)
        assert environment_fingerprint(env1) == environment_fingerprint(env2)

    def test_changes_on_kernel(self):
        env1 = parse_environment(ENV_FIXTURE)
        modified = ENV_FIXTURE.replace(
            "5.14.0-503.14.1",
            "5.14.0-503.16.1",
        )
        env2 = parse_environment(modified)
        assert environment_fingerprint(env1) != environment_fingerprint(env2)

    def test_changes_on_tuned(self):
        env1 = parse_environment(ENV_FIXTURE)
        modified = ENV_FIXTURE.replace(
            "throughput-performance",
            "latency-performance",
        )
        env2 = parse_environment(modified)
        assert environment_fingerprint(env1) != environment_fingerprint(env2)

    def test_changes_on_sysctl(self):
        env1 = parse_environment(ENV_FIXTURE)
        modified = ENV_FIXTURE.replace("cubic", "bbr")
        env2 = parse_environment(modified)
        assert environment_fingerprint(env1) != environment_fingerprint(env2)


class TestDiffSnapshots:
    def test_identical(self):
        env = parse_environment(ENV_FIXTURE)
        diffs = diff_snapshots(env, env)
        assert diffs == []

    def test_kernel_diff_with_expected(self):
        env1 = parse_environment(ENV_FIXTURE)
        modified = ENV_FIXTURE.replace(
            "5.14.0-503.14.1",
            "5.14.0-503.16.1",
        )
        env2 = parse_environment(modified)
        diffs = diff_snapshots(
            env1, env2, expected_to_differ={"kernel_release", "cmdline"}
        )
        assert len(diffs) == 0

    def test_tuned_diff_reported(self):
        env1 = parse_environment(ENV_FIXTURE)
        modified = ENV_FIXTURE.replace(
            "throughput-performance",
            "latency-performance",
        )
        env2 = parse_environment(modified)
        diffs = diff_snapshots(env1, env2)
        tuned_diffs = [d for d in diffs if d["key"] == "tuned_profile"]
        assert len(tuned_diffs) == 1
        assert tuned_diffs[0]["a"] == "throughput-performance"
        assert tuned_diffs[0]["b"] == "latency-performance"

    def test_kernel_diff_without_expected(self):
        env1 = parse_environment(ENV_FIXTURE)
        modified = ENV_FIXTURE.replace(
            "5.14.0-503.14.1",
            "5.14.0-503.16.1",
        )
        env2 = parse_environment(modified)
        diffs = diff_snapshots(env1, env2)
        kernel_diffs = [d for d in diffs if d["key"] == "kernel_release"]
        assert len(kernel_diffs) == 1
