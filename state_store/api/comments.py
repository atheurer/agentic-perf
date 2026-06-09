from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..models import AddCommentRequest
from ..store import TicketNotFound

router = APIRouter(prefix="/tickets", tags=["comments"])


@router.post("/{ticket_id}/comments")
def add_comment(ticket_id: str, body: AddCommentRequest, request: Request):
    store = request.app.state.store
    try:
        return store.add_comment(ticket_id, body)
    except TicketNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{ticket_id}/comments")
def list_comments(ticket_id: str, request: Request):
    store = request.app.state.store
    try:
        ticket = store.get_ticket(ticket_id)
        return ticket.comments
    except TicketNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
