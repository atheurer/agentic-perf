from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from ..models import AddCommentRequest, StopRequest, TicketStatus, TransitionRequest
from ..store import TicketNotFound

TERMINAL_STATUSES = {TicketStatus.CLOSED, TicketStatus.AWAITING_CUSTOMER_GUIDANCE}

router = APIRouter(tags=["stop"])


@router.post("/tickets/{ticket_id}/stop")
def stop_ticket(ticket_id: str, body: StopRequest, request: Request):
    store = request.app.state.store
    try:
        ticket = store.get_ticket(ticket_id)
    except TicketNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))

    if ticket.status in TERMINAL_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Ticket {ticket_id} is already in terminal state"
                f" '{ticket.status.value}'"
            ),
        )

    updated = store.update_fields(
        ticket_id,
        {
            "stop_requested": {
                "mode": body.mode.value,
                "requested_at": datetime.now(timezone.utc).isoformat(),
            },
        },
    )
    return updated


@router.post("/tickets/{ticket_id}/abort")
def abort_ticket(ticket_id: str, request: Request):
    store = request.app.state.store
    try:
        ticket = store.get_ticket(ticket_id)
    except TicketNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))

    if ticket.status != TicketStatus.AWAITING_CUSTOMER_GUIDANCE:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Abort is only available when the ticket is in"
                f" awaiting_customer_guidance (current: '{ticket.status.value}')"
            ),
        )

    store.add_comment(
        ticket_id,
        AddCommentRequest(
            author="user",
            body="**Abort requested:** User requested abort from web UI",
        ),
    )
    updated = store.transition_ticket(
        ticket_id,
        TransitionRequest(
            status=TicketStatus.AWAITING_TEARDOWN,
            comment="User requested abort, skipping to cleanup",
        ),
    )
    return updated


@router.post("/stop-all")
def stop_all(body: StopRequest, request: Request):
    store = request.app.state.store
    tickets = store.list_tickets()
    affected = []
    for ticket in tickets:
        if ticket.status in TERMINAL_STATUSES:
            continue
        updated = store.update_fields(
            ticket.id,
            {
                "stop_requested": {
                    "mode": body.mode.value,
                    "requested_at": datetime.now(timezone.utc).isoformat(),
                },
            },
        )
        affected.append(updated)
    return {"affected": affected, "count": len(affected)}
