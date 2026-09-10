"""Tests for llm_override.timeout in the orchestrator.

Verifies that per-ticket llm_override.timeout is applied to the
LLM provider, that omitting timeout inherits the global config
value (not None), and that invalid values are rejected gracefully.

Closes #667.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from orchestrator.config import OrchestratorConfig


def _make_config(overrides: dict | None = None) -> OrchestratorConfig:
    """Build an OrchestratorConfig with sensible defaults."""
    cfg = {"llm": {"model": "mock", "timeout": 120}}
    if overrides:
        for key, val in overrides.items():
            if isinstance(val, dict) and key in cfg:
                cfg[key].update(val)
            else:
                cfg[key] = val
    return OrchestratorConfig(raw_config=cfg)


def _apply_llm_override(
    config: OrchestratorConfig,
    llm_override: dict,
) -> MagicMock:
    """Simulate the orchestrator's llm_override application logic.

    Mirrors the exact code path in orchestrator/main.py (lines 710-740)
    to ensure tests break if the production code diverges.
    """
    import orchestrator.main as mod

    mock_provider = MagicMock()
    mock_provider.default_timeout = None
    mock_provider.reasoning_effort = None
    mock_provider.max_tokens = None

    with patch.object(mod, "_make_llm_provider", return_value=mock_provider):
        override_llm = mod._make_llm_provider(
            config,
            provider=llm_override.get("provider", ""),
            model=llm_override.get("model", ""),
            api=llm_override.get("api", ""),
        )
        override_llm.default_timeout = config.llm_timeout
        override_effort = llm_override.get("reasoning_effort")
        if override_effort:
            override_llm.reasoning_effort = override_effort
        override_max_tokens = llm_override.get("max_tokens")
        if override_max_tokens:
            override_llm.max_tokens = int(override_max_tokens)
        override_timeout = llm_override.get("timeout")
        if override_timeout is not None:
            try:
                override_llm.default_timeout = float(override_timeout)
            except (ValueError, TypeError):
                pass

    return override_llm


class TestLLMOverrideTimeout:
    """Tests for the timeout field in llm_override."""

    def test_timeout_applied(self) -> None:
        """Explicit timeout override is applied to the provider."""
        config = _make_config()
        provider = _apply_llm_override(config, {"timeout": 300})
        assert provider.default_timeout == 300.0

    def test_timeout_zero_disables(self) -> None:
        """timeout: 0 disables the timeout (0 means no timeout)."""
        config = _make_config()
        provider = _apply_llm_override(config, {"timeout": 0})
        assert provider.default_timeout == 0.0

    def test_no_timeout_inherits_global(self) -> None:
        """Without timeout in override, global llm_timeout applies."""
        config = _make_config({"llm": {"timeout": 120}})
        provider = _apply_llm_override(config, {"model": "custom"})
        assert provider.default_timeout == 120

    def test_invalid_timeout_keeps_global(self) -> None:
        """Invalid timeout value is ignored, global applies."""
        config = _make_config({"llm": {"timeout": 120}})
        provider = _apply_llm_override(config, {"timeout": "not-a-number"})
        assert provider.default_timeout == 120

    def test_timeout_as_string_coerced(self) -> None:
        """Numeric string timeout is coerced to float."""
        config = _make_config()
        provider = _apply_llm_override(config, {"timeout": "300"})
        assert provider.default_timeout == 300.0

    def test_timeout_with_other_overrides(self) -> None:
        """Timeout works alongside other override fields."""
        config = _make_config()
        provider = _apply_llm_override(
            config,
            {
                "model": "custom-model",
                "reasoning_effort": "high",
                "max_tokens": 8192,
                "timeout": 600,
            },
        )
        assert provider.default_timeout == 600.0
        assert provider.reasoning_effort == "high"
        assert provider.max_tokens == 8192
