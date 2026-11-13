"""Service-only ingestion for immutable trace envelopes."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Annotated, Callable, Iterable, Literal, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from paths import get_instance_name
from providers.tracing import TraceEventV1
from providers.tracing.payloads import PayloadBlobStore, PayloadStorageError
from providers.tracing.query import (
    TraceQuery,
    TraceQueryCancelled,
    TraceQueryResourceExhausted,
    TraceQueryTooBroad,
    decode_cursor,
    diagnostics,
    export_events,
    export_manifest,
    page_events,
)

from ..auth import Principal
from ..store import TicketNotFound
from ..trace_store import TraceEventConflictError, TraceStore, TraceStoreWriteError
from .events import invalidate_closed_ticket_usage_cache

router = APIRouter(prefix="/traces/events", tags=["traces"])
query_router = APIRouter(prefix="/traces", tags=["traces"])
_WorkerResult = TypeVar("_WorkerResult")
MAX_TRACE_RESPONSE_BYTES = 64 * 1024 * 1024


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
    except TicketNotFound as exc:
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


async def _run_trace_worker(request: Request, function, /, *args, **kwargs):
    """Run bounded query work outside both the ASGI loop and default executor."""
    state = getattr(getattr(request, "app", None), "state", None)
    executor = getattr(state, "trace_query_executor", None)
    if executor is None:
        return await asyncio.to_thread(function, *args, **kwargs)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, partial(function, *args, **kwargs))


async def _observe_trace_worker(
    request: Request,
    task: asyncio.Task[_WorkerResult],
    cancelled: threading.Event,
) -> _WorkerResult:
    def consume_result(done: asyncio.Task[_WorkerResult]) -> None:
        if not done.cancelled():
            done.exception()

    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=0.2)
            if task in done:
                return task.result()
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


async def _run_cancellable_trace_worker(
    request: Request, function, /, *args, **kwargs
) -> _WorkerResult:
    cancelled = threading.Event()
    state = getattr(getattr(request, "app", None), "state", None)
    shutdown = getattr(state, "trace_query_shutdown", None)

    def is_cancelled() -> bool:
        return cancelled.is_set() or bool(shutdown is not None and shutdown.is_set())

    kwargs["cancelled"] = is_cancelled
    task = asyncio.create_task(_run_trace_worker(request, function, *args, **kwargs))
    return await _observe_trace_worker(request, task, cancelled)


def _resident_bytes() -> int:
    try:
        pages = int(Path("/proc/self/statm").read_text(encoding="ascii").split()[1])
    except (IndexError, OSError, ValueError):
        return 0
    return pages * int(os.sysconf("SC_PAGE_SIZE"))


def _memory_guard_exceeded(request: Request) -> bool:
    state = getattr(getattr(request, "app", None), "state", None)
    limit = getattr(state, "trace_query_max_rss_bytes", 0)
    return bool(limit and limit > 0 and _resident_bytes() >= limit)


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


def _check_render_cancelled(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise TraceQueryCancelled("trace query cancelled")


def _json_dumps(
    value: object,
    *,
    indent: int | None = None,
    separators: tuple[str, str] | None = None,
    cancelled: Callable[[], bool] | None = None,
    max_bytes: int | None = None,
) -> str:
    if max_bytes is None:
        max_bytes = MAX_TRACE_RESPONSE_BYTES
    encoder = json.JSONEncoder(indent=indent, separators=separators)
    fragments: list[str] = []
    total_bytes = 0
    for fragment in encoder.iterencode(value):
        _check_render_cancelled(cancelled)
        total_bytes += len(fragment.encode("utf-8"))
        if total_bytes > max_bytes:
            raise TraceQueryTooBroad(
                "trace response is too broad; narrow the query or reduce its limit"
            )
        fragments.append(fragment)
    return "".join(fragments)


def _join_trace_response(
    parts: Iterable[str], *, cancelled: Callable[[], bool] | None = None
) -> str:
    fragments: list[str] = []
    total_bytes = 0
    for part in parts:
        _check_render_cancelled(cancelled)
        total_bytes += len(part.encode("utf-8"))
        if total_bytes > MAX_TRACE_RESPONSE_BYTES:
            raise TraceQueryTooBroad(
                "trace response is too broad; narrow the query or reduce its limit"
            )
        fragments.append(part)
    return "".join(fragments)


def _verified_blob_refs(
    events: list[TraceEventV1],
    *,
    cancelled: Callable[[], bool] | None = None,
) -> set[str]:
    refs: set[str] = set()
    for event in events:
        _check_render_cancelled(cancelled)
        if not event.ticket_id:
            continue
        store = PayloadBlobStore(ticket_id=event.ticket_id)
        for descriptor in (event.input, event.output):
            _check_render_cancelled(cancelled)
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

    if _memory_guard_exceeded(request):
        raise TraceQueryResourceExhausted(
            "trace queries are temporarily unavailable while the state store "
            "memory limit is exceeded"
        )

    method = (
        store.query_events_with_diagnostics
        if include_diagnostics
        else store.query_events
    )
    result = await _run_cancellable_trace_worker(
        request,
        method,
        query,
    )
    if include_diagnostics:
        return result
    return result, None


def _render_query_response(
    events: list[TraceEventV1],
    diagnostics_events: list[TraceEventV1] | None,
    *,
    cursor: str | None,
    limit: int,
    detailed: bool,
    include_payloads: bool,
    causal: bool,
    cancelled: Callable[[], bool] | None = None,
) -> Response:
    selected, has_more, next_cursor = page_events(
        events, cursor=cursor, limit=limit, cancelled=cancelled
    )
    rendered_events: list[dict[str, object]] = []
    for event in selected:
        _check_render_cancelled(cancelled)
        rendered_events.append(_event_json(event, detailed and include_payloads))
    rendered_diagnostics = (
        diagnostics(diagnostics_events or events, cancelled=cancelled) if causal else {}
    )
    payload = {
        "events": rendered_events,
        "count": len(selected),
        "next_cursor": next_cursor,
        "has_more": has_more,
        "diagnostics": rendered_diagnostics,
    }
    return Response(
        content=_json_dumps(payload, separators=(",", ":"), cancelled=cancelled),
        media_type="application/json",
    )


def _render_export_response(
    events: list[TraceEventV1],
    *,
    format: str,
    cursor: str | None,
    limit: int,
    detailed: bool,
    include_payloads: bool,
    cancelled: Callable[[], bool] | None = None,
) -> Response:
    selected, has_more, next_cursor = page_events(
        events, cursor=cursor, limit=limit, cancelled=cancelled
    )
    verified_blob_refs = (
        _verified_blob_refs(selected, cancelled=cancelled) if detailed else set()
    )
    media = (
        "text/csv"
        if format == "csv"
        else ("application/x-ndjson" if format == "jsonl" else "application/json")
    )
    if not detailed or not include_payloads:
        redacted: list[TraceEventV1] = []
        for event in selected:
            _check_render_cancelled(cancelled)
            redacted.append(
                event.model_copy(
                    update={"input": None, "output": None, "attributes": None}
                )
            )
        selected = redacted
    event_rows: list[dict[str, object]] | None = None
    if format == "json":
        event_rows = []
        for event in selected:
            _check_render_cancelled(cancelled)
            event_rows.append(event.model_dump(mode="json"))
        content = _json_dumps(event_rows, indent=2, cancelled=cancelled)
    else:
        content = export_events(
            selected,
            format,
            cancelled=cancelled,
            max_bytes=MAX_TRACE_RESPONSE_BYTES,
        )
    export_meta = export_manifest(
        selected,
        content,
        verified_blob_refs=verified_blob_refs,
        cancelled=cancelled,
    )
    export_meta["has_more"] = has_more
    export_meta["next_cursor"] = next_cursor
    if format == "json":
        content = _json_dumps(
            {"manifest": export_meta, "events": event_rows},
            indent=2,
            cancelled=cancelled,
        )
    elif format == "jsonl":
        content = _join_trace_response(
            (
                content,
                _json_dumps(
                    {"_manifest": export_meta},
                    separators=(",", ":"),
                    cancelled=cancelled,
                ),
                "\n",
            ),
            cancelled=cancelled,
        )
    else:
        content = _join_trace_response(
            (
                "# trace-export-manifest: ",
                _json_dumps(export_meta, separators=(",", ":"), cancelled=cancelled),
                "\n",
                content,
            ),
            cancelled=cancelled,
        )
    response = Response(content, media_type=media)
    response.headers["X-Trace-Manifest"] = _json_dumps(
        export_meta, separators=(",", ":"), cancelled=cancelled
    )
    return response


class _TraceAuditResponse(Response):
    """Record success only after ASGI accepts the complete response body."""

    def __init__(
        self,
        response: Response,
        *,
        request: Request,
        operation: str,
        ticket_id: str | None,
    ) -> None:
        super().__init__(
            content=response.body,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
            background=response.background,
        )
        self._audit_request = request
        self._audit_operation = operation
        self._audit_ticket_id = ticket_id

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        except asyncio.CancelledError:
            await _record_cancelled_query(
                self._audit_request,
                self._audit_operation,
                self._audit_ticket_id,
            )
            raise
        except Exception as exc:
            await _record_query_audit(
                self._audit_request,
                self._audit_operation,
                self._audit_ticket_id,
                "failed",
                _unexpected_audit_error(self._audit_operation.replace("_", " "), exc),
            )
            raise
        else:
            await _record_query_audit(
                self._audit_request,
                self._audit_operation,
                self._audit_ticket_id,
                "success",
            )


def _unexpected_audit_error(operation: str, exc: Exception) -> str:
    """Keep unexpected audit details useful without persisting exception text."""
    return f"{operation} failed ({type(exc).__name__})"


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
) -> Response:
    terminal_audit_started = False

    async def record_terminal(outcome: str, error: str | None = None) -> None:
        nonlocal terminal_audit_started
        terminal_audit_started = True
        await _record_query_audit(request, "trace_query", ticket_id, outcome, error)

    try:
        detailed = await _authorize_query_off_loop(request, ticket_id)
    except HTTPException as exc:
        await record_terminal("denied", str(exc.detail))
        raise
    except asyncio.CancelledError:
        await _record_cancelled_query(request, "trace_query", ticket_id)
        raise
    except Exception as exc:
        await record_terminal("failed", _unexpected_audit_error("trace query", exc))
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
        except TraceQueryResourceExhausted as exc:
            await record_terminal("failed", str(exc))
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except TraceQueryCancelled as exc:
            await record_terminal("cancelled", str(exc))
            raise HTTPException(status_code=408, detail=str(exc)) from exc
        try:
            response = await _run_cancellable_trace_worker(
                request,
                _render_query_response,
                all_selected,
                diagnostics_events,
                cursor=cursor,
                limit=limit,
                detailed=detailed,
                include_payloads=include_payloads,
                causal=causal,
            )
        except TraceQueryCancelled as exc:
            await record_terminal("cancelled", str(exc))
            raise HTTPException(status_code=408, detail=str(exc)) from exc
        except ValueError as exc:
            await record_terminal("failed", str(exc))
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    except asyncio.CancelledError:
        if not terminal_audit_started:
            await _record_cancelled_query(request, "trace_query", ticket_id)
        raise
    except Exception as exc:
        if not terminal_audit_started:
            await record_terminal("failed", _unexpected_audit_error("trace query", exc))
        raise
    return _TraceAuditResponse(
        response,
        request=request,
        operation="trace_query",
        ticket_id=ticket_id,
    )


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
        await record_terminal("denied", str(exc.detail))
        raise
    except asyncio.CancelledError:
        await _record_cancelled_query(request, "trace_export", ticket_id)
        raise
    except Exception as exc:
        await record_terminal("failed", _unexpected_audit_error("trace export", exc))
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
        except TraceQueryResourceExhausted as exc:
            await record_terminal("failed", str(exc))
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except TraceQueryCancelled as exc:
            await record_terminal("cancelled", str(exc))
            raise HTTPException(status_code=408, detail=str(exc)) from exc
        try:
            response = await _run_cancellable_trace_worker(
                request,
                _render_export_response,
                selected,
                format=format,
                cursor=cursor,
                limit=limit,
                detailed=detailed,
                include_payloads=include_payloads,
            )
        except TraceQueryCancelled as exc:
            await record_terminal("cancelled", str(exc))
            raise HTTPException(status_code=408, detail=str(exc)) from exc
        except ValueError as exc:
            await record_terminal("failed", str(exc))
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    except asyncio.CancelledError:
        if not terminal_audit_started:
            await _record_cancelled_query(request, "trace_export", ticket_id)
        raise
    except Exception as exc:
        if not terminal_audit_started:
            await record_terminal(
                "failed", _unexpected_audit_error("trace export", exc)
            )
        raise
    return _TraceAuditResponse(
        response,
        request=request,
        operation="trace_export",
        ticket_id=ticket_id,
    )


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
) -> Response:
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
) -> Response:
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
) -> Response:
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
