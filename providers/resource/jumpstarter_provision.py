"""Deterministic Jumpstarter provisioning via Python API.

Uses the Jumpstarter client SDK directly for flash, boot,
IP discovery, and SSH key injection. The lease context
(serve_unix_async) keeps the gRPC tunnel alive across
power cycles — unlike jmp shell which dies on flash.

The provisioning sequence is fully deterministic:
flash → power on → wait → tcp address → SSH key inject.
No LLM reasoning. Structured error capture at each step.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from providers.execution import FilesystemAuditError

logger = logging.getLogger(__name__)

# Timeout defaults (seconds)
_FLASH_TIMEOUT = 600
_BOOT_WAIT = 60
_TCP_TIMEOUT = 30
_SSH_TIMEOUT = 30


@dataclass
class ProvisionResult:
    """Structured result from deterministic provisioning."""

    success: bool = False
    ip: str = ""
    board_name: str = ""
    ssh_user: str = "root"
    ssh_key_path: str = ""
    diagnostics: list[str] = field(default_factory=list)
    flash_duration_s: float = 0.0
    boot_duration_s: float = 0.0
    serial_log_path: str = ""


_DEFAULT_PROVISION_LEASE_DURATION_SECONDS = 14_400


def _write_flash_diagnostics(
    artifact_dir: str,
    ticket_id: str,
    diag: list[str],
) -> None:
    """Write flash diagnostics to artifact file and log.

    Called on flash failure so diagnostics are durable even
    if the LLM is unavailable to process the tool result.
    """
    from providers.execution import (
        AuditedFilesystem,
        RootedPath,
        durable_filesystem_emitter,
    )
    from providers.redaction import get_shared_redactor

    redactor = get_shared_redactor()
    safe_diagnostics = [
        _redact_flash_detail(ticket_id, item, redactor=redactor) for item in diag
    ]
    summary = "\n".join(safe_diagnostics)
    logger.warning(
        "[platform] Flash diagnostics for %s:\n%s",
        ticket_id,
        summary,
    )
    if artifact_dir:
        payload = json.dumps(
            {
                "ticket_id": ticket_id,
                "diagnostics": safe_diagnostics,
            },
            indent=2,
        )
        if ticket_id:
            filesystem = AuditedFilesystem(
                RootedPath(
                    artifact_dir,
                    "artifact",
                    logical_prefix="platform-provision",
                ),
                ticket_id=ticket_id,
                emit=durable_filesystem_emitter(),
                critical=True,
            )
        else:
            filesystem = AuditedFilesystem.system(artifact_dir)
        filesystem.write("flash-diagnostics.json", payload, mode=0o600)
        logger.info(
            "[platform] Flash diagnostics written to %s",
            f"{artifact_dir}/flash-diagnostics.json",
        )


def _format_exception_chain(exc: BaseException) -> str:
    """Recursively unwrap exception chains and ExceptionGroups.

    FlashError wraps ExceptionGroup whose str() hides
    sub-exceptions.  This produces a flat, readable summary
    of the full chain.
    """
    parts: list[str] = []
    seen: set[int] = set()
    queue: list[BaseException] = [exc]
    while queue:
        e = queue.pop(0)
        if id(e) in seen:
            continue
        seen.add(id(e))
        parts.append(f"{type(e).__name__}: {e}")
        # Unwrap ExceptionGroup sub-exceptions
        if isinstance(e, BaseExceptionGroup):
            for sub in e.exceptions:
                queue.append(sub)
        # Follow the cause/context chain
        if e.__cause__ is not None:
            queue.append(e.__cause__)
        elif e.__context__ is not None:
            queue.append(e.__context__)
    return "; ".join(parts)


def _redact_flash_detail(
    ticket_id: str,
    detail: str,
    *,
    redactor: Any | None = None,
) -> str:
    """Redact exception text and remove potentially signed URL parameters."""
    from urllib.parse import urlsplit, urlunsplit

    from providers.redaction import get_shared_redactor

    safe_detail = (redactor or get_shared_redactor()).redact_string(ticket_id, detail)
    url_pattern = re.compile(r"\b(?:https?|s3|gs)://[^\s\"'<>]+", re.IGNORECASE)
    trailing_punctuation = ".,;:!?)]}"

    def strip_query(match: re.Match[str]) -> str:
        candidate = match.group(0)
        suffix = ""
        while candidate and candidate[-1] in trailing_punctuation:
            suffix = candidate[-1] + suffix
            candidate = candidate[:-1]
        try:
            parts = urlsplit(candidate)
        except ValueError:
            return "[REDACTED:url]" + suffix
        if parts.query or parts.fragment:
            candidate = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        return candidate + suffix

    return url_pattern.sub(strip_query, safe_detail)


async def provision_jumpstarter(
    lease_name: str,
    flash_url: str | dict[str, str],
    ssh_public_key: str,
    ssh_key_path: str = "",
    board_name: str = "",
    client_config_path: str = "",
    selector: str = "",
    serial_capture: bool = False,
    artifact_dir: str = "",
    ticket_id: str = "",
    lease_duration_seconds: int = _DEFAULT_PROVISION_LEASE_DURATION_SECONDS,
) -> ProvisionResult:
    """Run the deterministic flash + boot + verify sequence.

    Uses the Jumpstarter Python SDK directly. The lease
    context (serve_unix_async) keeps the gRPC tunnel alive
    across flash and power cycles.

    Args:
        lease_name: Jumpstarter lease ID.
        flash_url: URL of the OS image to flash (single
            string for EBBR), or dict of {partition: url}
            for multi-partition boards (SA8775P).
        ssh_public_key: Public key to inject.
        ssh_key_path: Path to the private key.
        board_name: Exporter name for diagnostics.
        client_config_path: Path to Jumpstarter client
            config. Auto-detected if empty.
        serial_capture: If True, capture serial output
            during provisioning via jmp serial pipe.
        artifact_dir: Directory for serial log. Falls
            back to a temp file if empty.
        lease_duration_seconds: Lease duration to retain while provisioning.
            Defaults to the resource-agent allocation default of four hours.

    Returns:
        ProvisionResult with success/failure and diagnostics.
    """
    diag: list[str] = []
    result = ProvisionResult(
        board_name=board_name,
        ssh_key_path=ssh_key_path,
    )

    # NOTE: serial capture (j serial pipe) is intentionally NOT
    # started during provisioning.  The serial pipe subprocess
    # opens a gRPC tunnel to the board's serial port, which
    # conflicts with the flash tool's own serial/pexpect
    # connection inside reboot_to_console().  Running both
    # concurrently on the same lease causes pexpect EOF — the
    # flash tool's TCP port-forward gets closed because the
    # serial port is already held by the pipe subprocess.
    #
    # Diagnosed by reproducing: "jmp shell -- j storage flash"
    # succeeds from the pod when no serial pipe is running, but
    # provision code that started serial pipe first failed
    # consistently with pexpect EOF on the U-Boot '=>' prompt.
    #
    # Flash diagnostics come from _format_exception_chain() and
    # flash-diagnostics.json.  Boot-phase serial capture is the
    # benchmark agent's responsibility.
    if serial_capture and lease_name:
        logger.info(
            "[platform] Skipping pre-flash serial capture (conflicts "
            "with flash tool serial access); boot-phase capture is "
            "handled by the benchmark agent"
        )

    try:
        # Run the blocking Jumpstarter SDK calls in a
        # thread to avoid blocking the asyncio loop.
        loop = asyncio.get_event_loop()
        prov_result = await loop.run_in_executor(
            None,
            _provision_sync,
            lease_name,
            flash_url,
            ssh_public_key,
            board_name,
            client_config_path,
            selector,
            lease_duration_seconds,
            ticket_id,
            artifact_dir,
        )
        result = prov_result
    except asyncio.CancelledError:
        # Cancellation is a control-flow signal.  The finally block still
        # stops serial capture, but the caller must observe cancellation.
        raise
    except FilesystemAuditError:
        # A critical ticket artifact must not be reported as safely handled
        # when its audit record could not be durably delivered.
        raise
    except Exception as exc:
        # Unwrap ExceptionGroup/TaskGroup to expose the
        # real error (e.g., 'Failed to get U-Boot prompt')
        # instead of the generic wrapper message.
        real_errors = []
        if hasattr(exc, "exceptions"):
            for sub in exc.exceptions:
                real_errors.append(str(sub))
                if hasattr(sub, "exceptions"):
                    for nested in sub.exceptions:
                        real_errors.append(str(nested))
        if real_errors:
            diag.append(f"Provisioning failed: {'; '.join(real_errors)}")
        else:
            diag.append(f"Provisioning exception: {exc}")
        logger.error(
            "[platform] Provisioning failed: %s",
            exc,
            exc_info=True,
        )
    finally:
        pass

    if diag:
        result.diagnostics.extend(diag)
    return result


def _provision_sync(
    lease_name: str,
    flash_url: str | dict[str, str],
    ssh_public_key: str,
    board_name: str,
    client_config_path: str,
    selector: str = "",
    lease_duration_seconds: int = _DEFAULT_PROVISION_LEASE_DURATION_SECONDS,
    ticket_id: str = "",
    artifact_dir: str = "",
) -> ProvisionResult:
    """Synchronous provisioning — runs in executor thread.

    Uses anyio.from_thread.BlockingPortal internally
    (required by the Jumpstarter SDK).
    """
    from anyio import run as anyio_run

    return anyio_run(
        _provision_async,
        lease_name,
        flash_url,
        ssh_public_key,
        board_name,
        client_config_path,
        selector,
        lease_duration_seconds,
        ticket_id,
        artifact_dir,
    )


def _parse_exporter_address(addr: Any) -> str:
    """Extract IP from an exporter address string.

    Handles formats: 'host:port', 'tcp://host:port', bare IP.
    """
    from urllib.parse import urlparse

    addr_str = str(addr)
    if "://" in addr_str:
        parsed = urlparse(addr_str)
        return parsed.hostname or ""
    if ":" in addr_str:
        return addr_str.split(":")[0]
    return addr_str


async def _provision_async(
    lease_name: str,
    flash_url: str | dict[str, str],
    ssh_public_key: str,
    board_name: str,
    client_config_path: str,
    selector: str = "",
    lease_duration_seconds: int = _DEFAULT_PROVISION_LEASE_DURATION_SECONDS,
    ticket_id: str = "",
    artifact_dir: str = "",
) -> ProvisionResult:
    """Async provisioning using the Jumpstarter SDK."""
    from anyio.from_thread import BlockingPortal
    from jumpstarter.client.client import client_from_path
    from jumpstarter.config.client import ClientConfigV1Alpha1

    diag: list[str] = []
    result = ProvisionResult(board_name=board_name)

    # Load client config
    if not client_config_path:
        config_dir = Path.home() / ".config" / "jumpstarter" / "clients"
        configs = list(config_dir.glob("*.yaml"))
        if not configs:
            result.diagnostics = ["No Jumpstarter client config found"]
            return result
        client_config_path = str(configs[0])

    config = ClientConfigV1Alpha1.from_file(client_config_path)

    async with BlockingPortal() as portal:
        # When lease_name is set, the resource agent already
        # created the lease. Don't pass selector — it causes
        # a mismatch if the key order differs and the SDK
        # creates a new lease instead of reusing the existing.
        async with config.lease_async(
            selector=None if lease_name else (selector or None),
            exporter_name=None,
            lease_name=lease_name,
            duration=timedelta(seconds=lease_duration_seconds),
            portal=portal,
        ) as lease:
            result.board_name = getattr(lease, "exporter_name", "") or board_name
            diag.append(f"Lease acquired: {lease.name} (exporter={result.board_name})")

            async with lease.serve_unix_async() as path:
                with ExitStack() as stack:
                    async with client_from_path(
                        path,
                        portal,
                        stack,
                        allow=config.drivers.allow,
                        unsafe=config.drivers.unsafe,
                    ) as client:
                        return await _run_provision_steps(
                            client,
                            flash_url,
                            ssh_public_key,
                            result,
                            diag,
                            board_name=board_name,
                            ticket_id=ticket_id,
                            artifact_dir=artifact_dir,
                        )


async def _run_provision_steps(
    client: Any,
    flash_url: str | dict[str, str],
    ssh_public_key: str,
    result: ProvisionResult,
    diag: list[str],
    *,
    board_name: str = "",
    ticket_id: str = "",
    artifact_dir: str = "",
) -> ProvisionResult:
    """Execute the deterministic provision steps."""
    # ── Step 1: Flash ────────────────────────────────
    # Ensure the board is in a known power state before
    # The flash tool (client.storage.flash) does its own
    # internal power cycle via reboot_to_console().  A
    # pre-flash power cycle through the same client session
    # leaves stale serial/port-forward state that causes
    # the flash tool's pexpect to get EOF on the U-Boot
    # prompt.  Diagnosed by comparing pod SDK calls (fail)
    # vs separate jmp shell sessions (succeed) — the latter
    # get fresh gRPC connections with no carried-over state.
    from anyio import to_thread

    if isinstance(flash_url, dict):
        logger.info(
            "[platform] Flashing %s (%d partitions: %s)",
            result.board_name,
            len(flash_url),
            ", ".join(flash_url.keys()),
        )
    else:
        logger.info("[platform] Flashing %s", result.board_name)
    t0 = time.monotonic()
    try:
        await to_thread.run_sync(lambda: client.storage.flash(flash_url))
        result.flash_duration_s = time.monotonic() - t0
        diag.append(f"Flash succeeded in {result.flash_duration_s:.0f}s")
    except Exception as exc:
        result.flash_duration_s = time.monotonic() - t0
        # Use repr() for ExceptionGroup/TaskGroup so sub-exception
        # messages are visible in diagnostics, not just the group label.
        safe_exc = _redact_flash_detail(ticket_id, _format_exception_chain(exc))
        diag.append(f"Flash failed ({result.flash_duration_s:.0f}s): {safe_exc}")
        logger.error(
            "[platform] Flash failed for %s (%s) after %.0fs: %s",
            board_name,
            ticket_id,
            result.flash_duration_s,
            safe_exc,
        )
        # Retry once
        logger.warning("[platform] Flash failed, retrying")
        diag.append("Retrying flash...")
        t0 = time.monotonic()
        try:
            await to_thread.run_sync(lambda: client.storage.flash(flash_url))
            result.flash_duration_s = time.monotonic() - t0
            diag.append(f"Flash retry succeeded in {result.flash_duration_s:.0f}s")
        except Exception as exc2:
            retry_duration = time.monotonic() - t0
            safe_exc2 = _redact_flash_detail(ticket_id, _format_exception_chain(exc2))
            diag.append(f"Flash retry failed ({retry_duration:.0f}s): {safe_exc2}")
            logger.error(
                "[platform] Flash retry failed for %s (%s) after %.0fs: %s",
                board_name,
                ticket_id,
                retry_duration,
                safe_exc2,
            )
            # Write diagnostics directly to artifact so they
            # survive even if the LLM is unavailable to process
            # the tool result.
            _write_flash_diagnostics(artifact_dir, ticket_id, diag)
            result.diagnostics = diag
            return result

    # ── Step 2: Power on ─────────────────────────────
    try:
        await to_thread.run_sync(client.power.on)
        diag.append("Power on OK")
    except Exception as exc:
        diag.append(f"Power on failed: {exc}")
        # Not fatal — flash may include power cycle

    # ── Step 3: Wait for boot ────────────────────────
    logger.info("[platform] Waiting %ds for boot", _BOOT_WAIT)
    diag.append(f"Waiting {_BOOT_WAIT}s for boot...")
    t0 = time.monotonic()
    await asyncio.sleep(_BOOT_WAIT)
    result.boot_duration_s = time.monotonic() - t0

    # ── Step 4: Discover IP ──────────────────────────
    ip = ""
    try:
        addr = await to_thread.run_sync(client.tcp.address)
        ip = _parse_exporter_address(addr)
        diag.append(f"IP discovered: {ip}")
    except Exception as exc:
        diag.append(f"TCP address failed: {exc}")
        # The exporter may have temporarily disconnected
        # during reboot. Retry with backoff — the gRPC
        # session often recovers after 30-60s.
        _ADDR_RETRIES = 3
        _ADDR_BACKOFF = [30, 45, 60]  # seconds between retries
        for attempt in range(_ADDR_RETRIES):
            wait = _ADDR_BACKOFF[attempt]
            diag.append(
                f"Retry {attempt + 1}/{_ADDR_RETRIES}: "
                f"waiting {wait}s for exporter recovery..."
            )
            try:
                await to_thread.run_sync(lambda: client.power.cycle())
            except Exception:
                pass
            await asyncio.sleep(wait)
            try:
                addr = await to_thread.run_sync(client.tcp.address)
                ip = _parse_exporter_address(addr)
                diag.append(f"IP discovered on retry {attempt + 1}: {ip}")
                break
            except Exception as exc2:
                diag.append(f"Retry {attempt + 1} failed: {exc2}")

    if not ip:
        diag.append("IP discovery failed")
        result.diagnostics = diag
        return result

    # Validate IP format
    if not re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", ip):
        diag.append(f"Invalid IP format: {ip!r}")
        result.diagnostics = diag
        return result

    result.ip = ip

    # Validate SSH connectivity before declaring the
    # platform ready. Catches: wrong/stale IP, network
    # not up, SSH not running, initramfs failures that
    # leave a login prompt but no network stack.
    import socket as _socket

    ssh_reachable = False
    for attempt in range(6):
        try:
            s = _socket.create_connection((ip, 22), timeout=10)
            s.close()
            ssh_reachable = True
            diag.append(f"SSH port 22 reachable on {ip} (attempt {attempt + 1})")
            break
        except (OSError, ConnectionRefusedError):
            if attempt < 5:
                await asyncio.sleep(10)

    if not ssh_reachable:
        diag.append(
            f"SSH port 22 unreachable on {ip} after 6"
            f" attempts (60s). The board may have booted"
            f" with no network, wrong IP, or a corrupt"
            f" image. Check serial output for boot errors."
        )
        result.diagnostics = diag
        return result

    # ── Step 5: Inject SSH key ───────────────────────
    if ssh_public_key:
        try:
            from jumpstarter_driver_ssh.client import (
                SSHCommandRunOptions,
            )

            # Pass the full command as a single string.
            # SSH concatenates argv with spaces for the
            # remote command. With ["bash", "-c", cmd],
            # SSH sends "bash -c mkdir ..." where bash
            # only sees "mkdir" as the -c argument.
            # A single string avoids the split.
            inject_cmd = (
                "mkdir -p /root/.ssh"
                " && chmod 700 /root/.ssh"
                " && echo '" + ssh_public_key + "'"
                " >> /root/.ssh/authorized_keys"
                " && chmod 600 /root/.ssh/authorized_keys"
            )
            ssh_result = await to_thread.run_sync(
                lambda: client.ssh.run(
                    SSHCommandRunOptions(capture_output=True),
                    [inject_cmd],
                )
            )
            if ssh_result.return_code != 0:
                stderr = getattr(ssh_result, "stderr", "")
                diag.append(
                    f"SSH key injection failed "
                    f"(exit={ssh_result.return_code}): "
                    f"{stderr[:200]}"
                )
                result.diagnostics = diag
                return result
            diag.append("SSH key injected")

            # Verify SSH works
            verify = await to_thread.run_sync(
                lambda: client.ssh.run(
                    SSHCommandRunOptions(capture_output=True),
                    ["echo", "SSH_OK"],
                )
            )
            stdout = getattr(verify, "stdout", "")
            if "SSH_OK" not in str(stdout):
                diag.append(
                    f"SSH verification failed: {getattr(verify, 'stderr', '')[:200]}"
                )
                result.diagnostics = diag
                return result
            diag.append("SSH verified")
        except Exception as exc:
            diag.append(f"SSH key injection error: {exc}")
            result.diagnostics = diag
            return result

    # ── Success ──────────────────────────────────────
    result.success = True
    result.ssh_user = "root"
    result.diagnostics = diag
    logger.info(
        "[platform] Provisioning complete: %s → %s",
        result.board_name,
        ip,
    )
    return result
