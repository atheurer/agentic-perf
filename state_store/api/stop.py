from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from ..auth import Principal, require_admin, require_write_access
from ..models import (
    NON_DISPATCHABLE_STATUSES,
    TERMINAL_STATUSES,
    AddCommentRequest,
    StopRequest,
    TicketStatus,
    TransitionRequest,
)
from ..store import TicketNotFound

router = APIRouter(tags=["stop"])


def _get_principal(request: Request) -> Principal:
    return request.state.principal


def _is_multi_user(request: Request) -> bool:
    return getattr(request.app.state, "multi_user", False)


@router.post("/tickets/{ticket_id}/stop")
def stop_ticket(ticket_id: str, body: StopRequest, request: Request):
    store = request.app.state.store
    try:
        ticket = store.get_ticket(ticket_id)
    except TicketNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))

    require_write_access(_get_principal(request), ticket, _is_multi_user(request))

    if ticket.status in NON_DISPATCHABLE_STATUSES:
        kind = "terminal" if ticket.status in TERMINAL_STATUSES else "paused"
        raise HTTPException(
            status_code=409,
            detail=(
                f"Ticket {ticket_id} is in {kind} state"
                f" '{ticket.status.value}' — nothing to stop"
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

    require_write_access(_get_principal(request), ticket, _is_multi_user(request))

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
    principal = _get_principal(request)
    multi_user = _is_multi_user(request)
    if multi_user:
        require_admin(principal)

    store = request.app.state.store
    tickets = store.list_tickets()
    affected = []
    for ticket in tickets:
        if ticket.status in NON_DISPATCHABLE_STATUSES:
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
