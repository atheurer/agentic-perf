"""Boot-time harness implementation.

Extracted from ``agents/benchmark/server.py`` so that file stays a generic
MCP tool layer.  ``server.execute_boot_time_test`` registers the tool and
dispatches here with a :class:`BootTimeContext`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from agents.server_utils import is_self_host
from providers.benchmark_identity import comparison_step_requested
from providers.execution import (
    AuditedFilesystem,
    AuditedSubprocessRunner,
    RootedPath,
    durable_filesystem_emitter,
)

logger = logging.getLogger(__name__)

_BOOT_TIME_DRAIN_TIMEOUT = 10.0
_boot_time_executed = False


@dataclass
class BootTimeContext:
    """Server-owned runtime state the boot-time harness needs.

    ``require_tool_owner`` is a zero-argument awaitable bound to the
    server's ownership check, so the harness cannot bypass the ticket's
    harness selection.
    """

    ssh: Any
    repo_cache: Any
    ticket: dict[str, Any] | None
    require_tool_owner: Callable[[], Awaitable[None]]


_MAX_BOOT_SERIAL_READ = 1024 * 1024


def _boot_serial_diagnostics(serial_log_path: Path) -> dict[str, Any]:
    """Read a bounded serial snapshot and report boot/failure indicators."""
    try:
        with serial_log_path.open("rb") as stream:
            file_size = stream.seek(0, os.SEEK_END)
            if not file_size:
                return {}
            stream.seek(max(0, file_size - _MAX_BOOT_SERIAL_READ))
            # A serial writer may append after the seek. Bound the read itself.
            serial_bytes = stream.read(_MAX_BOOT_SERIAL_READ)
    except OSError:
        return {}

    serial_text = serial_bytes.decode("utf-8", errors="replace")
    lower_text = serial_text.lower()
    indicators = [
        label
        for pattern, label in (
            ("kernel panic", "kernel_panic"),
            ("unable to mount root", "root_mount_failure"),
            ("not syncing", "kernel_not_syncing"),
            ("reboot: system halted", "system_halted"),
            ("out of memory", "oom"),
            ("oom-killer", "oom_killer"),
            ("call trace", "call_trace"),
            ("initramfs unpacking failed", "initramfs_failed"),
            ("reboot: power down", "power_down"),
            ("reboot: restarting system", "reboot_restart"),
            ("invalid gpt", "invalid_gpt"),
            ("autoboot", "uboot_autoboot"),
            ("login:", "reached_login"),
        )
        if pattern in lower_text
    ]
    if re.search(r"(?m)^u-boot(?:[ \t]+\d|[ \t]*$)", lower_text):
        indicators.append("uboot_banner")

    diagnostics: dict[str, Any] = {
        "serial_log_bytes": file_size,
        "serial_tail": serial_text[-2000:],
    }
    if indicators:
        diagnostics["serial_indicators"] = indicators
    return diagnostics


async def run_boot_time_test(
    ctx: BootTimeContext,
    sut_host: str,
    samples: int = 50,
    kpi_pattern: str = "",
    clean_journal: bool = False,
    description: str = "",
) -> str:
    """Run the boot-time harness against *sut_host*.

    See ``execute_boot_time_test`` in ``agents/benchmark/server.py`` for the
    user-facing tool contract.  ``ctx`` carries the server's SSH provider and
    ticket state so the harness does not depend on module globals.
    """
    await ctx.require_tool_owner()
    if comparison_step_requested(ctx.ticket or {}):
        return json.dumps(
            {
                "status": "rejected",
                "reason_code": "workload_group_harness_unsupported",
                "message": (
                    "Boot-time analysis cannot provide the durable execution "
                    "receipt required for a kernel comparison."
                ),
            }
        )

    # ── Guardrail: one execution per agent session ───────
    global _boot_time_executed
    if _boot_time_executed:
        return json.dumps(
            {
                "status": "rejected",
                "error": (
                    "Boot-time test already executed in "
                    "this session. Submit your result "
                    "and exit. Do not call this tool "
                    "again."
                ),
            }
        )
    _boot_time_executed = True

    # ── Guardrail: never reboot the orchestrator ──────────────
    if is_self_host(sut_host):
        return json.dumps(
            {
                "status": "rejected",
                "error": (
                    f"SAFETY: refusing to run boot-time test "
                    f"against '{sut_host}' — this would reboot "
                    f"the orchestrator host."
                ),
            }
        )

    # ── Locate boot-time-analysis-scripts repo ────────────────
    scripts_dir = None
    if ctx.repo_cache is not None:
        scripts_dir = ctx.repo_cache.get_path("boot-time-analysis-scripts")
    if scripts_dir is None:
        return json.dumps(
            {
                "status": "failed",
                "error": (
                    "boot-time-analysis-scripts repo not found "
                    "in skill cache. Ensure it is configured in "
                    "harness_repos."
                ),
            }
        )

    test_script = scripts_dir / "boot-timings-test.sh"
    install_script = scripts_dir / "install-boot-time-analysis-tool.sh"
    merge_script = scripts_dir / "boot-time-merge.py"

    if not test_script.exists():
        return json.dumps(
            {
                "status": "failed",
                "error": (f"boot-timings-test.sh not found at {test_script}"),
            }
        )

    # ── Wait for SSH readiness ─────────────────────────
    # Freshly provisioned boards may not have SSH ready
    # immediately. Wait up to 60s for port 22.  If a
    # Jumpstarter lease is available and SSH fails, power-
    # cycle the board and retry — the board may not have
    # booted cleanly after flashing.
    import asyncio as _asyncio
    import socket as _socket

    _js_lease_id = ""
    if ctx.ticket:
        _js_fields = ctx.ticket.get("custom_fields", {})
        _js_meta = _js_fields.get("resource_provider_metadata", {})
        _js_lease_id = _js_meta.get("lease_id", "")
        if _js_fields.get("resource_provider") != "jumpstarter":
            _js_lease_id = ""

    _MAX_POWER_CYCLES = 3
    _SSH_ATTEMPTS_PER_CYCLE = 12
    _ssh_ready = False
    _power_cycles_attempted = 0
    _ssh_polling_windows = 0

    while True:
        _ssh_polling_windows += 1
        for _attempt in range(_SSH_ATTEMPTS_PER_CYCLE):
            try:
                s = _socket.create_connection((sut_host, 22), timeout=5)
                s.close()
                _ssh_ready = True
                break
            except (OSError, ConnectionRefusedError):
                logger.info(
                    "[boot-time] Waiting for SSH on %s (cycle %d/%d, attempt %d/%d)",
                    sut_host,
                    _power_cycles_attempted + 1,
                    _MAX_POWER_CYCLES + 1,
                    _attempt + 1,
                    _SSH_ATTEMPTS_PER_CYCLE,
                )
                await _asyncio.sleep(5)

        if _ssh_ready:
            break

        if not _js_lease_id:
            # No Jumpstarter lease — cannot power-cycle
            break

        if _power_cycles_attempted >= _MAX_POWER_CYCLES:
            break

        _power_cycles_attempted += 1
        logger.warning(
            "[boot-time] SSH unreachable after %ds,"
            " power-cycling board via Jumpstarter"
            " (lease=%s, power cycle %d/%d)",
            _SSH_ATTEMPTS_PER_CYCLE * 5,
            _js_lease_id,
            _power_cycles_attempted,
            _MAX_POWER_CYCLES,
        )
        try:
            _pc_proc = await AuditedSubprocessRunner().start(
                [
                    "jmp",
                    "shell",
                    "--lease",
                    _js_lease_id,
                    "--",
                    "j",
                    "power",
                    "cycle",
                ],
                mutating=True,
            )
            _pc_out, _pc_err = await asyncio.wait_for(
                _pc_proc.communicate(),
                timeout=60,
            )
            logger.info(
                "[boot-time] Power cycle complete (rc=%d), waiting for SSH",
                _pc_proc.returncode,
            )
        except Exception as _pc_exc:
            logger.warning(
                "[boot-time] Power cycle failed: %s",
                _pc_exc,
            )
            break

    # ── Prep: install boot-time-analysis-tools on SUT ─────────
    if not _ssh_ready:
        return json.dumps(
            {
                "status": "failed",
                "error": (
                    f"SUT {sut_host} not SSH-reachable after"
                    f" {_power_cycles_attempted} Jumpstarter power-cycle attempt(s)"
                    f" across {_ssh_polling_windows} SSH polling window(s)."
                    f" Board may need manual intervention."
                ),
            }
        )

    ssh_user = "root"
    ssh_password = "password"
    if ctx.ticket:
        fields = ctx.ticket.get("custom_fields", {})
        ssh_user = fields.get("ssh_user", ssh_user)
        ssh_password = fields.get("ssh_password", ssh_password)

    if install_script.exists():
        logger.info(f"[boot-time] Installing boot-time-analysis-tools on {sut_host}")
        import asyncio as _asyncio

        # Security: password on argv is visible in /proc/pid/cmdline.
        # The external scripts require --password= on the command line;
        # upstream fix: accept --password-file or SSHPASS env var.
        install_proc = await AuditedSubprocessRunner().start(
            [
                str(install_script),
                sut_host,
                f"--username={ssh_user}",
                f"--password={ssh_password}",
            ],
            cwd=str(scripts_dir),
            mutating=True,
        )
        install_out, install_err = await install_proc.communicate()
        if install_proc.returncode != 0:
            return json.dumps(
                {
                    "status": "failed",
                    "error": (
                        f"Failed to install boot-time-analysis-tools on {sut_host}"
                    ),
                    "output": install_out.decode(errors="replace")[-2000:],
                    "stderr": install_err.decode(errors="replace")[-1000:],
                }
            )
        logger.info(f"[boot-time] boot-time-analysis-tools installed on {sut_host}")

    # ── U-Boot configuration ──────────────────────────────────
    # For S32G boards, disable the U-Boot autoboot delay so it does
    # not inflate boot-time measurements.  U-Boot's default bootdelay
    # adds a user-configurable pause (~3s) before the kernel loads;
    # with bootdelay=0 the board boots immediately after power-on.
    #
    # Enabled automatically whenever the leased board is an S32G.
    # The setting persists in U-Boot NVRAM across reboots (saveenv),
    # so running it every test ensures consistent configuration even
    # after a re-flash.
    #
    # Opt out per-ticket with directive: configure_bootdelay=false
    # Force on for non-S32G boards with: configure_bootdelay=true
    _uboot_directives = fields.get("directives", {}) if ctx.ticket else {}
    _uboot_meta = _js_meta if ctx.ticket else {}  # extracted in SSH-wait section
    _board_target = _uboot_meta.get("board_target", "").lower()
    _board_selector = _uboot_meta.get("selector", "").lower()
    _is_s32g = "s32g" in _board_target or "s32g" in _board_selector
    _configure_bootdelay = _uboot_directives.get("configure_bootdelay", _is_s32g)
    _uboot_env_applied: list[str] = []  # e.g. ["bootdelay=0"]
    _uboot_config_note: str = ""

    if _js_lease_id and _configure_bootdelay:
        _helper_script = scripts_dir / "jumpstarter-serial-helper.py"
        if not _helper_script.exists():
            logger.warning(
                "[boot-time] jumpstarter-serial-helper.py not found at %s; "
                "skipping U-Boot configuration",
                _helper_script,
            )
        else:
            logger.info(
                "[boot-time] Configuring U-Boot bootdelay=0 (board=%s, lease=%s)",
                _board_target or "unknown",
                _js_lease_id,
            )
            try:
                _uboot_proc = await AuditedSubprocessRunner().start(
                    [
                        "jmp",
                        "shell",
                        "--lease",
                        _js_lease_id,
                        "--",
                        sys.executable,
                        str(_helper_script),
                        "set-uboot",
                        "-s",
                        "bootdelay",
                        "0",
                    ],
                    mutating=True,
                )
                _uboot_out, _uboot_err = await asyncio.wait_for(
                    _uboot_proc.communicate(),
                    timeout=120,
                )
                if _uboot_proc.returncode == 0:
                    logger.info(
                        "[boot-time] U-Boot bootdelay=0 configured and verified "
                        "(board=%s)",
                        _board_target or "unknown",
                    )
                    _uboot_env_applied = ["bootdelay=0"]
                    _uboot_config_note = (
                        f"U-Boot configured: bootdelay=0"
                        f" (auto-applied for S32G board '{_board_target}')."
                        " Removes U-Boot autoboot delay from measurements."
                        " Disable with directive configure_bootdelay=false."
                    )
                    # set-uboot boots the board after saving env.
                    # Wait for SSH to come back before proceeding.
                    import socket as _uboot_sock

                    _uboot_ssh_ready = False
                    for _uboot_attempt in range(36):  # 36 × 5s = 3 min
                        try:
                            _s = _uboot_sock.create_connection(
                                (sut_host, 22), timeout=5
                            )
                            _s.close()
                            _uboot_ssh_ready = True
                            break
                        except (OSError, ConnectionRefusedError):
                            logger.info(
                                "[boot-time] Waiting for SSH after U-Boot config"
                                " (attempt %d/36)",
                                _uboot_attempt + 1,
                            )
                            await asyncio.sleep(5)
                    if _uboot_ssh_ready:
                        logger.info("[boot-time] SSH ready after U-Boot config")
                    else:
                        logger.warning(
                            "[boot-time] SSH did not recover within 3 min after"
                            " U-Boot config; proceeding anyway"
                        )
                else:
                    _stderr = _uboot_err.decode(errors="replace")[-500:]
                    logger.warning(
                        "[boot-time] U-Boot configuration failed (rc=%d): %s",
                        _uboot_proc.returncode,
                        _stderr,
                    )
                    _uboot_config_note = (
                        f"U-Boot configuration attempted but failed"
                        f" (rc={_uboot_proc.returncode})."
                        " bootdelay may be non-zero; measurements may include"
                        " U-Boot autoboot delay."
                    )
            except asyncio.TimeoutError:
                logger.warning("[boot-time] U-Boot configuration timed out after 120s")
                _uboot_config_note = (
                    "U-Boot configuration timed out (120s)."
                    " bootdelay may be non-zero; measurements may include"
                    " U-Boot autoboot delay."
                )
            except Exception as _uboot_exc:
                logger.warning(
                    "[boot-time] U-Boot configuration failed: %s", _uboot_exc
                )
                _uboot_config_note = (
                    f"U-Boot configuration failed: {_uboot_exc}."
                    " bootdelay may be non-zero; measurements may include"
                    " U-Boot autoboot delay."
                )

    # ── Build command ─────────────────────────────────────────
    import asyncio as _asyncio

    run_uuid = uuid.uuid4().hex[:8]

    # Use ticket-ID-based artifact directory so artifacts
    # are discoverable by ticket ID without querying the
    # state store. Configurable via AGENTIC_PERF_ARTIFACTS.
    from paths import create_artifact_dir

    _ticket_id = os.environ.get("TICKET_ID", "")
    output_dir = create_artifact_dir(_ticket_id, run_uuid)

    # Persist output_dir early so partial results are discoverable
    # if the subprocess is killed before completion.
    try:
        if _ticket_id:
            await _append_output_dir(_ticket_id, str(output_dir))
            logger.info("[boot-time] Persisted output_dir early: %s", output_dir)
    except Exception:
        logger.debug("[boot-time] Failed to persist early output_dir", exc_info=True)

    artifact_filesystem = (
        AuditedFilesystem(
            RootedPath(
                output_dir, "artifact", logical_prefix=f"{_ticket_id}/{run_uuid}"
            ),
            ticket_id=_ticket_id,
            emit=durable_filesystem_emitter(),
            critical=True,
        )
        if _ticket_id
        else AuditedFilesystem.system(output_dir)
    )
    artifact_file_mode = 0o600 if _ticket_id else 0o644

    # Security: password on argv — see comment at install_proc above.
    cmd = [
        str(test_script),
        sut_host,
        str(samples),
        f"--username={ssh_user}",
        f"--password={ssh_password}",
        "--folder-prefix=results",
    ]
    if clean_journal:
        cmd.append("--clean-journal=true")

    # Auto-enable Jumpstarter serial capture when a
    # Jumpstarter lease is active. Always use the lease
    # name so the harness creates its own scoped connection.
    # Do NOT search for existing sockets in /tmp/ — with
    # concurrent tickets, a socket from another ticket's
    # platform agent could be found and then go stale.
    jumpstarter_env: dict[str, str] = {}
    if ctx.ticket:
        fields = ctx.ticket.get("custom_fields", {})
        metadata = fields.get("resource_provider_metadata", {})
        lease_id = metadata.get("lease_id", "")
        directives = fields.get("directives", {})
        serial_enabled = directives.get("jumpstarter_serial", False)
        is_jumpstarter = lease_id and fields.get("resource_provider") == "jumpstarter"
        # Resolve reboot method once — used by both branches.
        requested_reboot = (
            directives.get("reboot_method", "") or directives.get("boot_type", "")
        ).lower()
        # Map directives to the script's --reboot-method flag:
        #   cold        — power cycle primary (default when Jumpstarter available)
        #   warm        — SSH primary, cold fallback when SSH reboot fails
        #   warm-only   — SSH only, no cold fallback
        # warm_boot_fallback_to_cold=false → warm-only (no fallback)
        # warm_boot_fallback_to_cold=true or unset → warm (with fallback)
        if requested_reboot == "warm":
            if directives.get("warm_boot_fallback_to_cold", True) is False:
                script_reboot = "warm-only"
            else:
                script_reboot = "warm"
        elif requested_reboot:
            script_reboot = requested_reboot
        else:
            script_reboot = ""  # let the script decide based on hardware

        if is_jumpstarter and serial_enabled:
            cmd.append("--jumpstarter-serial")
            cmd.append(f"--jumpstarter-lease-name={lease_id}")
            if script_reboot:
                cmd.append(f"--reboot-method={script_reboot}")
            logger.info(
                "[boot-time] Jumpstarter serial capture, reboot_method=%s (lease %s)",
                script_reboot or "default",
                lease_id,
            )
        elif is_jumpstarter:
            cmd.append(f"--jumpstarter-lease-name={lease_id}")
            if script_reboot:
                cmd.append(f"--reboot-method={script_reboot}")
            logger.info(
                "[boot-time] Jumpstarter reboot_method=%s (lease %s)",
                script_reboot or "default",
                lease_id,
            )

        # Pass power-off delay from directives
        power_off_delay = directives.get("power_off_delay") or directives.get(
            "jumpstarter_power_off_delay"
        )
        if power_off_delay is not None:
            cmd.append(f"--power-off-delay={power_off_delay}")

    # Separator for boot-time-analysis-tools arguments
    cmd.append("--")
    cmd.extend(["--max-time", "0"])
    if kpi_pattern:
        cmd.extend(["--kpi-re-pattern", kpi_pattern])
    if description:
        cmd.extend(["--description", description])

    safe_cmd = [a for a in cmd[:6] if not a.startswith("--password=")]
    logger.info(
        "[boot-time] Executing: %s ... (%d samples)",
        " ".join(safe_cmd),
        samples,
    )

    # ── Passive serial capture ───────────────────────────
    # Run `jmp shell --lease <id> -- j serial pipe` in the
    # background to capture serial output during SSH-based
    # reboots. Non-blocking — if serial fails, the benchmark
    # continues. Captures firmware/watchdog/kernel messages
    # that aren't available via SSH or journal.
    import os as _os

    serial_proc = None
    serial_log_fh = None
    exporter_log_fh = None
    harness_log_fh = None
    serial_log_path = output_dir / "serial-capture.log"
    exporter_log_path = output_dir / "exporter-logs.log"
    try:
        if ctx.ticket:
            _fields = ctx.ticket.get("custom_fields", {})
            _metadata = _fields.get(
                "resource_provider_metadata",
                {},
            )
            _lease_id = _metadata.get("lease_id", "")
            _directives = _fields.get("directives", {})
            # Default to serial capture for Jumpstarter boards.
            # Serial output is the only diagnostic evidence when
            # boards fail to come back from reboots.
            _is_jumpstarter = _fields.get("resource_provider") == "jumpstarter"
            _passive_serial = _directives.get(
                "serial_capture",
                _is_jumpstarter,
            )
            # Don't run passive serial alongside active serial
            _serial_active = _directives.get(
                "jumpstarter_serial",
                False,
            )
            if (
                _lease_id
                and _fields.get("resource_provider") == "jumpstarter"
                and _passive_serial
                and not _serial_active
            ):
                try:
                    serial_log_fh = artifact_filesystem.open_stream(
                        "serial-capture.log", mode=artifact_file_mode
                    )
                    # Exporter logs are best-effort — failure to open
                    # must not prevent serial capture from starting.
                    try:
                        exporter_log_fh = artifact_filesystem.open_stream(
                            "exporter-logs.log", mode=artifact_file_mode
                        )
                    except Exception:
                        logger.warning(
                            "[boot-time] Could not open exporter-logs.log, "
                            "using DEVNULL for exporter stderr"
                        )
                        exporter_log_fh = subprocess.DEVNULL
                    serial_proc = await AuditedSubprocessRunner().start(
                        [
                            "jmp",
                            "shell",
                            f"--lease={_lease_id}",
                            "--exporter-logs",
                            "--",
                            "j",
                            "serial",
                            "pipe",
                        ],
                        stdout=serial_log_fh,
                        stderr=exporter_log_fh,
                        mutating=True,
                    )
                    logger.info(
                        f"[boot-time] Passive serial capture "
                        f"started (lease={_lease_id}, "
                        f"pid={serial_proc.pid})"
                    )
                except Exception as e:
                    logger.warning(
                        f"[boot-time] Failed to start passive serial capture: {e}"
                    )
                    serial_proc = None
                    if serial_log_fh:
                        serial_log_fh.close()
                        serial_log_fh = None
                    if exporter_log_fh:
                        exporter_log_fh.close()
                        exporter_log_fh = None

        # ── Execute ───────────────────────────────────────────
        # Run from output_dir so boot-timings-test.sh creates
        # its results folder (and SCPs files) here, not relative
        # to the scripts repo.
        # Merge Jumpstarter env vars with current environment
        run_env = {**_os.environ, **jumpstarter_env} if jumpstarter_env else None
        # Timeout: samples × ~90s per sample + 15min overhead.
        # Prevents orphaned jmp shell subprocesses from keeping
        # the pipe open indefinitely (observed: 4hr hang when
        # capture-boot timed out but jmp shell child lingered).
        # Timeout: samples × per-sample estimate + 15 min overhead.
        # Per-sample time = boot duration (~18s) + power-off delay + SSH
        # reconnect overhead.  Cold reboots with a 45s power-off need ~70s
        # of infrastructure time per sample on top of the boot itself.
        # Default to 90s/sample when no power-off delay is configured (warm
        # reboots); add the full power-off delay when it is set.
        try:
            _power_off_s = max(0.0, float(power_off_delay or 0))
        except (TypeError, ValueError):
            logger.warning(
                "[boot-time] Ignoring non-numeric power_off_delay %r for timeout",
                power_off_delay,
            )
            _power_off_s = 0.0
        _per_sample_s = 90 + _power_off_s
        benchmark_timeout = int(round(samples * _per_sample_s)) + 900

        # Capture full subprocess output to an artifact log file.
        # The tool result truncates stdout to the last 3000 bytes,
        # which loses critical diagnostics (BOOT_CONFIG, power cycle
        # results, error messages) when wait_for_sut_up spam fills
        # the buffer. The log file preserves everything.
        try:
            harness_log_fh = artifact_filesystem.open_stream(
                "harness-output.log", mode=artifact_file_mode
            )
        except Exception as exc:
            logger.warning("[boot-time] Cannot open harness output log: %s", exc)

        proc = await AuditedSubprocessRunner().start(
            cmd,
            cwd=str(output_dir),
            env=run_env,
            mutating=True,
        )
        _STALL_CHECK_INTERVAL = 60
        # Allow 360s so the boot-time script's failure diagnostics
        # (exporter status, power status, serial probe) have time to
        # run after wait_for_sut_up times out at 300s.  The diagnostics
        # take ~30s and produce DIAGNOSTIC: lines that are critical for
        # differentiating power control, gRPC, and boot failures.
        _STALL_TIMEOUT = 360

        # Drain bounded chunks so an unterminated line cannot exceed the
        # StreamReader line limit and strand the benchmark subprocess.
        _stdout_chunks: list[bytes] = []
        _stderr_chunks: list[bytes] = []
        _output_chunk_count = 0
        harness_log_failed = False

        async def _drain_stream(
            stream: _asyncio.StreamReader | None,
            chunks: list[bytes],
            prefix: bytes = b"",
        ) -> None:
            nonlocal harness_log_failed
            if stream is None:
                return
            at_line_start = True
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                nonlocal _output_chunk_count
                _output_chunk_count += 1
                if harness_log_fh is not None and not harness_log_failed:
                    try:
                        # A line may span reads. Prefix each stderr line once,
                        # including empty lines and lines beyond 64 KiB.
                        if prefix:
                            tagged = (prefix if at_line_start else b"") + chunk.replace(
                                b"\n", b"\n" + prefix
                            )
                            if chunk.endswith(b"\n"):
                                tagged = tagged[: -len(prefix)]
                            at_line_start = chunk.endswith(b"\n")
                        else:
                            tagged = chunk
                        harness_log_fh.write(tagged)
                        harness_log_fh.flush()
                    except Exception as exc:
                        harness_log_failed = True
                        logger.warning(
                            "[boot-time] Cannot write harness output log: %s", exc
                        )

        _drain_stdout = _asyncio.create_task(_drain_stream(proc.stdout, _stdout_chunks))
        _drain_stderr = _asyncio.create_task(
            _drain_stream(proc.stderr, _stderr_chunks, b"STDERR: ")
        )
        drain_task = _asyncio.gather(_drain_stdout, _drain_stderr)

        async def _wait_drains() -> None:
            # asyncio Process.wait() can itself wait for pipe EOF. The parent
            # returncode is set independently, even when a descendant holds
            # an inherited write end open.
            while proc.returncode is None:
                if drain_task.done() and drain_task.exception() is not None:
                    await drain_task
                await _asyncio.sleep(0.05)
            await _asyncio.wait_for(
                _asyncio.shield(drain_task), timeout=_BOOT_TIME_DRAIN_TIMEOUT
            )

        communicate_task = _asyncio.create_task(_wait_drains())
        loop = _asyncio.get_running_loop()
        start_time = loop.time()
        deadline = start_time + benchmark_timeout
        try:
            last_file_count = sum(1 for path in output_dir.rglob("*") if path.is_file())
        except OSError:
            last_file_count = 0
        last_progress_time = start_time
        last_heartbeat = start_time
        last_progress_comment = start_time
        _last_chunk_count = 0
        stall_killed = False
        timed_out = False
        drain_complete = False
        cancelled = False
        drain_error = ""

        try:
            while not communicate_task.done():
                remaining = deadline - loop.time()
                if remaining <= 0:
                    logger.warning(
                        "[boot-time] Subprocess timed out after %ds, killing",
                        benchmark_timeout,
                    )
                    timed_out = True
                    if proc.returncode is None:
                        proc.kill()
                    break

                try:
                    await _asyncio.wait_for(
                        _asyncio.shield(communicate_task),
                        timeout=min(_STALL_CHECK_INTERVAL, remaining),
                    )
                    break
                except _asyncio.TimeoutError:
                    now = loop.time()
                    if now - last_heartbeat >= _PROGRESS_HEARTBEAT_INTERVAL_S:
                        last_heartbeat = now
                        post_comment = (
                            now - last_progress_comment >= _PROGRESS_COMMENT_INTERVAL_S
                        )
                        if post_comment:
                            last_progress_comment = now
                        await _publish_progress_heartbeat(
                            _ticket_id,
                            _stdout_chunks,
                            harness="boot-time",
                            elapsed_seconds=now - start_time,
                            samples_requested=samples,
                            post_comment=post_comment,
                        )
                    try:
                        file_count = sum(
                            1 for path in output_dir.rglob("*") if path.is_file()
                        )
                    except OSError:
                        file_count = last_file_count

                    if file_count > last_file_count:
                        last_file_count = file_count
                        last_progress_time = now
                    elif _output_chunk_count > _last_chunk_count:
                        # Script is producing output (e.g., retry
                        # loops, wait_for_sut_up polling) even if
                        # no new files have appeared yet.
                        _last_chunk_count = _output_chunk_count
                        last_progress_time = now
                    elif now - last_progress_time >= _STALL_TIMEOUT:
                        logger.warning(
                            "[boot-time] No new artifacts for %ds (stall detected at "
                            "%d files), killing subprocess",
                            int(now - last_progress_time),
                            file_count,
                        )
                        if proc.returncode is None:
                            proc.kill()
                        stall_killed = True
                        break
                except Exception as exc:
                    drain_error = f"subprocess output read failed: {exc}"
                    break

            if not drain_error:
                try:
                    await _asyncio.wait_for(
                        _asyncio.shield(communicate_task),
                        timeout=_BOOT_TIME_DRAIN_TIMEOUT,
                    )
                    drain_complete = True
                except _asyncio.TimeoutError:
                    drain_error = "subprocess output pipes remained open after exit"
                except Exception as exc:
                    drain_error = f"subprocess output read failed: {exc}"
        except _asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            # gather() can finish on one reader's error while its sibling is
            # still blocked. Join each reader before closing the shared log.
            for task in (communicate_task, _drain_stdout, _drain_stderr):
                if not task.done():
                    task.cancel()
            await _asyncio.gather(
                communicate_task,
                _drain_stdout,
                _drain_stderr,
                drain_task,
                return_exceptions=True,
            )
            for _ in range(200):
                if proc.returncode is not None:
                    break
                await _asyncio.sleep(0.05)
            if proc.returncode is None:
                logger.warning("[boot-time] Benchmark did not exit after kill")
            stdout_bytes = b"".join(_stdout_chunks)
            stderr_bytes = b"".join(_stderr_chunks)
            if drain_error:
                logger.warning("[boot-time] %s", drain_error)
            await proc.finish_streamed_output(
                stdout_bytes,
                stderr_bytes,
                drain_complete=drain_complete,
                timed_out=timed_out,
                cancelled=cancelled,
                failed=stall_killed,
            )

        exit_code = proc.returncode if proc.returncode is not None else 1
        if not drain_complete or timed_out or stall_killed:
            exit_code = exit_code or 1
        stdout_str = stdout_bytes.decode(errors="replace")
        stderr_str = stderr_bytes.decode(errors="replace")
        if drain_error:
            stderr_str += f"\n[agentic-perf] {drain_error}"
        if stall_killed:
            stderr_str += (
                "\n[agentic-perf] Benchmark killed: no new artifact files for 5 "
                "minutes (stall detected). This may indicate the board is "
                "unresponsive after a cold reboot or the serial connection is dead."
            )
        if timed_out:
            stderr_str += (
                f"\n[agentic-perf] Benchmark killed: subprocess timeout after "
                f"{benchmark_timeout}s ({benchmark_timeout // 60} min). "
                f"Consider increasing the power_off_delay budget or reducing "
                f"the requested sample count."
            )

        if stall_killed or timed_out:
            kill_reason: dict[str, Any] = {
                "code": "harness_stall" if stall_killed else "harness_timeout",
                "timestamp": round(time.time()),
                "mechanism": "benchmark_harness",
            }
            if timed_out:
                kill_reason["timeout_seconds"] = benchmark_timeout
            if stall_killed:
                kill_reason["stall_seconds"] = _STALL_TIMEOUT
            try:
                await _record_subprocess_kill_reason(_ticket_id, kill_reason)
            except Exception:
                logger.warning(
                    "[boot-time] Could not record harness kill reason", exc_info=True
                )

        # ── Parse reboot method from script output ───────
        reboot_method = ""
        for line in stdout_str.split("\n"):
            if line.startswith("Reboot mode:"):
                reboot_method = line.split(":", 1)[1].strip()
                break

        # ── Stall diagnostics ─────────────────────────────────
        # When the stall detector kills the process, capture
        # board state before reporting failure.
        stall_diag: dict[str, Any] = {}
        # Capture diagnostics on any failure, not just stall kills.
        # The script may exit with code 1 (serial timeout, boot
        # failure) before the stall detector triggers.
        # Check for boot sample files (boot_time_logs.json), not
        # raw file count — serial/metadata files are always present.
        sample_count = _count_boot_time_samples(output_dir)
        run_diag = stall_killed or (exit_code != 0 and sample_count == 0)
        if run_diag and ctx.ssh is not None and sut_host:
            logger.info("[boot-time] Capturing stall diagnostics for %s", sut_host)
            try:
                ping = await ctx.ssh.run(
                    sut_host,
                    "echo ALIVE",
                    timeout=5,
                )
                stall_diag["ssh_reachable"] = (
                    ping.exit_code == 0 and "ALIVE" in ping.stdout
                )
            except Exception:
                stall_diag["ssh_reachable"] = False

            if not stall_diag.get("ssh_reachable"):
                # Board not reachable via SSH — try ping
                try:
                    ping_proc = await _asyncio.create_subprocess_exec(
                        "ping",
                        "-c1",
                        "-W3",
                        sut_host,
                        stdout=_asyncio.subprocess.DEVNULL,
                        stderr=_asyncio.subprocess.DEVNULL,
                    )
                    await ping_proc.wait()
                    stall_diag["pingable"] = ping_proc.returncode == 0
                except Exception:
                    stall_diag["pingable"] = False

            stall_diag["samples_before_stall"] = sample_count
            stall_diag["stall_duration_s"] = _STALL_TIMEOUT

            # Preserve the raw tail in the artifact/tool response, not service logs.
            stall_diag.update(_boot_serial_diagnostics(serial_log_path))

            # Write diagnostics to artifact file
            try:
                import json as _json

                artifact_filesystem.write(
                    "stall-diagnostics.json",
                    _json.dumps(stall_diag, indent=2),
                    mode=artifact_file_mode,
                )
                logger.info(
                    "[boot-time] Stall diagnostics: %s",
                    {
                        key: value
                        for key, value in stall_diag.items()
                        if key != "serial_tail"
                    },
                )
            except Exception:
                pass

    finally:
        # Stop capture on command-start failures, monitoring errors, and cancellation.
        try:
            if serial_proc is not None:
                try:
                    if serial_proc.returncode is None:
                        try:
                            serial_proc.terminate()
                        except ProcessLookupError:
                            pass
                    try:
                        await serial_proc.wait(timeout=10)
                    except _asyncio.TimeoutError:
                        # The tracked wait has already escalated to kill.
                        pass
                    logger.info("[boot-time] Passive serial capture stopped")
                except Exception as exc:
                    logger.warning("[boot-time] Error stopping serial capture: %s", exc)
        finally:
            if serial_log_fh is not None:
                serial_log_fh.close()
            if exporter_log_fh is not None:
                exporter_log_fh.close()
            if harness_log_fh is not None:
                try:
                    harness_log_fh.close()
                except Exception as exc:
                    logger.warning(
                        "[boot-time] Cannot close harness output log: %s", exc
                    )
    if serial_log_path.exists():
        size = serial_log_path.stat().st_size
        if size > 0:
            logger.info(
                f"[boot-time] Serial capture log: {serial_log_path} ({size} bytes)"
            )
        else:
            # Remove empty log file
            artifact_filesystem.unlink("serial-capture.log", missing_ok=True)
    if exporter_log_path.exists():
        size = exporter_log_path.stat().st_size
        if size > 0:
            logger.info(
                f"[boot-time] Exporter logs: {exporter_log_path} ({size} bytes)"
            )
        else:
            artifact_filesystem.unlink("exporter-logs.log", missing_ok=True)

    # ── Parse results ─────────────────────────────────────────
    # Find the results folder created by boot-timings-test.sh
    result_folders = sorted(output_dir.glob("results-*"))
    if not result_folders:
        # Try without prefix pattern
        result_folders = [d for d in output_dir.iterdir() if d.is_dir()]

    boot_time_logs: list[Path] = []
    for folder in result_folders:
        boot_time_logs.extend(sorted(folder.glob("*boot_time_logs.json")))

    # ── Collect system metadata ───────────────────────
    metadata_file = output_dir / "metadata.json"
    collect_metadata = scripts_dir / "collect-system-metadata.sh"
    if collect_metadata.exists():
        logger.info(f"[boot-time] Collecting system metadata from {sut_host}")
        meta_proc = await AuditedSubprocessRunner().start(
            [
                str(collect_metadata),
                sut_host,
            ],
            cwd=str(scripts_dir),
            mutating=True,
        )
        meta_out, _ = await meta_proc.communicate()
        if meta_proc.returncode == 0 and meta_out:
            artifact_filesystem.write(
                "metadata.json", meta_out, mode=artifact_file_mode
            )
            logger.info("[boot-time] Metadata collected")
        else:
            # Create minimal stub so merge can proceed
            artifact_filesystem.write("metadata.json", "{}", mode=artifact_file_mode)
            logger.info("[boot-time] Metadata collection failed — using empty stub")
    else:
        artifact_filesystem.write("metadata.json", "{}", mode=artifact_file_mode)

    # ── Merge into Horreum-compatible JSON ─────────────
    merged_file = output_dir / "merged-results.json"
    if boot_time_logs and merge_script.exists():
        merge_cmd = [
            sys.executable,
            str(merge_script),
            "-m",
            str(metadata_file),
            "--schema",
            "urn:boot-time-verbose:07",
            "--run-source",
            "agentic-perf",
        ]
        if description:
            merge_cmd.extend(["--description", description])
        # Pass partial-run info if available
        for folder in result_folders:
            status_file = folder / "collection_status.json"
            if status_file.exists():
                try:
                    cs = json.loads(status_file.read_text())
                    merge_cmd.extend(
                        [
                            "--requested-samples",
                            str(cs.get("requested_samples", samples)),
                        ]
                    )
                    if cs.get("partial"):
                        merge_cmd.extend(
                            [
                                "--partial-run",
                                "--partial-failure-reason",
                                cs.get(
                                    "failure_reason",
                                    "unknown",
                                ),
                            ]
                        )
                except (json.JSONDecodeError, OSError):
                    pass
                break
        merge_cmd.extend(str(f) for f in boot_time_logs)

        merge_proc = await AuditedSubprocessRunner().start(
            merge_cmd,
            cwd=str(scripts_dir),
            mutating=True,
        )
        merge_out, merge_err = await merge_proc.communicate()
        if merge_proc.returncode == 0 and merge_out:
            artifact_filesystem.write(
                "merged-results.json", merge_out, mode=artifact_file_mode
            )
            logger.info(f"[boot-time] Merged results saved to {merged_file}")
        else:
            logger.warning(
                "[boot-time] Merge failed: " + merge_err.decode(errors="replace")[:200]
            )

    # ── Extract KPIs from per-sample summary files ───────
    kpis: dict[str, Any] = {}
    summary_files: list[Path] = []
    for folder in result_folders:
        summary_files.extend(sorted(folder.glob("*_summary.json")))
    # Exclude all_summary.json (combined file)
    summary_files = [f for f in summary_files if f.name != "all_summary.json"]
    if summary_files:
        sa_totals: list[float] = []
        sa_kernels: list[float] = []
        sa_initrds: list[float] = []
        sa_userspaces: list[float] = []
        for sf in summary_files:
            try:
                sd = json.loads(sf.read_text())
                sa = sd.get("satime", {})
                if "total" in sa:
                    sa_totals.append(sa["total"])
                if "kernel" in sa:
                    sa_kernels.append(sa["kernel"])
                if "initrd" in sa:
                    sa_initrds.append(sa["initrd"])
                if "userspace" in sa:
                    sa_userspaces.append(sa["userspace"])
            except (json.JSONDecodeError, OSError):
                continue

        def _avg(vals: list[float]) -> float | None:
            return round(sum(vals) / len(vals), 3) if vals else None

        kpis = {
            "sample_count": len(summary_files),
            "avg_total_boot_s": _avg(sa_totals),
            "avg_kernel_s": _avg(sa_kernels),
            "avg_initrd_s": _avg(sa_initrds),
            "avg_userspace_s": _avg(sa_userspaces),
        }

    # KPI pattern was requested but no matching data found.
    # This indicates the board didn't produce expected boot
    # metrics — treat as a node-level failure condition.
    kpi_failure = False
    if kpi_pattern and exit_code == 0:
        has_kpi_data = bool(
            kpis.get("avg_kernel_s")
            or kpis.get("avg_initrd_s")
            or kpis.get("avg_userspace_s")
        )
        if not has_kpi_data:
            kpi_failure = True

    status = "completed"
    if exit_code != 0:
        status = "failed"
    elif kpi_failure:
        status = "completed_no_kpi"

    response: dict[str, Any] = {
        "status": status,
        "exit_code": exit_code,
        "run_id": f"boot-time-{run_uuid}",
        "harness": "boot-time",
        "samples_requested": samples,
        "samples_collected": len(boot_time_logs),
        "output_dir": str(output_dir),
        "message": (
            f"Boot time test completed ({len(boot_time_logs)}/{samples} samples)"
            if exit_code in (0, 2) and not kpi_failure
            else (
                "Boot time test completed but expected KPI "
                "patterns not found (kernel/initrd/userspace). "
                "The board may not be producing valid boot "
                "metrics. This is a node-level issue."
            )
            if kpi_failure
            else f"Boot time test failed (exit {exit_code})"
        ),
    }
    if kpis:
        response["kpis"] = kpis
    if merged_file.exists():
        response["merged_results_file"] = str(merged_file)
        try:
            merged = json.loads(merged_file.read_text())
            cfg = merged.get("rhivos_config", {})
            if cfg:
                response["system_config"] = {
                    k: cfg[k]
                    for k in (
                        "kernel",
                        "os_name",
                        "architecture",
                    )
                    if cfg.get(k)
                }
        except (json.JSONDecodeError, OSError):
            pass
    if exit_code not in (0, 2):
        response["output"] = stdout_str[-3000:]
        response["error"] = stderr_str[-1000:]
        # Point the agent to the full artifact files for deeper analysis
        artifact_files = []
        if (output_dir / "harness-output.log").exists():
            artifact_files.append(str(output_dir / "harness-output.log"))
        if (output_dir / "serial-capture.log").exists():
            artifact_files.append(str(output_dir / "serial-capture.log"))
        if artifact_files:
            response["full_output_hint"] = (
                f"Output truncated. Full logs available via "
                f"read_artifact_file(): {', '.join(artifact_files)}"
            )
    else:
        # On success, include a compact summary so the
        # evaluate agent can see serial capture status
        # and key metrics without the full log.
        summary_lines = []
        for line in stdout_str.split("\n"):
            ll = line.lower()
            if any(
                k in ll
                for k in (
                    "serial capture",
                    "jumpstarter",
                    "sample",
                    "error",
                    "\u2713",
                    "\u2717",
                    "boot_time",
                    "kpi",
                    "warning",
                    "failed",
                )
            ):
                summary_lines.append(line.rstrip())
        if summary_lines:
            response["output_summary"] = "\n".join(summary_lines[-30:])

    # Save output_dir to ticket so the evaluate agent
    # can find artifacts. Accumulate across loop-back
    # runs so all artifacts remain accessible.
    if ctx.ticket and response.get("output_dir"):
        try:
            from providers.execution import AuditedAsyncHTTPClient

            store_url = os.environ.get("STATE_STORE_URL", "http://localhost:8090")
            from agents.server_utils import ticket_state_headers

            headers = ticket_state_headers()
            ticket_id = ctx.ticket.get("id", "")
            if ticket_id:
                async with AuditedAsyncHTTPClient(
                    timeout=10.0, headers=headers
                ) as _client:
                    # Fetch current list to append
                    r = await _client.get(
                        f"{store_url}/api/v1/tickets/{ticket_id}",
                    )
                    existing = r.json().get("custom_fields", {}).get("output_dirs", [])
                    new_dir = response["output_dir"]
                    if new_dir not in existing:
                        existing.append(new_dir)
                    result_fields = {
                        "output_dir": new_dir,
                        "output_dirs": existing,
                        # These per-run values are snapshotted by the fleet
                        # coordinator before the next board overwrites them.
                        "samples_collected": response.get("samples_collected", 0),
                        "benchmark_kpis": response.get("kpis") or {},
                    }
                    update_response = await _client.patch(
                        f"{store_url}/api/v1/tickets/{ticket_id}/fields",
                        json={"fields": result_fields},
                    )
                    update_response.raise_for_status()
                logger.info(
                    f"[boot-time] Saved output_dir to ticket"
                    f" {ticket_id} ({len(existing)} total)"
                )
        except Exception:
            logger.warning("Failed to save output_dir", exc_info=True)

    if stall_diag:
        response["stall_diagnostics"] = stall_diag

    if _uboot_env_applied:
        response["uboot_config_applied"] = _uboot_env_applied
    if _uboot_config_note:
        response["notes"] = _uboot_config_note

    if reboot_method:
        response["reboot_method"] = reboot_method
        # Flag mismatch: cold boot requested but SSH reboot used.
        # Check both canonical (reboot_method) and legacy (boot_type)
        # directive keys for the user's requested reboot method.
        _directives = (
            ctx.ticket.get("custom_fields", {}).get("directives", {})
            if ctx.ticket
            else {}
        )
        requested_method = _directives.get("reboot_method", "") or _directives.get(
            "boot_type", ""
        )
        if requested_method == "cold" and reboot_method == "ssh":
            response["reboot_method_mismatch"] = (
                "Cold boot requested but script used SSH "
                "reboots. Jumpstarter power control flags "
                "may not have been passed correctly."
            )

    return json.dumps(response)


def _count_boot_time_samples(output_dir: Path) -> int:
    """Return the number of boot-time sample artifacts in an output directory."""
    return sum(1 for _ in output_dir.glob("**/*boot_time_logs.json"))


async def _append_output_dir(ticket_id: str, output_dir_path: str) -> None:
    """Persist *output_dir_path* into ticket custom fields.

    Reads the current ``output_dirs`` list, appends the new path if it is not
    already present, and PATCHes both ``output_dir`` (scalar) and
    ``output_dirs`` (list) back to the state store.

    Does **not** catch exceptions — callers decide whether to treat failures
    as best-effort.
    """
    from agents.server_utils import ticket_state_headers
    from providers.execution import AuditedAsyncHTTPClient

    store_url = os.environ.get("STATE_STORE_URL", "http://localhost:8090")
    headers = ticket_state_headers()
    async with AuditedAsyncHTTPClient(timeout=10.0, headers=headers) as client:
        r = await client.get(f"{store_url}/api/v1/tickets/{ticket_id}")
        r.raise_for_status()
        ticket = r.json()
        if not isinstance(ticket, dict) or not isinstance(
            ticket.get("custom_fields"), dict
        ):
            raise ValueError("Ticket custom_fields must be an object")
        existing = ticket["custom_fields"].get("output_dirs", [])
        if existing is None:
            existing = []
        if not isinstance(existing, list):
            raise ValueError("Ticket output_dirs must be a list")
        existing_dirs = list(existing)
        if output_dir_path not in existing_dirs:
            existing_dirs.append(output_dir_path)
        patch_resp = await client.patch(
            f"{store_url}/api/v1/tickets/{ticket_id}/fields",
            json={
                "fields": {
                    "output_dir": output_dir_path,
                    "output_dirs": existing_dirs,
                    # A new run invalidates any kill reason from a previous
                    # attempt, so a later failure cannot inherit it.
                    **(
                        {"last_subprocess_kill_reason": None}
                        if ticket["custom_fields"].get("last_subprocess_kill_reason")
                        is not None
                        else {}
                    ),
                }
            },
        )
        patch_resp.raise_for_status()


async def _record_subprocess_kill_reason(
    ticket_id: str, reason: dict[str, Any]
) -> None:
    """Persist why the harness killed its subprocess (best effort).

    The benchmark agent reads the same field the orchestrator writes so
    both kill sources are reported identically instead of leaving a bare
    ``exit code -9``.
    """
    if not ticket_id:
        return
    from agents.server_utils import ticket_state_headers
    from providers.execution import AuditedAsyncHTTPClient

    store_url = os.environ.get("STATE_STORE_URL", "http://localhost:8090")
    async with AuditedAsyncHTTPClient(
        timeout=10.0, headers=ticket_state_headers()
    ) as client:
        response = await client.patch(
            f"{store_url}/api/v1/tickets/{ticket_id}/fields",
            json={"fields": {"last_subprocess_kill_reason": reason}},
        )
        response.raise_for_status()


# Progress heartbeat cadence for long subprocess runs.  Trace events are
# cheap and keep the orchestrator's stale-task detector informed; ticket
# comments also land in later agent context, so they are far less frequent.
_PROGRESS_HEARTBEAT_INTERVAL_S = 300.0
_PROGRESS_COMMENT_INTERVAL_S = 1800.0


def _publish_progress_trace(progress: dict[str, Any]) -> None:
    """Record one progress trace event.  Runs in a worker thread."""
    from providers.tracing import ActionType, LifecycleState, current_trace_context
    from providers.tracing.client import TraceClient
    from providers.tracing.runtime import TraceRecorder

    context = current_trace_context()
    token = os.environ.get("AGENTIC_PERF_API_TOKEN", "")
    if context is None or not token:
        return
    store_url = os.environ.get("STATE_STORE_URL", "http://localhost:8090")
    client = TraceClient(store_url, token)
    try:
        TraceRecorder(context, client).record(
            context,
            ActionType.SUBPROCESS,
            LifecycleState.STARTED,
            phase="progress",
            target="benchmark_progress",
            attributes=progress,
        )
        client.flush()
    finally:
        client.close()


async def _post_progress_comment(ticket_id: str, body: str) -> None:
    from agents.server_utils import ticket_state_headers
    from providers.execution import AuditedAsyncHTTPClient

    store_url = os.environ.get("STATE_STORE_URL", "http://localhost:8090")
    async with AuditedAsyncHTTPClient(
        timeout=10.0, headers=ticket_state_headers()
    ) as client:
        response = await client.post(
            f"{store_url}/api/v1/tickets/{ticket_id}/comments",
            json={"author": "benchmark", "body": body},
        )
        response.raise_for_status()


async def _publish_progress_heartbeat(
    ticket_id: str,
    stdout_chunks: list[bytes],
    *,
    harness: str,
    elapsed_seconds: float,
    samples_requested: int,
    post_comment: bool,
) -> dict[str, Any]:
    """Report subprocess progress to the trace store and ticket.

    The stale-task detector only sees durable trace events, so a long
    subprocess must publish some.  Failures here are never fatal to the
    benchmark.
    """
    import providers.skills.boot_time  # noqa: F401  # registers the parser
    from providers.skills.base import get_progress_parser

    progress: dict[str, Any] = {}
    parser = get_progress_parser(harness)
    if parser is not None:
        try:
            progress = parser(list(stdout_chunks)) or {}
        except Exception:
            logger.warning("[boot-time] Progress parser failed", exc_info=True)
    attributes = {
        "harness": harness,
        "elapsed_seconds": round(elapsed_seconds),
        "samples_requested": samples_requested,
        **progress,
    }
    if not ticket_id:
        return attributes
    try:
        await asyncio.to_thread(_publish_progress_trace, attributes)
    except Exception:
        logger.warning("[boot-time] Could not publish progress trace", exc_info=True)
    if post_comment:
        collected = attributes.get("samples_collected")
        detail = (
            f"{collected}/{samples_requested} samples"
            if collected is not None
            else "subprocess still running"
        )
        body = (
            f"**Benchmark progress:** {detail} after "
            f"{round(elapsed_seconds / 60)} min ({harness})."
        )
        try:
            await _post_progress_comment(ticket_id, body)
        except Exception:
            logger.warning("[boot-time] Could not post progress comment", exc_info=True)
    return attributes
