"""Formatting for ``custom_fields["last_subprocess_kill_reason"]`` records.

The orchestrator's stale-task detector and the benchmark harness both record
why they killed a subprocess into the same ticket field.  Consumers (the
benchmark agent after a failed run, and the orchestrator's cancellation
guidance summary) format that record the same way.
"""

from __future__ import annotations

from typing import Any


def format_kill_reason(kill_reason: dict[str, Any] | None) -> str | None:
    """Format a structured kill reason into a human-readable message.

    Arguments:
        kill_reason: dict from custom_fields["last_subprocess_kill_reason"],
            or None if not present.

    Returns:
        Formatted message string, or None if no kill reason is available.
    """
    if not isinstance(kill_reason, dict):
        return None

    code = kill_reason.get("code")
    if code == "stale_task_timeout":
        idle_s = kill_reason.get("idle_seconds", "?")
        threshold_s = kill_reason.get("threshold_seconds", "?")
        return (
            f"Benchmark killed by orchestrator: no heartbeat activity for {idle_s}s "
            f"(threshold: {threshold_s}s). The harness may have hung or the board "
            f"may be unresponsive."
        )
    if code == "harness_timeout":
        timeout_s = kill_reason.get("timeout_seconds", "?")
        return (
            f"Benchmark killed by harness timeout after {timeout_s}s: the "
            f"subprocess exceeded the per-sample budget. This is not an "
            f"orchestrator stale-task kill. Consider increasing the "
            f"power_off_delay budget or reducing the sample count."
        )
    if code == "harness_stall":
        stall_s = kill_reason.get("stall_seconds", "?")
        return (
            f"Benchmark killed by the harness after {stall_s}s without new "
            f"artifact files (stall detected). The board may be unresponsive "
            f"after a cold reboot or the serial connection may be dead."
        )
    if code:
        return (
            f"Benchmark was terminated by the orchestrator (reason: {code}). "
            f"Check orchestrator logs for details."
        )
    return None
