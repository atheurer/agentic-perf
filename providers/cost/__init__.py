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

logger = logging.getLogger(__name__)

_BUNDLED_PRICING = Path(__file__).parent / "pricing.yaml"
_USER_PRICING = Path.home() / ".agentic-perf" / "pricing.yaml"

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


def _match_model(model: str, pricing: dict[str, Any]) -> dict[str, float]:
    """Find pricing for a model, with prefix matching.

    Model names from APIs often include version suffixes
    (e.g., claude-sonnet-4-6, gpt-4o-2024-05-13). We match
    by checking if a pricing key is a prefix of the model.
    """
    models = pricing.get("models", {})

    # Exact match
    if model in models:
        entry = models[model]
        return {
            "input": entry.get("input_per_token", 0),
            "output": entry.get("output_per_token", 0),
        }

    # Prefix match
    for key, entry in models.items():
        if model.startswith(key):
            return {
                "input": entry.get("input_per_token", 0),
                "output": entry.get("output_per_token", 0),
            }

    # Fallback
    fallback = pricing.get("fallback", {})
    return {
        "input": fallback.get("input_per_token", 0),
        "output": fallback.get("output_per_token", 0),
    }


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> float:
    """Estimate USD cost for a single LLM call.

    Args:
        model: Model name (e.g., "claude-sonnet-4-6").
        input_tokens: Number of input/prompt tokens.
        output_tokens: Number of output/completion tokens.

    Returns:
        Estimated cost in USD.
    """
    pricing = _load_pricing()
    rates = _match_model(model, pricing)
    return input_tokens * rates["input"] + output_tokens * rates["output"]


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
    )


def reload_pricing() -> None:
    """Force reload of pricing data.

    Call this after updating pricing.yaml to pick up
    changes without restarting.
    """
    global _pricing_cache
    _pricing_cache = None
    _load_pricing()
