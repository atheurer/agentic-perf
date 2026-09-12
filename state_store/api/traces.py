"""Service-only ingestion for immutable trace envelopes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from paths import get_instance_name
from providers.tracing import TraceEventV1

from ..auth import Principal
from ..trace_store import TraceEventConflictError, TraceStoreWriteError

router = APIRouter(prefix="/traces/events", tags=["traces"])


class TraceBatch(BaseModel):
    events: list[TraceEventV1] = Field(min_length=1, max_length=500)


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
