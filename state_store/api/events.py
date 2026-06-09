from __future__ import annotations

from fastapi import APIRouter, Query, Request

router = APIRouter(prefix="/tickets", tags=["events"])


@router.get("/{ticket_id}/events")
def get_events(
    ticket_id: str,
    request: Request,
    since: int = Query(0, description="Return events with seq > this value"),
    limit: int = Query(200, description="Max events to return"),
):
    event_bus = getattr(request.app.state, "event_bus", None)
    if event_bus is None:
        return {"events": [], "latest_seq": 0}
    events = event_bus.get_events(ticket_id, since=since, limit=limit)
    latest_seq = events[-1]["seq"] if events else since
    return {"events": events, "latest_seq": latest_seq}
