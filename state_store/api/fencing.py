from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, Request


def mutation_fence(request: Request) -> tuple[UUID | None, int | None]:
    """Read optional internal fencing headers; absent headers preserve user writes."""
    raw_session = request.headers.get("X-Agentic-Perf-Orchestrator-Session")
    raw_epoch = request.headers.get("X-Agentic-Perf-Orchestrator-Epoch")
    if not raw_session and not raw_epoch:
        return None, None
    try:
        if not raw_session or not raw_epoch:
            raise ValueError
        return UUID(raw_session), int(raw_epoch)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail={"reason": "not_leader", "message": "invalid mutation fence"},
        ) from exc
