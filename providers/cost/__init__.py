"""LLM cost estimation.

Estimates USD cost from token counts and model name using
pricing data from pricing.yaml. Pricing is approximate and
may lag behind provider pricing changes — treat as a planning
estimate, not an invoice.

Pricing sources are documented in pricing.yaml. To update
prices, edit that file with current per-token rates from
the provider's pricing page.

Users can also provide a custom pricing file via
~/.agentic-perf/pricing.yaml, which takes precedence over
the bundled default.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from paths import PRICING_PATH as _USER_PRICING

logger = logging.getLogger(__name__)

_BUNDLED_PRICING = Path(__file__).parent / "pricing.yaml"

_pricing_cache: dict[str, Any] | None = None


def _load_pricing() -> dict[str, Any]:
    """Load pricing data, user override takes precedence."""
    global _pricing_cache
    if _pricing_cache is not None:
        return _pricing_cache

    # User override
    if _USER_PRICING.exists():
        try:
            data = yaml.safe_load(_USER_PRICING.read_text(encoding="utf-8"))
            logger.info(f"[cost] Loaded pricing from {_USER_PRICING}")
            _pricing_cache = data
            return data
        except Exception:
            logger.warning(
                f"[cost] Failed to load {_USER_PRICING}, using bundled pricing"
            )

    # Bundled default
    try:
        data = yaml.safe_load(_BUNDLED_PRICING.read_text(encoding="utf-8"))
        _pricing_cache = data
        return data
    except Exception:
        logger.warning("[cost] Failed to load bundled pricing")
        _pricing_cache = {}
        return {}


def _match_model_entry(model: str, pricing: dict[str, Any]) -> dict[str, Any]:
    """Find the pricing entry for a model, with prefix matching.

    Model names from APIs often include version suffixes
    (e.g., claude-sonnet-4-6, gpt-4o-2024-05-13). We match
    by checking if a pricing key is a prefix of the model.

    Returns the raw entry dict (rates + context_window).
    """
    models = pricing.get("models", {})

    # Exact match
    if model in models:
        return models[model]

    # Prefix match — longest prefix first so "gpt-5.4-mini"
    # matches before "gpt-5.4" for "gpt-5.4-mini-2026-08-01".
    for key, entry in sorted(
        models.items(),
        key=lambda x: len(x[0]),
        reverse=True,
    ):
        if model.startswith(key):
            return entry

    # Fallback
    return pricing.get("fallback", {})


def _match_model(model: str, pricing: dict[str, Any]) -> dict[str, float]:
    """Find pricing rates for a model.

    Returns input, output, cache_read, and cache_write rates.
    Cache rates fall back to the input rate when not specified.
    """
    entry = _match_model_entry(model, pricing)
    input_rate = entry.get("input_per_token", 0)
    return {
        "input": input_rate,
        "output": entry.get("output_per_token", 0),
        "cache_read": entry.get("cache_read_per_token", input_rate),
        "cache_write": entry.get("cache_write_per_token", input_rate),
    }


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
) -> float:
    """Estimate USD cost for a single LLM call.

    Args:
        model: Model name (e.g., "claude-sonnet-4-6").
        input_tokens: Number of input/prompt tokens (total,
            including cached — the API reports this as the
            full count).
        output_tokens: Number of output/completion tokens.
        cache_read_input_tokens: Tokens served from cache
            (discounted rate).
        cache_creation_input_tokens: Tokens written to cache
            (may have a write premium).

    Returns:
        Estimated cost in USD.
    """
    pricing = _load_pricing()
    rates = _match_model(model, pricing)
    uncached = input_tokens - cache_read_input_tokens - cache_creation_input_tokens
    return (
        max(0, uncached) * rates["input"]
        + cache_read_input_tokens * rates["cache_read"]
        + cache_creation_input_tokens * rates["cache_write"]
        + output_tokens * rates["output"]
    )


def estimate_cumulative_cost(
    usage: dict[str, object],
) -> float:
    """Estimate USD cost from a CumulativeUsage dict.

    Uses the first model in models_used for pricing. If
    multiple models were used, this is approximate.
    """
    models = usage.get("models_used", [])
    model = models[0] if models else ""
    return estimate_cost(
        model,
        int(usage.get("input_tokens", 0)),
        int(usage.get("output_tokens", 0)),
        cache_read_input_tokens=int(usage.get("cache_read_input_tokens", 0)),
        cache_creation_input_tokens=int(usage.get("cache_creation_input_tokens", 0)),
    )


def get_context_window(model: str) -> int:
    """Return the context window size (tokens) for a model.

    Looks up context_window in pricing.yaml per-model entries
    first, then falls back to the fallback entry. User pricing
    files that lack context_window fall back per-key (not
    per-file) to the bundled default.
    """
    pricing = _load_pricing()
    entry = _match_model_entry(model, pricing)
    window = entry.get("context_window")
    if window is not None:
        return int(window)
    # User pricing file may lack context_window — fall back
    # to bundled entry for this model.
    try:
        bundled = yaml.safe_load(_BUNDLED_PRICING.read_text(encoding="utf-8"))
        bundled_entry = _match_model_entry(model, bundled)
        window = bundled_entry.get("context_window")
        if window is not None:
            return int(window)
    except Exception:
        pass
    return 128000


def reload_pricing() -> None:
    """Force reload of pricing data.

    Call this after updating pricing.yaml to pick up
    changes without restarting.
    """
    global _pricing_cache
    _pricing_cache = None
    _load_pricing()
