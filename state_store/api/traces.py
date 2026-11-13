"""Service-only ingestion for immutable trace envelopes."""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from paths import get_instance_name
from providers.tracing import TraceEventV1
from providers.tracing.payloads import PayloadBlobStore, PayloadStorageError
from providers.tracing.query import (
    TraceQuery,
    TraceQueryCancelled,
    TraceQueryTooBroad,
    decode_cursor,
    diagnostics,
    export_events,
    export_manifest,
    page_events,
)

from ..auth import Principal
from ..trace_store import TraceEventConflictError, TraceStore, TraceStoreWriteError
from .events import invalidate_closed_ticket_usage_cache

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


async def _authorize_query_off_loop(request: Request, ticket_id: str | None) -> bool:
    """Keep ticket-store lock waits and deferred reads off the ASGI loop."""
    return await asyncio.to_thread(_authorize_query, request, ticket_id)


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
    cursor: int = 0,
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


def _cursor_sequence(cursor: str | None) -> int:
    marker = decode_cursor(cursor)
    if marker is None:
        return 0
    if marker[0] != 0:
        raise ValueError("invalid trace cursor for stored events")
    value = int(marker[1])
    if value < 0 or value > 2**63 - 1:
        raise ValueError("invalid trace cursor for stored events")
    return value


async def _record_query_audit(
    request: Request,
    operation: str,
    ticket_id: str | None,
    outcome: str,
    error: str | None = None,
) -> None:
    audit_log = getattr(request.app.state, "audit_log", None)
    if audit_log is not None:
        task = asyncio.create_task(
            asyncio.to_thread(
                audit_log.log,
                operation,
                ticket_id or "*",
                {
                    "outcome": outcome,
                    "error": error,
                    "ticket_scoped": bool(ticket_id),
                },
            )
        )
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            task.add_done_callback(_consume_audit_result)
            raise


def _consume_audit_result(task: asyncio.Task[None]) -> None:
    if not task.cancelled():
        task.exception()


async def _record_cancelled_query(
    request: Request,
    operation: str,
    ticket_id: str | None,
) -> None:
    """Finalize cancellation even if the request task is cancelled again."""
    await asyncio.shield(
        _record_query_audit(
            request,
            operation,
            ticket_id,
            "cancelled",
            "request cancelled",
        )
    )


def _verified_blob_refs(events: list[TraceEventV1]) -> set[str]:
    refs: set[str] = set()
    for event in events:
        if not event.ticket_id:
            continue
        store = PayloadBlobStore(ticket_id=event.ticket_id)
        for descriptor in (event.input, event.output):
            if not descriptor or not descriptor.blob_ref:
                continue
            try:
                store.get(descriptor.blob_ref, max_bytes=1_048_576)
            except PayloadStorageError:
                continue
            refs.add(descriptor.blob_ref)
    return refs


async def _query_store(
    request: Request,
    store: TraceStore,
    query: TraceQuery,
    *,
    include_diagnostics: bool = False,
) -> tuple[list[TraceEventV1], list[TraceEventV1] | None]:
    """Run a bounded trace scan off-loop and stop it when the client leaves."""

    def consume_result(task: asyncio.Task) -> None:
        if not task.cancelled():
            task.exception()

    cancelled = threading.Event()
    method = (
        store.query_events_with_diagnostics
        if include_diagnostics
        else store.query_events
    )
    task = asyncio.create_task(
        asyncio.to_thread(method, query, cancelled=cancelled.is_set)
    )
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=0.2)
            if task in done:
                result = task.result()
                if include_diagnostics:
                    return result
                return result, None
            if await request.is_disconnected():
                cancelled.set()
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
                except TimeoutError:
                    pass
                raise TraceQueryCancelled("trace query cancelled")
    finally:
        if task.done():
            consume_result(task)
        else:
            cancelled.set()
            task.add_done_callback(consume_result)


def _render_query_response(
    events: list[TraceEventV1],
    diagnostics_events: list[TraceEventV1] | None,
    *,
    cursor: str | None,
    limit: int,
    detailed: bool,
    include_payloads: bool,
    causal: bool,
) -> dict[str, object]:
    selected, has_more, next_cursor = page_events(events, cursor=cursor, limit=limit)
    return {
        "events": [
            _event_json(event, detailed and include_payloads) for event in selected
        ],
        "count": len(selected),
        "next_cursor": next_cursor,
        "has_more": has_more,
        "diagnostics": diagnostics(diagnostics_events or events) if causal else {},
    }


def _render_export_response(
    events: list[TraceEventV1],
    *,
    format: str,
    cursor: str | None,
    limit: int,
    detailed: bool,
    include_payloads: bool,
) -> tuple[str, str, dict[str, object]]:
    selected, has_more, next_cursor = page_events(events, cursor=cursor, limit=limit)
    verified_blob_refs = _verified_blob_refs(selected) if detailed else set()
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
    export_meta = export_manifest(
        selected, content, verified_blob_refs=verified_blob_refs
    )
    export_meta["has_more"] = has_more
    export_meta["next_cursor"] = next_cursor
    if format == "json":
        content = json.dumps(
            {"manifest": export_meta, "events": json.loads(content)}, indent=2
        )
    elif format == "jsonl":
        content += json.dumps({"_manifest": export_meta}, separators=(",", ":")) + "\n"
    else:
        content = (
            "# trace-export-manifest: "
            + json.dumps(export_meta, separators=(",", ":"))
            + "\n"
            + content
        )
    return content, media, export_meta


@query_router.get("/query")
async def query(
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
    cursor: str | None = None,
    include_payloads: bool = False,
) -> dict[str, object]:
    terminal_audit_started = False

    async def record_terminal(outcome: str, error: str | None = None) -> None:
        nonlocal terminal_audit_started
        terminal_audit_started = True
        await _record_query_audit(request, "trace_query", ticket_id, outcome, error)

    try:
        detailed = await _authorize_query_off_loop(request, ticket_id)
    except HTTPException as exc:
        await _record_query_audit(
            request, "trace_query", ticket_id, "denied", str(exc.detail)
        )
        raise
    except asyncio.CancelledError:
        await _record_cancelled_query(request, "trace_query", ticket_id)
        raise
    try:
        try:
            query_cursor = _cursor_sequence(cursor)
        except ValueError as exc:
            await record_terminal("failed", str(exc))
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        base_query = _query_from_params(
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
            limit=limit + 1,
            cursor=query_cursor,
        )
        try:
            all_selected, diagnostics_events = await _query_store(
                request,
                request.app.state.trace_store,
                base_query,
                include_diagnostics=causal,
            )
        except TraceQueryTooBroad as exc:
            await record_terminal("failed", str(exc))
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except TraceQueryCancelled as exc:
            await record_terminal("cancelled", str(exc))
            raise HTTPException(status_code=408, detail=str(exc)) from exc
        try:
            response = await asyncio.to_thread(
                _render_query_response,
                all_selected,
                diagnostics_events,
                cursor=cursor,
                limit=limit,
                detailed=detailed,
                include_payloads=include_payloads,
                causal=causal,
            )
        except ValueError as exc:
            await record_terminal("failed", str(exc))
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    except asyncio.CancelledError:
        if not terminal_audit_started:
            await _record_cancelled_query(request, "trace_query", ticket_id)
        raise
    await record_terminal("success")
    return response


@query_router.get("/export")
async def export(
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
    cursor: str | None = None,
    include_payloads: bool = False,
    manifest: bool = False,
) -> Response:
    terminal_audit_started = False

    async def record_terminal(outcome: str, error: str | None = None) -> None:
        nonlocal terminal_audit_started
        terminal_audit_started = True
        await _record_query_audit(request, "trace_export", ticket_id, outcome, error)

    try:
        detailed = await _authorize_query_off_loop(request, ticket_id)
    except HTTPException as exc:
        await _record_query_audit(
            request, "trace_export", ticket_id, "denied", str(exc.detail)
        )
        raise
    except asyncio.CancelledError:
        await _record_cancelled_query(request, "trace_export", ticket_id)
        raise
    try:
        try:
            query_cursor = _cursor_sequence(cursor)
        except ValueError as exc:
            await record_terminal("failed", str(exc))
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        base_query = TraceQuery(
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
            limit=limit + 1,
            cursor=query_cursor,
        )
        try:
            selected, _ = await _query_store(
                request, request.app.state.trace_store, base_query
            )
        except TraceQueryTooBroad as exc:
            await record_terminal("failed", str(exc))
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except TraceQueryCancelled as exc:
            await record_terminal("cancelled", str(exc))
            raise HTTPException(status_code=408, detail=str(exc)) from exc
        try:
            content, media, export_meta = await asyncio.to_thread(
                _render_export_response,
                selected,
                format=format,
                cursor=cursor,
                limit=limit,
                detailed=detailed,
                include_payloads=include_payloads,
            )
        except ValueError as exc:
            await record_terminal("failed", str(exc))
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    except asyncio.CancelledError:
        if not terminal_audit_started:
            await _record_cancelled_query(request, "trace_export", ticket_id)
        raise
    response = Response(content, media_type=media)
    response.headers["X-Trace-Manifest"] = json.dumps(
        export_meta, separators=(",", ":")
    )
    await record_terminal("success")
    return response


@query_router.get("/tickets/{ticket_id}")
async def ticket_trace(
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
    cursor: str | None = None,
    include_payloads: bool = False,
    limit: int = Query(1000, ge=1, le=10000),
) -> dict[str, object]:
    return await query(
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
async def invocation_trace(
    invocation_id: str,
    request: Request,
    ticket_id: str | None = None,
    trace_id: str | None = None,
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
    cursor: str | None = None,
    include_payloads: bool = False,
    limit: int = Query(1000, ge=1, le=10000),
) -> dict[str, object]:
    return await query(
        request,
        invocation_id=invocation_id,
        ticket_id=ticket_id,
        trace_id=trace_id,
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
        cursor=cursor,
        include_payloads=include_payloads,
        limit=limit,
    )


@query_router.get("/actions/{action_id}")
async def action_trace(
    action_id: str,
    request: Request,
    ticket_id: str | None = None,
    trace_id: str | None = None,
    invocation_id: str | None = None,
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
    cursor: str | None = None,
    include_payloads: bool = False,
    limit: int = Query(1000, ge=1, le=10000),
) -> dict[str, object]:
    return await query(
        request,
        action_id=action_id,
        ticket_id=ticket_id,
        trace_id=trace_id,
        invocation_id=invocation_id,
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
        cursor=cursor,
        include_payloads=include_payloads,
        limit=limit,
    )


@query_router.get("/{ticket_id}/payloads/{digest}")
def payload_content(ticket_id: str, digest: str, request: Request) -> Response:
    """Return a referenced redacted blob only to detailed trace principals."""
    audit_log = getattr(request.app.state, "audit_log", None)

    def audit(outcome: str, error: str = "") -> None:
        if audit_log is not None:
            audit_log.log(
                "trace_payload_read",
                ticket_id,
                {"digest": digest[:80], "outcome": outcome, "error": error},
            )

    try:
        detailed = _authorize_query(request, ticket_id)
    except HTTPException as exc:
        audit("denied", str(exc.detail))
        raise
    if not detailed:
        audit("denied", "owner redaction")
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
        audit("missing", "unreferenced payload")
        raise HTTPException(status_code=404, detail="payload not found")
    try:
        content = PayloadBlobStore(ticket_id=ticket_id).get(ref, max_bytes=1_048_576)
    except PayloadStorageError as exc:
        audit("unavailable", str(exc))
        raise HTTPException(status_code=404, detail="payload unavailable") from exc
    audit("success")
    return Response(content, media_type="application/octet-stream")


async def _service_principal(request: Request) -> Principal:
    principal = getattr(request.state, "principal", None)
    if principal is None or principal.kind != "service":
        raise HTTPException(
            status_code=403, detail="trace ingestion requires service authentication"
        )
    return principal


_trace_health_lock = threading.Lock()


def _bump_trace_health(request: Request, key: str) -> None:
    """Increment a trace health counter from any thread without racing."""
    with _trace_health_lock:
        request.app.state.trace_health[key] += 1


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
        _bump_trace_health(request, "ingested")
        legacy_event = (stored.attributes or {}).get("legacy_event", {})
        if (
            not duplicate
            and isinstance(legacy_event, dict)
            and legacy_event.get("event_type") == "llm_usage"
        ):
            request.app.state.store.invalidate_cached_usage_summary(stored.ticket_id)
            invalidate_closed_ticket_usage_cache(stored.ticket_id)
        return stored, duplicate
    except TraceEventConflictError as exc:
        _bump_trace_health(request, "ingestion_failures")
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TraceStoreWriteError as exc:
        _bump_trace_health(request, "ingestion_failures")
        raise HTTPException(status_code=503, detail="trace store unavailable") from exc


@router.post("")
async def ingest(
    event: TraceEventV1,
    request: Request,
    principal: Annotated[Principal, Depends(_service_principal)],
) -> dict[str, object]:
    # _insert() calls SQLite synchronously; run it in a thread so the event
    # loop remains free to serve health probes and claim renewals while the
    # write completes.  Exceptions (HTTPException) propagate normally.
    stored, duplicate = await asyncio.to_thread(_insert, request, event, principal)
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
            stored, duplicate = await asyncio.to_thread(
                _insert, request, event, principal
            )
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
