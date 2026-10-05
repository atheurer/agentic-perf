"""Tests for LLM cost estimation from pricing.yaml."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

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

    c_sonnet5 = estimate_cost("claude-sonnet-5", 1000, 500)
    # 1000 * 3.0/1M + 500 * 15.0/1M = 0.003 + 0.0075 = 0.0105
    # (standard pricing post Aug 31 2026)
    assert abs(c_sonnet5 - 0.0105) < 0.0001

    c_opus5 = estimate_cost("claude-opus-5", 1000, 500)
    # 1000 * 5.0/1M + 500 * 25.0/1M = 0.005 + 0.0125 = 0.0175
    assert abs(c_opus5 - 0.0175) < 0.0001


def test_versioned_model_prefix_match():
    """Model names with version suffixes match by prefix."""
    c = estimate_cost("claude-sonnet-4-6", 1000, 500)
    assert abs(c - 0.0105) < 0.0001

    c_opus46 = estimate_cost("claude-opus-4-6", 1000, 500)
    # 1000 * 5.0/1M + 500 * 25.0/1M = 0.005 + 0.0125 = 0.0175
    assert abs(c_opus46 - 0.0175) < 0.0001


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

    c_gpt56 = estimate_cost("gpt-5.6-sol", 1000, 500)
    # 1000 * 4.0/1M + 500 * 20.0/1M = 0.004 + 0.010 = 0.014
    assert abs(c_gpt56 - 0.014) < 0.0001

    c_o3 = estimate_cost("o3", 1000, 500)
    # 1000 * 2.0/1M + 500 * 8.0/1M = 0.002 + 0.004 = 0.006
    assert abs(c_o3 - 0.006) < 0.0001


def test_openai_gpt6_model_pricing():
    """GPT-6 models use their standard input, cache, and output rates."""
    expected = {
        "gpt-6-astra": (0.035, 0.02805),
        "gpt-6-sol": (0.007, 0.00561),
        "gpt-6-luna": (0.00035, 0.0002805),
    }

    for model, (uncached_cost, cached_cost) in expected.items():
        assert abs(estimate_cost(model, 1000, 500) - uncached_cost) < 1e-10
        assert (
            abs(
                estimate_cost(
                    model,
                    1000,
                    500,
                    cache_read_input_tokens=800,
                    cache_creation_input_tokens=100,
                )
                - cached_cost
            )
            < 1e-10
        )


def test_openai_missing_text_model_pricing():
    """Published OpenAI text model IDs and aliases use explicit rates."""
    expected = {
        "gpt-6.1-sol": 0.007,
        "gpt-5.6-cyber": 0.05,
        "gpt-5.5-pro": 0.12,
        "gpt-5.4-pro": 0.12,
        "gpt-5.4-nano": 0.000825,
        "gpt-5.2": 0.00875,
        "gpt-5.2-pro": 0.105,
        "gpt-5.1": 0.00625,
        "gpt-5.1-codex": 0.00625,
        "gpt-5.1-codex-max": 0.00625,
        "gpt-5.1-codex-mini": 0.00125,
        "gpt-5-nano": 0.00025,
        "gpt-5-pro": 0.075,
        "gpt-5.3-codex": 0.00875,
        "chat-latest": 0.02,
        "gpt-rosalind-research": 0.0175,
        "gpt-daybreak-blue-latest": 0.014,
        "gpt-daybreak-red-latest": 0.05,
    }
    for model, cost in expected.items():
        assert abs(estimate_cost(model, 1000, 500) - cost) < 1e-10

    # The official GPT-6.1 Sol cache rates include cache writes at 1.25x input.
    cached_sol = estimate_cost(
        "gpt-6.1-sol",
        1000,
        500,
        cache_read_input_tokens=800,
        cache_creation_input_tokens=100,
    )
    assert abs(cached_sol - 0.00553) < 1e-10


CONTEXT_TIER_CASES = [
    # Each rate tuple is input, cached input, cache write, and output USD/MTok.
    ("gpt-6-astra", 272000, False, (10, 1, 12.5, 50), (20, 2, 25, 75)),
    ("gpt-6.1-sol", 272000, False, (2, 0.1, 2.5, 10), (4, 0.2, 5, 15)),
    ("gpt-6-sol", 272000, False, (2, 0.2, 2.5, 10), (4, 0.4, 5, 15)),
    ("gpt-6-luna", 272000, False, (0.1, 0.01, 0.125, 0.5), (0.2, 0.02, 0.25, 0.75)),
    ("gpt-5.6-sol", 272000, False, (4, 0.4, 5, 20), (8, 0.8, 10, 30)),
    ("gpt-5.6", 272000, False, (4, 0.4, 5, 20), (8, 0.8, 10, 30)),
    ("gpt-5.6-terra", 272000, False, (2, 0.2, 2.5, 12), (4, 0.4, 5, 18)),
    ("gpt-5.6-luna", 272000, False, (0.2, 0.02, 0.25, 1.2), (0.4, 0.04, 0.5, 1.8)),
    ("gpt-5.6-cyber", 272000, False, (12.5, 1.25, 15.625, 75), (25, 2.5, 31.25, 112.5)),
    ("gpt-daybreak-blue-latest", 272000, False, (4, 0.4, 5, 20), (8, 0.8, 10, 30)),
    (
        "gpt-daybreak-red-latest",
        272000,
        False,
        (12.5, 1.25, 15.625, 75),
        (25, 2.5, 31.25, 112.5),
    ),
    ("gpt-5.5", 272000, False, (5, 0.5, 5, 30), (10, 1, 10, 45)),
    ("gpt-5.4", 272000, False, (2.5, 0.25, 2.5, 15), (5, 0.5, 5, 22.5)),
    ("gpt-5.4-pro", 272000, False, (30, 30, 30, 180), (60, 60, 60, 270)),
    ("gemini-3.1-pro", 200000, False, (2, 0.2, 2, 12), (4, 0.4, 4, 18)),
    ("gemini-3.1-pro-preview", 200000, False, (2, 0.2, 2, 12), (4, 0.4, 4, 18)),
    ("gemini-2.5-pro", 200000, False, (1.25, 0.125, 1.25, 10), (2.5, 0.25, 2.5, 15)),
    ("grok-4.7", 200000, True, (2, 0.5, 2, 6), (4, 1, 4, 12)),
    ("grok-4.6", 200000, True, (2, 0.5, 2, 6), (4, 1, 4, 12)),
    ("grok-4.5", 200000, True, (2, 0.3, 2, 6), (4, 0.6, 4, 12)),
    ("grok-4.3", 200000, True, (1.25, 0.2, 1.25, 2.5), (2.5, 0.4, 2.5, 5)),
    ("grok-4.20", 200000, True, (1.25, 0.2, 1.25, 2.5), (2.5, 0.4, 2.5, 5)),
    ("grok-build", 200000, True, (1, 0.2, 1, 2), (2, 0.4, 2, 4)),
]


@pytest.mark.parametrize(
    ("model", "threshold", "inclusive", "short_rates", "long_rates"),
    CONTEXT_TIER_CASES,
)
def test_context_tiers_switch_full_request_at_documented_boundary(
    model: str,
    threshold: int,
    inclusive: bool,
    short_rates: tuple[float, float, float, float],
    long_rates: tuple[float, float, float, float],
):
    """Each provider's threshold applies to every token class in the request."""
    for prompt_tokens in (threshold - 1, threshold, threshold + 1):
        is_long_context = (
            prompt_tokens >= threshold if inclusive else prompt_tokens > threshold
        )
        rates = long_rates if is_long_context else short_rates
        cache_read_tokens = prompt_tokens // 4
        cache_write_tokens = prompt_tokens // 5
        uncached_tokens = prompt_tokens - cache_read_tokens - cache_write_tokens
        output_tokens = 317
        expected = (
            uncached_tokens * rates[0]
            + cache_read_tokens * rates[1]
            + cache_write_tokens * rates[2]
            + output_tokens * rates[3]
        ) / 1_000_000

        assert (
            abs(
                estimate_cost(
                    model,
                    prompt_tokens,
                    output_tokens,
                    cache_read_input_tokens=cache_read_tokens,
                    cache_creation_input_tokens=cache_write_tokens,
                )
                - expected
            )
            < 1e-12
        )


@pytest.mark.parametrize(
    ("versioned_model", "canonical_model"),
    [
        ("gpt-6.1-sol-2026-09-29", "gpt-6.1-sol"),
        ("gpt-5.6-terra-2026-02-16", "gpt-5.6-terra"),
        ("gpt-5.6-2026-02-16", "gpt-5.6"),
        ("gpt-daybreak-blue-latest-2026-10-04", "gpt-daybreak-blue-latest"),
        ("gpt-daybreak-red-latest-2026-10-04", "gpt-daybreak-red-latest"),
        ("gemini-3.1-pro-preview-001", "gemini-3.1-pro-preview"),
        ("gemini-2.5-pro-001", "gemini-2.5-pro"),
        ("grok-4.7-latest", "grok-4.7"),
        ("grok-4.6-latest", "grok-4.6"),
        ("grok-build-0.1", "grok-build"),
    ],
)
def test_versioned_model_ids_keep_context_tier_pricing(
    versioned_model: str,
    canonical_model: str,
):
    """Version suffixes preserve the canonical model's tiered rate selection."""
    kwargs = {
        "input_tokens": 300_001,
        "output_tokens": 100,
        "cache_read_input_tokens": 80_000,
        "cache_creation_input_tokens": 20_000,
    }
    assert estimate_cost(versioned_model, **kwargs) == estimate_cost(
        canonical_model, **kwargs
    )


def test_google_model():
    """Google models have their own pricing."""
    c = estimate_cost("gemini-2.5-pro", 1000, 500)
    assert c > 0

    c_flash37 = estimate_cost("gemini-3.7-flash", 1000, 500)
    # 1000 * 0.75/1M + 500 * 3.75/1M = 0.00075 + 0.001875 = 0.002625
    assert abs(c_flash37 - 0.002625) < 0.00001


def test_xai_grok_model():
    """xAI (Grok) models have their own pricing."""
    c_grok46 = estimate_cost("grok-4.6", 1000, 500)
    # 1000 * 2.0/1M + 500 * 6.0/1M = 0.002 + 0.003 = 0.005
    assert abs(c_grok46 - 0.005) < 0.0001

    c_grok3 = estimate_cost("grok-3", 1000, 500)
    # 1000 * 3.0/1M + 500 * 15.0/1M = 0.003 + 0.0075 = 0.0105
    assert abs(c_grok3 - 0.0105) < 0.0001

    c_cached = estimate_cost("grok-4.6", 1000, 500, cache_read_input_tokens=800)
    # (1000 - 800) * 2.0/1M + 800 * 0.50/1M + 500 * 6.0/1M
    # = 200 * 0.000002 + 800 * 0.0000005 + 500 * 0.000006
    # = 0.0004 + 0.0004 + 0.003 = 0.0038
    assert abs(c_cached - 0.0038) < 0.0001


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


def test_cache_aware_pricing():
    """Cache tokens are priced at discounted rates."""
    # Without cache: 1000 input * $3/1M + 500 output * $15/1M = $0.0105
    cost_no_cache = estimate_cost("claude-sonnet-4", 1000, 500)

    # With 800 tokens from cache read (90% off): much cheaper
    cost_with_cache = estimate_cost(
        "claude-sonnet-4",
        1000,
        500,
        cache_read_input_tokens=800,
    )
    assert cost_with_cache < cost_no_cache

    # Verify the math:
    # uncached: (1000-800) * $3/1M = $0.0006
    # cache_read: 800 * $0.30/1M = $0.00024
    # output: 500 * $15/1M = $0.0075
    expected = 200 * 0.000003 + 800 * 0.0000003 + 500 * 0.000015
    assert abs(cost_with_cache - expected) < 0.000001


def test_cache_write_premium():
    """Cache write tokens cost more than regular input."""
    cost_no_cache = estimate_cost("claude-sonnet-4", 1000, 0)
    cost_with_write = estimate_cost(
        "claude-sonnet-4",
        1000,
        0,
        cache_creation_input_tokens=1000,
    )
    # 1.25x premium on writes: more expensive than regular input
    assert cost_with_write > cost_no_cache


def test_cache_fallback_to_input_rate():
    """Models without cache rates in pricing.yaml use input rate."""
    # gpt-4-turbo has no cache_read_per_token in pricing.yaml
    cost_no_cache = estimate_cost("gpt-4-turbo", 1000, 500)
    cost_with_cache = estimate_cost(
        "gpt-4-turbo",
        1000,
        500,
        cache_read_input_tokens=800,
    )
    # Falls back to input rate, so cost is the same
    assert abs(cost_with_cache - cost_no_cache) < 0.000001


def test_cumulative_cost_with_cache():
    """Cumulative cost accounts for cache tokens."""
    usage = {
        "input_tokens": 10000,
        "output_tokens": 5000,
        "cache_read_input_tokens": 8000,
        "cache_creation_input_tokens": 0,
        "models_used": ["claude-sonnet-4-6"],
    }
    cost_cached = estimate_cumulative_cost(usage)

    usage_no_cache = {
        "input_tokens": 10000,
        "output_tokens": 5000,
        "models_used": ["claude-sonnet-4-6"],
    }
    cost_no_cache = estimate_cumulative_cost(usage_no_cache)
    assert cost_cached < cost_no_cache


def test_pricing_yaml_exists():
    """Bundled pricing.yaml exists and is valid."""
    pricing_file = Path(__file__).parent.parent / "providers" / "cost" / "pricing.yaml"
    assert pricing_file.exists()

    import yaml

    data = yaml.safe_load(pricing_file.read_text(encoding="utf-8"))
    assert "fallback" in data
    assert "models" in data
    assert len(data["models"]) > 0
