from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request

from ..auth import Principal, require_write_access
from ..models import (
    ClaimRequest,
    CreateTicketRequest,
    TicketStatus,
    UpdateFieldsRequest,
)
from ..store import TicketNotFound
from .action_hints import after_create

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tickets", tags=["tickets"])

# Custom fields stripped from list responses to reduce payload size.
# These are only needed by agents resuming work via the single-ticket
# GET endpoint, not by the browser list page or the orchestrator's
# dispatch/stop-request polling.
_HEAVY_FIELDS = {
    "previous_messages",
    "detailed_analysis",
    "review_summary",
    "execution_plan",
    "run_file_used",
    "validated_run_file",
    "host_inventory",
    "parsed_specs",
    "chart_data",
    "retrospective",
    "scoped_context",
    "hypothesis",
    "recommendations",
    "key_metrics",
    "configuration_applied",
    "follow_up_needed",
    "verdict",
}


def _strip_heavy_fields(ticket: dict) -> dict:
    cf = ticket.get("custom_fields")
    if cf and any(k in cf for k in _HEAVY_FIELDS):
        ticket = dict(ticket)
        ticket["custom_fields"] = {
            k: v for k, v in cf.items() if k not in _HEAVY_FIELDS
        }
    return ticket


def _get_store(request: Request):
    return request.app.state.store


def _get_principal(request: Request) -> Principal:
    return request.state.principal


def _is_multi_user(request: Request) -> bool:
    return getattr(request.app.state, "multi_user", False)


def _check_creation_quota(
    request: Request,
    created_by: str,
) -> str | None:
    """Advisory quota check at ticket creation.

    Returns a warning string if the user is over quota,
    None otherwise.  Never blocks creation in phase 1
    (warn-only).
    """
    if not created_by or not _is_multi_user(request):
        return None
    try:
        from providers.quota import (
            UsageLedger,
            check_user_quota,
            quota_from_config,
        )

        user_store = getattr(request.app.state, "user_store", None)
        if user_store is None:
            return None
        config = getattr(request.app.state, "config_raw", {})
        user = user_store.get_user(created_by)
        user_quota = user.llm_quota
        if user_quota is None:
            user_quota = quota_from_config(config)
        if user_quota is None:
            return None

        ledger = UsageLedger()
        result = check_user_quota(
            created_by,
            user_quota,
            None,
            ledger,
            is_service_account=user.service_account,
        )
        ledger.close()
        if result.exceeded:
            return "; ".join(result.reasons)
    except Exception:
        logger.debug("Quota check at creation failed", exc_info=True)
    return None


@router.post("")
def create_ticket(body: CreateTicketRequest, request: Request):
    store = _get_store(request)
    principal = _get_principal(request)
    multi_user = _is_multi_user(request)

    created_by = ""
    owners: list[str] = []

    if multi_user and principal.kind == "user":
        created_by = principal.username
        owners = list(body.owners) if body.owners else [principal.username]
        if principal.username not in owners:
            owners.append(principal.username)
    elif body.owners:
        owners = list(body.owners)

    # Advisory quota check at creation time.
    quota_warning = _check_creation_quota(request, created_by)

    ticket = store.create_ticket(
        body,
        created_by=created_by,
        owners=owners,
    )
    result = ticket.model_dump(mode="json")
    result["action_required"] = after_create(ticket)
    if quota_warning:
        result["quota_warning"] = quota_warning
    return result


_SUMMARY_FIELDS = ("id", "summary", "status", "owners", "created_at", "updated_at")

# Outcome-relevant custom_fields included in summary responses
# so the dashboard can render the Outcome column without fetching
# full ticket data.
_OUTCOME_FIELDS = {
    "verdict",
    "dedup_result",
    "synthesis_result",
    "evaluation_result",
    "analysis_result",
    "abort_requested",
    "stop_requested",
}


@router.get("")
def list_tickets(
    request: Request,
    status: TicketStatus | None = Query(None),
    exclude_status: TicketStatus | None = Query(None),
    fields: str | None = Query(None),
):
    store = _get_store(request)
    tickets = store.list_tickets(status=status)
    if exclude_status is not None:
        tickets = [t for t in tickets if t.status != exclude_status]
    if fields == "summary":
        result = []
        for t in tickets:
            d = t.model_dump(mode="json")
            row = {k: d[k] for k in _SUMMARY_FIELDS}
            cf = d.get("custom_fields", {})
            outcome_cf = {k: v for k, v in cf.items() if k in _OUTCOME_FIELDS}
            if outcome_cf:
                row["custom_fields"] = outcome_cf
            result.append(row)
        return result
    return [_strip_heavy_fields(t.model_dump(mode="json")) for t in tickets]


@router.get("/{ticket_id}")
def get_ticket(ticket_id: str, request: Request):
    store = _get_store(request)
    try:
        return store.get_ticket(ticket_id)
    except TicketNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.patch("/{ticket_id}/fields")
def update_fields(ticket_id: str, body: UpdateFieldsRequest, request: Request):
    store = _get_store(request)
    try:
        ticket = store.get_ticket(ticket_id)
    except TicketNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))

    require_write_access(_get_principal(request), ticket, _is_multi_user(request))

    return store.update_fields(ticket_id, body.fields)


@router.post("/{ticket_id}/claim")
def claim_ticket(ticket_id: str, body: ClaimRequest, request: Request):
    store = _get_store(request)
    try:
        result = store.claim_ticket(ticket_id, body.owner, body.duration_seconds)
    except TicketNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    if result is None:
        existing = store.get_ticket(ticket_id).custom_fields.get("claim", {})
        raise HTTPException(
            status_code=409,
            detail=f"Ticket already claimed by {existing.get('owner', 'unknown')}",
        )
    return result


@router.delete("/{ticket_id}")
def archive_ticket(ticket_id: str, request: Request):
    """Archive a closed ticket: remove from active memory and move files to archive dir."""
    store = _get_store(request)
    try:
        ticket = store.get_ticket(ticket_id)
    except TicketNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    require_write_access(_get_principal(request), ticket, _is_multi_user(request))
    try:
        result = store.archive_ticket(ticket_id)
    except TicketNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return result


@router.delete("/{ticket_id}/claim")
def release_claim(ticket_id: str, body: ClaimRequest, request: Request):
    store = _get_store(request)
    try:
        released = store.release_claim(ticket_id, body.owner)
    except TicketNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"released": released}


@router.post("/{ticket_id}/claim/renew")
def renew_claim(ticket_id: str, body: ClaimRequest, request: Request):
    store = _get_store(request)
    try:
        result = store.renew_claim(ticket_id, body.owner, body.duration_seconds)
    except TicketNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    if result is None:
        raise HTTPException(status_code=409, detail="Claim not owned by this owner")
    return result


@router.get("/since/{seq}")
def tickets_since(seq: int, request: Request):
    """Return tickets modified after the given sequence number."""
    store = _get_store(request)
    return store.get_tickets_since(seq)
