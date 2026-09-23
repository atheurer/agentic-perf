"""RHEL kernel command contract — command builders and parsers.

All commands are built from a validated KernelSpec. Sentinel markers
(@@section) make each probe a single SSH round trip with strict parsing.
The LLM never supplies a command string; every SSH call uses these
builders.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

KERNEL_RELEASE_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+-[A-Za-z0-9._+~]+$")
_MAX_RELEASE_LEN = 96


@dataclass(frozen=True)
class KernelSpec:
    """Validated kernel release identifier."""

    release: str
    package: str = "kernel"
    companion_packages: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.release:
            raise ValueError("kernel release must not be empty")
        if len(self.release) > _MAX_RELEASE_LEN:
            raise ValueError(f"kernel release exceeds {_MAX_RELEASE_LEN} chars")
        if not KERNEL_RELEASE_RE.match(self.release):
            raise ValueError(
                f"kernel release {self.release!r} does not match"
                f" {KERNEL_RELEASE_RE.pattern}"
            )
        for cp in self.companion_packages:
            if not KERNEL_RELEASE_RE.match(cp):
                raise ValueError(f"companion package {cp!r} is not a valid release")

    @classmethod
    def parse(cls, raw: dict | str) -> KernelSpec:
        if isinstance(raw, str):
            return cls(release=raw)
        if not isinstance(raw, dict):
            raise ValueError(f"expected str or dict, got {type(raw).__name__}")
        release = raw.get("release", "")
        if not release:
            raise ValueError("missing 'release' in kernel spec")
        package = raw.get("package", "kernel")
        companions = tuple(raw.get("companion_packages", ()))
        return cls(
            release=release,
            package=package,
            companion_packages=companions,
        )

    @property
    def nevra(self) -> str:
        return f"{self.package}-{self.release}"

    @property
    def vmlinuz(self) -> str:
        return f"/boot/vmlinuz-{self.release}"

    @property
    def initramfs(self) -> str:
        return f"/boot/initramfs-{self.release}.img"

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "release": self.release,
            "package": self.package,
        }
        if self.companion_packages:
            d["companion_packages"] = list(self.companion_packages)
        return d


# ---------------------------------------------------------------------------
# Command builders
# ---------------------------------------------------------------------------


def build_probe_command() -> str:
    return (
        "echo @@boot_id; cat /proc/sys/kernel/random/boot_id;"
        " echo @@uname; uname -r;"
        " echo @@default; grubby --default-kernel 2>&1"
    )


def build_inventory_command() -> str:
    return (
        "echo @@uname; uname -r;"
        " echo @@arch; uname -m;"
        " echo @@cmdline; cat /proc/cmdline;"
        " echo @@boot_id; cat /proc/sys/kernel/random/boot_id;"
        " echo @@os; cat /etc/os-release;"
        " echo @@rpm; rpm -q kernel kernel-core"
        " --qf '%{NAME} %{VERSION}-%{RELEASE}.%{ARCH}\\n' 2>&1;"
        " echo @@default; grubby --default-kernel 2>&1;"
        " echo @@default_index; grubby --default-index 2>&1;"
        " echo @@entries; grubby --info=ALL 2>&1;"
        " echo @@tuned; tuned-adm active 2>&1"
    )


def build_available_command(spec: KernelSpec) -> str:
    return f"dnf -q list --available {spec.nevra} 2>&1"


def build_install_command(spec: KernelSpec) -> str:
    parts = [spec.nevra]
    for cp in spec.companion_packages:
        parts.append(f"{spec.package}-{cp}")
    return f"dnf install -y {' '.join(parts)} 2>&1"


def build_select_command(spec: KernelSpec) -> str:
    return (
        f"test -f {spec.vmlinuz}"
        f" && test -f {spec.initramfs}"
        f" && grubby --info={spec.vmlinuz} >/dev/null 2>&1"
        f" && grubby --set-default={spec.vmlinuz} 2>&1"
        f" && grubby --default-kernel"
    )


def build_reboot_command() -> str:
    return "nohup sh -c 'sleep 2; systemctl reboot' >/dev/null 2>&1 & echo @@rebooting"


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def _split_sections(stdout: str) -> dict[str, str]:
    """Split sentinel-delimited output into {section: content}."""
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


@dataclass
class ProbeResult:
    boot_id: str
    kernel: str
    default_entry: str

    def to_dict(self) -> dict[str, str]:
        return {
            "boot_id": self.boot_id,
            "kernel": self.kernel,
            "default_entry": self.default_entry,
        }


def parse_probe(stdout: str) -> ProbeResult:
    sections = _split_sections(stdout)
    boot_id = sections.get("boot_id", "").strip()
    kernel = sections.get("uname", "").strip()
    default_entry = sections.get("default", "").strip()
    if not boot_id or not kernel:
        raise ValueError(
            f"PROBE output missing required sections (got: {sorted(sections.keys())})"
        )
    return ProbeResult(
        boot_id=boot_id,
        kernel=kernel,
        default_entry=default_entry,
    )


@dataclass
class GrubbyEntry:
    index: int
    kernel: str
    initrd: str
    title: str
    entry_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "kernel": self.kernel,
            "initrd": self.initrd,
            "title": self.title,
            "id": self.entry_id,
        }


def _parse_grubby_entries(text: str) -> list[GrubbyEntry]:
    entries: list[GrubbyEntry] = []
    current: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("index="):
            if current:
                entries.append(_grubby_entry_from_dict(current))
            current = {"index": line.split("=", 1)[1]}
        elif "=" in line:
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"')
            current[key] = val
    if current:
        entries.append(_grubby_entry_from_dict(current))
    return entries


def _grubby_entry_from_dict(d: dict[str, str]) -> GrubbyEntry:
    return GrubbyEntry(
        index=int(d.get("index", "-1")),
        kernel=d.get("kernel", ""),
        initrd=d.get("initrd", ""),
        title=d.get("title", ""),
        entry_id=d.get("id", ""),
    )


@dataclass
class KernelInventory:
    running: str
    arch: str
    cmdline: str
    boot_id: str
    os_id: str
    os_version_id: str
    installed: list[str]
    entries: list[GrubbyEntry]
    default_entry: str
    default_index: int
    tuned_profile: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "arch": self.arch,
            "cmdline": self.cmdline,
            "boot_id": self.boot_id,
            "os_id": self.os_id,
            "os_version_id": self.os_version_id,
            "installed": self.installed,
            "entries": [e.to_dict() for e in self.entries],
            "default_entry": self.default_entry,
            "default_index": self.default_index,
            "tuned_profile": self.tuned_profile,
        }


def parse_inventory(stdout: str) -> KernelInventory:
    sections = _split_sections(stdout)

    running = sections.get("uname", "").strip()
    arch = sections.get("arch", "").strip()
    cmdline = sections.get("cmdline", "").strip()
    boot_id = sections.get("boot_id", "").strip()

    os_id = ""
    os_version_id = ""
    for line in sections.get("os", "").splitlines():
        if line.startswith("ID="):
            os_id = line.split("=", 1)[1].strip().strip('"')
        elif line.startswith("VERSION_ID="):
            os_version_id = line.split("=", 1)[1].strip().strip('"')

    installed: list[str] = []
    for line in sections.get("rpm", "").splitlines():
        line = line.strip()
        if not line or "is not installed" in line:
            continue
        parts = line.split()
        if len(parts) >= 2:
            installed.append(parts[1])

    default_entry = sections.get("default", "").strip()
    try:
        default_index = int(sections.get("default_index", "-1").strip())
    except ValueError:
        default_index = -1

    entries = _parse_grubby_entries(sections.get("entries", ""))

    tuned_raw = sections.get("tuned", "").strip()
    tuned_profile = ""
    for line in tuned_raw.splitlines():
        if ":" in line:
            tuned_profile = line.split(":", 1)[1].strip()
            break
    if not tuned_profile:
        tuned_profile = tuned_raw.split("\n")[0].strip()

    return KernelInventory(
        running=running,
        arch=arch,
        cmdline=cmdline,
        boot_id=boot_id,
        os_id=os_id,
        os_version_id=os_version_id,
        installed=installed,
        entries=entries,
        default_entry=default_entry,
        default_index=default_index,
        tuned_profile=tuned_profile,
    )


def classify_available(exit_code: int, stdout: str) -> str:
    if exit_code == 0:
        return "available"
    if "No matching Packages" in stdout or "No matching packages" in stdout:
        return "not_found"
    return "not_checked"


def inventory_fingerprint(inv: KernelInventory) -> str:
    stable = {
        "running": inv.running,
        "installed": sorted(inv.installed),
        "default_entry": inv.default_entry,
        "arch": inv.arch,
        "os_id": inv.os_id,
        "os_version_id": inv.os_version_id,
    }
    canonical = json.dumps(stable, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Host state machine for reboot operations (PR2 uses this)
# ---------------------------------------------------------------------------

HOST_STATES = frozenset(
    {
        "pending",
        "already_verified",
        "precheck_failed",
        "refused",
        "rebooting",
        "reconnecting",
        "verified",
        "fallback_boot",
        "kernel_mismatch",
        "reconnect_timeout",
        "reboot_not_observed",
        "indeterminate",
        "not_attempted",
    }
)

TERMINAL_STATES = frozenset(
    {
        "already_verified",
        "precheck_failed",
        "refused",
        "verified",
        "fallback_boot",
        "kernel_mismatch",
        "reconnect_timeout",
        "reboot_not_observed",
        "indeterminate",
        "not_attempted",
    }
)

HOST_STATE_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset(
        {
            "already_verified",
            "precheck_failed",
            "refused",
            "rebooting",
            "verified",
            "not_attempted",
        }
    ),
    "rebooting": frozenset(
        {
            "reconnecting",
            "verified",
            "fallback_boot",
            "kernel_mismatch",
            "reboot_not_observed",
            "indeterminate",
            "reconnect_timeout",
        }
    ),
    "reconnecting": frozenset(
        {
            "verified",
            "fallback_boot",
            "kernel_mismatch",
            "reconnect_timeout",
            "indeterminate",
        }
    ),
    "indeterminate": frozenset(
        {
            "verified",
        }
    ),
}


def validate_host_transition(from_state: str, to_state: str) -> None:
    """Raise ValueError if the transition is not legal."""
    if from_state not in HOST_STATES:
        raise ValueError(f"unknown host state: {from_state!r}")
    if to_state not in HOST_STATES:
        raise ValueError(f"unknown host state: {to_state!r}")
    allowed = HOST_STATE_TRANSITIONS.get(from_state, frozenset())
    if to_state not in allowed:
        raise ValueError(f"illegal host transition: {from_state!r} -> {to_state!r}")
