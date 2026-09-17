from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..auth import Principal, require_write_access
from ..models import AddCommentRequest, ResolveApprovalRequest
from ..store import ClaimFenceError, TicketNotFound
from .action_hints import after_comment, approval_decision
from .fencing import mutation_fence

router = APIRouter(prefix="/tickets", tags=["comments"])


def _get_principal(request: Request) -> Principal:
    return request.state.principal


def _is_multi_user(request: Request) -> bool:
    return getattr(request.app.state, "multi_user", False)


@router.post("/{ticket_id}/comments")
def add_comment(ticket_id: str, body: AddCommentRequest, request: Request):
    store = request.app.state.store
    try:
        ticket = store.get_ticket(ticket_id)
    except TicketNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))

    principal = _get_principal(request)
    multi_user = _is_multi_user(request)
    require_write_access(principal, ticket, multi_user)

    if multi_user and principal.kind == "user":
        body = AddCommentRequest(author=principal.username, body=body.body)

    # Approval comments are capabilities, not generic guidance.  Resolve the
    # one pending immutable benchmark request before adding a comment or
    # returning the normal resume hint; otherwise the UI's follow-up
    # transition would cancel the request.
    decision = None
    if body.author not in {
        "system",
        "orchestrator",
        "triage-agent",
        "resource-agent",
        "platform-agent",
        "provisioning-agent",
        "benchmark-agent",
        "review-agent",
        "teardown-agent",
    }:
        decision = approval_decision(body.body)
    if decision:
        pending = [
            item
            for item in store.list_approval_requests(ticket_id)
            if item.status == "pending"
        ]
        if len(pending) == 1:
            try:
                approval = store.resolve_approval_request(
                    ticket_id,
                    pending[0].approval_request_id,
                    ResolveApprovalRequest(decision=decision, comment=body.body),
                    resolved_by=body.author,
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            resolved_ticket = store.get_ticket(ticket_id)
            resolved_comment = next(
                comment
                for comment in resolved_ticket.comments
                if comment.id == approval.resolution_comment_id
            )
            result = resolved_comment.model_dump(mode="json")
            result["approval_resolved"] = approval.model_dump(mode="json")
            result["action_required"] = None
            return result

    session_id, epoch, claim_id = mutation_fence(request)
    try:
        comment = store.add_comment(
            ticket_id,
            body,
            session_id=session_id,
            epoch=epoch,
            claim_id=claim_id,
        )
    except ClaimFenceError as e:
        raise HTTPException(
            status_code=409,
            detail={"reason": e.reason, "message": str(e)},
        ) from e
    result = comment.model_dump(mode="json")

    ticket = store.get_ticket(ticket_id)
    hint = after_comment(ticket, body.author)
    result["action_required"] = hint

    return result


@router.get("/{ticket_id}/comments")
def list_comments(ticket_id: str, request: Request):
    store = request.app.state.store
    try:
        ticket = store.get_ticket(ticket_id)
        return ticket.comments
    except TicketNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
