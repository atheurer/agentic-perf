"""Authenticated service API for durable fenced operations."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..auth import Principal
from ..trace_store import (
    OperationConflictError,
    OperationLeaseError,
    OperationRecord,
    OperationTransitionError,
    TraceStoreWriteError,
)
from .traces import _service_principal

router = APIRouter(prefix="/traces/operations", tags=["traces"])


class RegisterRequest(BaseModel):
    operation_key: str = Field(min_length=1, max_length=512)
    request_hash: str = Field(min_length=1, max_length=256)


class LeaseRequest(RegisterRequest):
    ttl_seconds: float = Field(gt=0, le=86_400)


class FencedRequest(BaseModel):
    fencing_token: int = Field(ge=1)
    descriptor: dict[str, Any] | None = None
    external_ids: dict[str, Any] | None = None
    ttl_seconds: float | None = Field(default=None, gt=0, le=86_400)
    reconciliation_outcome: str = "indeterminate"


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, OperationConflictError):
        return HTTPException(409, str(exc))
    if isinstance(exc, (OperationLeaseError, OperationTransitionError)):
        return HTTPException(409, str(exc))
    return HTTPException(503, "trace store unavailable")


@router.post("/register")
async def register(
    body: RegisterRequest,
    request: Request,
    _: Annotated[Principal, Depends(_service_principal)],
) -> dict[str, Any]:
    try:
        record, existed = request.app.state.trace_store.register_or_get(
            OperationRecord(body.operation_key, body.request_hash, "registered")
        )
        return {
            "operation": record.__dict__,
            "status": "existing" if existed else "registered",
        }
    except (
        OperationConflictError,
        OperationLeaseError,
        OperationTransitionError,
        TraceStoreWriteError,
    ) as exc:
        raise _error(exc) from exc


@router.post("/acquire")
async def acquire(
    body: LeaseRequest,
    request: Request,
    principal: Annotated[Principal, Depends(_service_principal)],
) -> dict[str, Any]:
    try:
        record, status = request.app.state.trace_store.acquire_operation_result(
            body.operation_key, body.request_hash, principal.username, body.ttl_seconds
        )
        return {"operation": record.__dict__, "status": status}
    except (
        OperationConflictError,
        OperationLeaseError,
        OperationTransitionError,
        TraceStoreWriteError,
    ) as exc:
        raise _error(exc) from exc


@router.post("/{operation_key}/{action}")
async def transition(
    operation_key: str,
    action: str,
    body: FencedRequest,
    request: Request,
    principal: Annotated[Principal, Depends(_service_principal)],
) -> dict[str, Any]:
    store = request.app.state.trace_store
    try:
        if action == "renew":
            if body.ttl_seconds is None:
                raise HTTPException(422, "ttl_seconds is required for renew")
            record = store.renew_operation(
                operation_key, principal.username, body.fencing_token, body.ttl_seconds
            )
        elif action == "prepared":
            record = store.mark_prepared(
                operation_key, principal.username, body.fencing_token
            )
        elif action == "side-effect-started":
            record = store.mark_side_effect_started(
                operation_key, principal.username, body.fencing_token
            )
        elif action == "complete":
            record = store.complete(
                operation_key,
                principal.username,
                body.fencing_token,
                body.descriptor or {},
            )
        elif action == "fail":
            record = store.fail(
                operation_key,
                principal.username,
                body.fencing_token,
                body.descriptor or {},
            )
        elif action == "reject":
            record = store.reject(
                operation_key,
                principal.username,
                body.fencing_token,
                body.descriptor or {},
            )
        elif action == "indeterminate":
            record = store.mark_indeterminate(
                operation_key,
                principal.username,
                body.fencing_token,
                body.descriptor or {},
            )
        elif action == "reconcile":
            if body.reconciliation_outcome not in {
                "success",
                "failure",
                "rejected",
                "indeterminate",
            }:
                raise HTTPException(422, "invalid reconciliation outcome")
            record = store.reconcile(
                operation_key,
                principal.username,
                body.fencing_token,
                body.descriptor or {},
                body.reconciliation_outcome,
            )
        elif action == "external-id":
            record = store.attach_external_id(
                operation_key,
                principal.username,
                body.fencing_token,
                body.external_ids or {},
            )
        else:
            raise HTTPException(404, "unknown operation action")
        return {"operation": record.__dict__}
    except HTTPException:
        raise
    except (OperationLeaseError, OperationTransitionError, TraceStoreWriteError) as exc:
        raise _error(exc) from exc
