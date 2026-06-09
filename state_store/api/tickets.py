from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from ..models import CreateTicketRequest, TicketStatus, UpdateFieldsRequest
from ..store import TicketNotFound

router = APIRouter(prefix="/tickets", tags=["tickets"])


def _get_store(request: Request):
    return request.app.state.store


@router.post("")
def create_ticket(body: CreateTicketRequest, request: Request):
    store = _get_store(request)
    ticket = store.create_ticket(body)
    return ticket


@router.get("")
def list_tickets(request: Request, status: TicketStatus | None = Query(None)):
    store = _get_store(request)
    return store.list_tickets(status=status)


@router.get("/{ticket_id}")
def get_ticket(ticket_id: str, request: Request):
    store = _get_store(request)
    try:
        return store.get_ticket(ticket_id)
    except TicketNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.patch("/{ticket_id}/fields")
def update_fields(ticket_id: str, body: UpdateFieldsRequest, request: Request):
    store = _get_store(request)
    try:
        return store.update_fields(ticket_id, body.fields)
    except TicketNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
