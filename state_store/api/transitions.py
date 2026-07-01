from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..models import TransitionRequest
from ..store import InvalidTransition, TicketNotFound

router = APIRouter(prefix="/tickets", tags=["transitions"])


@router.post("/{ticket_id}/transition")
def transition_ticket(ticket_id: str, body: TransitionRequest, request: Request):
    store = request.app.state.store
    try:
        result = store.transition_ticket(ticket_id, body)
    except TicketNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except InvalidTransition as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Transition events are emitted by the caller (agent or
    # orchestrator) through their own EventBus, not here.  The
    # state store and orchestrator run as separate processes with
    # independent EventBus seq counters.  Emitting here caused
    # seq collisions that dropped agent events during merge and
    # made the UI miss transitions after the first poll batch.
    return result
