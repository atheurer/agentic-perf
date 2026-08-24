"""Tests for _validate_models startup check in orchestrator/main.py."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from orchestrator.main import _validate_models


def _make_config(
    provider="anthropic",
    model="claude-haiku-4-5",
    region="us-east5",
    agent_models=None,
):
    cfg = MagicMock()
    cfg.llm_provider = provider
    cfg.llm_model = model
    cfg.llm_region = region
    cfg.llm_timeout = 30.0
    cfg.llm_reasoning_effort = None
    cfg.llm_max_tokens = 4096
    cfg.raw = {"agent_models": agent_models or {}}

    def get_agent_llm_config(agent_type):
        base = {"provider": provider, "model": model}
        if agent_models and agent_type in agent_models:
            base.update(agent_models[agent_type])
        return base

    cfg.get_agent_llm_config = get_agent_llm_config
    return cfg


class TestValidateModels:
    async def test_all_models_pass(self, caplog):
        config = _make_config()
        mock_provider = AsyncMock()
        mock_provider.complete = AsyncMock(return_value=MagicMock())

        with patch("orchestrator.main._make_llm_provider", return_value=mock_provider):
            with caplog.at_level("INFO", logger="orchestrator.main"):
                await _validate_models(config)

        assert mock_provider.complete.call_count == 1
        assert any("OK" in r.message for r in caplog.records)
        assert not any("FAILED" in r.message for r in caplog.records)

    async def test_failed_model_logs_error(self, caplog):
        config = _make_config()
        mock_provider = AsyncMock()
        mock_provider.complete = AsyncMock(side_effect=Exception("404 model not found"))

        with patch("orchestrator.main._make_llm_provider", return_value=mock_provider):
            with caplog.at_level("ERROR", logger="orchestrator.main"):
                # Should not raise
                await _validate_models(config)

        assert any("FAILED" in r.message for r in caplog.records)

    async def test_deduplication_same_model_tested_once(self, caplog):
        config = _make_config(
            agent_models={
                "benchmark": {"provider": "anthropic", "model": "claude-haiku-4-5"},
                "review": {"provider": "anthropic", "model": "claude-haiku-4-5"},
            }
        )
        mock_provider = AsyncMock()
        mock_provider.complete = AsyncMock(return_value=MagicMock())

        with patch("orchestrator.main._make_llm_provider", return_value=mock_provider):
            await _validate_models(config)

        # All three (default + benchmark + review) share same (provider, model, region)
        # so complete should be called exactly once.
        assert mock_provider.complete.call_count == 1

    async def test_distinct_models_each_tested(self, caplog):
        config = _make_config(
            model="claude-haiku-4-5",
            agent_models={
                "benchmark": {"provider": "anthropic", "model": "claude-sonnet-5"},
            },
        )
        mock_provider = AsyncMock()
        mock_provider.complete = AsyncMock(return_value=MagicMock())

        with patch("orchestrator.main._make_llm_provider", return_value=mock_provider):
            await _validate_models(config)

        # default (haiku) + benchmark (sonnet) = 2 distinct models
        assert mock_provider.complete.call_count == 2

    async def test_empty_agent_models_tests_default_only(self, caplog):
        config = _make_config(agent_models={})
        mock_provider = AsyncMock()
        mock_provider.complete = AsyncMock(return_value=MagicMock())

        with patch("orchestrator.main._make_llm_provider", return_value=mock_provider):
            await _validate_models(config)

        assert mock_provider.complete.call_count == 1

    async def test_timeout_logs_error_and_continues(self, caplog):
        config = _make_config()
        mock_provider = AsyncMock()

        async def slow(*args, **kwargs):
            await asyncio.sleep(100)

        mock_provider.complete = slow

        with patch("orchestrator.main._make_llm_provider", return_value=mock_provider):
            with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
                with caplog.at_level("ERROR", logger="orchestrator.main"):
                    await _validate_models(config)

        assert any("TIMED OUT" in r.message for r in caplog.records)
