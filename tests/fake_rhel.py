"""Fake RHEL host simulator for kernel tool testing.

Provides stateful SSH simulation — reboots change the running kernel,
dnf installs move packages from available to installed, grubby tracks
the default entry. Used by all kernel-related test files.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from providers.ssh import SSHResult


@dataclass
class FakeRHELHost:
    """Simulates the RHEL 9 command surface for one host."""

    name: str
    running: str = "5.14.0-503.14.1.el9_5.x86_64"
    installed: list[str] = field(
        default_factory=lambda: [
            "5.14.0-503.14.1.el9_5.x86_64",
        ]
    )
    available: list[str] = field(
        default_factory=lambda: [
            "5.14.0-503.16.1.el9_5.x86_64",
        ]
    )
    default: str | None = None
    arch: str = "x86_64"
    os_id: str = "rhel"
    version_id: str = "9.5"
    behavior: str = "normal"
    reconnect_after_polls: int = 3
    boot_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    _down_polls_remaining: int = field(default=0, init=False)
    _pre_reboot_kernel: str = field(default="", init=False)

    def __post_init__(self) -> None:
        if self.default is None:
            self.default = f"/boot/vmlinuz-{self.running}"

    def handle(self, command: str) -> SSHResult:
        if self.behavior == "unreachable":
            return SSHResult(stdout="", stderr="Connection refused", exit_code=255)

        if self._down_polls_remaining > 0:
            self._down_polls_remaining -= 1
            return SSHResult(stdout="", stderr="Connection refused", exit_code=255)

        if "@@boot_id" in command and "@@uname" in command and "@@default" in command:
            if "@@rpm" in command or "@@arch" in command:
                return self._handle_inventory(command)
            return self._handle_probe()

        if "dnf -q list --available" in command:
            return self._handle_available(command)

        if "dnf install -y" in command:
            return self._handle_install(command)

        if "grubby --set-default=" in command:
            return self._handle_select(command)

        if "grubby --default-kernel" in command and "--set-default" not in command:
            return SSHResult(
                stdout=self.default or "",
                stderr="",
                exit_code=0,
            )

        if "test -f /boot/vmlinuz-" in command:
            for rel in self.installed:
                if f"/boot/vmlinuz-{rel}" in command:
                    return SSHResult(stdout="", stderr="", exit_code=0)
            return SSHResult(stdout="", stderr="file not found", exit_code=1)

        if "systemctl reboot" in command:
            return self._handle_reboot()

        return SSHResult(stdout="ok", stderr="", exit_code=0)

    def _handle_probe(self) -> SSHResult:
        stdout = (
            f"@@boot_id\n{self.boot_id}\n"
            f"@@uname\n{self.running}\n"
            f"@@default\n{self.default}\n"
        )
        return SSHResult(stdout=stdout, stderr="", exit_code=0)

    def _handle_inventory(self, command: str) -> SSHResult:
        pkg = "kernel"
        if "rpm -q " in command:
            rpm_part = command.split("rpm -q ", 1)[1].split(" --qf")[0]
            pkg = rpm_part.split()[0]

        rpm_lines = []
        for rel in self.installed:
            rpm_lines.append(f"{pkg} {rel}")
        if not rpm_lines:
            rpm_lines.append(f"package {pkg} is not installed")

        entries_text = ""
        for i, rel in enumerate(self.installed):
            vmlinuz = f"/boot/vmlinuz-{rel}"
            entries_text += (
                f"index={i}\n"
                f'kernel="{vmlinuz}"\n'
                f'initrd="/boot/initramfs-{rel}.img"\n'
                f'title="Red Hat Enterprise Linux ({rel}) {self.version_id}"\n'
                f'id="{rel}"\n'
            )

        default_index = 0
        for i, rel in enumerate(self.installed):
            if self.default and rel in self.default:
                default_index = i
                break

        stdout = (
            f"@@uname\n{self.running}\n"
            f"@@arch\n{self.arch}\n"
            f"@@cmdline\nBOOT_IMAGE={self.default} root=/dev/sda1\n"
            f"@@boot_id\n{self.boot_id}\n"
            f'@@os\nID="{self.os_id}"\nVERSION_ID="{self.version_id}"\n'
            f"@@rpm\n" + "\n".join(rpm_lines) + "\n"
            f"@@default\n{self.default}\n"
            f"@@default_index\n{default_index}\n"
            f"@@entries\n{entries_text}"
            f"@@tuned\nCurrent active profile: throughput-performance\n"
        )
        return SSHResult(stdout=stdout, stderr="", exit_code=0)

    def _handle_available(self, command: str) -> SSHResult:
        for rel in self.available:
            if rel in command:
                return SSHResult(
                    stdout=f"kernel.{self.arch}  {rel}  baseos",
                    stderr="",
                    exit_code=0,
                )
        return SSHResult(
            stdout="Error: No matching Packages to list",
            stderr="",
            exit_code=1,
        )

    def _handle_install(self, command: str) -> SSHResult:
        installed_any = False
        for rel in list(self.available):
            if rel in command:
                self.available.remove(rel)
                if rel not in self.installed:
                    self.installed.append(rel)
                installed_any = True
        if installed_any:
            return SSHResult(stdout="Complete!", stderr="", exit_code=0)
        return SSHResult(
            stdout="",
            stderr="No package matched",
            exit_code=1,
        )

    def _handle_select(self, command: str) -> SSHResult:
        for rel in self.installed:
            vmlinuz = f"/boot/vmlinuz-{rel}"
            if vmlinuz in command:
                self.default = vmlinuz
                return SSHResult(
                    stdout=f"{vmlinuz}",
                    stderr="",
                    exit_code=0,
                )
        return SSHResult(stdout="", stderr="entry not found", exit_code=1)

    def _handle_reboot(self) -> SSHResult:
        if self.behavior == "ignores_reboot":
            return SSHResult(stdout="@@rebooting", stderr="", exit_code=0)

        self._pre_reboot_kernel = self.running

        if self.behavior == "never_returns":
            self._down_polls_remaining = 999999
            return SSHResult(stdout="@@rebooting", stderr="", exit_code=0)

        self._down_polls_remaining = self.reconnect_after_polls
        self.boot_id = str(uuid.uuid4())

        if self.behavior == "normal":
            for rel in self.installed:
                if self.default and rel in self.default:
                    self.running = rel
                    break
        elif self.behavior == "fallback":
            self.running = self._pre_reboot_kernel
        elif self.behavior.startswith("wrong_kernel:"):
            self.running = self.behavior.split(":", 1)[1]

        return SSHResult(stdout="@@rebooting", stderr="", exit_code=0)

    def save(self, path: Path) -> None:
        state = {
            "name": self.name,
            "running": self.running,
            "installed": self.installed,
            "available": self.available,
            "default": self.default,
            "arch": self.arch,
            "os_id": self.os_id,
            "version_id": self.version_id,
            "behavior": self.behavior,
            "reconnect_after_polls": self.reconnect_after_polls,
            "boot_id": self.boot_id,
            "_down_polls_remaining": self._down_polls_remaining,
        }
        path.write_text(json.dumps(state))

    @classmethod
    def load(cls, path: Path) -> FakeRHELHost:
        state = json.loads(path.read_text())
        host = cls(
            name=state["name"],
            running=state["running"],
            installed=state["installed"],
            available=state["available"],
            default=state["default"],
            arch=state["arch"],
            os_id=state["os_id"],
            version_id=state["version_id"],
            behavior=state["behavior"],
            reconnect_after_polls=state["reconnect_after_polls"],
            boot_id=state["boot_id"],
        )
        host._down_polls_remaining = state.get("_down_polls_remaining", 0)
        return host


class FakeFleetSSH:
    """MockSSHExecutor-compatible executor routing by host."""

    def __init__(self, hosts: dict[str, FakeRHELHost]) -> None:
        self._hosts = hosts
        self.calls: list[dict[str, Any]] = []

    async def run(
        self,
        host: str,
        command: str,
        timeout: int = 300,
        mutating: bool = False,
    ) -> SSHResult:
        self.calls.append(
            {
                "method": "run",
                "host": host,
                "command": command,
                "mutating": mutating,
            }
        )
        fake = self._hosts.get(host)
        if fake is None:
            return SSHResult(stdout="", stderr="No route to host", exit_code=255)
        return fake.handle(command)

    async def copy_from(
        self,
        host: str,
        remote_path: str,
        local_path: str,
        timeout: int = 120,
    ) -> SSHResult:
        self.calls.append(
            {
                "method": "copy_from",
                "host": host,
                "remote_path": remote_path,
                "local_path": local_path,
            }
        )
        return SSHResult(stdout="ok", stderr="", exit_code=0)

    async def copy_to(
        self,
        host: str,
        local_path: str,
        remote_path: str,
        timeout: int = 60,
        mutating: bool = False,
    ) -> SSHResult:
        self.calls.append(
            {
                "method": "copy_to",
                "host": host,
                "local_path": local_path,
                "remote_path": remote_path,
            }
        )
        return SSHResult(stdout="ok", stderr="", exit_code=0)

    async def run_with_progress(
        self,
        host: str,
        command: str,
        progress_callback: Any = None,
    ) -> SSHResult:
        return await self.run(host, command)
