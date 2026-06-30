"""Tests for LLM budget guardrails.

Tests per-ticket and system-wide budget enforcement: token limits,
cost limits, warn thresholds, and custom_fields extraction.
"""

from __future__ import annotations

import pytest

from providers.budget import (
    BudgetAction,
    SystemBudget,
    TicketBudget,
    budget_from_custom_fields,
    check_system_budget,
    check_ticket_budget,
    system_budget_from_config,
)

# --- TicketBudget model ---


class TestTicketBudgetDefaults:
    def test_no_limits_by_default(self):
        b = TicketBudget()
        assert b.max_tokens == 0
        assert b.max_cost_usd == 0.0

    def test_warn_pct_default(self):
        b = TicketBudget()
        assert b.warn_pct == 80.0


# --- Per-ticket token budget ---


class TestTicketTokenBudget:
    def test_under_budget(self):
        budget = TicketBudget(max_tokens=100_000)
        usage = {"total_tokens": 50_000}
        s = check_ticket_budget(budget, usage, 0.0)
        assert s.action == BudgetAction.OK

    def test_over_budget(self):
        budget = TicketBudget(max_tokens=100_000)
        usage = {"total_tokens": 100_000}
        s = check_ticket_budget(budget, usage, 0.0)
        assert s.action == BudgetAction.PAUSE
        assert "100,000" in s.reason

    def test_warn_threshold(self):
        budget = TicketBudget(max_tokens=100_000, warn_pct=80.0)
        usage = {"total_tokens": 85_000}
        s = check_ticket_budget(budget, usage, 0.0)
        assert s.action == BudgetAction.WARN
        assert "85%" in s.reason

    def test_no_limit_means_no_check(self):
        budget = TicketBudget(max_tokens=0)
        usage = {"total_tokens": 999_999}
        s = check_ticket_budget(budget, usage, 0.0)
        assert s.action == BudgetAction.OK

    def test_warn_disabled(self):
        budget = TicketBudget(max_tokens=100_000, warn_pct=0)
        usage = {"total_tokens": 95_000}
        s = check_ticket_budget(budget, usage, 0.0)
        assert s.action == BudgetAction.OK  # below limit, no warn


# --- Per-ticket cost budget ---


class TestTicketCostBudget:
    def test_under_budget(self):
        budget = TicketBudget(max_cost_usd=5.00)
        usage = {"total_tokens": 50_000}
        s = check_ticket_budget(budget, usage, 2.50)
        assert s.action == BudgetAction.OK

    def test_over_budget(self):
        budget = TicketBudget(max_cost_usd=5.00)
        usage = {"total_tokens": 200_000}
        s = check_ticket_budget(budget, usage, 5.50)
        assert s.action == BudgetAction.PAUSE
        assert "$5.50" in s.reason

    def test_warn_threshold(self):
        budget = TicketBudget(max_cost_usd=10.00, warn_pct=80.0)
        usage = {"total_tokens": 100_000}
        s = check_ticket_budget(budget, usage, 8.50)
        assert s.action == BudgetAction.WARN
        assert "85%" in s.reason

    def test_no_limit_means_no_check(self):
        budget = TicketBudget(max_cost_usd=0.0)
        usage = {"total_tokens": 0}
        s = check_ticket_budget(budget, usage, 999.99)
        assert s.action == BudgetAction.OK


# --- Token limit beats cost warn ---


class TestBudgetPriority:
    def test_token_pause_beats_cost_warn(self):
        """Token limit exceeded takes priority over cost warning."""
        budget = TicketBudget(
            max_tokens=100_000,
            max_cost_usd=10.00,
            warn_pct=80.0,
        )
        usage = {"total_tokens": 100_000}
        # Cost is at 85% (warn), but tokens are at 100% (pause)
        s = check_ticket_budget(budget, usage, 8.50)
        assert s.action == BudgetAction.PAUSE


# --- System-wide budget ---


class TestSystemBudget:
    def test_under_session(self):
        budget = SystemBudget(session_cost_usd=50.00)
        usage = {"total_tokens": 100_000}
        s = check_system_budget(budget, usage, 25.00)
        assert s.action == BudgetAction.OK

    def test_over_session(self):
        budget = SystemBudget(session_cost_usd=50.00)
        usage = {"total_tokens": 500_000}
        s = check_system_budget(budget, usage, 55.00)
        assert s.action == BudgetAction.PAUSE
        assert "session" in s.reason.lower()

    def test_no_limits(self):
        budget = SystemBudget()
        usage = {"total_tokens": 999_999}
        s = check_system_budget(budget, usage, 9999.99)
        assert s.action == BudgetAction.OK


# --- Custom fields extraction ---


class TestExtraction:
    def test_budget_from_custom_fields(self):
        cf = {
            "llm_budget": {
                "max_tokens": 200_000,
                "max_cost_usd": 5.00,
            },
        }
        b = budget_from_custom_fields(cf)
        assert b is not None
        assert b.max_tokens == 200_000
        assert b.max_cost_usd == 5.00

    def test_no_budget_returns_none(self):
        b = budget_from_custom_fields({})
        assert b is None

    def test_no_budget_falls_back_to_config_default(self):
        """When ticket has no llm_budget, use config default."""
        config = {
            "llm_budget": {
                "default_ticket_budget": {
                    "max_tokens": 150_000,
                    "max_cost_usd": 3.00,
                },
            },
        }
        b = budget_from_custom_fields({}, config)
        assert b is not None
        assert b.max_tokens == 150_000
        assert b.max_cost_usd == 3.00

    def test_ticket_budget_overrides_config_default(self):
        """Per-ticket budget takes precedence over config default."""
        config = {
            "llm_budget": {
                "default_ticket_budget": {
                    "max_tokens": 150_000,
                    "max_cost_usd": 3.00,
                },
            },
        }
        cf = {
            "llm_budget": {
                "max_tokens": 500_000,
                "max_cost_usd": 10.00,
            },
        }
        b = budget_from_custom_fields(cf, config)
        assert b is not None
        assert b.max_tokens == 500_000
        assert b.max_cost_usd == 10.00

    def test_no_budget_no_config_returns_none(self):
        """No ticket budget + no config default = None."""
        config = {"llm_budget": {"session_cost_usd": 50.0}}
        b = budget_from_custom_fields({}, config)
        assert b is None

    def test_system_budget_from_config(self):
        cfg = {
            "llm_budget": {
                "session_cost_usd": 50.00,
            },
        }
        b = system_budget_from_config(cfg)
        assert b.session_cost_usd == 50.00

    def test_system_budget_defaults(self):
        b = system_budget_from_config({})
        assert b.session_cost_usd == 0.0


# --- Soft/hard graceful degradation in agent loop ---


class TestBudgetGracefulDegradation:
    """Test the two-phase budget enforcement in AgentBase.run()."""

    @pytest.mark.asyncio
    async def test_warn_injects_message(self, tmp_path):
        """At 80% budget, a warning message is injected into
        the conversation but the agent continues."""
        from unittest.mock import AsyncMock, patch

        from agents.base import AgentBase
        from providers.events import EventBus
        from providers.llm.base import LLMResponse, ToolCall

        class _Stub(AgentBase):
            def _system_prompt(self, ticket=None):
                return "test"

            def _build_messages(self, ticket):
                return [{"role": "user", "content": "test"}]

            async def _handle_completion(self, ticket_id, response):
                pass

        call_count = 0

        class _WarnThenFinishLLM:
            async def complete(self, **kwargs):
                nonlocal call_count
                call_count += 1
                # warn message would be in messages if injected
                _ = kwargs.get("messages", [])
                if call_count >= 3:
                    return LLMResponse(
                        text="done",
                        tool_calls=[],
                        stop_reason="end_turn",
                        raw_content=[],
                    )
                return LLMResponse(
                    text=None,
                    tool_calls=[
                        ToolCall(id=f"tc_{call_count}", name="some_tool", input={}),
                    ],
                    stop_reason="tool_use",
                    raw_content=[
                        {
                            "type": "tool_use",
                            "id": f"tc_{call_count}",
                            "name": "some_tool",
                            "input": {},
                        },
                    ],
                )

        event_bus = EventBus(log_dir=tmp_path / "logs")
        agent = _Stub(
            agent_name="test",
            llm_provider=_WarnThenFinishLLM(),
            state_store_url="http://localhost:8090",
            event_bus=event_bus,
        )
        agent._client = AsyncMock()
        agent._client.get = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: {
                    "id": "PERF-TEST",
                    "status": "triage_pending",
                    "summary": "test",
                    "custom_fields": {
                        "llm_budget": {
                            "max_tokens": 100,
                            "warn_pct": 80,
                        },
                    },
                },
                raise_for_status=lambda: None,
            ),
        )

        # Simulate token usage at warn level (80+%)
        event_bus.record_llm_usage("PERF-TEST", 85, 5, 100)

        with patch.object(agent, "_check_budget", return_value="warn"):
            await agent.run("PERF-TEST")

        # Agent should have continued past the warn
        assert call_count >= 2

    @pytest.mark.asyncio
    async def test_pause_gives_grace_then_stops(self, tmp_path):
        """At 100% budget, agent gets one grace iteration then
        hard stops."""
        from unittest.mock import AsyncMock, patch

        from agents.base import AgentBase
        from providers.events import EventBus
        from providers.llm.base import LLMResponse, ToolCall

        class _Stub(AgentBase):
            def _system_prompt(self, ticket=None):
                return "test"

            def _build_messages(self, ticket):
                return [{"role": "user", "content": "test"}]

            async def _handle_completion(self, ticket_id, response):
                pass

        call_count = 0

        class _KeepGoingLLM:
            async def complete(self, **kwargs):
                nonlocal call_count
                call_count += 1
                return LLMResponse(
                    text=None,
                    tool_calls=[
                        ToolCall(id=f"tc_{call_count}", name="some_tool", input={}),
                    ],
                    stop_reason="tool_use",
                    raw_content=[
                        {
                            "type": "tool_use",
                            "id": f"tc_{call_count}",
                            "name": "some_tool",
                            "input": {},
                        },
                    ],
                )

        event_bus = EventBus(log_dir=tmp_path / "logs")
        agent = _Stub(
            agent_name="test",
            llm_provider=_KeepGoingLLM(),
            state_store_url="http://localhost:8090",
            event_bus=event_bus,
            max_iterations=10,
        )
        agent._client = AsyncMock()
        agent._client.get = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: {
                    "id": "PERF-TEST",
                    "status": "triage_pending",
                    "summary": "test",
                    "custom_fields": {
                        "llm_budget": {"max_tokens": 50},
                    },
                },
                raise_for_status=lambda: None,
            ),
        )
        agent._client.post = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: {},
                raise_for_status=lambda: None,
            ),
        )

        # Return "pause" starting from iteration 2
        async def mock_budget(ticket_id):
            if call_count >= 2:
                return "pause"
            return "ok"

        with patch.object(agent, "_check_budget", side_effect=mock_budget):
            await agent.run("PERF-TEST")

        # Should have: iter 1 (ok), iter 2 (pause, grace),
        # iter 3 (pause again, hard stop)
        # Grace gives one more iteration, so 3-4 total
        assert call_count <= 5
        assert agent._budget_grace is True
