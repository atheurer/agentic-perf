from __future__ import annotations

import time

from fastapi import APIRouter, Request

from paths import TRACE_SPOOL_DIR

from ..models import TERMINAL_STATUSES, TicketStatus

router = APIRouter(tags=["health"])


@router.get("/health")
def health(request: Request):
    store = request.app.state.store
    all_tickets = store.list_tickets()
    counts = {}
    for status in TicketStatus:
        counts[status.value] = sum(1 for t in all_tickets if t.status == status)
    spool_bytes = 0
    oldest = None
    quarantined = 0
    try:
        if TRACE_SPOOL_DIR.is_dir() and not TRACE_SPOOL_DIR.is_symlink():
            for item in TRACE_SPOOL_DIR.iterdir():
                if item.is_symlink() or not item.is_file():
                    continue
                if item.name.endswith(".spool"):
                    stat = item.stat()
                    spool_bytes += stat.st_size
                    age = time.time() - stat.st_mtime
                    oldest = age if oldest is None else max(oldest, age)
                elif item.name.endswith(".bad"):
                    quarantined += 1
    except OSError:
        pass
    return {
        "status": "ok",
        "ticket_counts": counts,
        "total": len(all_tickets),
        "terminal_statuses": [s.value for s in TERMINAL_STATUSES],
        "trace": {
            **getattr(request.app.state, "trace_health", {}),
            "spool_bytes": spool_bytes,
            "oldest_unacked_age_seconds": oldest,
            "quarantined_frames": quarantined,
        },
    }
