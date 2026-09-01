"""Repeated-failure circuit breaker for the agent loop.

Detects when a tool produces consecutive failures and injects
a system message nudging the agent to try a different approach.
Without this, some models (especially non-Claude) loop on the
same failing tool call indefinitely, wasting all iterations.

Configuration via config.json (top-level):

    {
        "circuit_breaker": {
            "enabled": true,
            "threshold": 3,
            "max_trips_per_tool": 2,
            "exempt_tools": []
        }
    }

Per-ticket override via custom_fields.circuit_breaker:

    {
        "circuit_breaker": {
            "threshold": 5,
            "exempt_tools": ["poll_run_status"]
        }
    }
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "threshold": 3,
    "max_trips_per_tool": 2,
    "exempt_tools": [],
}

_EMPTY_CONTENT = frozenset(("", "[]", "{}", "null"))

_NO_RESULT_STATUSES = frozenset(
    (
        "no_files_found",
        "no_results",
        "not_found",
        "no results found",
        "no matches found",
    )
)


def classify_result(
    tool_name: str,
    content: str,
    is_error: bool,
) -> bool:
    """Determine whether a tool result counts as a failure.

    Returns True if the result should increment the tool's
    consecutive failure streak. Conservative: a false positive
    nags a healthy agent, so only clear failures count.
    """
    if is_error:
        return True

    stripped = content.strip() if content else ""

    if stripped in _EMPTY_CONTENT:
        return True

    try:
        parsed = json.loads(stripped) if isinstance(stripped, str) else stripped
    except (json.JSONDecodeError, TypeError, ValueError):
        return False

    if not isinstance(parsed, dict):
        return False

    # Explicit error indicators (matching introspection's
    # classifier at agents/introspection/server.py:91-128).
    # Checked before file_ref so spilled error payloads are
    # still classified as failures.
    try:
        exit_code = int(parsed.get("exit_code", 0))
    except (TypeError, ValueError):
        exit_code = 0
    if exit_code != 0:
        return True
    if parsed.get("success") is False:
        return True
    status_val = str(parsed.get("status", "")).lower().strip()
    if status_val in ("failed", "error"):
        return True
    if status_val in _NO_RESULT_STATUSES:
        return True
    err_val = parsed.get("error")
    if err_val and str(err_val).lower() not in (
        "none",
        "null",
        "n/a",
        "false",
    ):
        return True

    # Spill descriptors are large payloads written to disk.
    # If no explicit error indicator was found above, the
    # tool produced substantial output — not a failure.
    if "file_ref" in parsed:
        return False

    # Empty collection in well-known result keys.
    for key in ("results", "matches", "files"):
        val = parsed.get(key)
        if val is not None and val in ([], {}):
            return True

    return False


class CircuitBreakerState:
    """Per-run mutable state tracking consecutive tool failures.

    Each tool has an independent failure streak — a failure on
    tool A does not reset tool B's counter (interleaving).
    """

    __slots__ = ("_consecutive", "_trips")

    def __init__(self) -> None:
        self._consecutive: dict[str, int] = {}
        self._trips: dict[str, int] = {}

    @property
    def trips(self) -> dict[str, int]:
        return self._trips

    def record(self, tool_name: str, is_failure: bool) -> None:
        """Record a tool result. Resets the streak on success."""
        if is_failure:
            self._consecutive[tool_name] = self._consecutive.get(tool_name, 0) + 1
        else:
            self._consecutive[tool_name] = 0

    def check(
        self,
        tool_name: str,
        threshold: int,
        max_trips: int,
        exempt_tools: list[str],
    ) -> tuple[bool, int]:
        """Check whether the circuit breaker should trip.

        Returns (should_trip, consecutive_count). Trips at
        every multiple of threshold, capped by max_trips.
        """
        if tool_name in exempt_tools:
            return False, self._consecutive.get(tool_name, 0)

        consec = self._consecutive.get(tool_name, 0)
        if threshold <= 0 or consec < threshold:
            return False, consec

        current_trips = self._trips.get(tool_name, 0)
        if current_trips >= max_trips:
            return False, consec

        if consec % threshold == 0:
            self._trips[tool_name] = current_trips + 1
            return True, consec

        return False, consec

    def get_consecutive(self, tool_name: str) -> int:
        return self._consecutive.get(tool_name, 0)


def _coerce_config(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize and type-coerce circuit breaker settings."""
    merged = {k: raw.get(k, v) for k, v in _DEFAULTS.items()}
    merged["enabled"] = bool(merged["enabled"])
    try:
        merged["threshold"] = int(merged["threshold"])
    except (TypeError, ValueError):
        merged["threshold"] = _DEFAULTS["threshold"]
    try:
        merged["max_trips_per_tool"] = int(merged["max_trips_per_tool"])
    except (TypeError, ValueError):
        merged["max_trips_per_tool"] = _DEFAULTS["max_trips_per_tool"]
    if not isinstance(merged["exempt_tools"], list):
        merged["exempt_tools"] = _DEFAULTS["exempt_tools"]
    return merged


def circuit_breaker_from_config(
    config: dict[str, Any],
) -> dict[str, Any]:
    """Extract circuit breaker settings from orchestrator config.

    Returns a dict with keys: enabled, threshold,
    max_trips_per_tool, exempt_tools. Missing keys use defaults.
    """
    raw = config.get("circuit_breaker") or {}
    return _coerce_config(raw)


def circuit_breaker_from_custom_fields(
    custom_fields: dict[str, Any],
    config_cb: dict[str, Any],
) -> dict[str, Any]:
    """Merge per-ticket circuit_breaker overrides with config.

    Per-ticket fields override config values key-by-key.
    Values are coerced to the expected types.
    """
    ticket_cb = custom_fields.get("circuit_breaker") or {}
    merged = dict(config_cb)
    for key in ("enabled", "threshold", "max_trips_per_tool", "exempt_tools"):
        if key in ticket_cb:
            merged[key] = ticket_cb[key]
    return _coerce_config(merged)
