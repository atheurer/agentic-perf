"""Tests for LLM budget guardrails.

Tests per-ticket and system-wide budget enforcement: token limits,
cost limits, warn thresholds, and custom_fields extraction.
"""

from __future__ import annotations

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
