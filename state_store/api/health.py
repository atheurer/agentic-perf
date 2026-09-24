from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request

from paths import TRACE_SPOOL_DIR

from ..models import TERMINAL_STATUSES, TicketStatus

router = APIRouter(tags=["health"])


@router.get("/health")
def health(request: Request):
    store = request.app.state.store
    lease = store.get_orchestrator_lease()
    counts = store.count_by_status()
    # Ensure all statuses are present (including zero counts)
    for status in TicketStatus:
        counts.setdefault(status.value, 0)
    total = sum(counts.values())
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
        "total": total,
        "terminal_statuses": [s.value for s in TERMINAL_STATUSES],
        "trace": {
            **getattr(request.app.state, "trace_health", {}),
            "spool_bytes": spool_bytes,
            "oldest_unacked_age_seconds": oldest,
            "quarantined_frames": quarantined,
        },
        # Public health reports only liveness.  Holder identity and fencing
        # metadata belong behind the authenticated control endpoint.
        "orchestrator_lease": {"active": lease is not None},
    }


async def _require_authenticated(request: Request):
    principal = await request.app.state.auth_dependency(request)
    if principal.kind == "anonymous":
        raise HTTPException(status_code=401, detail="Authentication required")
    return principal


@router.get("/diagnostics", dependencies=[Depends(_require_authenticated)])
def diagnostics(request: Request):
    """Authenticated operator diagnostics; do not add these fields to health."""
    return dict(request.app.state.store_diagnostics)
