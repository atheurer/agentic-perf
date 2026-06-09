from __future__ import annotations

from fastapi import APIRouter, Query, Request

from ..models import TicketStatus

router = APIRouter(tags=["health"])


@router.get("/health")
def health(request: Request):
    store = request.app.state.store
    all_tickets = store.list_tickets()
    counts = {}
    for status in TicketStatus:
        counts[status.value] = sum(1 for t in all_tickets if t.status == status)
    return {"status": "ok", "ticket_counts": counts, "total": len(all_tickets)}


@router.get("/tickets/since/{seq}")
def tickets_since(seq: int, request: Request):
    store = request.app.state.store
    return store.get_tickets_since(seq)
