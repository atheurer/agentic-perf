from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class TicketStatus(str, Enum):
    NEW = "new"
    TRIAGE_PENDING = "triage_pending"
    AWAITING_HARDWARE = "awaiting_hardware"
    AWAITING_PROVISION = "awaiting_provision"
    EXECUTING_BENCHMARK = "executing_benchmark"
    AWAITING_REVIEW = "awaiting_review"
    AWAITING_TEARDOWN = "awaiting_teardown"
    AWAITING_CUSTOMER_GUIDANCE = "awaiting_customer_guidance"
    CLOSED = "closed"

    # Recursive investigation loop statuses (RHIVOS 03A)
    GATHERING_CONTEXT = "gathering_context"
    PLANNING_INVESTIGATION = "planning_investigation"
    EVALUATING_CONVERGENCE = "evaluating_convergence"
    SYNTHESIZING_RESULTS = "synthesizing_results"


VALID_TRANSITIONS: dict[TicketStatus, list[TicketStatus]] = {
    # --- Original linear pipeline ---
    TicketStatus.NEW: [TicketStatus.TRIAGE_PENDING],
    TicketStatus.TRIAGE_PENDING: [
        TicketStatus.AWAITING_HARDWARE,
        TicketStatus.GATHERING_CONTEXT,  # investigation path
        TicketStatus.AWAITING_CUSTOMER_GUIDANCE,
    ],
    TicketStatus.AWAITING_HARDWARE: [
        TicketStatus.AWAITING_PROVISION,
        TicketStatus.AWAITING_CUSTOMER_GUIDANCE,
    ],
    TicketStatus.AWAITING_PROVISION: [
        TicketStatus.EXECUTING_BENCHMARK,
        TicketStatus.AWAITING_CUSTOMER_GUIDANCE,
    ],
    TicketStatus.EXECUTING_BENCHMARK: [
        TicketStatus.AWAITING_REVIEW,
        TicketStatus.EVALUATING_CONVERGENCE,  # investigation path
        TicketStatus.AWAITING_CUSTOMER_GUIDANCE,
    ],
    TicketStatus.AWAITING_REVIEW: [
        TicketStatus.AWAITING_TEARDOWN,
        TicketStatus.TRIAGE_PENDING,  # ad-hoc rerun loop
        TicketStatus.EXECUTING_BENCHMARK,  # plan-driven re-benchmark
        TicketStatus.AWAITING_CUSTOMER_GUIDANCE,
    ],
    TicketStatus.AWAITING_TEARDOWN: [
        TicketStatus.CLOSED,
        TicketStatus.AWAITING_CUSTOMER_GUIDANCE,
    ],
    TicketStatus.AWAITING_CUSTOMER_GUIDANCE: [],  # filled dynamically
    TicketStatus.CLOSED: [],
    # --- Recursive investigation loop ---
    # Gathering context: check Investigation Records for dedup,
    # collect change-context from source control.
    TicketStatus.GATHERING_CONTEXT: [
        TicketStatus.PLANNING_INVESTIGATION,
        TicketStatus.CLOSED,  # matched existing record, skip
        TicketStatus.AWAITING_CUSTOMER_GUIDANCE,
    ],
    # Planning investigation: form test plan from hypothesis.
    # Aligns with upstream #59 (concurrent agent negotiation)
    # and #92 (multi-turn execution sequences).
    TicketStatus.PLANNING_INVESTIGATION: [
        TicketStatus.AWAITING_PROVISION,  # plan agreed, provision
        TicketStatus.AWAITING_HARDWARE,  # need new resources
        TicketStatus.AWAITING_CUSTOMER_GUIDANCE,
    ],
    # Evaluating convergence: assess results after benchmark.
    # Loop-back to planning (refine params) or provision
    # (tainted hardware). Supports #92 multi-turn by allowing
    # the evaluate agent to sequence additional benchmark runs.
    TicketStatus.EVALUATING_CONVERGENCE: [
        TicketStatus.PLANNING_INVESTIGATION,  # refine params
        TicketStatus.AWAITING_PROVISION,  # re-flash hardware
        TicketStatus.SYNTHESIZING_RESULTS,  # convergence gate met
        TicketStatus.AWAITING_CUSTOMER_GUIDANCE,  # manual interrupt
    ],
    # Synthesizing results: produce Investigation Record,
    # action handoff.
    TicketStatus.SYNTHESIZING_RESULTS: [
        TicketStatus.AWAITING_TEARDOWN,
        TicketStatus.AWAITING_CUSTOMER_GUIDANCE,
    ],
}


class Comment(BaseModel):
    id: str
    author: str
    body: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Ticket(BaseModel):
    id: str
    summary: str
    description: str
    status: TicketStatus = TicketStatus.NEW
    custom_fields: dict[str, Any] = Field(default_factory=dict)
    comments: list[Comment] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    previous_status: TicketStatus | None = None
    transition_seq: int = 0


class CreateTicketRequest(BaseModel):
    summary: str
    description: str
    custom_fields: dict[str, Any] = Field(default_factory=dict)


class TransitionRequest(BaseModel):
    status: TicketStatus
    comment: str | None = None


class UpdateFieldsRequest(BaseModel):
    fields: dict[str, Any]


class AddCommentRequest(BaseModel):
    author: str
    body: str
