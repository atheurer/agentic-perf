"""Persistent, bounded retries for orchestrator recovery and agent dispatch."""

from __future__ import annotations

from typing import Any

RETRY_STATE_FIELD = "orchestrator_retry_state"
HANDOFF_RETRY_LIMIT = 10
HANDOFF_RETRY_BASE_SECONDS = 5.0
HANDOFF_RETRY_MAX_SECONDS = 300.0
DISPATCH_RETRY_LIMIT = 5
DISPATCH_RETRY_BASE_SECONDS = 5.0
DISPATCH_RETRY_MAX_SECONDS = 300.0


def _states(custom_fields: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = custom_fields.get(RETRY_STATE_FIELD, {})
    if not isinstance(raw, dict):
        return {}
    return {
        key: dict(value)
        for key, value in raw.items()
        if isinstance(key, str) and isinstance(value, dict)
    }


def retry_entry(
    custom_fields: dict[str, Any], kind: str, status: str
) -> dict[str, Any] | None:
    """Return retry metadata only when it belongs to the current ticket status."""
    entry = _states(custom_fields).get(kind)
    if entry is None or entry.get("status") != status:
        return None
    return entry


def retry_is_suppressed(
    custom_fields: dict[str, Any],
    kind: str,
    status: str,
    now: float,
) -> bool:
    """Whether this status should wait or remain stopped for human review."""
    entry = retry_entry(custom_fields, kind, status)
    if entry is None:
        return False
    if entry.get("exhausted") is True:
        return True
    try:
        return float(entry.get("next_retry_at", 0)) > now
    except (TypeError, ValueError):
        return False


def record_retry_failure(
    custom_fields: dict[str, Any],
    kind: str,
    status: str,
    *,
    now: float,
    retry_limit: int,
    base_seconds: float,
    max_seconds: float,
) -> tuple[dict[str, Any] | None, bool, int]:
    """Return updated retry state, exhaustion flag, and consecutive failures."""
    states = _states(custom_fields)
    previous = states.get(kind)
    attempts = 1
    if previous is not None and previous.get("status") == status:
        try:
            attempts = int(previous.get("attempts", 0)) + 1
        except (TypeError, ValueError):
            attempts = 1

    exhausted = attempts >= retry_limit
    delay = min(base_seconds * (2 ** (attempts - 1)), max_seconds)
    states[kind] = {
        "status": status,
        "attempts": attempts,
        "next_retry_at": None if exhausted else now + delay,
        "exhausted": exhausted,
        "last_failure_at": now,
    }
    return (states or None), exhausted, attempts


def clear_retry_entry(
    custom_fields: dict[str, Any], kind: str
) -> dict[str, Any] | None:
    """Remove one retry category while preserving unrelated retry metadata."""
    states = _states(custom_fields)
    states.pop(kind, None)
    return states or None


def prune_stale_retry_entries(
    custom_fields: dict[str, Any], current_status: str
) -> dict[str, Any] | None:
    """Clear retry limits when a human moves the ticket to a different status."""
    states = _states(custom_fields)
    states = {
        kind: entry
        for kind, entry in states.items()
        if entry.get("status") == current_status
    }
    return states or None
