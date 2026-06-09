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


VALID_TRANSITIONS: dict[TicketStatus, list[TicketStatus]] = {
    TicketStatus.NEW: [TicketStatus.TRIAGE_PENDING],
    TicketStatus.TRIAGE_PENDING: [
        TicketStatus.AWAITING_HARDWARE,
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
        TicketStatus.AWAITING_CUSTOMER_GUIDANCE,
    ],
    TicketStatus.AWAITING_REVIEW: [
        TicketStatus.AWAITING_TEARDOWN,
        TicketStatus.TRIAGE_PENDING,  # /rerun loop
        TicketStatus.AWAITING_CUSTOMER_GUIDANCE,
    ],
    TicketStatus.AWAITING_TEARDOWN: [
        TicketStatus.CLOSED,
        TicketStatus.AWAITING_CUSTOMER_GUIDANCE,
    ],
    TicketStatus.AWAITING_CUSTOMER_GUIDANCE: [],  # filled dynamically from previous_status
    TicketStatus.CLOSED: [],
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
