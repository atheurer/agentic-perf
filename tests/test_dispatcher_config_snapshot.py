"""Tests for per-dispatch config snapshot (#601).

Verifies that:
- _fresh_config re-reads config.json on each call
- Malformed JSON falls back to last-good config
- create_agent respects override factories
- gathering_context uses the per-agent LLM (not the startup fallback)
- Backward compatibility: bare Dispatcher construction still works
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from orchestrator.config import OrchestratorConfig
from orchestrator.dispatcher import Dispatcher


def _make_config(cfg_dict: dict) -> OrchestratorConfig:
    """Build an OrchestratorConfig from an in-memory dict."""
    return OrchestratorConfig(raw_config=cfg_dict)


class TestFreshConfig:
    """_fresh_config reads config.json and falls back on errors."""

    def _call_fresh(self, fallback, **kwargs):
        """Import and call _fresh_config, resetting module state first."""
        import orchestrator.main as mod

        mod._last_good_config = kwargs.get("last_good")
        mod._last_good_digest = kwargs.get("last_digest", "")
        return mod._fresh_config(fallback)

    def test_returns_fresh_config_from_valid_json(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"llm": {"model": "new-model"}}))

        fallback = _make_config({})
        with patch("paths.CONFIG_PATH", cfg_file):
            result = self._call_fresh(fallback)

        assert result.llm_model == "new-model"

    def test_malformed_json_uses_last_good(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text("{invalid json")

        last_good = _make_config({"llm": {"model": "last-good-model"}})
        fallback = _make_config({})
        with patch("paths.CONFIG_PATH", cfg_file):
            result = self._call_fresh(fallback, last_good=last_good)

        assert result is last_good
        assert result.llm_model == "last-good-model"

    def test_malformed_json_no_last_good_uses_fallback(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text("{bad")

        fallback = _make_config({"llm": {"model": "fallback-model"}})
        with patch("paths.CONFIG_PATH", cfg_file):
            result = self._call_fresh(fallback, last_good=None)

        assert result is fallback

    def test_missing_file_uses_last_good(self, tmp_path):
        cfg_file = tmp_path / "nonexistent.json"

        last_good = _make_config({"llm": {"model": "cached-model"}})
        fallback = _make_config({})
        with patch("paths.CONFIG_PATH", cfg_file):
            result = self._call_fresh(fallback, last_good=last_good)

        assert result is last_good

    def test_missing_file_no_last_good_constructs_defaults(self, tmp_path):
        cfg_file = tmp_path / "nonexistent.json"

        fallback = _make_config({})
        with patch("paths.CONFIG_PATH", cfg_file):
            result = self._call_fresh(fallback, last_good=None)

        assert result is not None
        assert result.llm_provider == "mock"

    def test_successive_reads_see_updates(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        fallback = _make_config({})

        cfg_file.write_text(json.dumps({"llm": {"model": "model-a"}}))
        with patch("paths.CONFIG_PATH", cfg_file):
            r1 = self._call_fresh(fallback)
            assert r1.llm_model == "model-a"

        cfg_file.write_text(json.dumps({"llm": {"model": "model-b"}}))
        with patch("paths.CONFIG_PATH", cfg_file):
            import orchestrator.main as mod

            mod._last_good_config = r1
            r2 = mod._fresh_config(fallback)
            assert r2.llm_model == "model-b"

    def test_config_change_logged(self, tmp_path, caplog):
        import orchestrator.main as mod

        cfg_file = tmp_path / "config.json"
        fallback = _make_config({})

        cfg_file.write_text(json.dumps({"llm": {"model": "m1"}}))
        with patch("paths.CONFIG_PATH", cfg_file):
            mod._last_good_config = None
            mod._last_good_digest = ""
            r1 = mod._fresh_config(fallback)

        cfg_file.write_text(json.dumps({"llm": {"model": "m2"}}))
        with patch("paths.CONFIG_PATH", cfg_file):
            mod._last_good_config = r1
            with caplog.at_level("INFO", logger="orchestrator.main"):
                mod._fresh_config(fallback)

        assert any("Config snapshot changed" in r.message for r in caplog.records)


class TestRawConfigParam:
    """OrchestratorConfig accepts raw_config to avoid double file reads."""

    def test_raw_config_used_when_provided(self):
        config = OrchestratorConfig(raw_config={"llm": {"model": "injected-model"}})
        assert config.llm_model == "injected-model"

    def test_raw_config_none_reads_file(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"llm": {"model": "from-file"}}))
        with patch("orchestrator.config.CONFIG_PATH", cfg_file):
            config = OrchestratorConfig()
        assert config.llm_model == "from-file"


class TestCreateAgentOverrides:
    """Dispatcher.create_agent respects llm_factory/iterations_factory overrides."""

    def test_llm_factory_override(self):
        startup_llm = MagicMock()
        override_llm = MagicMock()

        dispatcher = Dispatcher(
            state_store_url="http://localhost:8090",
            llm_provider=startup_llm,
            skill_provider=MagicMock(),
        )

        agent = dispatcher.create_agent(
            "triage_pending",
            llm_factory=lambda _: override_llm,
        )
        assert agent is not None
        assert agent.llm is override_llm

    def test_iterations_factory_override(self):
        dispatcher = Dispatcher(
            state_store_url="http://localhost:8090",
            llm_provider=MagicMock(),
            skill_provider=MagicMock(),
            iterations_factory=lambda _: 20,
        )

        agent = dispatcher.create_agent(
            "awaiting_review",
            iterations_factory=lambda _: 999,
        )
        assert agent is not None
        assert agent.max_iterations == 999

    def test_no_override_uses_dispatcher_factory(self):
        custom_llm = MagicMock()
        dispatcher = Dispatcher(
            state_store_url="http://localhost:8090",
            llm_provider=MagicMock(),
            skill_provider=MagicMock(),
            llm_factory=lambda _: custom_llm,
        )

        agent = dispatcher.create_agent("triage_pending")
        assert agent is not None
        assert agent.llm is custom_llm

    def test_backward_compat_bare_dispatcher(self):
        fallback_llm = MagicMock()
        dispatcher = Dispatcher(
            state_store_url="http://localhost:8090",
            llm_provider=fallback_llm,
            skill_provider=MagicMock(),
        )

        agent = dispatcher.create_agent("triage_pending")
        assert agent is not None
        assert agent.llm is fallback_llm


class TestGatheringContextLLM:
    """gathering_context now uses the per-agent LLM, not the startup fallback."""

    def test_uses_factory_llm(self):
        startup_llm = MagicMock()
        factory_llm = MagicMock()

        dispatcher = Dispatcher(
            state_store_url="http://localhost:8090",
            llm_provider=startup_llm,
            skill_provider=MagicMock(),
            llm_factory=lambda _: factory_llm,
        )

        agent = dispatcher.create_agent("gathering_context")
        assert agent is not None
        assert agent.llm is factory_llm

    def test_uses_override_factory(self):
        startup_llm = MagicMock()
        override_llm = MagicMock()

        dispatcher = Dispatcher(
            state_store_url="http://localhost:8090",
            llm_provider=startup_llm,
            skill_provider=MagicMock(),
        )

        agent = dispatcher.create_agent(
            "gathering_context",
            llm_factory=lambda _: override_llm,
        )
        assert agent is not None
        assert agent.llm is override_llm

    def test_fallback_to_dispatcher_llm_without_factory(self):
        startup_llm = MagicMock()
        dispatcher = Dispatcher(
            state_store_url="http://localhost:8090",
            llm_provider=startup_llm,
            skill_provider=MagicMock(),
        )

        agent = dispatcher.create_agent("gathering_context")
        assert agent is not None
        assert agent.llm is startup_llm
