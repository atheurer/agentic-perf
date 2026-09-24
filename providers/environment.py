"""Environment fingerprinting for kernel A/B comparisons.

Captures a stable per-host environment snapshot and computes a
deterministic fingerprint. The fingerprint changes when the kernel,
tuning, or hardware configuration changes but is invariant to
boot_id, MACs, IPs, and timestamps.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


def build_env_command() -> str:
    """Build the SSH command to capture environment data."""
    return (
        "echo @@uname; uname -r;"
        " echo @@arch; uname -m;"
        " echo @@cmdline; cat /proc/cmdline;"
        " echo @@os; cat /etc/os-release;"
        " echo @@cpu; lscpu 2>&1;"
        " echo @@numa; ls -d /sys/devices/system/node/node* 2>&1;"
        " echo @@tuned; tuned-adm active 2>&1;"
        " echo @@thp; cat /sys/kernel/mm/transparent_hugepage/enabled 2>&1;"
        " echo @@sysctl; sysctl -n"
        " net.core.rmem_max net.core.wmem_max"
        " net.ipv4.tcp_congestion_control net.core.default_qdisc"
        " kernel.numa_balancing 2>&1;"
        " echo @@nics; for n in /sys/class/net/*; do"
        ' b=$(basename $n); [ "$b" = lo ] && continue;'
        " printf '%s %s %s %s\\n' \"$b\""
        ' "$(basename $(readlink $n/device/driver 2>/dev/null) 2>/dev/null)"'
        ' "$(cat $n/speed 2>/dev/null)"'
        ' "$(cat $n/mtu 2>/dev/null)";'
        " done"
    )


def _split_sections(stdout: str) -> dict[str, str]:
    """Split @@-delimited output into sections."""
    sections: dict[str, str] = {}
    current: str | None = None
    lines: list[str] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("@@"):
            if current is not None:
                sections[current] = "\n".join(lines).strip()
            current = stripped[2:]
            lines = []
        elif current is not None:
            lines.append(line)
    if current is not None:
        sections[current] = "\n".join(lines).strip()
    return sections


def parse_environment(stdout: str) -> dict[str, Any]:
    """Parse environment probe output into a structured snapshot."""
    sections = _split_sections(stdout)

    kernel = sections.get("uname", "").strip()
    arch = sections.get("arch", "").strip()
    if not kernel:
        raise ValueError(
            f"ENV output missing kernel release (sections: {sorted(sections.keys())})"
        )
    cmdline = sections.get("cmdline", "").strip()

    os_id = ""
    os_version_id = ""
    for line in sections.get("os", "").splitlines():
        if line.startswith("ID="):
            os_id = line.split("=", 1)[1].strip().strip('"')
        elif line.startswith("VERSION_ID="):
            os_version_id = line.split("=", 1)[1].strip().strip('"')

    cpu = _parse_lscpu(sections.get("cpu", ""))

    numa_raw = sections.get("numa", "")
    numa_nodes = 0
    if "No such file" not in numa_raw:
        numa_nodes = len([ln for ln in numa_raw.splitlines() if ln.strip()])

    tuned_raw = sections.get("tuned", "").strip()
    tuned_profile = ""
    for line in tuned_raw.splitlines():
        if ":" in line:
            tuned_profile = line.split(":", 1)[1].strip()
            break
    if not tuned_profile:
        tuned_profile = tuned_raw.split("\n")[0].strip()

    thp_raw = sections.get("thp", "").strip()
    thp = ""
    match = re.search(r"\[(\w+)\]", thp_raw)
    if match:
        thp = match.group(1)
    elif thp_raw:
        thp = thp_raw

    sysctl_lines = sections.get("sysctl", "").strip().splitlines()
    sysctl_keys = [
        "net.core.rmem_max",
        "net.core.wmem_max",
        "net.ipv4.tcp_congestion_control",
        "net.core.default_qdisc",
        "kernel.numa_balancing",
    ]
    sysctls: dict[str, str] = {}
    for i, key in enumerate(sysctl_keys):
        if i < len(sysctl_lines):
            sysctls[key] = sysctl_lines[i].strip()

    nics = _parse_nics(sections.get("nics", ""))

    return {
        "kernel_release": kernel,
        "arch": arch,
        "cmdline": cmdline,
        "os_id": os_id,
        "os_version_id": os_version_id,
        "cpu": cpu,
        "numa_nodes": numa_nodes,
        "tuned_profile": tuned_profile,
        "thp": thp,
        "sysctls": sysctls,
        "nics": nics,
    }


def _parse_lscpu(text: str) -> dict[str, str]:
    """Extract key fields from lscpu output."""
    result: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key = key.strip()
        val = val.strip()
        if key == "Model name":
            result["model"] = val
        elif key == "CPU(s)":
            result["count"] = val
        elif key == "Socket(s)":
            result["sockets"] = val
    return result


def _parse_nics(text: str) -> list[dict[str, str]]:
    """Parse NIC info from the probe output."""
    nics: list[dict[str, str]] = []
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) >= 1:
            nic: dict[str, str] = {"name": parts[0]}
            if len(parts) >= 2:
                nic["driver"] = parts[1]
            if len(parts) >= 3:
                nic["speed"] = parts[2]
            if len(parts) >= 4:
                nic["mtu"] = parts[3]
            nics.append(nic)
    return nics


_CMDLINE_STRIP = re.compile(
    r"\bBOOT_IMAGE=\S*|\broot=\S*|"
    r"\b[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)


def _stable_cmdline(cmdline: str) -> str:
    """Remove boot-specific tokens from the kernel cmdline."""
    stripped = _CMDLINE_STRIP.sub("", cmdline)
    return " ".join(stripped.split())


def environment_fingerprint(snapshot: dict[str, Any]) -> str:
    """Compute a stable fingerprint from an environment snapshot."""
    stable = {
        "kernel_release": snapshot.get("kernel_release", ""),
        "cmdline": _stable_cmdline(snapshot.get("cmdline", "")),
        "arch": snapshot.get("arch", ""),
        "os_id": snapshot.get("os_id", ""),
        "os_version_id": snapshot.get("os_version_id", ""),
        "cpu": snapshot.get("cpu", {}),
        "numa_nodes": snapshot.get("numa_nodes", 0),
        "tuned_profile": snapshot.get("tuned_profile", ""),
        "thp": snapshot.get("thp", ""),
        "sysctls": snapshot.get("sysctls", {}),
        "nics": sorted(
            [
                {
                    "driver": n.get("driver", ""),
                    "speed": n.get("speed", ""),
                    "mtu": n.get("mtu", ""),
                }
                for n in snapshot.get("nics", [])
            ],
            key=lambda x: x.get("driver", ""),
        ),
    }
    canonical = json.dumps(stable, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def diff_snapshots(
    a: dict[str, Any],
    b: dict[str, Any],
    expected_to_differ: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Compare two environment snapshots.

    Returns a list of {key, a_value, b_value} for each difference,
    excluding keys in expected_to_differ.
    """
    expected = expected_to_differ or set()
    diffs: list[dict[str, Any]] = []

    all_keys = sorted(set(a.keys()) | set(b.keys()))
    for key in all_keys:
        if key in expected:
            continue
        a_val = a.get(key)
        b_val = b.get(key)
        if a_val != b_val:
            diffs.append({"key": key, "a": a_val, "b": b_val})
    return diffs
