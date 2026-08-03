from __future__ import annotations

from fastapi import APIRouter, Query, Request

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("")
def get_audit_entries(
    request: Request,
    ticket_id: str | None = Query(None),
    since: int = Query(0),
    limit: int = Query(200, le=1000),
):
    audit_log = getattr(request.app.state, "audit_log", None)
    if audit_log is None:
        return {"entries": [], "latest_seq": 0}
    entries = audit_log.read(ticket_id=ticket_id, since=since, limit=limit)
    return {"entries": entries, "latest_seq": audit_log.latest_seq}
