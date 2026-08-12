"""Tests for config-driven per-agent max_iterations (#384).

Verifies the resolution order (config per-agent > config default >
builtin > constructor), dispatcher wiring, global cap propagation,
and that 0 (unlimited) is a valid value.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock


class TestConfigResolution:
    """OrchestratorConfig.get_agent_max_iterations resolution order."""

    def _make_config(self, cfg_dict: dict):
        """Build an OrchestratorConfig from an in-memory dict."""
        from unittest.mock import patch

        from orchestrator.config import OrchestratorConfig

        with patch("orchestrator.config.CONFIG_PATH") as mock_path:
            mock_path.exists.return_value = True
            mock_path.read_text.return_value = json.dumps(cfg_dict)
            return OrchestratorConfig()

    def test_builtin_when_no_config(self):
        """No agent_iterations config falls back to builtin defaults."""
        config = self._make_config({})
        assert config.get_agent_max_iterations("review") == 50
        assert config.get_agent_max_iterations("platform") == 10
        assert config.get_agent_max_iterations("evaluating_convergence") == 0
        assert config.get_agent_max_iterations("analyze") == 0
        assert config.get_agent_max_iterations("provisioning") == 30

    def test_config_per_agent_overrides_builtin(self):
        """Explicit agent_iterations.<type> beats the builtin default."""
        config = self._make_config(
            {
                "agent_iterations": {"review": 75},
            }
        )
        assert config.get_agent_max_iterations("review") == 75
        assert config.get_agent_max_iterations("platform") == 10

    def test_config_default_fills_gaps(self):
        """agent_iterations.default applies to agents without a specific entry."""
        config = self._make_config(
            {
                "agent_iterations": {"default": 30},
            }
        )
        assert config.get_agent_max_iterations("triage") == 30
        assert config.get_agent_max_iterations("benchmark") == 30
        assert config.get_agent_max_iterations("review") == 30

    def test_per_agent_beats_default(self):
        """agent_iterations.<type> takes precedence over default."""
        config = self._make_config(
            {
                "agent_iterations": {
                    "default": 30,
                    "review": 100,
                },
            }
        )
        assert config.get_agent_max_iterations("review") == 100
        assert config.get_agent_max_iterations("triage") == 30

    def test_zero_is_valid_not_skipped(self):
        """0 means unlimited — must not be treated as falsy/missing."""
        config = self._make_config(
            {
                "agent_iterations": {"review": 0},
            }
        )
        assert config.get_agent_max_iterations("review") == 0

    def test_unknown_agent_returns_none(self):
        """Agents not in builtins or config return None (use constructor default)."""
        config = self._make_config({})
        assert config.get_agent_max_iterations("nonexistent") is None

    def test_global_max_iterations_default(self):
        """Default global_max_iterations is 100."""
        config = self._make_config({})
        assert config.global_max_iterations == 100

    def test_global_max_iterations_from_config(self):
        """Config can override global_max_iterations."""
        config = self._make_config({"global_max_iterations": 50})
        assert config.global_max_iterations == 50

    def test_global_max_iterations_from_env(self, monkeypatch):
        """Env var GLOBAL_MAX_ITERATIONS overrides config."""
        monkeypatch.setenv("GLOBAL_MAX_ITERATIONS", "200")
        config = self._make_config({"global_max_iterations": 50})
        assert config.global_max_iterations == 200


class TestDispatcherIterationsFactory:
    """Dispatcher.create_agent applies iterations_factory."""

    def test_factory_overrides_constructor_default(self):
        """iterations_factory value overrides the agent's constructor default."""
        from orchestrator.dispatcher import Dispatcher

        dispatcher = Dispatcher(
            state_store_url="http://localhost:8090",
            llm_provider=MagicMock(),
            skill_provider=MagicMock(),
            iterations_factory=lambda agent_type: 75
            if agent_type == "review"
            else None,
        )

        agent = dispatcher.create_agent("awaiting_review")
        assert agent is not None
        assert agent.max_iterations == 75

    def test_factory_none_keeps_constructor_default(self):
        """When factory returns None, the agent keeps its constructor default."""
        from orchestrator.dispatcher import Dispatcher

        dispatcher = Dispatcher(
            state_store_url="http://localhost:8090",
            llm_provider=MagicMock(),
            skill_provider=MagicMock(),
            iterations_factory=lambda agent_type: None,
        )

        agent = dispatcher.create_agent("awaiting_review")
        assert agent is not None
        assert agent.max_iterations == 50

    def test_no_factory_keeps_constructor_default(self):
        """No iterations_factory at all preserves agent defaults."""
        from orchestrator.dispatcher import Dispatcher

        dispatcher = Dispatcher(
            state_store_url="http://localhost:8090",
            llm_provider=MagicMock(),
            skill_provider=MagicMock(),
        )

        agent = dispatcher.create_agent("triage_pending")
        assert agent is not None
        assert agent.max_iterations == 20

    def test_factory_zero_sets_unlimited(self):
        """Factory returning 0 sets unlimited iterations."""
        from orchestrator.dispatcher import Dispatcher

        dispatcher = Dispatcher(
            state_store_url="http://localhost:8090",
            llm_provider=MagicMock(),
            skill_provider=MagicMock(),
            iterations_factory=lambda agent_type: 0,
        )

        agent = dispatcher.create_agent("awaiting_review")
        assert agent is not None
        assert agent.max_iterations == 0

    def test_stub_agent_not_affected(self):
        """StubAgent has no max_iterations — factory should not error."""
        from orchestrator.dispatcher import Dispatcher

        dispatcher = Dispatcher(
            state_store_url="http://localhost:8090",
            llm_provider=MagicMock(),
            skill_provider=MagicMock(),
            iterations_factory=lambda agent_type: 99,
        )

        agent = dispatcher.create_agent("planning_investigation")
        assert agent is not None
        assert not hasattr(agent, "max_iterations")


class TestGlobalMaxIterations:
    """DEFAULT_GLOBAL_MAX_ITERATIONS on AgentBase."""

    def test_base_default_is_100(self):
        """Class-level default matches previous hardcoded value."""
        from agents.base import AgentBase

        assert AgentBase.DEFAULT_GLOBAL_MAX_ITERATIONS == 100

    def test_instance_override(self):
        """Orchestrator can override the instance attribute."""
        from tests.test_agent_iterations import _CountingLLM, _StubAgent

        agent = _StubAgent(
            agent_name="test",
            llm_provider=_CountingLLM(),
            state_store_url="http://localhost:8090",
        )
        assert agent.DEFAULT_GLOBAL_MAX_ITERATIONS == 100
        agent.DEFAULT_GLOBAL_MAX_ITERATIONS = 50
        assert agent.DEFAULT_GLOBAL_MAX_ITERATIONS == 50
