"""Service-only ingestion for immutable trace envelopes."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from paths import get_instance_name
from providers.tracing import TraceEventV1
from providers.tracing.payloads import PayloadBlobStore, PayloadStorageError
from providers.tracing.query import (
    TraceQuery,
    diagnostics,
    export_events,
    export_manifest,
    query_events,
)

from ..auth import Principal
from ..trace_store import TraceEventConflictError, TraceStoreWriteError

router = APIRouter(prefix="/traces/events", tags=["traces"])
query_router = APIRouter(prefix="/traces", tags=["traces"])


class TraceBatch(BaseModel):
    events: list[TraceEventV1] = Field(min_length=1, max_length=500)


def _authorize_query(request: Request, ticket_id: str | None) -> bool:
    """Apply the same ownership boundary as ticket mutations to trace reads."""
    principal = getattr(request.state, "principal", None)
    if principal is None or principal.kind == "anonymous":
        raise HTTPException(
            status_code=403, detail="trace query requires authentication"
        )
    if principal.kind == "service" or principal.is_admin or not ticket_id:
        if not ticket_id and principal.kind == "user" and not principal.is_admin:
            raise HTTPException(
                status_code=403, detail="ticket_id is required for user trace queries"
            )
        return True
    if not getattr(request.app.state, "multi_user", False):
        return True
    try:
        ticket = request.app.state.store.get_ticket(ticket_id)
    except Exception as exc:
        # Do not reveal whether an inaccessible ticket exists.
        raise HTTPException(status_code=404, detail="ticket not found") from exc
    owners = getattr(ticket, "owners", [])
    if owners and principal.username not in owners:
        raise HTTPException(
            status_code=403, detail="trace access requires ticket ownership"
        )
    return False


def _event_json(event: TraceEventV1, detailed: bool) -> dict[str, object]:
    data = event.model_dump(mode="json")
    if not detailed:
        data["input"] = None
        data["output"] = None
        data["attributes"] = None
    return data


def _query_from_params(
    *,
    ticket_id: str | None,
    trace_id: str | None,
    invocation_id: str | None,
    action_id: str | None,
    parent_action_id: str | None,
    action_type: str | None,
    lifecycle_state: str | None,
    outcome: str | None,
    producer_component: str | None,
    retry_kind: str | None,
    idempotency_outcome: str | None,
    since: datetime | None,
    until: datetime | None,
    causal: bool,
    limit: int,
    cursor: int,
) -> TraceQuery:
    return TraceQuery(
        ticket_id=ticket_id,
        trace_id=trace_id,
        invocation_id=invocation_id,
        action_id=action_id,
        parent_action_id=parent_action_id,
        action_type=action_type,
        lifecycle_state=lifecycle_state,
        outcome=outcome,
        producer_component=producer_component,
        retry_kind=retry_kind,
        idempotency_outcome=idempotency_outcome,
        since=since,
        until=until,
        causal=causal,
        limit=limit,
        cursor=cursor,
    )


@query_router.get("/query")
def query(
    request: Request,
    ticket_id: str | None = None,
    trace_id: str | None = None,
    invocation_id: str | None = None,
    action_id: str | None = None,
    parent_action_id: str | None = None,
    action_type: str | None = None,
    lifecycle_state: str | None = None,
    outcome: str | None = None,
    retry_kind: str | None = None,
    idempotency_outcome: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    producer_component: str | None = None,
    causal: bool = False,
    limit: int = Query(default=1000, ge=1, le=10000),
    cursor: int = Query(default=0, ge=0),
    include_payloads: bool = False,
) -> dict[str, object]:
    detailed = _authorize_query(request, ticket_id)
    audit_log = getattr(request.app.state, "audit_log", None)
    if audit_log is not None:
        audit_log.log(
            "trace_query", ticket_id or "*", {"detailed": detailed, "causal": causal}
        )
    selected = query_events(
        request.app.state.trace_store.list_events(),
        _query_from_params(
            ticket_id=ticket_id,
            trace_id=trace_id,
            invocation_id=invocation_id,
            action_id=action_id,
            parent_action_id=parent_action_id,
            action_type=action_type,
            lifecycle_state=lifecycle_state,
            outcome=outcome,
            producer_component=producer_component,
            retry_kind=retry_kind,
            idempotency_outcome=idempotency_outcome,
            since=since,
            until=until,
            causal=causal,
            limit=limit,
            cursor=cursor,
        ),
    )
    return {
        "events": [
            _event_json(event, detailed and include_payloads) for event in selected
        ],
        "count": len(selected),
        "next_cursor": selected[-1].global_seq if selected else None,
        "diagnostics": diagnostics(selected) if causal else {},
    }


@query_router.get("/export")
def export(
    request: Request,
    format: Literal["json", "jsonl", "csv"] = "json",
    ticket_id: str | None = None,
    trace_id: str | None = None,
    invocation_id: str | None = None,
    action_id: str | None = None,
    parent_action_id: str | None = None,
    action_type: str | None = None,
    lifecycle_state: str | None = None,
    outcome: str | None = None,
    producer_component: str | None = None,
    retry_kind: str | None = None,
    idempotency_outcome: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    causal: bool = False,
    limit: int = Query(default=10000, ge=1, le=10000),
    cursor: int = Query(default=0, ge=0),
    include_payloads: bool = False,
    manifest: bool = False,
) -> Response:
    detailed = _authorize_query(request, ticket_id)
    audit_log = getattr(request.app.state, "audit_log", None)
    if audit_log is not None:
        audit_log.log(
            "trace_export", ticket_id or "*", {"format": format, "detailed": detailed}
        )
    selected = query_events(
        request.app.state.trace_store.list_events(),
        TraceQuery(
            ticket_id=ticket_id,
            trace_id=trace_id,
            invocation_id=invocation_id,
            action_id=action_id,
            parent_action_id=parent_action_id,
            action_type=action_type,
            lifecycle_state=lifecycle_state,
            outcome=outcome,
            producer_component=producer_component,
            retry_kind=retry_kind,
            idempotency_outcome=idempotency_outcome,
            since=since,
            until=until,
            causal=causal,
            limit=limit,
            cursor=cursor,
        ),
    )
    media = (
        "text/csv"
        if format == "csv"
        else ("application/x-ndjson" if format == "jsonl" else "application/json")
    )
    if not detailed or not include_payloads:
        selected = [
            event.model_copy(update={"input": None, "output": None, "attributes": None})
            for event in selected
        ]
    content = export_events(selected, format)
    if manifest and format == "jsonl":
        content += (
            json.dumps(
                {"_manifest": export_manifest(selected, content)}, separators=(",", ":")
            )
            + "\n"
        )
    response = Response(content, media_type=media)
    response.headers["X-Trace-Manifest"] = json.dumps(
        export_manifest(selected, content), separators=(",", ":")
    )
    return response


@query_router.get("/tickets/{ticket_id}")
def ticket_trace(
    ticket_id: str,
    request: Request,
    trace_id: str | None = None,
    invocation_id: str | None = None,
    action_id: str | None = None,
    parent_action_id: str | None = None,
    action_type: str | None = None,
    lifecycle_state: str | None = None,
    outcome: str | None = None,
    producer_component: str | None = None,
    retry_kind: str | None = None,
    idempotency_outcome: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    causal: bool = False,
    cursor: int = Query(0, ge=0),
    include_payloads: bool = False,
    limit: int = Query(1000, ge=1, le=10000),
) -> dict[str, object]:
    return query(
        request,
        ticket_id=ticket_id,
        trace_id=trace_id,
        invocation_id=invocation_id,
        action_id=action_id,
        parent_action_id=parent_action_id,
        action_type=action_type,
        lifecycle_state=lifecycle_state,
        outcome=outcome,
        retry_kind=retry_kind,
        idempotency_outcome=idempotency_outcome,
        since=since,
        until=until,
        producer_component=producer_component,
        causal=causal,
        cursor=cursor,
        include_payloads=include_payloads,
        limit=limit,
    )


@query_router.get("/invocations/{invocation_id}")
def invocation_trace(
    invocation_id: str,
    request: Request,
    ticket_id: str | None = None,
    trace_id: str | None = None,
    action_id: str | None = None,
    causal: bool = False,
    cursor: int = Query(0, ge=0),
    include_payloads: bool = False,
    limit: int = Query(1000, ge=1, le=10000),
) -> dict[str, object]:
    return query(
        request,
        invocation_id=invocation_id,
        ticket_id=ticket_id,
        trace_id=trace_id,
        action_id=action_id,
        causal=causal,
        cursor=cursor,
        include_payloads=include_payloads,
        limit=limit,
    )


@query_router.get("/actions/{action_id}")
def action_trace(
    action_id: str,
    request: Request,
    ticket_id: str | None = None,
    trace_id: str | None = None,
    invocation_id: str | None = None,
    parent_action_id: str | None = None,
    action_type: str | None = None,
    lifecycle_state: str | None = None,
    outcome: str | None = None,
    causal: bool = False,
    cursor: int = Query(0, ge=0),
    include_payloads: bool = False,
    limit: int = Query(1000, ge=1, le=10000),
) -> dict[str, object]:
    return query(
        request,
        action_id=action_id,
        ticket_id=ticket_id,
        trace_id=trace_id,
        invocation_id=invocation_id,
        parent_action_id=parent_action_id,
        action_type=action_type,
        lifecycle_state=lifecycle_state,
        outcome=outcome,
        causal=causal,
        cursor=cursor,
        include_payloads=include_payloads,
        limit=limit,
    )


@query_router.get("/{ticket_id}/payloads/{digest}")
def payload_content(ticket_id: str, digest: str, request: Request) -> Response:
    """Return a referenced redacted blob only to detailed trace principals."""
    if not _authorize_query(request, ticket_id):
        raise HTTPException(
            status_code=403, detail="payload access requires operator authentication"
        )
    ref = digest if digest.startswith("sha256:") else f"sha256:{digest}"
    events = request.app.state.trace_store.list_events(ticket_id=ticket_id)
    refs = {
        descriptor.blob_ref
        for event in events
        for descriptor in (event.input, event.output)
        if descriptor
    }
    if ref not in refs:
        raise HTTPException(status_code=404, detail="payload not found")
    try:
        content = PayloadBlobStore(ticket_id=ticket_id).get(ref, max_bytes=1_048_576)
    except PayloadStorageError as exc:
        raise HTTPException(status_code=404, detail="payload unavailable") from exc
    audit_log = getattr(request.app.state, "audit_log", None)
    if audit_log is not None:
        audit_log.log("trace_payload_read", ticket_id, {"digest": ref})
    return Response(content, media_type="application/octet-stream")


async def _service_principal(request: Request) -> Principal:
    principal = getattr(request.state, "principal", None)
    if principal is None or principal.kind != "service":
        raise HTTPException(
            status_code=403, detail="trace ingestion requires service authentication"
        )
    return principal


def _bound(event: TraceEventV1, principal: Principal, request: Request) -> TraceEventV1:
    # Producer identity is authority data.  Never trust an envelope supplied by
    # a client, even one that accidentally copied identity from another process.
    # The server deployment is the authority for its instance identity.  A
    # producer header is deliberately ignored: it can be forged by any caller.
    instance = getattr(request.app.state, "trace_instance_id", get_instance_name())
    attributes = dict(event.attributes or {})
    attributes["authenticated_principal"] = principal.username
    return event.model_copy(
        update={
            "producer": event.producer.model_copy(update={"instance_id": instance}),
            "attributes": attributes,
        }
    )


def _insert(
    request: Request, event: TraceEventV1, principal: Principal
) -> tuple[TraceEventV1, bool]:
    try:
        stored, duplicate = request.app.state.trace_store.insert_event_result(
            _bound(event, principal, request)
        )
        request.app.state.trace_health["ingested"] += 1
        return stored, duplicate
    except TraceEventConflictError as exc:
        request.app.state.trace_health["ingestion_failures"] += 1
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TraceStoreWriteError as exc:
        request.app.state.trace_health["ingestion_failures"] += 1
        raise HTTPException(status_code=503, detail="trace store unavailable") from exc


@router.post("")
async def ingest(
    event: TraceEventV1,
    request: Request,
    principal: Annotated[Principal, Depends(_service_principal)],
) -> dict[str, object]:
    stored, duplicate = _insert(request, event, principal)
    return {
        "event": stored.model_dump(mode="json"),
        "status": "duplicate" if duplicate else "stored",
    }


@router.post("/batch")
async def ingest_batch(
    batch: TraceBatch,
    request: Request,
    principal: Annotated[Principal, Depends(_service_principal)],
) -> dict[str, list[dict[str, object]]]:
    # Per-event acknowledgements make retrying a partial HTTP response safe.
    acknowledgements: list[dict[str, object]] = []
    for event in batch.events:
        try:
            stored, duplicate = _insert(request, event, principal)
            acknowledgements.append(
                {
                    "event_id": str(event.event_id),
                    "accepted": True,
                    "status": "duplicate" if duplicate else "stored",
                    "event": stored.model_dump(mode="json"),
                }
            )
        except HTTPException as exc:
            acknowledgements.append(
                {
                    "event_id": str(event.event_id),
                    "accepted": False,
                    "status": exc.status_code,
                    "detail": exc.detail,
                }
            )
    return {"acknowledgements": acknowledgements}
