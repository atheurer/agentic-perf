"""Investigation ledger for tracking iteration context.

The ledger captures the reasoning history of an investigation:
what was hypothesized, what was tried, what was concluded, and
how much information was gained. It lives alongside the execution
plan in custom_fields — the plan handles sequencing (what agents
run next), the ledger handles reasoning (what we learned).

Each ledger entry references the execution plan step(s) it
corresponds to via plan_steps indices. This links the two
structures without duplication: the plan records what ran, the
ledger records what we concluded.

The ledger is append-only. Entries are never modified after
creation — they form a write-once audit trail of investigation
reasoning that is safe from concurrent modification by users
editing plan steps via HITL (#135).

Usage:
    from providers.ledger import (
        LedgerEntry,
        get_ledger,
        append_ledger_entry,
    )

    # Read the ledger from a ticket
    ledger = get_ledger(ticket["custom_fields"])

    # Append a new entry
    entry = LedgerEntry(
        iteration=len(ledger) + 1,
        plan_steps=[2],
        hypothesis="regression isolated to single-queue path",
        conclusion="61% degradation confirmed at iodepth=1",
        info_gain=0.61,
    )
    new_fields = append_ledger_entry(
        ticket["custom_fields"],
        entry,
    )
    await agent._update_fields(ticket_id, new_fields)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class LedgerEntry(BaseModel):
    """One iteration of investigation reasoning.

    Each entry captures what was hypothesized, what was tried
    (via plan_steps reference), and what was concluded. The
    info_gain field feeds the entropy stall convergence gate.
    """

    iteration: int = Field(
        description="1-based iteration number.",
    )
    plan_steps: list[int] = Field(
        default_factory=list,
        description=(
            "Indices into execution_plan.steps that this "
            "iteration corresponds to. An iteration may span "
            "multiple plan steps (e.g., context-gathering + "
            "benchmark)."
        ),
    )
    hypothesis: str = Field(
        default="",
        description="Working hypothesis for this iteration.",
    )
    params_rationale: str = Field(
        default="",
        description=(
            "Why these parameters were chosen for this "
            "iteration (informed by prior results)."
        ),
    )
    conclusion: str = Field(
        default="",
        description="What was learned from this iteration.",
    )
    info_gain: float = Field(
        default=0.0,
        description=(
            "Information gained this iteration (0.0-1.0). "
            "Fed to the entropy stall convergence gate."
        ),
    )
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


def get_ledger(custom_fields: dict[str, Any]) -> list[LedgerEntry]:
    """Read the investigation ledger from ticket custom_fields.

    Returns an empty list if no ledger exists.
    """
    raw = custom_fields.get("investigation_ledger", [])
    return [LedgerEntry(**entry) for entry in raw]


def append_ledger_entry(
    custom_fields: dict[str, Any],
    entry: LedgerEntry,
) -> dict[str, Any]:
    """Create the fields dict for appending a ledger entry.

    Returns the fields to pass to _update_fields(). Performs
    a read-modify-write on the investigation_ledger list since
    the PATCH API does a shallow merge.

    The caller must pass the current custom_fields (from a
    fresh _get_ticket() call) to avoid race conditions.
    """
    existing = custom_fields.get("investigation_ledger", [])
    updated = existing + [entry.model_dump(mode="json")]
    return {"investigation_ledger": updated}


def get_working_hypothesis(
    custom_fields: dict[str, Any],
) -> str:
    """Get the current working hypothesis.

    Returns the hypothesis from the most recent ledger entry,
    or the triage hypothesis if no ledger entries exist.
    """
    ledger = custom_fields.get("investigation_ledger", [])
    if ledger:
        return ledger[-1].get("hypothesis", "")
    return custom_fields.get("hypothesis", "")
