"""Tests for LLM model configuration layering."""

from __future__ import annotations

import logging
from unittest.mock import patch

from orchestrator.config import OrchestratorConfig


class TestModelConfigLayering:
    """Verify model selection uses simple global + per-agent."""

    def _make_config(self, **overrides):
        """Create config with controlled settings."""
        cfg = {
            "llm": overrides.get("llm", {}),
            "agent_models": overrides.get("agent_models", {}),
        }
        with patch(
            "orchestrator.config._load_config_file",
            return_value=cfg,
        ):
            return OrchestratorConfig()

    def test_global_model_applies_to_all_agents(self):
        """llm.model is the default for every agent."""
        config = self._make_config(
            llm={"provider": "claude", "model": "claude-sonnet-4-6"}
        )
        for agent in [
            "triage",
            "benchmark",
            "review",
            "resource",
            "introspection",
            "retrospective",
            "evaluating_convergence",
        ]:
            result = config.get_agent_llm_config(agent)
            assert result["model"] == "claude-sonnet-4-6", (
                f"{agent} got {result['model']}"
            )
            assert result["provider"] == "claude"

    def test_per_agent_override(self):
        """agent_models.<type> overrides global model."""
        config = self._make_config(
            llm={"provider": "claude", "model": "claude-sonnet-4-6"},
            agent_models={
                "introspection": {"model": "claude-haiku-4-5"},
            },
        )
        assert (
            config.get_agent_llm_config("introspection")["model"] == "claude-haiku-4-5"
        )
        # Other agents still get global
        assert config.get_agent_llm_config("triage")["model"] == "claude-sonnet-4-6"

    def test_capabilities_always_applied(self):
        """review gets max_tokens regardless of model config."""
        config = self._make_config(llm={"provider": "gemini", "model": "gemini-pro"})
        result = config.get_agent_llm_config("review")
        assert result["max_tokens"] == "32000"
        assert result["model"] == "gemini-pro"

    def test_capabilities_dont_override_model(self):
        """Capability defaults never set model/provider."""
        config = self._make_config(
            llm={"provider": "claude", "model": "claude-sonnet-4-6"}
        )
        result = config.get_agent_llm_config("review")
        assert result["model"] == "claude-sonnet-4-6"

    def test_no_builtin_model_overrides(self):
        """No hardcoded model overrides for any agent."""
        config = self._make_config(llm={"provider": "gemini", "model": "gemini-pro"})
        # Previously triage got claude-sonnet-4-6 from builtins
        result = config.get_agent_llm_config("triage")
        assert result["model"] == "gemini-pro"
        assert result["provider"] == "gemini"

    def test_env_var_model(self):
        """LLM_MODEL env var is the global default."""
        with patch.dict("os.environ", {"LLM_MODEL": "gemini-pro"}):
            config = self._make_config(llm={"provider": "gemini"})
        result = config.get_agent_llm_config("triage")
        assert result["model"] == "gemini-pro"

    def test_no_model_configured_warns(self, caplog):
        """Non-mock provider with no model logs a warning."""
        with caplog.at_level(logging.WARNING):
            config = self._make_config(llm={"provider": "claude"})
        assert config.llm_model == ""
        assert "No LLM model configured" in caplog.text

    def test_mock_provider_no_warning(self, caplog):
        """Mock provider doesn't need a model."""
        with caplog.at_level(logging.WARNING):
            config = self._make_config()
        assert config.llm_provider == "mock"
        assert config.llm_model == ""
        assert "No LLM model configured" not in caplog.text

    def test_agent_models_default_ignored_with_warning(self, caplog):
        """agent_models.default is ignored and warns."""
        with caplog.at_level(logging.WARNING):
            config = self._make_config(
                llm={
                    "provider": "claude",
                    "model": "claude-sonnet-4-6",
                },
                agent_models={
                    "default": {"model": "claude-haiku-4-5"},
                },
            )
        # default is ignored — global model wins
        result = config.get_agent_llm_config("triage")
        assert result["model"] == "claude-sonnet-4-6"
        assert "agent_models.default is deprecated" in caplog.text
