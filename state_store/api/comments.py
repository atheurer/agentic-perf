from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..auth import Principal, require_write_access
from ..models import AddCommentRequest
from ..store import TicketNotFound

router = APIRouter(prefix="/tickets", tags=["comments"])


def _get_principal(request: Request) -> Principal:
    return request.state.principal


def _is_multi_user(request: Request) -> bool:
    return getattr(request.app.state, "multi_user", False)


@router.post("/{ticket_id}/comments")
def add_comment(ticket_id: str, body: AddCommentRequest, request: Request):
    store = request.app.state.store
    try:
        ticket = store.get_ticket(ticket_id)
    except TicketNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))

    principal = _get_principal(request)
    multi_user = _is_multi_user(request)
    require_write_access(principal, ticket, multi_user)

    if multi_user and principal.kind == "user":
        body = AddCommentRequest(author=principal.username, body=body.body)

    return store.add_comment(ticket_id, body)


@router.get("/{ticket_id}/comments")
def list_comments(ticket_id: str, request: Request):
    store = request.app.state.store
    try:
        ticket = store.get_ticket(ticket_id)
        return ticket.comments
    except TicketNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
