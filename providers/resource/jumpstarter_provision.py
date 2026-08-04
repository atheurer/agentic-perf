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
import logging
import re
import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

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


async def provision_jumpstarter(
    lease_name: str,
    flash_url: str | dict[str, str],
    ssh_public_key: str,
    ssh_key_path: str = "",
    board_name: str = "",
    client_config_path: str = "",
    selector: str = "",
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

    Returns:
        ProvisionResult with success/failure and diagnostics.
    """
    diag: list[str] = []
    result = ProvisionResult(
        board_name=board_name,
        ssh_key_path=ssh_key_path,
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
        )
        return prov_result
    except Exception as exc:
        diag.append(f"Provisioning exception: {exc}")
        result.diagnostics = diag
        logger.exception(
            "[platform] Provisioning failed for %s",
            board_name,
        )
        return result


def _provision_sync(
    lease_name: str,
    flash_url: str | dict[str, str],
    ssh_public_key: str,
    board_name: str,
    client_config_path: str,
    selector: str = "",
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
    )


async def _provision_async(
    lease_name: str,
    flash_url: str | dict[str, str],
    ssh_public_key: str,
    board_name: str,
    client_config_path: str,
    selector: str = "",
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
            duration=timedelta(hours=2),
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
                        )


async def _run_provision_steps(
    client: Any,
    flash_url: str | dict[str, str],
    ssh_public_key: str,
    result: ProvisionResult,
    diag: list[str],
) -> ProvisionResult:
    """Execute the deterministic provision steps."""
    # ── Step 1: Flash ────────────────────────────────
    # Ensure the board is in a known power state before
    # flashing.  After a lease expiry mid-benchmark the
    # board may be mid-boot or hung — flashing without a
    # clean power cycle fails with "Failed to get U-Boot
    # prompt."
    import asyncio as _asyncio

    from anyio import to_thread

    logger.info("[platform] Power cycling %s before flash", result.board_name)
    try:
        await to_thread.run_sync(lambda: client.power.off())
        await _asyncio.sleep(5)
        await to_thread.run_sync(lambda: client.power.on())
        await _asyncio.sleep(10)
        diag.append("Pre-flash power cycle OK")
    except Exception as exc:
        diag.append(f"Pre-flash power cycle warning: {exc}")
        logger.warning("[platform] Pre-flash power cycle failed: %s", exc)

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
        diag.append(f"Flash failed: {exc}")
        # Retry once
        logger.warning("[platform] Flash failed, retrying")
        diag.append("Retrying flash...")
        t0 = time.monotonic()
        try:
            await to_thread.run_sync(lambda: client.storage.flash(flash_url))
            result.flash_duration_s = time.monotonic() - t0
            diag.append(f"Flash retry succeeded in {result.flash_duration_s:.0f}s")
        except Exception as exc2:
            diag.append(f"Flash retry failed: {exc2}")
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
        # Format: "host:port" or "tcp://host:port"
        addr_str = str(addr)
        if "://" in addr_str:
            from urllib.parse import urlparse

            parsed = urlparse(addr_str)
            ip = parsed.hostname or ""
        elif ":" in addr_str:
            ip = addr_str.split(":")[0]
        else:
            ip = addr_str
        diag.append(f"IP discovered: {ip}")
    except Exception as exc:
        diag.append(f"TCP address failed: {exc}")
        # Power cycle and retry
        diag.append("Power cycling and retrying...")
        try:
            await to_thread.run_sync(lambda: client.power.cycle())
        except Exception:
            pass
        await asyncio.sleep(_BOOT_WAIT)
        try:
            addr = await to_thread.run_sync(client.tcp.address)
            addr_str = str(addr)
            if ":" in addr_str:
                ip = addr_str.split(":")[0]
            else:
                ip = addr_str
            diag.append(f"IP discovered on retry: {ip}")
        except Exception as exc2:
            diag.append(f"TCP address retry failed: {exc2}")

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
