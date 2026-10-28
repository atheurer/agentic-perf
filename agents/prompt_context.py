"""Formatting helpers for bounded ticket data in LLM user messages."""

from __future__ import annotations

import json
from typing import Any


def bounded_prompt_value(value: Any, max_chars: int) -> str | None:
    """Render a ticket value as bounded text without assuming it is a string."""
    if value is None:
        return None

    if isinstance(value, str):
        rendered = value
    else:
        try:
            rendered = json.dumps(value, ensure_ascii=True, indent=2, default=str)
        except (TypeError, ValueError, RecursionError):
            try:
                rendered = str(value)
            except Exception:
                rendered = "<unrenderable value>"

    if max_chars <= 0:
        return ""
    if len(rendered) <= max_chars:
        return rendered

    marker = "...[truncated]"
    if max_chars <= len(marker):
        return marker[:max_chars]
    return rendered[: max_chars - len(marker)] + marker
