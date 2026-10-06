from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, Request


def mutation_fence(
    request: Request,
) -> tuple[UUID | None, int | None, str | None, bool]:
    """Read optional internal fencing headers; absent headers preserve user writes."""
    raw_session = request.headers.get("X-Agentic-Perf-Orchestrator-Session")
    raw_epoch = request.headers.get("X-Agentic-Perf-Orchestrator-Epoch")
    raw_claim = request.headers.get("X-Agentic-Perf-Claim-Id")
    scope = request.headers.get("X-Agentic-Perf-Mutation-Scope")
    if all(value is None for value in (raw_session, raw_epoch, raw_claim, scope)):
        return None, None, None, False
    try:
        leader_only = scope == "leader"
        if (
            not raw_session
            or not raw_epoch
            or scope not in (None, "leader")
            or (leader_only and raw_claim)
            or (not leader_only and not raw_claim)
        ):
            raise ValueError
        identity = UUID(raw_session), int(raw_epoch), raw_claim, leader_only
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail={"reason": "not_leader", "message": "invalid mutation fence"},
        ) from exc
    if leader_only and request.state.principal.kind != "service":
        raise HTTPException(
            status_code=403, detail="Leader recovery requires service auth"
        )
    return identity
