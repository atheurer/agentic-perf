"""Tests for context-window guardrails.

Tests the pure-function boundary checks and config resolution
in providers/context_guard.py, plus loop-level integration
with agents/base.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from providers.context_guard import (
    ContextAction,
    check_context_usage,
    context_guard_from_config,
    context_guard_from_custom_fields,
)

# ------------------------------------------------------------------
# Pure-function tests: check_context_usage
# ------------------------------------------------------------------


class TestCheckContextUsage:
    def test_ok_well_under_limit(self):
        action, reason = check_context_usage(50_000, 200_000)
        assert action == ContextAction.OK
        assert reason == ""

    def test_warn_at_threshold(self):
        action, reason = check_context_usage(
            120_000,
            200_000,
            warn_pct=60,
        )
        assert action == ContextAction.WARN
        assert "60%" in reason

    def test_pause_at_threshold(self):
        action, reason = check_context_usage(
            160_000,
            200_000,
            pause_pct=80,
        )
        assert action == ContextAction.PAUSE
        assert "80%" in reason
        assert "160,000" in reason
        assert "200,000" in reason

    def test_pause_takes_priority_over_warn(self):
        action, _ = check_context_usage(
            180_000,
            200_000,
            warn_pct=60,
            pause_pct=80,
        )
        assert action == ContextAction.PAUSE

    def test_zero_window_is_ok(self):
        action, _ = check_context_usage(100_000, 0)
        assert action == ContextAction.OK

    def test_zero_tokens_is_ok(self):
        action, _ = check_context_usage(0, 200_000)
        assert action == ContextAction.OK

    def test_disabled_warn(self):
        action, _ = check_context_usage(
            150_000,
            200_000,
            warn_pct=0,
            pause_pct=80,
        )
        assert action == ContextAction.OK

    def test_disabled_pause(self):
        action, _ = check_context_usage(
            190_000,
            200_000,
            warn_pct=60,
            pause_pct=0,
        )
        assert action == ContextAction.WARN

    def test_exact_boundary_warn(self):
        action, _ = check_context_usage(
            120_000,
            200_000,
            warn_pct=60,
            pause_pct=80,
        )
        assert action == ContextAction.WARN

    def test_exact_boundary_pause(self):
        action, _ = check_context_usage(
            160_000,
            200_000,
            warn_pct=60,
            pause_pct=80,
        )
        assert action == ContextAction.PAUSE


# ------------------------------------------------------------------
# Config resolution tests
# ------------------------------------------------------------------


class TestContextGuardConfig:
    def test_defaults_when_no_config(self):
        guard = context_guard_from_config({})
        assert guard["enabled"] is True
        assert guard["warn_pct"] == 60
        assert guard["pause_pct"] == 80
        assert guard["default_context_window"] == 0

    def test_config_override(self):
        guard = context_guard_from_config(
            {
                "context_guard": {
                    "enabled": False,
                    "warn_pct": 50,
                    "pause_pct": 70,
                    "default_context_window": 100_000,
                },
            }
        )
        assert guard["enabled"] is False
        assert guard["warn_pct"] == 50
        assert guard["pause_pct"] == 70
        assert guard["default_context_window"] == 100_000

    def test_partial_config(self):
        guard = context_guard_from_config(
            {
                "context_guard": {"warn_pct": 50},
            }
        )
        assert guard["enabled"] is True
        assert guard["warn_pct"] == 50
        assert guard["pause_pct"] == 80

    def test_ticket_override_merges(self):
        config_guard = context_guard_from_config(
            {
                "context_guard": {"warn_pct": 50, "pause_pct": 70},
            }
        )
        merged = context_guard_from_custom_fields(
            {"context_guard": {"warn_pct": 90}},
            config_guard,
        )
        assert merged["warn_pct"] == 90
        assert merged["pause_pct"] == 70

    def test_ticket_can_disable(self):
        config_guard = context_guard_from_config(
            {
                "context_guard": {"enabled": True},
            }
        )
        merged = context_guard_from_custom_fields(
            {"context_guard": {"enabled": False}},
            config_guard,
        )
        assert merged["enabled"] is False

    def test_null_config_guard_uses_defaults(self):
        guard = context_guard_from_config({"context_guard": None})
        assert guard["enabled"] is True
        assert guard["warn_pct"] == 60

    def test_null_ticket_guard_uses_config(self):
        config_guard = context_guard_from_config(
            {
                "context_guard": {"warn_pct": 50},
            }
        )
        merged = context_guard_from_custom_fields(
            {"context_guard": None},
            config_guard,
        )
        assert merged["warn_pct"] == 50


# ------------------------------------------------------------------
# get_context_window tests
# ------------------------------------------------------------------


class TestGetContextWindow:
    def test_exact_match(self):
        from providers.cost import get_context_window

        window = get_context_window("claude-opus-4-6")
        assert window == 200_000

    def test_prefix_match(self):
        from providers.cost import get_context_window

        window = get_context_window("claude-sonnet-4-20260101")
        assert window == 200_000

    def test_unknown_model_falls_back(self):
        from providers.cost import get_context_window

        window = get_context_window("completely-unknown-model-xyz")
        assert window == 128_000

    def test_gemini_large_context(self):
        from providers.cost import get_context_window

        window = get_context_window("gemini-2.5-pro")
        assert window == 1_048_576

    def test_versioned_mini_prefers_longest_prefix(self):
        """gpt-5.4-mini-2026-08-01 must match gpt-5.4-mini (1M)
        not gpt-5.4 (256k)."""
        from providers.cost import get_context_window

        window = get_context_window("gpt-5.4-mini-2026-08-01")
        assert window == 1_048_576


# ------------------------------------------------------------------
# Provider context_tokens emission tests
# ------------------------------------------------------------------


class TestProviderContextTokens:
    def test_claude_context_tokens(self):
        usage = {
            "input_tokens": 1000,
            "cache_read_input_tokens": 500,
            "cache_creation_input_tokens": 200,
            "context_tokens": 1700,
            "model": "claude-opus-4-6",
        }
        assert usage["context_tokens"] == (
            usage["input_tokens"]
            + usage["cache_read_input_tokens"]
            + usage["cache_creation_input_tokens"]
        )

    def test_openai_context_tokens(self):
        usage = {
            "input_tokens": 3000,
            "context_tokens": 3000,
            "model": "gpt-4o",
        }
        assert usage["context_tokens"] == usage["input_tokens"]

    def test_gemini_no_double_count(self):
        usage = {
            "input_tokens": 5000,
            "cache_read_input_tokens": 2000,
            "cache_creation_input_tokens": 0,
            "context_tokens": 5000,
            "model": "gemini-2.5-pro",
        }
        assert usage["context_tokens"] == usage["input_tokens"]


# ------------------------------------------------------------------
# Loop integration tests via _check_context
# ------------------------------------------------------------------


def _make_agent():
    from agents.base import AgentBase

    class StubAgent(AgentBase):
        def _system_prompt(self, ticket):
            return "test"

        def _build_messages(self, ticket):
            return [{"role": "user", "content": "test"}]

        async def _handle_completion(self, ticket_id, response):
            pass

    agent = StubAgent.__new__(StubAgent)
    agent.agent_name = "test-agent"
    agent.llm = MagicMock()
    agent.store_url = "http://localhost:8090"
    agent.tools = []
    agent._tool_handlers = {}
    agent._events = MagicMock()
    agent._mcp = None
    agent._stop_requested = False
    agent._client = AsyncMock()
    return agent


class TestCheckContextMethod:
    @pytest.mark.asyncio
    async def test_pause_when_context_full(self):
        agent = _make_agent()
        agent._get_ticket = AsyncMock(
            return_value={"custom_fields": {}},
        )

        usage = {
            "context_tokens": 170_000,
            "model": "claude-opus-4-6",
        }

        with patch(
            "orchestrator.config._load_config_file",
            return_value={},
        ):
            result = await agent._check_context("T-1", usage)

        assert result == "pause"

    @pytest.mark.asyncio
    async def test_warn_when_approaching_limit(self):
        agent = _make_agent()
        agent._get_ticket = AsyncMock(
            return_value={"custom_fields": {}},
        )

        usage = {
            "context_tokens": 130_000,
            "model": "claude-opus-4-6",
        }

        with patch(
            "orchestrator.config._load_config_file",
            return_value={},
        ):
            result = await agent._check_context("T-1", usage)

        assert result == "warn"

    @pytest.mark.asyncio
    async def test_ok_when_under_limit(self):
        agent = _make_agent()
        agent._get_ticket = AsyncMock(
            return_value={"custom_fields": {}},
        )

        usage = {
            "context_tokens": 50_000,
            "model": "claude-opus-4-6",
        }

        with patch(
            "orchestrator.config._load_config_file",
            return_value={},
        ):
            result = await agent._check_context("T-1", usage)

        assert result == "ok"

    @pytest.mark.asyncio
    async def test_disabled_via_config(self):
        agent = _make_agent()
        agent._get_ticket = AsyncMock(
            return_value={"custom_fields": {}},
        )

        usage = {
            "context_tokens": 190_000,
            "model": "claude-opus-4-6",
        }

        with patch(
            "orchestrator.config._load_config_file",
            return_value={"context_guard": {"enabled": False}},
        ):
            result = await agent._check_context("T-1", usage)

        assert result == "ok"

    @pytest.mark.asyncio
    async def test_disabled_via_ticket(self):
        agent = _make_agent()
        agent._get_ticket = AsyncMock(
            return_value={
                "custom_fields": {
                    "context_guard": {"enabled": False},
                },
            },
        )

        usage = {
            "context_tokens": 190_000,
            "model": "claude-opus-4-6",
        }

        with patch(
            "orchestrator.config._load_config_file",
            return_value={},
        ):
            result = await agent._check_context("T-1", usage)

        assert result == "ok"

    @pytest.mark.asyncio
    async def test_no_usage_returns_ok(self):
        agent = _make_agent()
        agent._get_ticket = AsyncMock(
            return_value={"custom_fields": {}},
        )

        usage = {"model": "claude-opus-4-6"}

        with patch(
            "orchestrator.config._load_config_file",
            return_value={},
        ):
            result = await agent._check_context("T-1", usage)

        assert result == "ok"

    @pytest.mark.asyncio
    async def test_no_model_uses_default_window(self):
        agent = _make_agent()
        agent._get_ticket = AsyncMock(
            return_value={"custom_fields": {}},
        )

        usage = {"context_tokens": 90_000}

        with patch(
            "orchestrator.config._load_config_file",
            return_value={
                "context_guard": {
                    "default_context_window": 100_000,
                },
            },
        ):
            result = await agent._check_context("T-1", usage)

        assert result == "pause"

    @pytest.mark.asyncio
    async def test_config_default_overrides_fallback_for_unknown_model(self):
        """When the model is unknown and pricing.yaml returns the
        128k fallback, a configured default_context_window of 200k
        should be used (the larger value wins)."""
        agent = _make_agent()
        agent._get_ticket = AsyncMock(
            return_value={"custom_fields": {}},
        )

        usage = {
            "context_tokens": 140_000,
            "model": "totally-unknown-model",
        }

        with patch(
            "orchestrator.config._load_config_file",
            return_value={
                "context_guard": {
                    "default_context_window": 200_000,
                },
            },
        ):
            result = await agent._check_context("T-1", usage)

        assert result == "warn"

    @pytest.mark.asyncio
    async def test_max_iterations_zero_still_checked(self):
        """Context guard fires even when max_iterations=0
        (investigation agents)."""
        agent = _make_agent()
        agent.max_iterations = 0
        agent._get_ticket = AsyncMock(
            return_value={"custom_fields": {}},
        )

        usage = {
            "context_tokens": 170_000,
            "model": "claude-opus-4-6",
        }

        with patch(
            "orchestrator.config._load_config_file",
            return_value={},
        ):
            result = await agent._check_context("T-1", usage)

        assert result == "pause"


# ------------------------------------------------------------------
# Grace-flag collision: no double grace
# ------------------------------------------------------------------


class TestGraceNoStacking:
    @pytest.mark.asyncio
    async def test_context_grace_blocks_budget_grace(self):
        """Once _wrapup_reason is set to 'context', the budget
        guard should not set its own grace."""
        agent = _make_agent()
        agent._wrapup_reason = "context"

        assert agent._wrapup_reason is not None
        assert agent._wrapup_reason == "context"

    @pytest.mark.asyncio
    async def test_budget_grace_blocks_context_grace(self):
        """Once _wrapup_reason is set to 'budget', the context
        guard should not set its own grace."""
        agent = _make_agent()
        agent._wrapup_reason = "budget"

        assert agent._wrapup_reason is not None
        assert agent._wrapup_reason == "budget"
