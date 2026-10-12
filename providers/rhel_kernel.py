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
_PACKAGE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
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
        if not _PACKAGE_NAME_RE.match(self.package):
            raise ValueError(
                f"package name {self.package!r} contains invalid characters"
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


def build_inventory_command(package: str = "kernel") -> str:
    if not _PACKAGE_NAME_RE.match(package):
        raise ValueError(f"invalid package name: {package!r}")
    return (
        "echo @@uname; uname -r;"
        " echo @@arch; uname -m;"
        " echo @@cmdline; cat /proc/cmdline;"
        " echo @@boot_id; cat /proc/sys/kernel/random/boot_id;"
        " echo @@os; cat /etc/os-release;"
        f" echo @@rpm; rpm -q {package} {package}-core"
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
    if not sections.get("uname", "").strip() or not sections.get("boot_id", "").strip():
        raise ValueError(
            f"INVENTORY output missing required sections"
            f" (got: {sorted(sections.keys())})"
        )

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

    installed_set: set[str] = set()
    for line in sections.get("rpm", "").splitlines():
        line = line.strip()
        if not line or "is not installed" in line:
            continue
        parts = line.split()
        if len(parts) >= 2:
            installed_set.add(parts[1])
    installed = sorted(installed_set)

    default_entry = sections.get("default", "").strip()
    try:
        default_index = int(sections.get("default_index", "-1").strip())
    except ValueError:
        default_index = -1

    entries = _parse_grubby_entries(sections.get("entries", ""))

    tuned_raw = sections.get("tuned", "").strip()
    tuned_profile = ""
    if "command not found" in tuned_raw or "No such file" in tuned_raw:
        tuned_profile = ""
    elif "No current active profile" in tuned_raw:
        tuned_profile = "No current active profile."
    else:
        for line in tuned_raw.splitlines():
            if "Current active profile:" in line:
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
            # verify_kernel_state can observe a never-attempted host already
            # running the wrong kernel.
            "kernel_mismatch",
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
    # A reconciliation after an indeterminate record can discover any
    # post-reboot outcome, not only success.
    "indeterminate": frozenset(
        {
            "verified",
            "fallback_boot",
            "kernel_mismatch",
        }
    ),
}


def prelaunch_kernel_check(
    probes: dict[str, ProbeResult],
    expected_release: str,
    checkpoints: dict[str, dict[str, Any]],
) -> tuple[bool, str, dict[str, str]]:
    """Check that all probed hosts are on the expected kernel.

    Returns (ok, reason_code, {host: issue}).
    """
    issues: dict[str, str] = {}
    for host, probe in probes.items():
        if probe.kernel != expected_release:
            issues[host] = (
                f"kernel_drift: running {probe.kernel}, expected {expected_release}"
            )
            continue
        cp = checkpoints.get(host, {})
        cp_boot_id = cp.get("post_boot_id")
        if cp_boot_id and probe.boot_id != cp_boot_id:
            issues[host] = (
                f"boot_drift: boot_id {probe.boot_id[:8]}..."
                f" != checkpoint {cp_boot_id[:8]}..."
            )
    if not issues:
        return True, "", {}
    if any("kernel_drift" in v for v in issues.values()):
        return False, "kernel_drift", issues
    return False, "boot_drift", issues


def validate_host_transition(from_state: str, to_state: str) -> None:
    """Raise ValueError if the transition is not legal.

    Writing the same state again (a checkpoint refresh) is always legal.
    """
    if from_state not in HOST_STATES:
        raise ValueError(f"unknown host state: {from_state!r}")
    if to_state not in HOST_STATES:
        raise ValueError(f"unknown host state: {to_state!r}")
    if from_state == to_state:
        return
    allowed = HOST_STATE_TRANSITIONS.get(from_state, frozenset())
    if to_state not in allowed:
        raise ValueError(f"illegal host transition: {from_state!r} -> {to_state!r}")


# Recovery decisions returned by recover_host().  They are deliberately not
# host states: the reboot driver maps each decision onto a state transition
# that the table above permits.
RECOVERY_DECISIONS = frozenset(
    {
        "normal",  # no usable prior record: run the ordinary pre-check path
        "already_verified",  # prior verified and the live probe still matches
        "refresh",  # prior verified, expected kernel, new boot_id, if_needed
        "reconciled",  # reboot already happened; expected kernel is up
        "fallback_boot",
        "kernel_mismatch",
        "in_flight",  # reboot sent but the host is still on the old boot
        "continue_reconnect",  # reboot sent and the host is unreachable
        "indeterminate",
    }
)

_RECOVERABLE_STATES = frozenset({"rebooting", "reconnecting", "indeterminate"})


def recover_host(
    prior: dict[str, Any] | None,
    probe: ProbeResult | None,
    reachable: bool,
    spec: KernelSpec,
    reboot_policy: str,
) -> tuple[str, str]:
    """Decide what a reboot invocation must do for *host* before anything else.

    This is the recovery table from the kernel A/B design: every invocation
    probes first, and a reboot that already happened in this plan step is
    never repeated.  ``prior`` is the host's checkpoint record from the
    current step (or None), ``probe`` is the live PROBE result (None when
    unparsable or unreachable), ``reachable`` says whether SSH answered.

    Returns ``(decision, note)`` with ``decision`` in RECOVERY_DECISIONS.
    """
    state = (prior or {}).get("state", "pending")
    if prior is None or state == "pending":
        return "normal", ""

    if state in ("verified", "already_verified"):
        if not reachable:
            return "normal", "previously verified host is unreachable"
        if probe is None:
            return "indeterminate", "unparsable probe on a verified host"
        if probe.kernel != spec.release:
            return "normal", "kernel changed since verification"
        if probe.boot_id == prior.get("post_boot_id"):
            return "already_verified", ""
        note = "boot_drift: host rebooted after verification"
        if reboot_policy == "if_needed":
            return "refresh", note
        return "normal", note + "; rebooting again for parity"

    if state in _RECOVERABLE_STATES:
        if not reachable:
            return "continue_reconnect", ""
        if probe is None:
            return "indeterminate", "unparsable probe during recovery"
        if probe.boot_id != prior.get("pre_boot_id"):
            if probe.kernel == spec.release:
                return "reconciled", "reboot completed before this invocation"
            if probe.kernel == prior.get("pre_kernel"):
                return "fallback_boot", ""
            return "kernel_mismatch", ""
        if state == "indeterminate":
            return "normal", "indeterminate prior record, host still on old boot"
        return "in_flight", "reboot command was sent; host still on the old boot"

    # Every other terminal failure state starts over; the caller must have a
    # fresh approval for any new reboot.
    return "normal", f"prior state {state!r} requires a new attempt"


def transition_state(
    host_records: dict[str, dict[str, Any]],
    required_hosts: list[str],
) -> str:
    """Summarise per-host records into the step-level transition state.

    ``verified`` requires every required host to be verified; a missing or
    refused host can never produce ``verified``.
    """
    states = {
        host: host_records.get(host, {}).get("state", "") for host in required_hosts
    }
    if not required_hosts:
        return "failed"
    if all(state in ("verified", "already_verified") for state in states.values()):
        return "verified"
    if any(
        state in ("pending", "rebooting", "reconnecting", "")
        for state in states.values()
    ):
        return "in_progress"
    if any(state == "indeterminate" for state in states.values()):
        return "indeterminate"
    if any(state in ("verified", "already_verified") for state in states.values()):
        return "partial_failure"
    return "failed"
