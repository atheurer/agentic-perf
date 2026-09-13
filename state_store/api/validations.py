"""Immutable, versioned benchmark-validation records."""

from __future__ import annotations

import hashlib
import json
import secrets
import time

from fastapi import APIRouter, HTTPException, Request

from ..auth import require_write_access
from ..models import CreateValidationRequest, SupersedeValidationRequest
from ..store import TicketNotFound

router = APIRouter(prefix="/tickets/{ticket_id}/validations", tags=["validations"])


def _store(request: Request):
    return request.app.state.store


@router.post("/capability")
def issue_capability(ticket_id: str, request: Request):
    """Issue a one-time, short-lived capability bound to this MCP invocation."""
    seed = request.headers.get("X-Agentic-Perf-Benchmark-Validator", "")
    if request.state.principal.kind != "service" or not secrets.compare_digest(
        seed, request.app.state.benchmark_validator_token
    ):
        raise HTTPException(
            status_code=403, detail="benchmark validator capability required"
        )
    agent = request.headers.get("X-Agentic-Perf-Agent-Id", "")
    invocation = request.headers.get("X-Agentic-Perf-Invocation-Id", "")
    action = request.headers.get("X-Agentic-Perf-Action-Id", "")
    if agent != "benchmark" or not invocation or not action:
        raise HTTPException(
            status_code=403, detail="benchmark invocation identity and action required"
        )
    nonce = secrets.token_urlsafe(24)
    request.app.state.benchmark_validation_capabilities[nonce] = {
        "ticket_id": ticket_id,
        "agent": agent,
        "invocation": invocation,
        "action": action,
        "session": request.headers.get("X-Agentic-Perf-Session-Id", ""),
        "epoch": request.headers.get("X-Agentic-Perf-Session-Epoch", ""),
        "request": request.headers.get("X-Agentic-Perf-Request-Id", ""),
        "expires_at": time.monotonic() + 60,
    }
    _store(request)._audit_log(
        "issue_validation_capability",
        ticket_id,
        {"agent": agent, "invocation": invocation},
    )
    return {"capability": nonce, "expires_in_seconds": 60}


@router.get("/{validation_id}")
def get_validation(ticket_id: str, validation_id: str, request: Request):
    try:
        ticket = _store(request).get_ticket(ticket_id)
    except TicketNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    require_write_access(request.state.principal, ticket, request.app.state.multi_user)
    manifest = ticket.custom_fields.get("benchmark_validations", {})
    records = manifest.get("records", {}) if isinstance(manifest, dict) else {}
    record = records.get(validation_id)
    if not isinstance(record, dict) or record.get("record_type") != "validation":
        raise HTTPException(status_code=404, detail="unknown validation_id")
    supersession = next(
        (
            item
            for item in records.values()
            if isinstance(item, dict)
            and item.get("record_type") == "supersession"
            and item.get("supersedes_validation_id") == validation_id
        ),
        None,
    )
    _store(request)._trace_mutation(
        ticket_id, "read_validation", attributes={"validation_id": validation_id}
    )
    return {
        "record": record,
        "version": manifest.get("version", 0),
        "active_validation_id": manifest.get("active_validation_id"),
        "supersession": supersession,
    }


@router.post("")
def create_validation(ticket_id: str, body: CreateValidationRequest, request: Request):
    if request.state.principal.kind != "service":
        raise HTTPException(
            status_code=403,
            detail="validation creation requires a bound, unexpired capability",
        )
    capability = request.headers.get("X-Agentic-Perf-Validation-Capability", "")
    grants = request.app.state.benchmark_validation_capabilities
    grant = grants.get(capability)
    identity = {
        "action": request.headers.get("X-Agentic-Perf-Action-Id", ""),
        "session": request.headers.get("X-Agentic-Perf-Session-Id", ""),
        "epoch": request.headers.get("X-Agentic-Perf-Session-Epoch", ""),
        "request": request.headers.get("X-Agentic-Perf-Request-Id", ""),
    }
    if (
        not grant
        or grant["expires_at"] < time.monotonic()
        or grant["ticket_id"] != ticket_id
        or grant["agent"] != request.headers.get("X-Agentic-Perf-Agent-Id", "")
        or grant["invocation"]
        != request.headers.get("X-Agentic-Perf-Invocation-Id", "")
        or not identity["action"]
        or grant["action"] != identity["action"]
        or any(grant[key] != value for key, value in identity.items() if grant[key])
    ):
        raise HTTPException(
            status_code=403,
            detail="validation creation requires a bound, unexpired capability",
        )
    creator = body.record.creator
    creator_bindings = {
        "action": creator.get("action_id", ""),
        "session": creator.get("session_id", "") or creator.get("mcp_session_id", ""),
        "epoch": creator.get("epoch", "") or creator.get("session_epoch", ""),
        "request": creator.get("request_id", ""),
    }
    if any(
        creator_bindings[key] and creator_bindings[key] != grant[key]
        for key in creator_bindings
    ):
        raise HTTPException(
            status_code=403,
            detail="validation creator identity does not match capability",
        )
    # Consume only after every capability binding has been checked.  A rejected
    # cross-ticket/action request must not burn the caller's valid capability.
    grants.pop(capability, None)
    canonical_runfile_digest = hashlib.sha256(
        json.dumps(body.record.run_file, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if body.record.runfile_fingerprint != canonical_runfile_digest:
        raise HTTPException(
            status_code=422, detail="runfile fingerprint does not match"
        )
    store = _store(request)
    try:
        ticket = store.get_ticket(ticket_id)
    except TicketNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    require_write_access(request.state.principal, ticket, request.app.state.multi_user)
    try:
        updated, conflict = store.create_validation(
            ticket_id, body.record.model_dump(mode="json"), body.expected_version
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if conflict:
        raise HTTPException(status_code=409, detail=conflict)
    manifest = updated.custom_fields["benchmark_validations"]
    return {
        "record": manifest["records"][body.record.validation_id],
        "version": manifest["version"],
        "active_validation_id": manifest["active_validation_id"],
    }


@router.post("/{validation_id}/supersede")
def supersede_validation(
    ticket_id: str,
    validation_id: str,
    body: SupersedeValidationRequest,
    request: Request,
):
    if body.validation_id != validation_id:
        raise HTTPException(status_code=422, detail="validation_id path/body mismatch")
    store = _store(request)
    try:
        ticket = store.get_ticket(ticket_id)
    except TicketNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    require_write_access(request.state.principal, ticket, request.app.state.multi_user)
    try:
        updated, conflict = store.supersede_validation(
            ticket_id,
            validation_id,
            body.replacement_validation_id,
            body.reason.value,
            body.expected_version,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if conflict:
        raise HTTPException(status_code=409, detail=conflict)
    manifest = updated.custom_fields["benchmark_validations"]
    return {
        "version": manifest["version"],
        "active_validation_id": manifest["active_validation_id"],
    }
