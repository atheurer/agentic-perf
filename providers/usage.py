"""Pure helpers for aggregating persisted LLM usage events."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from providers.cost import estimate_cost


def summarize_usage_events(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Return the dashboard usage fields for a stream of usage events."""
    total_in = 0
    total_out = 0
    total_cache_read = 0
    total_cache_create = 0
    total_cost = 0.0
    llm_calls = 0
    models_seen: set[str] = set()

    for event in events:
        if event.get("event_type") != "llm_usage":
            continue
        data = event.get("data", {})
        input_tokens = data.get("input_tokens", 0) or 0
        output_tokens = data.get("output_tokens", 0) or 0
        if not input_tokens and not output_tokens:
            continue
        cache_read = data.get("cache_read_input_tokens", 0) or 0
        cache_create = data.get("cache_creation_input_tokens", 0) or 0
        model = data.get("model", "")
        total_in += input_tokens
        total_out += output_tokens
        total_cache_read += cache_read
        total_cache_create += cache_create
        llm_calls += 1
        if model:
            models_seen.add(model)
        total_cost += estimate_cost(
            model,
            input_tokens,
            output_tokens,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_create,
        )

    return {
        "input_tokens": total_in,
        "output_tokens": total_out,
        "cache_read_input_tokens": total_cache_read,
        "cache_creation_input_tokens": total_cache_create,
        "total_tokens": total_in + total_out + total_cache_read + total_cache_create,
        "llm_calls": llm_calls,
        "models_used": sorted(models_seen),
        "estimated_cost_usd": round(total_cost, 6),
    }
