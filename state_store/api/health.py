from __future__ import annotations

from fastapi import APIRouter, Request

from ..models import TERMINAL_STATUSES, TicketStatus

router = APIRouter(tags=["health"])


@router.get("/health")
def health(request: Request):
    store = request.app.state.store
    all_tickets = store.list_tickets()
    counts = {}
    for status in TicketStatus:
        counts[status.value] = sum(1 for t in all_tickets if t.status == status)
    return {
        "status": "ok",
        "ticket_counts": counts,
        "total": len(all_tickets),
        "terminal_statuses": [s.value for s in TERMINAL_STATUSES],
    }


@router.get("/tickets/since/{seq}")
def tickets_since(seq: int, request: Request):
    store = request.app.state.store
    return store.get_tickets_since(seq)
