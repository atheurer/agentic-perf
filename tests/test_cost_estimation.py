"""Tests for LLM cost estimation from pricing.yaml."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import providers.cost as cost_module
from providers.cost import (
    estimate_cost,
    estimate_cumulative_cost,
)


def setup_function():
    """Clear the pricing cache before each test."""
    cost_module._pricing_cache = None


def test_known_model_pricing():
    """Known models use their specific pricing."""
    c = estimate_cost("claude-sonnet-4", 1000, 500)
    # 1000 * 3.0/1M + 500 * 15.0/1M = 0.003 + 0.0075
    assert abs(c - 0.0105) < 0.0001


def test_versioned_model_prefix_match():
    """Model names with version suffixes match by prefix."""
    c = estimate_cost("claude-sonnet-4-6", 1000, 500)
    assert abs(c - 0.0105) < 0.0001


def test_unknown_model_uses_fallback():
    """Unknown models use fallback pricing."""
    c = estimate_cost("unknown-model-v1", 1000, 500)
    # Fallback: 3.0/1M input, 15.0/1M output
    assert abs(c - 0.0105) < 0.0001


def test_zero_tokens():
    """Zero tokens costs nothing."""
    assert estimate_cost("claude-sonnet-4", 0, 0) == 0.0


def test_openai_model():
    """OpenAI models have their own pricing."""
    c = estimate_cost("gpt-4o", 1000, 500)
    # 1000 * 2.5/1M + 500 * 10.0/1M = 0.0025 + 0.005
    assert abs(c - 0.0075) < 0.0001


def test_google_model():
    """Google models have their own pricing."""
    c = estimate_cost("gemini-2.5-pro", 1000, 500)
    assert c > 0


def test_cumulative_cost():
    """Estimate from a CumulativeUsage dict."""
    usage = {
        "input_tokens": 10000,
        "output_tokens": 5000,
        "models_used": ["claude-sonnet-4-6"],
    }
    c = estimate_cumulative_cost(usage)
    assert abs(c - 0.105) < 0.001


def test_cumulative_cost_no_model():
    """No model info falls back to default pricing."""
    usage = {
        "input_tokens": 1000,
        "output_tokens": 500,
        "models_used": [],
    }
    c = estimate_cumulative_cost(usage)
    assert c > 0


def test_user_pricing_override(tmp_path: Path):
    """User pricing.yaml overrides bundled pricing."""
    custom = tmp_path / "pricing.yaml"
    custom.write_text(
        "fallback:\n"
        "  input_per_token: 0.001\n"
        "  output_per_token: 0.002\n"
        "models:\n"
        "  test-model:\n"
        "    input_per_token: 0.01\n"
        "    output_per_token: 0.02\n"
    )

    with patch.object(cost_module, "_USER_PRICING", custom):
        cost_module._pricing_cache = None
        c = estimate_cost("test-model", 100, 50)
        # 100 * 0.01 + 50 * 0.02 = 1.0 + 1.0
        assert abs(c - 2.0) < 0.001


def test_pricing_yaml_exists():
    """Bundled pricing.yaml exists and is valid."""
    pricing_file = Path(__file__).parent.parent / "providers" / "cost" / "pricing.yaml"
    assert pricing_file.exists()

    import yaml

    data = yaml.safe_load(pricing_file.read_text(encoding="utf-8"))
    assert "fallback" in data
    assert "models" in data
    assert len(data["models"]) > 0
