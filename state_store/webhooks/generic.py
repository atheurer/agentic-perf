"""Generic webhook translator — passthrough that stores raw payload."""

from __future__ import annotations

from typing import Any


def translate(payload: dict[str, Any]) -> dict[str, Any]:
    """Return ticket fields with the raw payload stored as-is."""
    summary = payload.get("summary", "Webhook event")
    description = payload.get("description", "")
    return {
        "summary": str(summary),
        "description": str(description),
        "custom_fields": {
            "trigger_source": "generic",
            "raw_payload": payload,
        },
    }


def dedup_key(payload: dict[str, Any]) -> str | None:
    """Return a dedup key if the payload provides an ``id`` field."""
    raw_id = payload.get("id")
    if raw_id is not None:
        return f"generic:{raw_id}"
    return None
