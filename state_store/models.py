from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class TicketStatus(str, Enum):
    NEW = "new"
    TRIAGE_PENDING = "triage_pending"
    AWAITING_HARDWARE = "awaiting_hardware"
    PREPARING_PLATFORM = "preparing_platform"
    AWAITING_PROVISION = "awaiting_provision"
    EXECUTING_BENCHMARK = "executing_benchmark"
    AWAITING_REVIEW = "awaiting_review"
    AWAITING_TEARDOWN = "awaiting_teardown"
    AWAITING_CUSTOMER_GUIDANCE = "awaiting_customer_guidance"
    RETROSPECTIVE_PENDING = "retrospective_pending"
    CLOSED = "closed"

    # Data analysis before hardware provisioning
    ANALYZING = "analyzing"

    # Custom image build before hardware provisioning
    BUILDING_IMAGE = "building_image"

    # Recursive investigation loop statuses (RHIVOS 03A)
    GATHERING_CONTEXT = "gathering_context"
    PLANNING_INVESTIGATION = "planning_investigation"
    EVALUATING_CONVERGENCE = "evaluating_convergence"
    COORDINATING_FLEET = "coordinating_fleet"
    SYNTHESIZING_RESULTS = "synthesizing_results"


TERMINAL_STATUSES: set[TicketStatus] = {TicketStatus.CLOSED}
PAUSED_STATUSES: set[TicketStatus] = {TicketStatus.AWAITING_CUSTOMER_GUIDANCE}
NON_DISPATCHABLE_STATUSES: set[TicketStatus] = TERMINAL_STATUSES | PAUSED_STATUSES

VALID_TRANSITIONS: dict[TicketStatus, list[TicketStatus]] = {
    # --- Original linear pipeline ---
    TicketStatus.NEW: [TicketStatus.TRIAGE_PENDING],
    TicketStatus.TRIAGE_PENDING: [
        TicketStatus.AWAITING_HARDWARE,
        TicketStatus.ANALYZING,  # data analysis path
        TicketStatus.BUILDING_IMAGE,  # custom image build
        TicketStatus.GATHERING_CONTEXT,  # investigation path
        TicketStatus.AWAITING_CUSTOMER_GUIDANCE,
    ],
    TicketStatus.AWAITING_HARDWARE: [
        TicketStatus.PREPARING_PLATFORM,
        TicketStatus.AWAITING_PROVISION,  # providers that return ready hosts
        TicketStatus.GATHERING_CONTEXT,  # investigation redirect
        TicketStatus.COORDINATING_FLEET,  # fleet: no boards available
        TicketStatus.AWAITING_CUSTOMER_GUIDANCE,
    ],
    TicketStatus.PREPARING_PLATFORM: [
        TicketStatus.AWAITING_PROVISION,
        TicketStatus.COORDINATING_FLEET,  # fleet: flash failed
        TicketStatus.AWAITING_CUSTOMER_GUIDANCE,
    ],
    TicketStatus.AWAITING_PROVISION: [
        TicketStatus.EXECUTING_BENCHMARK,
        TicketStatus.AWAITING_HARDWARE,  # handoff retry
        TicketStatus.AWAITING_TEARDOWN,  # plan-driven
        TicketStatus.AWAITING_REVIEW,  # plan-driven
        TicketStatus.AWAITING_CUSTOMER_GUIDANCE,
    ],
    TicketStatus.EXECUTING_BENCHMARK: [
        TicketStatus.AWAITING_REVIEW,
        TicketStatus.EVALUATING_CONVERGENCE,  # investigation path
        TicketStatus.COORDINATING_FLEET,  # fleet iteration
        TicketStatus.AWAITING_PROVISION,  # handoff retry
        TicketStatus.AWAITING_TEARDOWN,  # plan-driven teardown after benchmark
        TicketStatus.AWAITING_HARDWARE,  # plan-driven infrastructure cycle
        TicketStatus.AWAITING_CUSTOMER_GUIDANCE,
    ],
    TicketStatus.AWAITING_REVIEW: [
        TicketStatus.AWAITING_TEARDOWN,
        TicketStatus.SYNTHESIZING_RESULTS,  # record findings
        TicketStatus.ANALYZING,  # loop back to data analysis
        TicketStatus.TRIAGE_PENDING,  # ad-hoc rerun loop
        TicketStatus.EXECUTING_BENCHMARK,  # plan-driven re-benchmark
        TicketStatus.AWAITING_HARDWARE,  # plan-driven infrastructure cycle
        TicketStatus.AWAITING_PROVISION,  # plan-driven re-provision
        TicketStatus.AWAITING_CUSTOMER_GUIDANCE,
    ],
    TicketStatus.AWAITING_TEARDOWN: [
        TicketStatus.RETROSPECTIVE_PENDING,
        TicketStatus.CLOSED,
        TicketStatus.AWAITING_HARDWARE,  # plan-driven infrastructure cycle
        TicketStatus.AWAITING_PROVISION,  # plan-driven
        TicketStatus.EXECUTING_BENCHMARK,  # plan-driven
        TicketStatus.AWAITING_REVIEW,  # plan-driven
        TicketStatus.AWAITING_CUSTOMER_GUIDANCE,
    ],
    TicketStatus.AWAITING_CUSTOMER_GUIDANCE: [],  # filled dynamically
    TicketStatus.RETROSPECTIVE_PENDING: [
        TicketStatus.CLOSED,
    ],
    TicketStatus.CLOSED: [],
    # --- Data analysis path ---
    # Analyzing: query external data sources and prior ticket
    # results to investigate without provisioning hardware.
    # Skip forward to review if conclusive, or continue to
    # the hardware pipeline if new measurements are needed.
    TicketStatus.ANALYZING: [
        TicketStatus.AWAITING_REVIEW,  # analysis conclusive
        TicketStatus.AWAITING_HARDWARE,  # need benchmark data
        TicketStatus.BUILDING_IMAGE,  # custom build needed
        TicketStatus.AWAITING_CUSTOMER_GUIDANCE,
    ],
    # Custom image build: deterministic, no LLM.
    TicketStatus.BUILDING_IMAGE: [
        TicketStatus.AWAITING_HARDWARE,  # build complete
        TicketStatus.AWAITING_CUSTOMER_GUIDANCE,  # build failed
    ],
    # --- Recursive investigation loop ---
    # Gathering context: check Investigation Records for dedup,
    # collect change-context from source control.
    TicketStatus.GATHERING_CONTEXT: [
        TicketStatus.ANALYZING,  # analyze existing data first
        TicketStatus.PLANNING_INVESTIGATION,
        TicketStatus.RETROSPECTIVE_PENDING,  # dedup match, skip to retro
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
        TicketStatus.ANALYZING,  # loop back to data analysis
        TicketStatus.PLANNING_INVESTIGATION,  # refine params
        TicketStatus.PREPARING_PLATFORM,  # re-provision hardware
        TicketStatus.AWAITING_PROVISION,  # re-install harness only
        TicketStatus.SYNTHESIZING_RESULTS,  # convergence gate met
        TicketStatus.AWAITING_CUSTOMER_GUIDANCE,  # manual interrupt
    ],
    # Fleet coordinator: record host result, check exhaustion,
    # route to next board or convergence evaluation.
    TicketStatus.COORDINATING_FLEET: [
        TicketStatus.AWAITING_HARDWARE,  # next board
        TicketStatus.EVALUATING_CONVERGENCE,  # fleet complete
        TicketStatus.AWAITING_CUSTOMER_GUIDANCE,  # error
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
    status_trail: list[str] = Field(default_factory=lambda: ["new"])
    transition_seq: int = 0
    created_by: str = ""
    owners: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _backfill_status_trail(self) -> "Ticket":
        """Ensure status_trail reflects current status for existing tickets.

        Tickets persisted before status_trail was added will load
        with the default ["new"]. If the ticket's actual status
        differs, append it so the dashboard shows at least the
        current state.
        """
        if self.status_trail == ["new"] and self.status != TicketStatus.NEW:
            self.status_trail.append(self.status.value)
        return self


class CreateTicketRequest(BaseModel):
    summary: str
    description: str
    custom_fields: dict[str, Any] = Field(default_factory=dict)
    owners: list[str] | None = None


class TransitionRequest(BaseModel):
    status: TicketStatus
    comment: str | None = None


class UpdateFieldsRequest(BaseModel):
    fields: dict[str, Any]


class AddCommentRequest(BaseModel):
    author: str
    body: str


class StopMode(str, Enum):
    GRACEFUL = "graceful"
    HARD = "hard"


class StopRequest(BaseModel):
    mode: StopMode = StopMode.GRACEFUL


class AbortRequest(BaseModel):
    reason: str = Field(
        default="User requested abort",
        max_length=500,
    )


class ClaimRequest(BaseModel):
    owner: str
    duration_seconds: int = 300
