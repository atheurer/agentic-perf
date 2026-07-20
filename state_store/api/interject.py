from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from state_store.auth import require_write_access
from state_store.models import (
    PAUSED_STATUSES,
    TERMINAL_STATUSES,
    AddCommentRequest,
)
from state_store.store import TicketNotFound

router = APIRouter(prefix="/tickets", tags=["interject"])


class InterjectRequest(BaseModel):
    message: str


@router.post("/{ticket_id}/interject")
def interject(
    ticket_id: str,
    body: InterjectRequest,
    request: Request,
) -> JSONResponse:
    store = request.app.state.store

    try:
        ticket = store.get_ticket(ticket_id)
    except TicketNotFound:
        return JSONResponse(
            status_code=404,
            content={"detail": f"Ticket {ticket_id} not found"},
        )

    principal = request.state.principal
    multi_user = getattr(request.app.state, "multi_user", False)
    require_write_access(principal, ticket, multi_user)

    if ticket.status in TERMINAL_STATUSES:
        return JSONResponse(
            status_code=409,
            content={
                "detail": (
                    f"Ticket {ticket_id} is in terminal status '{ticket.status.value}'"
                ),
            },
        )

    if ticket.status in PAUSED_STATUSES:
        return JSONResponse(
            status_code=409,
            content={
                "detail": (
                    f"Ticket {ticket_id} is in"
                    f" '{ticket.status.value}'. Use the HITL"
                    f" reply flow (POST comment + POST"
                    f" transition) instead of interject."
                ),
            },
        )

    store.add_comment(
        ticket_id,
        AddCommentRequest(author="user", body=body.message),
    )

    store.update_fields(
        ticket_id,
        {
            "pending_interject": {
                "message": body.message,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        },
    )

    return JSONResponse(
        status_code=200,
        content={"status": "queued", "ticket_id": ticket_id},
    )
