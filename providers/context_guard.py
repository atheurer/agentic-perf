"""Context-window guardrails.

Monitors per-call context usage against the model's context
window and pauses the agent before it hits the provider's
hard limit. Without this, a context overflow surfaces as an
opaque API error, wasting the current iteration.

Configuration via config.json (top-level):

    {
        "context_guard": {
            "enabled": true,
            "warn_pct": 60,
            "pause_pct": 80,
            "default_context_window": 200000
        }
    }

Per-ticket override via custom_fields.context_guard:

    {
        "context_guard": {
            "warn_pct": 70,
            "pause_pct": 90
        }
    }
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULTS = {
    "enabled": True,
    "warn_pct": 60,
    "pause_pct": 80,
    "default_context_window": 0,
}


class ContextAction(str, Enum):
    OK = "ok"
    WARN = "warn"
    PAUSE = "pause"


def check_context_usage(
    context_tokens: int,
    context_window: int,
    warn_pct: float = 60.0,
    pause_pct: float = 80.0,
) -> tuple[ContextAction, str]:
    """Check context usage against the model's window.

    Args:
        context_tokens: Tokens used in the current call's
            input context (input + cache_read + cache_creation
            for Anthropic; prompt_tokens for others).
        context_window: Model's total context window size.
        warn_pct: Percentage at which to warn (0 disables).
        pause_pct: Percentage at which to pause (0 disables).

    Returns:
        (action, reason) tuple.
    """
    if context_window <= 0 or context_tokens <= 0:
        return ContextAction.OK, ""

    pct = context_tokens * 100.0 / context_window

    if pause_pct > 0 and pct >= pause_pct:
        return ContextAction.PAUSE, (
            f"Context window {pct:.0f}% full"
            f" ({context_tokens:,} / {context_window:,} tokens)."
            f" Pausing to prevent overflow."
        )

    if warn_pct > 0 and pct >= warn_pct:
        return ContextAction.WARN, (
            f"Context window {pct:.0f}% full"
            f" ({context_tokens:,} / {context_window:,} tokens)."
            f" Begin wrapping up."
        )

    return ContextAction.OK, ""


def context_guard_from_config(
    config: dict[str, Any],
) -> dict[str, Any]:
    """Extract context guard settings from orchestrator config.

    Returns a dict with keys: enabled, warn_pct, pause_pct,
    default_context_window. Missing keys use defaults.
    """
    raw = config.get("context_guard") or {}
    return {k: raw.get(k, v) for k, v in _DEFAULTS.items()}


def context_guard_from_custom_fields(
    custom_fields: dict[str, Any],
    config_guard: dict[str, Any],
) -> dict[str, Any]:
    """Merge per-ticket context_guard overrides with config.

    Per-ticket fields override config values key-by-key.
    """
    ticket_guard = custom_fields.get("context_guard") or {}
    merged = dict(config_guard)
    for key in ("enabled", "warn_pct", "pause_pct"):
        if key in ticket_guard:
            merged[key] = ticket_guard[key]
    return merged
