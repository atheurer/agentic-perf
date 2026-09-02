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
    llm_reasoning_effort=None,
):
    cfg = MagicMock()
    cfg.llm_provider = provider
    cfg.llm_model = model
    cfg.llm_region = region
    cfg.llm_timeout = 30.0
    cfg.llm_reasoning_effort = llm_reasoning_effort
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
        assert mock_provider.complete.call_args.kwargs["max_tokens"] == 1024
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

    async def test_effort_set_on_probe_provider(self):
        config = _make_config(llm_reasoning_effort="low")
        mock_provider = AsyncMock()
        mock_provider.complete = AsyncMock(return_value=MagicMock())

        with patch("orchestrator.main._make_llm_provider", return_value=mock_provider):
            await _validate_models(config)

        assert mock_provider.reasoning_effort == "low"

    async def test_no_effort_leaves_provider_unchanged(self):
        config = _make_config(llm_reasoning_effort=None)
        mock_provider = AsyncMock()
        mock_provider.complete = AsyncMock(return_value=MagicMock())
        mock_provider.reasoning_effort = None

        with patch("orchestrator.main._make_llm_provider", return_value=mock_provider):
            await _validate_models(config)

        assert mock_provider.reasoning_effort is None

    async def test_different_efforts_probe_separately(self):
        config = _make_config(
            agent_models={
                "benchmark": {
                    "provider": "anthropic",
                    "model": "claude-haiku-4-5",
                    "reasoning_effort": "high",
                },
            },
            llm_reasoning_effort=None,
        )
        mock_provider = AsyncMock()
        mock_provider.complete = AsyncMock(return_value=MagicMock())

        with patch("orchestrator.main._make_llm_provider", return_value=mock_provider):
            await _validate_models(config)

        # default (haiku, no effort) + benchmark (haiku, effort=high) = 2 probes
        assert mock_provider.complete.call_count == 2

    async def test_effort_failure_logs_targeted_error(self, caplog):
        config = _make_config(
            agent_models={
                "introspection": {
                    "provider": "anthropic",
                    "model": "claude-haiku-4-5",
                    "reasoning_effort": "low",
                },
            },
        )
        mock_provider = AsyncMock()

        def complete_side_effect(*args, **kwargs):
            raise Exception("output_config.effort: Extra inputs are not permitted")

        mock_provider.complete = AsyncMock(side_effect=complete_side_effect)
        mock_provider.reasoning_effort = None

        with patch("orchestrator.main._make_llm_provider", return_value=mock_provider):
            with caplog.at_level("ERROR", logger="orchestrator.main"):
                await _validate_models(config)

        error_records = [r for r in caplog.records if "FAILED" in r.message]
        # Find the record for the effort-carrying probe (introspection),
        # not the default probe which also fails with the shared mock.
        effort_records = [
            r for r in error_records if "reasoning_effort=low" in r.message
        ]
        assert len(effort_records) == 1
        msg = effort_records[0].message
        assert "introspection" in msg
        assert "remove reasoning_effort" in msg or "unsupported" in msg

    async def test_effort_timeout_includes_effort_context(self, caplog):
        config = _make_config(llm_reasoning_effort="high")
        mock_provider = AsyncMock()

        async def slow(*args, **kwargs):
            await asyncio.sleep(100)

        mock_provider.complete = slow

        with patch("orchestrator.main._make_llm_provider", return_value=mock_provider):
            with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
                with caplog.at_level("ERROR", logger="orchestrator.main"):
                    await _validate_models(config)

        timeout_records = [r for r in caplog.records if "TIMED OUT" in r.message]
        assert len(timeout_records) >= 1
        assert "reasoning_effort=high" in timeout_records[0].message

    async def test_per_agent_effort_overrides_global(self):
        config = _make_config(
            llm_reasoning_effort="low",
            agent_models={
                "review": {
                    "provider": "anthropic",
                    "model": "claude-haiku-4-5",
                    "reasoning_effort": "high",
                },
            },
        )
        mock_provider = AsyncMock()
        mock_provider.complete = AsyncMock(return_value=MagicMock())

        efforts_seen = []

        def track_provider(*args, **kwargs):
            p = AsyncMock()
            p.complete = AsyncMock(return_value=MagicMock())
            p.reasoning_effort = None
            efforts_seen.append(p)
            return p

        with patch("orchestrator.main._make_llm_provider", side_effect=track_provider):
            await _validate_models(config)

        # default (effort=low) + review (effort=high) = 2 probes
        assert len(efforts_seen) == 2
        effort_values = {p.reasoning_effort for p in efforts_seen}
        assert "low" in effort_values
        assert "high" in effort_values

    async def test_multiple_agents_sharing_failed_key_all_named(self, caplog):
        config = _make_config(
            llm_reasoning_effort="low",
            agent_models={
                "benchmark": {
                    "provider": "anthropic",
                    "model": "claude-haiku-4-5",
                },
                "review": {
                    "provider": "anthropic",
                    "model": "claude-haiku-4-5",
                },
            },
        )
        mock_provider = AsyncMock()
        mock_provider.complete = AsyncMock(
            side_effect=Exception("effort not supported"),
        )
        mock_provider.reasoning_effort = None

        with patch("orchestrator.main._make_llm_provider", return_value=mock_provider):
            with caplog.at_level("ERROR", logger="orchestrator.main"):
                await _validate_models(config)

        error_records = [r for r in caplog.records if "FAILED" in r.message]
        assert len(error_records) >= 1
        msg = error_records[0].message
        # All three agents sharing this config should be named
        assert "default" in msg
        assert "benchmark" in msg
        assert "review" in msg
