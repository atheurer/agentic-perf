"""LLM budget guardrails.

Provides per-ticket and system-wide budget enforcement to prevent
runaway LLM costs. Checks are deterministic — no LLM call needed.

Per-ticket budgets are stored in custom_fields:
    custom_fields.llm_budget = {
        "max_tokens": 200000,   # total input + output
        "max_cost_usd": 5.00,   # estimated dollar cost
    }

System-wide budgets are configured in ~/.agentic-perf/config.json:
    {
        "llm_budget": {
            "session_cost_usd": 50.00
        }
    }

Budget checks return a BudgetStatus indicating whether the budget
is within limits, over a soft limit (warn but continue), or over
a hard limit (stop).
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class BudgetAction(str, Enum):
    """What to do when a budget limit is hit."""

    OK = "ok"
    WARN = "warn"  # soft limit — log warning, continue
    PAUSE = "pause"  # pause ticket for human decision


class BudgetStatus(BaseModel):
    """Result of a budget check."""

    action: BudgetAction = BudgetAction.OK
    reason: str = ""
    usage_tokens: int = 0
    usage_cost_usd: float = 0.0
    limit_tokens: int = 0
    limit_cost_usd: float = 0.0


class TicketBudget(BaseModel):
    """Per-ticket budget limits from custom_fields.llm_budget."""

    max_tokens: int = Field(
        default=0,
        description="Max total tokens (input + output). 0 = no limit.",
    )
    max_cost_usd: float = Field(
        default=0.0,
        description="Max estimated cost in USD. 0.0 = no limit.",
    )
    warn_pct: float = Field(
        default=80.0,
        description=(
            "Percentage of budget at which to warn. Set to 0 to disable warnings."
        ),
    )


class SystemBudget(BaseModel):
    """System-wide budget limits from config.

    Limits are per orchestrator session (since process start),
    not calendar-based. A future enhancement could add
    timestamp-based daily/monthly tracking.
    """

    session_cost_usd: float = Field(
        default=0.0,
        description=(
            "Max LLM cost across all tickets for this "
            "orchestrator session. 0 = no limit."
        ),
    )


def check_ticket_budget(
    budget: TicketBudget,
    usage: dict[str, Any],
    estimated_cost: float,
) -> BudgetStatus:
    """Check a ticket's LLM usage against its budget.

    Args:
        budget: The ticket's budget limits.
        usage: Usage dict from EventBus.get_cumulative_usage()
            (has total_tokens, input_tokens, output_tokens, etc.)
        estimated_cost: Estimated cost in USD from
            providers.cost.estimate_cumulative_cost().

    Returns:
        BudgetStatus with action OK, WARN, or PAUSE.
    """
    total_tokens = usage.get("total_tokens", 0)

    # Check token limit
    if budget.max_tokens > 0 and total_tokens >= budget.max_tokens:
        return BudgetStatus(
            action=BudgetAction.PAUSE,
            reason=(
                f"Token budget exceeded: {total_tokens:,} tokens "
                f"used, limit is {budget.max_tokens:,}"
            ),
            usage_tokens=total_tokens,
            usage_cost_usd=estimated_cost,
            limit_tokens=budget.max_tokens,
        )

    # Check cost limit
    if budget.max_cost_usd > 0 and estimated_cost >= budget.max_cost_usd:
        return BudgetStatus(
            action=BudgetAction.PAUSE,
            reason=(
                f"Cost budget exceeded: ${estimated_cost:.4f} "
                f"spent, limit is ${budget.max_cost_usd:.2f}"
            ),
            usage_tokens=total_tokens,
            usage_cost_usd=estimated_cost,
            limit_cost_usd=budget.max_cost_usd,
        )

    # Check warn thresholds
    if budget.warn_pct > 0:
        if (
            budget.max_tokens > 0
            and total_tokens >= budget.max_tokens * budget.warn_pct / 100
        ):
            return BudgetStatus(
                action=BudgetAction.WARN,
                reason=(
                    f"Approaching token budget: {total_tokens:,} "
                    f"of {budget.max_tokens:,} "
                    f"({total_tokens * 100 / budget.max_tokens:.0f}%)"
                ),
                usage_tokens=total_tokens,
                limit_tokens=budget.max_tokens,
            )
        if (
            budget.max_cost_usd > 0
            and estimated_cost >= budget.max_cost_usd * budget.warn_pct / 100
        ):
            return BudgetStatus(
                action=BudgetAction.WARN,
                reason=(
                    f"Approaching cost budget: "
                    f"${estimated_cost:.4f} "
                    f"of ${budget.max_cost_usd:.2f} "
                    f"({estimated_cost * 100 / budget.max_cost_usd:.0f}%)"
                ),
                usage_cost_usd=estimated_cost,
                limit_cost_usd=budget.max_cost_usd,
            )

    return BudgetStatus(
        action=BudgetAction.OK,
        usage_tokens=total_tokens,
        usage_cost_usd=estimated_cost,
    )


def check_system_budget(
    budget: SystemBudget,
    global_usage: dict[str, Any],
    global_cost: float,
) -> BudgetStatus:
    """Check system-wide LLM usage against budget.

    Args:
        budget: System-wide budget limits.
        global_usage: Usage dict from EventBus.get_global_usage().
        global_cost: Estimated total cost across all tickets.

    Returns:
        BudgetStatus with action OK or PAUSE.
        System-wide budgets don't warn — they block dispatching.
    """
    if budget.session_cost_usd > 0 and global_cost >= budget.session_cost_usd:
        return BudgetStatus(
            action=BudgetAction.PAUSE,
            reason=(
                f"System session budget exceeded: "
                f"${global_cost:.2f} spent, "
                f"limit is ${budget.session_cost_usd:.2f}"
            ),
            usage_cost_usd=global_cost,
            limit_cost_usd=budget.session_cost_usd,
        )

    return BudgetStatus(action=BudgetAction.OK, usage_cost_usd=global_cost)


def budget_from_custom_fields(
    custom_fields: dict[str, Any],
) -> TicketBudget | None:
    """Extract ticket budget from custom_fields.

    Returns None if no budget is configured — caller should
    skip budget checks entirely.
    """
    raw = custom_fields.get("llm_budget")
    if not raw:
        return None
    return TicketBudget(**raw)


def system_budget_from_config(
    config: dict[str, Any],
) -> SystemBudget:
    """Extract system budget from orchestrator config.

    Returns default (no limits) if not configured.
    """
    raw = config.get("llm_budget", {})
    if not raw:
        return SystemBudget()
    return SystemBudget(**raw)
