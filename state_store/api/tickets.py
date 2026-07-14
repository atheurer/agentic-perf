from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from ..models import (
    ClaimRequest,
    CreateTicketRequest,
    TicketStatus,
    UpdateFieldsRequest,
)
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


@router.post("/{ticket_id}/claim")
def claim_ticket(ticket_id: str, body: ClaimRequest, request: Request):
    store = _get_store(request)
    try:
        result = store.claim_ticket(ticket_id, body.owner, body.duration_seconds)
    except TicketNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    if result is None:
        existing = store.get_ticket(ticket_id).custom_fields.get("claim", {})
        raise HTTPException(
            status_code=409,
            detail=f"Ticket already claimed by {existing.get('owner', 'unknown')}",
        )
    return result


@router.delete("/{ticket_id}/claim")
def release_claim(ticket_id: str, body: ClaimRequest, request: Request):
    store = _get_store(request)
    try:
        released = store.release_claim(ticket_id, body.owner)
    except TicketNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"released": released}


@router.post("/{ticket_id}/claim/renew")
def renew_claim(ticket_id: str, body: ClaimRequest, request: Request):
    store = _get_store(request)
    try:
        result = store.renew_claim(ticket_id, body.owner, body.duration_seconds)
    except TicketNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    if result is None:
        raise HTTPException(status_code=409, detail="Claim not owned by this owner")
    return result
