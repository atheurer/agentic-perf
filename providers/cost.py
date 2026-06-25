"""LLM cost estimation.

Estimates USD cost from token counts and model name. Pricing
is approximate and may lag behind provider pricing changes —
treat as a planning estimate, not an invoice.

Pricing sources:
- Anthropic: https://www.anthropic.com/pricing
- OpenAI: https://openai.com/api/pricing
"""

from __future__ import annotations

# Per-token pricing in USD. Input and output tokens are
# priced differently for most models.
# Prices as of June 2026 — update as needed.
MODEL_PRICING: dict[str, dict[str, float]] = {
    # Anthropic models
    "claude-opus-4": {
        "input": 15.0 / 1_000_000,
        "output": 75.0 / 1_000_000,
    },
    "claude-sonnet-4": {
        "input": 3.0 / 1_000_000,
        "output": 15.0 / 1_000_000,
    },
    "claude-haiku-3.5": {
        "input": 0.80 / 1_000_000,
        "output": 4.0 / 1_000_000,
    },
    # OpenAI models
    "gpt-4o": {
        "input": 2.50 / 1_000_000,
        "output": 10.0 / 1_000_000,
    },
    "gpt-4o-mini": {
        "input": 0.15 / 1_000_000,
        "output": 0.60 / 1_000_000,
    },
    "gpt-4-turbo": {
        "input": 10.0 / 1_000_000,
        "output": 30.0 / 1_000_000,
    },
}

# Fallback pricing for unknown models — uses a middle-range
# estimate so costs aren't zero but aren't wildly off.
_FALLBACK_PRICING = {
    "input": 3.0 / 1_000_000,
    "output": 15.0 / 1_000_000,
}


def _match_model(model: str) -> dict[str, float]:
    """Find pricing for a model name, with prefix matching.

    Model names from APIs often include version suffixes
    (e.g., claude-sonnet-4-6, gpt-4o-2024-05-13). We match
    by checking if the pricing key is a prefix of the model.
    """
    if model in MODEL_PRICING:
        return MODEL_PRICING[model]

    for key, pricing in MODEL_PRICING.items():
        if model.startswith(key):
            return pricing

    return _FALLBACK_PRICING


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
    pricing = _match_model(model)
    return input_tokens * pricing["input"] + output_tokens * pricing["output"]


def estimate_cumulative_cost(
    usage: dict[str, object],
) -> float:
    """Estimate USD cost from a CumulativeUsage dict.

    Uses the first model in models_used for pricing. If
    multiple models were used, this is approximate — for
    precise per-model cost, use estimate_cost() per call.
    """
    models = usage.get("models_used", [])
    model = models[0] if models else ""
    return estimate_cost(
        model,
        int(usage.get("input_tokens", 0)),
        int(usage.get("output_tokens", 0)),
    )
