"""Tests for LLM cost estimation."""

from __future__ import annotations

from providers.cost import (
    estimate_cost,
    estimate_cumulative_cost,
)


def test_known_model_pricing():
    """Known models use their specific pricing."""
    cost = estimate_cost("claude-sonnet-4", 1000, 500)
    # 1000 * 3.0/1M + 500 * 15.0/1M = 0.003 + 0.0075
    assert abs(cost - 0.0105) < 0.0001


def test_versioned_model_prefix_match():
    """Model names with version suffixes match by prefix."""
    cost = estimate_cost("claude-sonnet-4-6", 1000, 500)
    assert abs(cost - 0.0105) < 0.0001


def test_unknown_model_uses_fallback():
    """Unknown models use fallback pricing."""
    cost = estimate_cost("unknown-model-v1", 1000, 500)
    # Fallback: 3.0/1M input, 15.0/1M output
    assert abs(cost - 0.0105) < 0.0001


def test_zero_tokens():
    """Zero tokens costs nothing."""
    cost = estimate_cost("claude-sonnet-4", 0, 0)
    assert cost == 0.0


def test_openai_model():
    """OpenAI models have their own pricing."""
    cost = estimate_cost("gpt-4o", 1000, 500)
    # 1000 * 2.5/1M + 500 * 10.0/1M = 0.0025 + 0.005
    assert abs(cost - 0.0075) < 0.0001


def test_cumulative_cost():
    """Estimate from a CumulativeUsage dict."""
    usage = {
        "input_tokens": 10000,
        "output_tokens": 5000,
        "models_used": ["claude-sonnet-4-6"],
    }
    cost = estimate_cumulative_cost(usage)
    # 10000 * 3.0/1M + 5000 * 15.0/1M = 0.03 + 0.075
    assert abs(cost - 0.105) < 0.001


def test_cumulative_cost_no_model():
    """No model info falls back to default pricing."""
    usage = {
        "input_tokens": 1000,
        "output_tokens": 500,
        "models_used": [],
    }
    cost = estimate_cumulative_cost(usage)
    assert cost > 0
