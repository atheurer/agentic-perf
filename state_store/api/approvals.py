"""Durable, intent-bound benchmark approval requests."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..auth import require_write_access
from ..models import (
    ConsumeApprovalRequest,
    CreateApprovalRequest,
    ResolveApprovalRequest,
)
from ..store import TicketNotFound

router = APIRouter(prefix="/tickets/{ticket_id}/approvals", tags=["approvals"])


def _ticket(request: Request, ticket_id: str):
    try:
        return request.app.state.store.get_ticket(ticket_id)
    except TicketNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("")
def list_approvals(ticket_id: str, request: Request):
    ticket = _ticket(request, ticket_id)
    require_write_access(request.state.principal, ticket, request.app.state.multi_user)
    return {
        "approvals": [
            item.model_dump(mode="json")
            for item in request.app.state.store.list_approval_requests(ticket_id)
        ]
    }


@router.post("")
def create_approval(ticket_id: str, body: CreateApprovalRequest, request: Request):
    ticket = _ticket(request, ticket_id)
    principal = request.state.principal
    require_write_access(principal, ticket, request.app.state.multi_user)
    if principal.kind != "service" and not principal.is_admin:
        raise HTTPException(
            status_code=403, detail="approval creation requires service"
        )
    required = {
        "invocation_id": body.invocation_id,
        "tool_call_id": body.tool_call_id,
        "session_id": body.session_id,
        "session_epoch": body.session_epoch,
        "waiter_owner": body.waiter_owner,
        "ticket_attempt": body.ticket_attempt,
        "claim_id": body.claim_id,
    }
    if any(
        not isinstance(value, str) or not value.strip() for value in required.values()
    ):
        raise HTTPException(
            status_code=422,
            detail="approval creation requires invocation, tool, session, epoch, waiter, and claim identity",
        )
    header_pairs = {
        "session_id": request.headers.get("X-Agentic-Perf-Orchestrator-Session"),
        "session_epoch": request.headers.get("X-Agentic-Perf-Orchestrator-Epoch"),
        "claim_id": request.headers.get("X-Agentic-Perf-Claim-Id"),
        "invocation_id": request.headers.get("X-Agentic-Perf-Invocation-Id"),
        "tool_call_id": request.headers.get("X-Agentic-Perf-Tool-Call-Id"),
    }
    if any(value is None or not value.strip() for value in header_pairs.values()):
        raise HTTPException(
            status_code=422, detail="approval trusted fence context is required"
        )
    if body.waiter_owner != body.invocation_id:
        raise HTTPException(
            status_code=409, detail="approval waiter must be the invocation identity"
        )
    for field, header_value in header_pairs.items():
        if header_value != required[field]:
            raise HTTPException(
                status_code=409,
                detail=f"approval {field} does not match trusted context",
            )
    try:
        approval = request.app.state.store.create_approval_request(
            ticket_id, body, created_by=principal.username
        )
    except (TicketNotFound, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return approval.model_dump(mode="json")


@router.post("/{approval_request_id}/resolve")
def resolve_approval(
    ticket_id: str,
    approval_request_id: str,
    body: ResolveApprovalRequest,
    request: Request,
):
    ticket = _ticket(request, ticket_id)
    principal = request.state.principal
    require_write_access(principal, ticket, request.app.state.multi_user)
    try:
        approval = request.app.state.store.resolve_approval_request(
            ticket_id,
            approval_request_id,
            body,
            resolved_by=principal.username,
        )
    except TicketNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return approval.model_dump(mode="json")


@router.post("/{approval_request_id}/consume")
def consume_approval(
    ticket_id: str,
    approval_request_id: str,
    body: ConsumeApprovalRequest,
    request: Request,
):
    ticket = _ticket(request, ticket_id)
    principal = request.state.principal
    require_write_access(principal, ticket, request.app.state.multi_user)
    if principal.kind != "service" and not principal.is_admin:
        raise HTTPException(
            status_code=403, detail="approval consumption requires service"
        )
    try:
        approval = request.app.state.store.consume_approval_request(
            ticket_id,
            approval_request_id,
            body,
            consumed_by=principal.username,
        )
    except TicketNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return approval.model_dump(mode="json")
