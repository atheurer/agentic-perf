"""Immutable, versioned benchmark-validation records."""

from __future__ import annotations

import hashlib
import json
import secrets

from fastapi import APIRouter, HTTPException, Request

from ..auth import require_write_access
from ..models import CreateValidationRequest, SupersedeValidationRequest
from ..store import TicketNotFound

router = APIRouter(prefix="/tickets/{ticket_id}/validations", tags=["validations"])


def _store(request: Request):
    return request.app.state.store


@router.get("/{validation_id}")
def get_validation(ticket_id: str, validation_id: str, request: Request):
    try:
        ticket = _store(request).get_ticket(ticket_id)
    except TicketNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
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
    capability = request.headers.get("X-Agentic-Perf-Benchmark-Validator", "")
    if request.state.principal.kind != "service" or not secrets.compare_digest(
        capability, request.app.state.benchmark_validator_token
    ):
        raise HTTPException(
            status_code=403,
            detail="validation creation requires the benchmark validator capability",
        )
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
