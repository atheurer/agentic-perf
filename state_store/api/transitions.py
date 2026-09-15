from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..auth import Principal, require_write_access
from ..models import TransitionRequest
from ..store import InvalidTransition, TicketNotFound

router = APIRouter(prefix="/tickets", tags=["transitions"])


def _get_principal(request: Request) -> Principal:
    return request.state.principal


def _is_multi_user(request: Request) -> bool:
    return getattr(request.app.state, "multi_user", False)


@router.post("/{ticket_id}/transition")
def transition_ticket(ticket_id: str, body: TransitionRequest, request: Request):
    store = request.app.state.store
    try:
        ticket = store.get_ticket(ticket_id)
    except TicketNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))

    require_write_access(_get_principal(request), ticket, _is_multi_user(request))

    try:
        result = store.transition_ticket(ticket_id, body)
    except TicketNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except InvalidTransition as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result
