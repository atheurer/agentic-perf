"""Service-only ingestion for immutable trace envelopes."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from paths import get_instance_name
from providers.tracing import TraceEventV1
from providers.tracing.query import TraceQuery, export_events, query_events

from ..auth import Principal
from ..trace_store import TraceEventConflictError, TraceStoreWriteError

router = APIRouter(prefix="/traces/events", tags=["traces"])
query_router = APIRouter(prefix="/traces", tags=["traces"])


class TraceBatch(BaseModel):
    events: list[TraceEventV1] = Field(min_length=1, max_length=500)


def _authorize_query(request: Request, ticket_id: str | None) -> None:
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
        return
    if not getattr(request.app.state, "multi_user", False):
        return
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
    since: datetime | None,
    until: datetime | None,
    causal: bool,
    limit: int,
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
        since=since,
        until=until,
        causal=causal,
        limit=limit,
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
    producer_component: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    causal: bool = False,
    limit: int = Query(default=1000, ge=1, le=10000),
) -> dict[str, object]:
    _authorize_query(request, ticket_id)
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
            since=since,
            until=until,
            causal=causal,
            limit=limit,
        ),
    )
    return {
        "events": [event.model_dump(mode="json") for event in selected],
        "count": len(selected),
    }


@query_router.get("/export")
def export(
    request: Request,
    format: Literal["json", "jsonl", "csv"] = "json",
    ticket_id: str | None = None,
    trace_id: str | None = None,
    causal: bool = False,
    limit: int = Query(default=10000, ge=1, le=10000),
) -> Response:
    _authorize_query(request, ticket_id)
    selected = query_events(
        request.app.state.trace_store.list_events(),
        TraceQuery(ticket_id=ticket_id, trace_id=trace_id, causal=causal, limit=limit),
    )
    media = "text/csv" if format == "csv" else "application/json"
    return Response(export_events(selected, format), media_type=media)


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
