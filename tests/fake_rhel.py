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
    # "normal" | "timeout_installs" (dnf times out but installs) |
    # "timeout_fails" (dnf times out and nothing is installed)
    dnf_behavior: str = "normal"
    # "normal" | "timeout_sets" | "timeout_fails"
    grubby_behavior: str = "normal"
    # "normal" | "timeout" (the reboot is queued but the SSH session times out)
    reboot_behavior: str = "normal"
    # The RHEL 9.4 lab hosts have no tuned-adm: the inventory's last command
    # fails with "command not found" and exit 127 unless the command tolerates it.
    has_tuned: bool = True
    _cancel_next_probe: bool = field(default=False, init=False)
    boot_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    _down_polls_remaining: int = field(default=0, init=False)
    _pre_reboot_kernel: str = field(default="", init=False)

    def __post_init__(self) -> None:
        if self.default is None:
            self.default = f"/boot/vmlinuz-{self.running}"

    def handle(self, command: str) -> SSHResult:
        if self.behavior == "unreachable":
            return SSHResult(stdout="", stderr="Connection refused", exit_code=255)

        if self._cancel_next_probe and "@@boot_id" in command:
            import asyncio

            self._cancel_next_probe = False
            raise asyncio.CancelledError()

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
        )
        if self.has_tuned:
            stdout += "@@tuned\nCurrent active profile: throughput-performance\n"
            return SSHResult(stdout=stdout, stderr="", exit_code=0)
        stdout += "@@tuned\nbash: line 1: tuned-adm: command not found\n"
        exit_code = 0 if "|| true" in command else 127
        return SSHResult(stdout=stdout, stderr="", exit_code=exit_code)

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
        if self.dnf_behavior == "timeout_fails":
            return SSHResult(stdout="", stderr="Command timed out", exit_code=-1)
        installed_any = False
        for rel in list(self.available):
            if rel in command:
                self.available.remove(rel)
                if rel not in self.installed:
                    self.installed.append(rel)
                installed_any = True
        if installed_any:
            if self.dnf_behavior == "timeout_installs":
                return SSHResult(stdout="", stderr="Command timed out", exit_code=-1)
            return SSHResult(stdout="Complete!", stderr="", exit_code=0)
        return SSHResult(
            stdout="",
            stderr="No package matched",
            exit_code=1,
        )

    def _handle_select(self, command: str) -> SSHResult:
        if self.grubby_behavior == "timeout_fails":
            return SSHResult(stdout="", stderr="Command timed out", exit_code=-1)
        for rel in self.installed:
            vmlinuz = f"/boot/vmlinuz-{rel}"
            if vmlinuz in command:
                self.default = vmlinuz
                if self.grubby_behavior == "timeout_sets":
                    return SSHResult(
                        stdout="", stderr="Command timed out", exit_code=-1
                    )
                return SSHResult(
                    stdout=f"{vmlinuz}",
                    stderr="",
                    exit_code=0,
                )
        return SSHResult(stdout="", stderr="entry not found", exit_code=1)

    def _handle_reboot(self) -> SSHResult:
        if self.behavior == "ignores_reboot":
            if self.reboot_behavior == "timeout":
                return SSHResult(stdout="", stderr="Command timed out", exit_code=-1)
            return SSHResult(stdout="@@rebooting", stderr="", exit_code=0)

        self._pre_reboot_kernel = self.running

        if self.behavior == "never_returns":
            self._down_polls_remaining = 999999
            return SSHResult(stdout="@@rebooting", stderr="", exit_code=0)

        if self.behavior == "cancel_after_reboot":
            self._cancel_next_probe = True

        self._down_polls_remaining = self.reconnect_after_polls
        self.boot_id = str(uuid.uuid4())

        if self.behavior in ("normal", "cancel_after_reboot"):
            for rel in self.installed:
                if self.default and rel in self.default:
                    self.running = rel
                    break
        elif self.behavior == "fallback":
            self.running = self._pre_reboot_kernel
        elif self.behavior.startswith("wrong_kernel:"):
            self.running = self.behavior.split(":", 1)[1]

        if self.reboot_behavior == "timeout":
            return SSHResult(stdout="", stderr="Command timed out", exit_code=-1)
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


# ---------------------------------------------------------------------------
# Fake state-store client
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, payload: Any = None) -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self) -> Any:
        return json.loads(json.dumps(self._payload))

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeKernelStore:
    """Stand-in for ``AuditedAsyncHTTPClient`` with the state-store routes the
    kernel tools use: ticket read, field PATCH, approvals list / create /
    resolve / consume, and ticket transition.  Class-level state so the
    class itself can be patched in as the client.  ``tamper_versions``
    makes every re-read report a foreign ``record_version`` to exercise
    the checkpoint writer's overwrite check."""

    ticket: dict[str, Any] = {}
    approvals: dict[str, dict[str, Any]] = {}
    patches: list[dict[str, Any]] = []
    posts: list[tuple[str, dict[str, Any]]] = []
    consumes: list[dict[str, Any]] = []
    transitions: list[dict[str, Any]] = []
    tamper_versions: bool = False
    _counter: int = 0

    @classmethod
    def reset(cls, ticket: dict[str, Any]) -> None:
        cls.ticket = ticket
        cls.approvals = {}
        cls.patches = []
        cls.posts = []
        cls.consumes = []
        cls.transitions = []
        cls.tamper_versions = False
        cls._counter = 0

    @classmethod
    def custom_fields(cls) -> dict[str, Any]:
        return cls.ticket.setdefault("custom_fields", {})

    @classmethod
    def seed_intent(cls, intent: dict[str, Any]) -> None:
        records = cls.custom_fields().setdefault(
            "kernel_change_intents", {"records": {}}
        )["records"]
        records[intent["intent_id"]] = intent

    @classmethod
    def intent(cls, intent_id: str) -> dict[str, Any]:
        return cls.custom_fields()["kernel_change_intents"]["records"][intent_id]

    @classmethod
    def seed_approval(
        cls,
        intent: dict[str, Any],
        *,
        status: str = "approved",
        consumed: bool = False,
        kind: str = "kernel_change",
        expires_at: str | None = None,
        digest: str | None = None,
    ) -> str:
        cls._counter += 1
        approval_id = f"apr-{cls._counter:032x}"
        cls.approvals[approval_id] = {
            "approval_request_id": approval_id,
            "ticket_id": cls.ticket.get("id", ""),
            "kind": kind,
            "validation_id": intent["intent_id"],
            "presented_run_file_digest": digest or intent["intent_digest"],
            "execution_intent_digest": digest or intent["intent_digest"],
            "status": status,
            "consumed_at": "2026-01-01T00:00:00Z" if consumed else None,
            "expires_at": expires_at,
        }
        return approval_id

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> FakeKernelStore:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def get(self, url: str, *args: Any, **kwargs: Any) -> _FakeResponse:
        url = str(url)
        if url.endswith("/approvals"):
            return _FakeResponse(200, {"approvals": list(self.approvals.values())})
        ticket = json.loads(json.dumps(self.ticket))
        if self.tamper_versions:
            for record in (
                ticket.get("custom_fields", {}).get("kernel_transitions", {}).values()
            ):
                record["record_version"] = 999
        return _FakeResponse(200, ticket)

    async def patch(self, url: str, *args: Any, **kwargs: Any) -> _FakeResponse:
        body = kwargs.get("json", {})
        type(self).patches.append(json.loads(json.dumps(body)))
        self.custom_fields().update(body.get("fields", {}))
        return _FakeResponse(200, {})

    async def post(self, url: str, *args: Any, **kwargs: Any) -> _FakeResponse:
        url = str(url)
        body = kwargs.get("json", {})
        type(self).posts.append((url, json.loads(json.dumps(body))))
        if url.endswith("/kernel-control"):
            kind, record_id = body["kind"], body["record_id"]
            field = (
                "kernel_change_intents" if kind == "intent" else "kernel_transitions"
            )
            container = self.custom_fields().setdefault(
                field, {"records": {}} if kind == "intent" else {}
            )
            records = container["records"] if kind == "intent" else container
            if (
                records.get(record_id, {}).get("record_version", 0)
                != body["expected_version"]
            ):
                return _FakeResponse(409, {"detail": "kernel record version conflict"})
            record = json.loads(json.dumps(body["record"]))
            record["record_version"] = body["expected_version"] + 1
            records[record_id] = record
            # Retain snapshots for existing persistence-order assertions.
            type(self).patches.append(
                json.loads(json.dumps({"fields": {field: container}}))
            )
            return _FakeResponse(200, record)
        if url.endswith("/transition"):
            type(self).transitions.append(body)
            self.ticket["status"] = body.get("status", self.ticket.get("status"))
            return _FakeResponse(200, {})
        if url.endswith("/consume"):
            approval_id = url.rsplit("/", 2)[-2]
            approval = self.approvals.get(approval_id)
            type(self).consumes.append({"approval_id": approval_id, **body})
            if approval is None:
                return _FakeResponse(404, {"detail": "not found"})
            if approval["status"] != "approved":
                return _FakeResponse(409, {"detail": "not approved"})
            reserving_reboot = body.get("kernel_action") == "reboot"
            if approval.get("kernel_reboot_reserved"):
                return _FakeResponse(409, {"detail": "reboot already reserved"})
            if approval["consumed_at"] is not None and body.get(
                "kernel_action"
            ) not in ("install", "select_default", "reboot"):
                return _FakeResponse(409, {"detail": "already consumed"})
            if approval["kind"] != body.get("kind", "benchmark_run_file"):
                return _FakeResponse(409, {"detail": "kind mismatch"})
            if approval["validation_id"] != body.get("validation_id"):
                return _FakeResponse(409, {"detail": "validation mismatch"})
            if reserving_reboot:
                approval["kernel_reboot_reserved"] = True
            approval["consumed_at"] = "2026-01-01T00:00:01Z"
            return _FakeResponse(200, approval)
        if url.endswith("/resolve"):
            approval_id = url.rsplit("/", 2)[-2]
            approval = self.approvals.get(approval_id)
            if approval is None:
                return _FakeResponse(404, {})
            approval["status"] = body.get("decision", approval["status"])
            return _FakeResponse(200, approval)
        if url.endswith("/approvals"):
            type(self)._counter += 1
            approval_id = f"apr-{type(self)._counter:032x}"
            approval = {
                "approval_request_id": approval_id,
                "ticket_id": self.ticket.get("id", ""),
                "kind": body.get("kind", "benchmark_run_file"),
                "validation_id": body.get("validation_id"),
                "presented_run_file_digest": body.get("presented_run_file_digest"),
                "execution_intent_digest": body.get("execution_intent_digest"),
                "status": "pending",
                "consumed_at": None,
                "expires_at": None,
                "summary": body.get("summary", ""),
            }
            self.approvals[approval_id] = approval
            return _FakeResponse(201, approval)
        if url.endswith("/comments"):
            return _FakeResponse(200, {"id": "c-1"})
        return _FakeResponse(200, {})
