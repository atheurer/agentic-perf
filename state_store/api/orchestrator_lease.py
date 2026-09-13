from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from ..auth import Principal
from ..models import (
    AcquireOrchestratorLeaseRequest,
    ReleaseOrchestratorLeaseRequest,
    RenewOrchestratorLeaseRequest,
)
from ..store import OrchestratorLeaseHeld

router = APIRouter(prefix="/control/orchestrator-lease", tags=["control-plane"])


async def _service_or_admin(request: Request) -> Principal:
    """Only control-plane services or explicitly privileged operators may fence."""
    principal = getattr(request.state, "principal", None)
    if principal is None or (principal.kind != "service" and not principal.is_admin):
        raise HTTPException(
            status_code=403,
            detail="orchestrator lease requires service or admin authentication",
        )
    return principal


def _store(request: Request):
    return request.app.state.store


@router.get("", dependencies=[Depends(_service_or_admin)])
def inspect_lease(request: Request):
    lease = _store(request).get_orchestrator_lease()
    return {"lease": lease.model_dump(mode="json") if lease else None}


@router.post("/acquire", status_code=200, dependencies=[Depends(_service_or_admin)])
def acquire_lease(body: AcquireOrchestratorLeaseRequest, request: Request):
    try:
        lease = _store(request).acquire_orchestrator_lease(body)
    except OrchestratorLeaseHeld as exc:
        holder = exc.holder.model_dump(mode="json")
        # Never return session credentials to a competing process.
        holder["session_id"] = "redacted"
        raise HTTPException(
            status_code=409,
            detail={
                "error": "lease_held",
                "holder": holder,
                "remaining_seconds": exc.remaining_seconds,
            },
        ) from exc
    return lease.model_dump(mode="json")


@router.post("/renew", dependencies=[Depends(_service_or_admin)])
def renew_lease(body: RenewOrchestratorLeaseRequest, request: Request):
    try:
        lease = _store(request).renew_orchestrator_lease(
            body.session_id, body.epoch, body.ttl_seconds
        )
    except PermissionError as exc:
        raise HTTPException(
            status_code=409, detail="lease is not owned or has expired"
        ) from exc
    return lease.model_dump(mode="json")


@router.post("/release", dependencies=[Depends(_service_or_admin)])
def release_lease(body: ReleaseOrchestratorLeaseRequest, request: Request):
    return {
        "released": _store(request).release_orchestrator_lease(
            body.session_id, body.epoch
        )
    }
