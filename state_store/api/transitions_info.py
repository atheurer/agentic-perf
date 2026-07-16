from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from state_store.models import (
    VALID_TRANSITIONS,
    TicketStatus,
)
from state_store.store import TicketNotFound

router = APIRouter(prefix="/tickets", tags=["transitions"])


@router.get("/{ticket_id}/transitions")
def get_valid_transitions(
    ticket_id: str,
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

    current = ticket.status
    valid = VALID_TRANSITIONS.get(current, [])
    valid_values = [s.value for s in valid]

    if current == TicketStatus.AWAITING_CUSTOMER_GUIDANCE:
        if ticket.previous_status is not None:
            valid_values = [ticket.previous_status.value]
        else:
            valid_values = []

    return JSONResponse(
        status_code=200,
        content={
            "current": current.value,
            "valid": valid_values,
        },
    )
