from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException, Response

import state_store.api.traces as traces_api
from providers.tracing import (
    ActionDescriptor,
    ActionType,
    LifecycleDescriptor,
    LifecycleState,
    PayloadDescriptor,
    ProducerIdentity,
    TraceEventV1,
)
from providers.tracing.query import TraceQuery, TraceQueryCancelled, TraceQueryTooBroad
from state_store.api.executors import (
    BoundedThreadPoolExecutor,
    run_control_worker,
    run_lease_worker,
)
from state_store.api.health import health as health_endpoint
from state_store.api.router import api_router
from state_store.api.traces import (
    _authorize_query,
    _event_json,
    _json_dumps,
    _PayloadAuditResponse,
    _query_store,
    _record_cancelled_query,
    _record_query_audit,
    _render_export_response,
    _TraceAuditResponse,
    _verified_blob_refs,
)
from state_store.api.traces import (
    export as export_trace,
)
from state_store.api.traces import (
    query as query_trace,
)
from state_store.audit import get_actor, set_actor
from state_store.auth import Principal, make_auth_dependency
from state_store.identity import UserStore
from state_store.main import (
    _close_runtime,
    _set_audit_actor,
    _trace_read_operation,
    create_app,
)
from state_store.trace_store import TraceStore, TraceStoreWriteError

_TEST_AUDIT_FALLBACK_EXECUTOR = BoundedThreadPoolExecutor(
    max_workers=1,
    queue_size=0,
    thread_name_prefix="test-trace-audit-fallback",
)
_TEST_AUDIT_EXECUTOR = BoundedThreadPoolExecutor(
    max_workers=1,
    queue_size=1,
    thread_name_prefix="test-trace-audit",
)


def payload(event: TraceEventV1) -> dict:
    return event.model_dump(mode="json")


def test_json_rendering_enforces_response_size_budget(monkeypatch) -> None:
    monkeypatch.setattr(traces_api, "MAX_TRACE_RESPONSE_BYTES", 64)

    with pytest.raises(TraceQueryTooBroad, match="response is too broad"):
        _json_dumps({"payload": "x" * 256})


def test_export_rendering_enforces_aggregate_response_size_budget(monkeypatch) -> None:
    monkeypatch.setattr(traces_api, "MAX_TRACE_RESPONSE_BYTES", 2048)
    exported = event().model_copy(update={"attributes": {"payload": "x" * 4096}})

    with pytest.raises(TraceQueryTooBroad, match="response is too broad"):
        _render_export_response(
            [exported],
            format="jsonl",
            cursor=None,
            limit=1,
            detailed=True,
            include_payloads=True,
        )


def test_export_skips_blob_verification_when_payloads_are_omitted(monkeypatch) -> None:
    def unexpected_verification(*_args, **_kwargs):
        raise AssertionError("verified payloads even though export omitted them")

    monkeypatch.setattr(traces_api, "_verified_blob_refs", unexpected_verification)
    response = _render_export_response(
        [event()],
        format="json",
        cursor=None,
        limit=1,
        detailed=True,
        include_payloads=False,
    )

    assert response.status_code == 200


def test_export_blob_verification_deduplicates_and_budgets_reads(monkeypatch) -> None:
    refs = [f"sha256:{index:064x}" for index in range(1, 6)]
    calls: list[tuple[str, int]] = []

    class Store:
        def __init__(self, *, ticket_id: str) -> None:
            self.ticket_id = ticket_id

        def get(self, ref: str, *, max_bytes: int) -> bytes:
            calls.append((f"{self.ticket_id}:{ref}", max_bytes))
            return b"x" * 5

    monkeypatch.setattr(traces_api, "PayloadBlobStore", Store)
    first = event().model_copy(
        update={
            "input": PayloadDescriptor(blob_ref=refs[0]),
            "output": PayloadDescriptor(blob_ref=refs[0]),
        }
    )
    other_ticket = event().model_copy(
        update={
            "ticket_id": "PERF-2",
            "input": PayloadDescriptor(blob_ref=refs[0]),
        }
    )
    remaining = [
        event().model_copy(update={"input": PayloadDescriptor(blob_ref=ref)})
        for ref in refs[1:]
    ]

    verified = _verified_blob_refs(
        [first, other_ticket, *remaining],
        max_bytes=20,
    )

    assert verified == set(refs[:3])
    assert [budget for _, budget in calls] == [20, 15, 10, 5]
    assert calls[0][0] == f"PERF-1:{refs[0]}"
    assert calls[1][0] == f"PERF-2:{refs[0]}"


def test_export_blob_verification_charges_failed_read_to_full_budget(
    monkeypatch,
) -> None:
    refs = [f"sha256:{index:064x}" for index in range(1, 3)]
    calls: list[int] = []

    class Store:
        def __init__(self, *, ticket_id: str) -> None:
            self.ticket_id = ticket_id

        def get(self, ref: str, *, max_bytes: int) -> bytes:
            calls.append(max_bytes)
            if ref == refs[0]:
                raise traces_api.PayloadStorageError("unreadable payload")
            return b"x" * 5

    monkeypatch.setattr(traces_api, "PayloadBlobStore", Store)
    events = [
        event().model_copy(update={"input": PayloadDescriptor(blob_ref=ref)})
        for ref in refs
    ]

    verified = _verified_blob_refs(events, max_bytes=11)

    # The failed read may have inspected its entire allowance, so it must not
    # let a later corrupt or oversized blob borrow aggregate verification budget.
    assert verified == set()
    assert calls == [11]


def make_app(tmp_path, user_store: UserStore | None = None) -> FastAPI:
    app = FastAPI()
    app.state.auth_dependency = make_auth_dependency(
        "service", multi_user=True, user_store=user_store
    )
    app.state.trace_store = TraceStore(tmp_path / "trace.db")
    app.state.trace_instance_id = "server-instance"
    app.state.trace_health = {
        "ingested": 0,
        "ingestion_failures": 0,
        "schema_rejections": 0,
        "quarantined_frames": 0,
        "audit_queue_fallbacks": 0,
        "ingestion_queue_rejections": 0,
        "ingestion_body_rejections": 0,
    }
    app.state.trace_query_audit_fallback_executor = _TEST_AUDIT_FALLBACK_EXECUTOR
    app.state.trace_query_audit_fallback_slots = _TEST_AUDIT_FALLBACK_EXECUTOR.slots
    app.state.trace_query_audit_executor = _TEST_AUDIT_EXECUTOR
    app.state.trace_query_audit_slots = _TEST_AUDIT_EXECUTOR.slots
    app.add_middleware(traces_api.TraceIngestBodyLimitMiddleware)
    app.include_router(
        api_router,
        dependencies=[
            Depends(
                make_auth_dependency("service", multi_user=True, user_store=user_store)
            ),
            Depends(_set_audit_actor),
        ],
    )
    return app


def middleware_scope() -> dict:
    app = FastAPI()
    app.state.auth_dependency = make_auth_dependency("service")
    return {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/traces/events",
        "headers": [(b"authorization", b"Bearer service")],
        "app": app,
    }


def trace_audit_state(audit_log, **attributes):
    return SimpleNamespace(
        audit_log=audit_log,
        trace_query_audit_executor=_TEST_AUDIT_EXECUTOR,
        trace_query_audit_slots=_TEST_AUDIT_EXECUTOR.slots,
        trace_query_audit_fallback_executor=_TEST_AUDIT_FALLBACK_EXECUTOR,
        trace_query_audit_fallback_slots=_TEST_AUDIT_FALLBACK_EXECUTOR.slots,
        **attributes,
    )


def event() -> TraceEventV1:
    return TraceEventV1(
        ticket_id="PERF-1",
        action=ActionDescriptor(type=ActionType.DISPATCH),
        lifecycle=LifecycleDescriptor(state=LifecycleState.STARTED),
    )


async def test_service_ingests_and_server_binds_identity(tmp_path) -> None:
    original = event().model_copy(
        update={
            "producer": ProducerIdentity(
                component="mcp",
                instance_id="forged-instance",
                host="producer-host",
                pid=123,
                process_start_id="start-1",
                boot_id="boot-1",
            ),
            "attributes": {
                "authenticated_principal": "forged-principal",
                "kept": True,
            },
        }
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=make_app(tmp_path)), base_url="http://test"
    )
    response = await client.post(
        "/api/v1/traces/events",
        json=payload(original),
        headers={"Authorization": "Bearer service", "X-Trace-Instance": "process-a"},
    )
    await client.aclose()
    assert response.status_code == 200
    assert response.json()["event"]["producer"] == {
        "component": "mcp",
        "instance_id": "server-instance",
        "host": "producer-host",
        "pid": 123,
        "process_start_id": "start-1",
        "boot_id": "boot-1",
    }
    assert (
        response.json()["event"]["attributes"]["authenticated_principal"]
        == "deployment"
    )


@pytest.mark.parametrize(
    "path",
    ["/api/v1/traces/events", "/api/v1/traces/events/batch"],
)
async def test_oversized_trace_body_is_rejected_before_validation(
    tmp_path, monkeypatch, path: str
) -> None:
    app = make_app(tmp_path)
    monkeypatch.setattr(traces_api, "MAX_TRACE_INGEST_BODY_BYTES", 128)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )

    response = await client.post(
        path,
        content=b'{"broken": "' + b"x" * 256,
        headers={
            "Authorization": "Bearer service",
            "Content-Type": "application/json",
        },
    )
    await client.aclose()

    assert response.status_code == 413
    assert response.json()["detail"] == "trace ingestion request body is too large"
    assert app.state.trace_health["ingestion_body_rejections"] == 1
    assert app.state.trace_health["schema_rejections"] == 0


async def test_trace_body_limit_counts_streamed_chunks_without_content_length(
    tmp_path, monkeypatch
) -> None:
    app = make_app(tmp_path)
    monkeypatch.setattr(traces_api, "MAX_TRACE_INGEST_BODY_BYTES", 8)
    downstream_called = False

    async def downstream(_scope, _receive, _send) -> None:
        nonlocal downstream_called
        downstream_called = True

    middleware = traces_api.TraceIngestBodyLimitMiddleware(downstream)
    messages = [
        {"type": "http.request", "body": b"12345", "more_body": True},
        {"type": "http.request", "body": b"6789", "more_body": True},
    ]

    async def receive():
        return messages.pop(0)

    sent = []

    async def send(message) -> None:
        sent.append(message)

    await middleware(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/traces/events",
            "headers": [(b"authorization", b"Bearer service")],
            "app": app,
        },
        receive,
        send,
    )

    assert downstream_called is False
    assert sent[0]["status"] == 413
    assert app.state.trace_health["ingestion_body_rejections"] == 1


async def test_trace_body_limit_aggregates_chunks_for_asgi_replay() -> None:
    downstream_messages = []

    async def downstream(_scope, receive, _send) -> None:
        while True:
            message = await receive()
            downstream_messages.append(message)
            if message["type"] == "http.disconnect":
                return

    middleware = traces_api.TraceIngestBodyLimitMiddleware(downstream)
    messages = [
        {"type": "http.request", "body": b"", "more_body": True},
        {"type": "http.request", "body": b"1", "more_body": True},
        {"type": "http.request", "body": b"", "more_body": True},
        {"type": "http.request", "body": b"2", "more_body": False},
    ]

    async def receive():
        return messages.pop(0)

    async def send(_message) -> None:
        raise AssertionError("valid request should reach downstream")

    await middleware(
        middleware_scope(),
        receive,
        send,
    )

    assert downstream_messages == [
        {"type": "http.request", "body": b"12", "more_body": False},
        {"type": "http.disconnect"},
    ]


async def test_unauthenticated_maximum_trace_body_is_never_buffered() -> None:
    scope = middleware_scope()
    scope["headers"] = [(b"authorization", b"Bearer invalid")]
    receive_calls = 0

    async def receive():
        nonlocal receive_calls
        receive_calls += 1
        raise AssertionError("authentication must reject before reading the body")

    async def downstream(_scope, _receive, _send) -> None:
        raise AssertionError("unauthenticated requests must not reach downstream")

    responses = []

    async def send(message):
        if message["type"] == "http.response.start":
            responses.append(message["status"])

    middleware = traces_api.TraceIngestBodyLimitMiddleware(downstream)
    await asyncio.gather(*(middleware(scope, receive, send) for _ in range(16)))

    assert receive_calls == 0
    assert responses == [401] * 16


async def test_trace_body_buffers_are_bounded_under_concurrent_maximum_requests() -> (
    None
):
    buffers_admitted = asyncio.Event()
    release_buffers = asyncio.Event()
    downstream_bodies = []

    async def downstream(_scope, receive, _send) -> None:
        message = await receive()
        downstream_bodies.append(len(message["body"]))
        await release_buffers.wait()

    def make_receive():
        first_message = True

        async def receive():
            nonlocal first_message
            if first_message:
                first_message = False
                return {
                    "type": "http.request",
                    "body": b"x" * traces_api.MAX_TRACE_INGEST_BODY_BYTES,
                    "more_body": True,
                }
            buffers_admitted.set()
            await release_buffers.wait()
            return {"type": "http.request", "body": b"", "more_body": False}

        return receive

    middleware = traces_api.TraceIngestBodyLimitMiddleware(
        downstream, max_buffered_requests=2
    )
    sent_messages = []

    async def send(message):
        sent_messages.append(message)

    requests = [
        asyncio.create_task(middleware(middleware_scope(), make_receive(), send))
        for _ in range(4)
    ]

    await asyncio.wait_for(buffers_admitted.wait(), 1)
    await asyncio.sleep(0)
    done, pending = await asyncio.wait(requests, timeout=0.1)
    assert len(done) == 2
    assert len(pending) == 2
    assert [
        message["status"]
        for message in sent_messages
        if message["type"] == "http.response.start"
    ] == [503, 503]

    release_buffers.set()
    await asyncio.gather(*requests)
    assert downstream_bodies == [
        traces_api.MAX_TRACE_INGEST_BODY_BYTES,
        traces_api.MAX_TRACE_INGEST_BODY_BYTES,
    ]


@pytest.mark.parametrize("chunk", [b"", b"1"])
async def test_trace_body_limit_bounds_zero_or_one_byte_chunks(
    monkeypatch, chunk
) -> None:
    monkeypatch.setattr(traces_api, "MAX_TRACE_INGEST_MESSAGES", 2)
    downstream_called = False

    async def downstream(_scope, _receive, _send) -> None:
        nonlocal downstream_called
        downstream_called = True

    middleware = traces_api.TraceIngestBodyLimitMiddleware(downstream)
    messages = [
        {"type": "http.request", "body": chunk, "more_body": True},
        {"type": "http.request", "body": chunk, "more_body": True},
        {"type": "http.request", "body": chunk, "more_body": True},
    ]

    async def receive():
        return messages.pop(0)

    sent = []

    async def send(message) -> None:
        sent.append(message)

    await middleware(
        middleware_scope(),
        receive,
        send,
    )

    assert downstream_called is False
    assert sent[0]["status"] == 413


async def test_query_and_export_continuations_are_exposed(tmp_path) -> None:
    app = make_app(tmp_path)
    first = event()
    second = event().model_copy(update={"action_id": "2" * 16})
    app.state.trace_store.insert_event(first)
    app.state.trace_store.insert_event(second)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    headers = {"Authorization": "Bearer service"}
    query = await client.get(
        "/api/v1/traces/query?ticket_id=PERF-1&limit=1", headers=headers
    )
    assert query.status_code == 200
    body = query.json()
    assert body["has_more"] and body["next_cursor"]
    continuation = await client.get(
        "/api/v1/traces/query",
        params={"ticket_id": "PERF-1", "limit": 1, "cursor": body["next_cursor"]},
        headers=headers,
    )
    assert continuation.status_code == 200
    assert continuation.json()["events"][0]["action_id"] == second.action_id
    exported = await client.get(
        "/api/v1/traces/export",
        params={"ticket_id": "PERF-1", "limit": 1, "format": "json"},
        headers=headers,
    )
    assert exported.status_code == 200
    manifest = exported.json()["manifest"]
    assert manifest["has_more"] and manifest["next_cursor"]
    await client.aclose()


async def test_large_export_manifest_header_is_replaced_with_bounded_digest(
    tmp_path, monkeypatch
) -> None:
    refs = [f"sha256:{index:064x}" for index in range(1600)]

    monkeypatch.setattr(
        traces_api,
        "_verified_blob_refs",
        lambda events, **_kwargs: set(refs),
    )
    app = make_app(tmp_path)
    for index, ref in enumerate(refs):
        app.state.trace_store.insert_event(
            event().model_copy(
                update={
                    "action_id": f"{index + 1:016x}",
                    "input": PayloadDescriptor(blob_ref=ref),
                }
            )
        )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    try:
        response = await client.get(
            "/api/v1/traces/export",
            params={
                "ticket_id": "PERF-1",
                "limit": len(refs),
                "format": "json",
                "detailed": "true",
                "include_payloads": "true",
            },
            headers={"Authorization": "Bearer service"},
        )
    finally:
        await client.aclose()
        app.state.trace_store.close()

    assert response.status_code == 200
    manifest = response.json()["manifest"]
    assert manifest["blob_digests"] == refs
    full_manifest = traces_api._json_dumps(manifest, separators=(",", ":")).encode(
        "utf-8"
    )
    assert len(full_manifest) > traces_api.MAX_TRACE_MANIFEST_HEADER_BYTES
    header = response.headers["X-Trace-Manifest"]
    assert len(header.encode("utf-8")) <= (traces_api.MAX_TRACE_MANIFEST_HEADER_BYTES)
    header_manifest = json.loads(header)
    assert (
        header_manifest["full_manifest_digest"]
        == hashlib.sha256(full_manifest).hexdigest()
    )


async def test_causal_query_continuation_advances_through_expanded_scope(
    tmp_path,
) -> None:
    app = make_app(tmp_path)
    root = app.state.trace_store.insert_event(event())
    child = app.state.trace_store.insert_event(
        event().model_copy(
            update={
                "trace_id": root.trace_id,
                "parent_action_id": root.action_id,
            }
        )
    )
    grandchild = app.state.trace_store.insert_event(
        event().model_copy(
            update={
                "trace_id": root.trace_id,
                "parent_action_id": child.action_id,
            }
        )
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    headers = {"Authorization": "Bearer service"}
    first = await client.get(
        "/api/v1/traces/query",
        params={"action_id": child.action_id, "causal": True, "limit": 1},
        headers=headers,
    )
    continuation = await client.get(
        "/api/v1/traces/query",
        params={
            "action_id": child.action_id,
            "causal": True,
            "limit": 1,
            "cursor": first.json()["next_cursor"],
        },
        headers=headers,
    )

    assert first.status_code == 200, first.text
    assert [item["action_id"] for item in first.json()["events"]] == [root.action_id]
    assert continuation.status_code == 200, continuation.text
    assert [item["action_id"] for item in continuation.json()["events"]] == [
        child.action_id
    ]
    assert continuation.json()["has_more"]
    assert continuation.json()["diagnostics"]["missing_parents"] == []
    final = await client.get(
        "/api/v1/traces/query",
        params={
            "action_id": child.action_id,
            "causal": True,
            "limit": 1,
            "cursor": continuation.json()["next_cursor"],
        },
        headers=headers,
    )
    assert final.status_code == 200, final.text
    assert [item["action_id"] for item in final.json()["events"]] == [
        grandchild.action_id
    ]
    await client.aclose()


async def test_query_accepts_naive_timestamps_without_loading_all_events(
    tmp_path, monkeypatch
) -> None:
    app = make_app(tmp_path)
    occurred_at = datetime(2026, 11, 12, 20, 0, tzinfo=timezone.utc)
    app.state.trace_store.insert_event(
        event().model_copy(update={"occurred_at": occurred_at})
    )

    def forbidden_list_events(*_args, **_kwargs):
        raise AssertionError("trace query fell back to a full table read")

    monkeypatch.setattr(app.state.trace_store, "list_events", forbidden_list_events)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    headers = {"Authorization": "Bearer service"}
    response = await client.get(
        "/api/v1/traces/query",
        params={
            "ticket_id": "PERF-1",
            "since": occurred_at.replace(tzinfo=None).isoformat(),
            "until": occurred_at.isoformat(),
            "limit": 1,
        },
        headers=headers,
    )
    exported = await client.get(
        "/api/v1/traces/export",
        params={
            "ticket_id": "PERF-1",
            "since": occurred_at.replace(tzinfo=None).isoformat(),
            "until": occurred_at.isoformat(),
            "limit": 1,
        },
        headers=headers,
    )
    await client.aclose()

    assert response.status_code == 200, response.text
    assert response.json()["count"] == 1
    assert exported.status_code == 200, exported.text


async def test_query_store_stops_worker_when_client_disconnects() -> None:
    class Store:
        def query_events(self, _query, *, cancelled):
            while not cancelled():
                time.sleep(0.01)
            raise TraceQueryCancelled("trace query cancelled")

    class Request:
        async def is_disconnected(self) -> bool:
            return True

    with pytest.raises(TraceQueryCancelled):
        await _query_store(Request(), Store(), TraceQuery())


async def test_query_store_cancellation_signals_and_observes_worker() -> None:
    entered = threading.Event()
    release = threading.Event()
    observed_cancellation: list[bool] = []
    loop_errors: list[dict[str, object]] = []

    class Store:
        def query_events(self, _query, *, cancelled):
            entered.set()
            release.wait(2)
            observed_cancellation.append(cancelled())
            raise RuntimeError("late worker failure")

    class Request:
        async def is_disconnected(self) -> bool:
            return False

    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
    task = asyncio.create_task(_query_store(Request(), Store(), TraceQuery()))
    try:
        while not entered.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        await asyncio.sleep(0.05)
    finally:
        release.set()
        loop.set_exception_handler(previous_handler)

    assert observed_cancellation == [True]
    assert loop_errors == []


async def test_query_store_observes_worker_that_finishes_during_disconnect_check() -> (
    None
):
    release = threading.Event()
    loop_errors: list[dict[str, object]] = []

    class Store:
        def query_events(self, _query, *, cancelled):
            release.wait(2)
            raise RuntimeError("late failed result")

    class Request:
        async def is_disconnected(self) -> bool:
            release.set()
            await asyncio.sleep(0.02)
            asyncio.current_task().cancel()
            await asyncio.sleep(0)
            return False

    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
    task = asyncio.create_task(_query_store(Request(), Store(), TraceQuery()))
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.02)
    finally:
        release.set()
        loop.set_exception_handler(previous_handler)

    assert loop_errors == []


async def test_cancelled_queries_do_not_consume_shared_executor(tmp_path) -> None:
    store = TraceStore(tmp_path / "trace.db")
    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="trace-query")
    disconnected = asyncio.Event()

    async def is_disconnected() -> bool:
        return disconnected.is_set()

    request = SimpleNamespace(
        is_disconnected=is_disconnected,
        app=SimpleNamespace(
            state=SimpleNamespace(
                trace_query_executor=executor,
                trace_query_shutdown=threading.Event(),
                trace_query_max_rss_bytes=0,
            )
        ),
    )
    tasks = [
        asyncio.create_task(_query_store(request, store, TraceQuery(limit=1)))
        for _ in range(2)
    ]
    try:
        with store._lock:
            await asyncio.sleep(0.25)
            assert (
                await asyncio.wait_for(asyncio.to_thread(lambda: 42), timeout=0.5) == 42
            )
            disconnected.set()
            for task in tasks:
                with pytest.raises(TraceQueryCancelled):
                    await asyncio.wait_for(task, timeout=2)
    finally:
        disconnected.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        executor.shutdown(wait=True)
        store.close()


async def test_cancelled_trace_worker_keeps_admission_until_thread_finishes() -> None:
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="trace-query")
    slots = threading.BoundedSemaphore(1)
    entered = threading.Event()
    release = threading.Event()
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                trace_query_executor=executor,
                trace_query_slots=slots,
            )
        )
    )

    def blocking_worker() -> None:
        entered.set()
        release.wait(2)

    task = asyncio.create_task(traces_api._run_trace_worker(request, blocking_worker))
    try:
        deadline = asyncio.get_running_loop().time() + 1
        while not entered.is_set() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.001)
        assert entered.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert not slots.acquire(blocking=False)
        release.set()
        deadline = asyncio.get_running_loop().time() + 1
        while asyncio.get_running_loop().time() < deadline:
            if slots.acquire(blocking=False):
                slots.release()
                break
            await asyncio.sleep(0.001)
        else:
            raise AssertionError("completed worker did not release admission")
    finally:
        release.set()
        executor.shutdown(wait=True)


async def test_control_executor_survives_blocked_trace_workers() -> None:
    query_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="trace-query")
    control_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="control")
    release = threading.Event()
    blocker = query_executor.submit(release.wait, 2)
    store = SimpleNamespace(
        get_orchestrator_lease=lambda: None,
        count_by_status=lambda: {},
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                store=store,
                trace_health={},
                trace_query_executor=query_executor,
                control_executor=control_executor,
            )
        )
    )
    try:
        response = await asyncio.wait_for(health_endpoint(request), timeout=0.5)
        assert response["status"] == "ok"
        assert response["orchestrator_lease"] == {"active": False}
    finally:
        release.set()
        blocker.result(timeout=2)
        query_executor.shutdown(wait=True)
        control_executor.shutdown(wait=True)


async def test_lease_executor_survives_blocked_health_workers() -> None:
    control_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="control")
    lease_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lease")
    release = threading.Event()
    blocker = control_executor.submit(release.wait, 2)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                control_executor=control_executor,
                lease_executor=lease_executor,
            )
        )
    )
    try:
        result = await asyncio.wait_for(
            run_lease_worker(request, lambda: "renewed"), timeout=0.5
        )
        assert result == "renewed"
    finally:
        release.set()
        blocker.result(timeout=2)
        control_executor.shutdown(wait=True)
        lease_executor.shutdown(wait=True)


async def test_lease_executor_preserves_audit_actor() -> None:
    lease_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lease")
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(lease_executor=lease_executor),
        )
    )
    set_actor("service", "lease-owner", "127.0.0.1")
    try:
        actor = await asyncio.wait_for(
            run_lease_worker(request, get_actor), timeout=0.5
        )
    finally:
        lease_executor.shutdown(wait=True)

    assert actor == {
        "kind": "service",
        "username": "lease-owner",
        "ip": "127.0.0.1",
    }


@pytest.mark.parametrize(
    ("worker", "executor_name", "slots_name", "detail"),
    [
        (run_control_worker, "control_executor", "control_slots", "control worker"),
        (run_lease_worker, "lease_executor", "lease_slots", "lease worker"),
    ],
)
async def test_reserved_worker_rejects_saturated_admission(
    worker, executor_name, slots_name, detail
) -> None:
    class ObservedExecutor(ThreadPoolExecutor):
        def __init__(self) -> None:
            super().__init__(max_workers=1)
            self.submissions = 0

        def submit(self, *args, **kwargs):
            self.submissions += 1
            return super().submit(*args, **kwargs)

    executor = ObservedExecutor()
    slots = threading.BoundedSemaphore(1)
    assert slots.acquire(blocking=False)
    state = SimpleNamespace(**{executor_name: executor, slots_name: slots})
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    try:
        with pytest.raises(HTTPException) as caught:
            await worker(request, lambda: "unexpected")
    finally:
        slots.release()
        executor.shutdown(wait=True)

    assert caught.value.status_code == 503
    assert caught.value.detail == f"{detail} queue is full"
    assert executor.submissions == 0


@pytest.mark.parametrize(
    ("path", "render_name", "body_key"),
    [
        ("/api/v1/traces/query", "_render_query_response", "events"),
        ("/api/v1/traces/export", "_render_export_response", "manifest"),
    ],
)
async def test_trace_response_is_preserialized_off_event_loop(
    tmp_path, monkeypatch, path, render_name, body_key
) -> None:
    app = make_app(tmp_path)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="trace-query")
    app.state.trace_query_executor = executor
    app.state.trace_query_shutdown = threading.Event()
    app.state.trace_query_max_rss_bytes = 0
    app.state.trace_store.insert_event(event())
    render_threads: list[str] = []
    real_render = getattr(
        __import__("state_store.api.traces", fromlist=[render_name]), render_name
    )

    def record_render(*args, **kwargs):
        render_threads.append(threading.current_thread().name)
        return real_render(*args, **kwargs)

    def fail_serialization(*_args, **_kwargs):
        raise AssertionError("FastAPI serialized a trace response on the event loop")

    monkeypatch.setattr(f"state_store.api.traces.{render_name}", record_render)
    monkeypatch.setattr("fastapi.routing.serialize_response", fail_serialization)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    try:
        response = await client.get(
            path,
            params={"ticket_id": "PERF-1", "limit": 1},
            headers={"Authorization": "Bearer service"},
        )
    finally:
        await client.aclose()
        executor.shutdown(wait=True)
        app.state.trace_store.close()

    assert response.status_code == 200, response.text
    assert body_key in response.json()
    assert render_threads
    assert render_threads[0].startswith("trace-query")
    assert render_threads[0] != threading.main_thread().name


@pytest.mark.parametrize(
    ("send_error", "outcome", "error_prefix"),
    [
        (None, "success", None),
        (RuntimeError("ASGI send failed"), "failed", "trace query failed"),
        (asyncio.CancelledError(), "cancelled", "request cancelled"),
    ],
)
async def test_trace_audit_response_records_once_after_delivery(
    send_error, outcome, error_prefix
) -> None:
    audits: list[tuple] = []
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=trace_audit_state(
                SimpleNamespace(log=lambda *args: audits.append(args))
            )
        )
    )
    response = _TraceAuditResponse(
        Response(b'{"events":[]}', media_type="application/json"),
        request=request,
        operation="trace_query",
        ticket_id="PERF-1",
    )
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        sent.append(message)
        if message["type"] == "http.response.body" and send_error is not None:
            raise send_error

    if send_error is None:
        await response({"type": "http"}, receive, send)
        assert [message["type"] for message in sent] == [
            "http.response.start",
            "http.response.body",
        ]
    else:
        with pytest.raises(type(send_error)):
            await response({"type": "http"}, receive, send)

    assert len(audits) == 1
    assert audits[0][0] == "trace_query"
    assert audits[0][1] == "PERF-1"
    assert audits[0][2]["outcome"] == outcome
    if error_prefix is not None:
        assert audits[0][2]["error"].startswith(error_prefix)


@pytest.mark.parametrize(
    ("send_error", "outcome", "error"),
    [
        (None, "success", ""),
        (RuntimeError("ASGI send failed"), "failed", "trace payload read failed"),
        (asyncio.CancelledError(), "cancelled", "request cancelled"),
    ],
)
async def test_payload_audit_response_records_once_after_delivery(
    send_error, outcome, error
) -> None:
    audits: list[tuple[str, str]] = []

    async def audit(audit_outcome: str, audit_error: str) -> None:
        audits.append((audit_outcome, audit_error))

    response = _PayloadAuditResponse(
        Response(b"payload", media_type="application/octet-stream"), audit=audit
    )
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        sent.append(message)
        if message["type"] == "http.response.body" and send_error is not None:
            raise send_error

    if send_error is None:
        await response({"type": "http"}, receive, send)
        assert [message["type"] for message in sent] == [
            "http.response.start",
            "http.response.body",
        ]
    else:
        with pytest.raises(type(send_error)):
            await response({"type": "http"}, receive, send)

    assert len(audits) == 1
    assert audits[0][0] == outcome
    assert audits[0][1].startswith(error)


@pytest.mark.parametrize(
    ("path", "operation"),
    [
        ("/api/v1/traces/query", "trace_query"),
        ("/api/v1/traces/export", "trace_export"),
        ("/api/v1/traces/tickets/PERF-1", "trace_query"),
        ("/api/v1/traces/invocations/invocation-1", "trace_query"),
        ("/api/v1/traces/actions/action-1", "trace_query"),
        ("/api/v1/traces/PERF-1/payloads/" + "a" * 64, "trace_payload_read"),
    ],
)
def test_trace_read_operation_covers_alias_routes(path, operation) -> None:
    assert _trace_read_operation(path) == operation


async def test_pre_route_trace_validation_is_audited_for_alias_routes(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("state_store.main.TRACE_DB_PATH", tmp_path / "trace.db")
    app = create_app(initialize_immediately=True)
    audits: list[tuple] = []
    audit_threads: list[str] = []
    original_audit_log = app.state.audit_log

    def audit(*args) -> None:
        audits.append(args)
        audit_threads.append(threading.current_thread().name)

    app.state.audit_log = SimpleNamespace(log=audit)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    try:
        query = await client.get(
            "/api/v1/traces/query",
            params={"ticket_id": "PERF-1", "limit": 0},
            headers={"Authorization": f"Bearer {app.state.api_token}"},
        )
        alias = await client.get(
            "/api/v1/traces/tickets/PERF-1",
            params={"limit": 0},
            headers={"Authorization": f"Bearer {app.state.api_token}"},
        )
        payload_read = await client.get("/api/v1/traces/PERF-1/payloads/" + "a" * 64)
    finally:
        await client.aclose()
        app.state.audit_log = original_audit_log
        _close_runtime(app)

    assert query.status_code == 422
    assert alias.status_code == 422
    assert payload_read.status_code == 401
    assert audits == [
        (
            "trace_query",
            "PERF-1",
            {
                "outcome": "denied",
                "error": "request validation rejected",
            },
        ),
        (
            "trace_query",
            "PERF-1",
            {
                "outcome": "denied",
                "error": "request validation rejected",
            },
        ),
        (
            "trace_payload_read",
            "PERF-1",
            {
                "outcome": "denied",
                "error": "authentication rejected",
            },
        ),
    ]
    assert all("trace-query-audit" in name for name in audit_threads)


async def test_pre_route_denied_audit_preserves_actor_identity(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("state_store.main.TRACE_DB_PATH", tmp_path / "trace.db")
    app = create_app(initialize_immediately=True)
    actors: list[dict[str, str]] = []
    original_audit_log = app.state.audit_log

    def audit(*_args) -> None:
        actors.append(get_actor())

    app.state.audit_log = SimpleNamespace(log_required=audit)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    try:
        response = await client.get(
            "/api/v1/traces/query",
            params={"ticket_id": "PERF-1", "limit": 0},
            headers={"Authorization": f"Bearer {app.state.api_token}"},
        )
    finally:
        await client.aclose()
        app.state.audit_log = original_audit_log
        _close_runtime(app)

    assert response.status_code == 422
    assert actors == [{"kind": "service", "username": "deployment", "ip": "127.0.0.1"}]


async def test_pre_route_denied_audit_failure_preserves_validation_response(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("state_store.main.TRACE_DB_PATH", tmp_path / "trace.db")
    app = create_app(initialize_immediately=True)
    original_audit_log = app.state.audit_log
    app.state.audit_log = SimpleNamespace(
        log_required=lambda *_args: (_ for _ in ()).throw(
            TraceStoreWriteError("audit store unavailable")
        )
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    try:
        response = await client.get(
            "/api/v1/traces/query",
            params={"ticket_id": "PERF-1", "limit": 0},
            headers={"Authorization": f"Bearer {app.state.api_token}"},
        )
    finally:
        await client.aclose()
        app.state.audit_log = original_audit_log
        _close_runtime(app)

    assert response.status_code == 422


async def test_memory_guard_returns_audited_503(tmp_path) -> None:
    app = make_app(tmp_path)
    app.state.trace_query_max_rss_bytes = 1
    audits: list[tuple] = []
    app.state.audit_log = SimpleNamespace(log=lambda *args: audits.append(args))
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    try:
        response = await client.get(
            "/api/v1/traces/query",
            params={"ticket_id": "PERF-1"},
            headers={"Authorization": "Bearer service"},
        )
    finally:
        await client.aclose()
        app.state.trace_store.close()

    assert response.status_code == 503
    assert audits == [
        (
            "trace_query",
            "PERF-1",
            {
                "outcome": "failed",
                "error": (
                    "trace queries are temporarily unavailable while the state "
                    "store memory limit is exceeded"
                ),
                "ticket_scoped": True,
            },
        )
    ]


@pytest.mark.parametrize(
    ("path", "operation"),
    [
        ("/api/v1/traces/query", "trace_query"),
        ("/api/v1/traces/export", "trace_export"),
    ],
)
async def test_full_query_queue_returns_audited_503(
    path, operation, tmp_path, monkeypatch
) -> None:
    authorization_attempts = 0

    def unexpected_authorization(*_args, **_kwargs):
        nonlocal authorization_attempts
        authorization_attempts += 1
        return True

    class ObservedExecutor(ThreadPoolExecutor):
        def __init__(self) -> None:
            super().__init__(max_workers=1)
            self.submissions = 0

        def submit(self, *args, **kwargs):
            self.submissions += 1
            return super().submit(*args, **kwargs)

    monkeypatch.setattr(traces_api, "_authorize_query", unexpected_authorization)
    app = make_app(tmp_path)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="trace-query")
    audit_executor = ObservedExecutor()
    slots = threading.BoundedSemaphore(1)
    assert slots.acquire(blocking=False)
    app.state.trace_query_executor = executor
    app.state.trace_query_slots = slots
    app.state.trace_query_audit_executor = audit_executor
    app.state.trace_query_audit_slots = threading.BoundedSemaphore(1)
    app.state.trace_query_shutdown = threading.Event()
    app.state.trace_query_max_rss_bytes = 0
    audits: list[tuple] = []
    app.state.audit_log = SimpleNamespace(log=lambda *args: audits.append(args))
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    try:
        response = await client.get(
            path,
            params={"ticket_id": "PERF-1"},
            headers={"Authorization": "Bearer service"},
        )
    finally:
        slots.release()
        await client.aclose()
        executor.shutdown(wait=True)
        audit_executor.shutdown(wait=True)
        app.state.trace_store.close()

    assert response.status_code == 503
    assert response.json()["detail"] == "trace query queue is full"
    assert authorization_attempts == 0
    assert audit_executor.submissions == 1
    assert audits == [
        (
            operation,
            "PERF-1",
            {
                "outcome": "failed",
                "error": "trace query queue is full",
                "ticket_scoped": True,
            },
        )
    ]


@pytest.mark.parametrize(
    ("path", "operation"),
    [
        ("/api/v1/traces/query", "trace_query"),
        ("/api/v1/traces/export", "trace_export"),
    ],
)
async def test_render_queue_saturation_returns_audited_503(
    path, operation, tmp_path, monkeypatch
) -> None:
    app = make_app(tmp_path)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="trace-query")
    audit_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="audit")
    slots = threading.BoundedSemaphore(1)
    slot_held = False

    async def saturate_after_store(*_args, **_kwargs):
        nonlocal slot_held
        slot_held = slots.acquire(blocking=False)
        assert slot_held
        return [], []

    monkeypatch.setattr(traces_api, "_query_store", saturate_after_store)
    app.state.trace_query_executor = executor
    app.state.trace_query_slots = slots
    app.state.trace_query_audit_executor = audit_executor
    app.state.trace_query_audit_slots = threading.BoundedSemaphore(1)
    app.state.trace_query_shutdown = threading.Event()
    app.state.trace_query_max_rss_bytes = 0
    audits: list[tuple] = []
    app.state.audit_log = SimpleNamespace(log=lambda *args: audits.append(args))
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    try:
        response = await client.get(
            path,
            params={"ticket_id": "PERF-1"},
            headers={"Authorization": "Bearer service"},
        )
    finally:
        if slot_held:
            slots.release()
        await client.aclose()
        executor.shutdown(wait=True)
        audit_executor.shutdown(wait=True)
        app.state.trace_store.close()

    assert response.status_code == 503
    assert response.json()["detail"] == "trace query queue is full"
    assert audits == [
        (
            operation,
            "PERF-1",
            {
                "outcome": "failed",
                "error": "trace query queue is full",
                "ticket_scoped": True,
            },
        )
    ]


async def test_payload_lookup_uses_bounded_trace_worker(tmp_path, monkeypatch) -> None:
    app = make_app(tmp_path)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="trace-query")
    slots = threading.BoundedSemaphore(1)
    worker_threads: list[str] = []
    digest = "a" * 64

    def payload_is_referenced(_ticket_id, _blob_ref, *, cancelled):
        assert not cancelled()
        worker_threads.append(threading.current_thread().name)
        return True

    def unexpected_list_events(*_args, **_kwargs):
        raise AssertionError("payload lookup materialized ticket history")

    app.state.trace_query_executor = executor
    app.state.trace_query_slots = slots
    app.state.trace_query_shutdown = threading.Event()
    app.state.trace_store.payload_is_referenced = payload_is_referenced
    app.state.trace_store.list_events = unexpected_list_events
    app.state.audit_log = SimpleNamespace(log=lambda *_args: None)
    monkeypatch.setattr(
        traces_api,
        "PayloadBlobStore",
        lambda **_kwargs: SimpleNamespace(get=lambda *_args, **_kwargs: b"payload"),
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    try:
        response = await client.get(
            f"/api/v1/traces/PERF-1/payloads/{digest}",
            headers={"Authorization": "Bearer service"},
        )
    finally:
        await client.aclose()
        executor.shutdown(wait=True)
        app.state.trace_store.close()

    assert response.status_code == 200
    assert response.content == b"payload"
    assert worker_threads
    assert worker_threads[0].startswith("trace-query")


async def test_full_payload_queue_returns_audited_503(tmp_path, monkeypatch) -> None:
    app = make_app(tmp_path)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="trace-query")
    slots = threading.BoundedSemaphore(1)
    assert slots.acquire(blocking=False)
    digest = "a" * 64
    audits: list[tuple] = []

    async def authorize(*_args, **_kwargs):
        return True

    monkeypatch.setattr(traces_api, "_authorize_query_off_loop", authorize)
    app.state.trace_query_executor = executor
    app.state.trace_query_slots = slots
    app.state.trace_query_shutdown = threading.Event()
    app.state.audit_log = SimpleNamespace(log=lambda *args: audits.append(args))
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    try:
        response = await client.get(
            f"/api/v1/traces/PERF-1/payloads/{digest}",
            headers={"Authorization": "Bearer service"},
        )
    finally:
        slots.release()
        await client.aclose()
        executor.shutdown(wait=True)
        app.state.trace_store.close()

    assert response.status_code == 503
    assert response.json()["detail"] == "trace query queue is full"
    assert audits == [
        (
            "trace_payload_read",
            "PERF-1",
            {
                "digest": digest,
                "outcome": "failed",
                "error": "trace query queue is full",
            },
        )
    ]


async def test_payload_audit_failure_preserves_404(tmp_path) -> None:
    app = make_app(tmp_path)
    digest = "a" * 64
    app.state.audit_log = SimpleNamespace(
        log_required=lambda *_args: (_ for _ in ()).throw(
            TraceStoreWriteError("audit store unavailable")
        )
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    try:
        response = await client.get(
            f"/api/v1/traces/PERF-1/payloads/{digest}",
            headers={"Authorization": "Bearer service"},
        )
    finally:
        await client.aclose()
        app.state.trace_store.close()

    assert response.status_code == 404
    assert response.json()["detail"] == "payload not found"


async def test_payload_audit_failure_preserves_delivered_200(
    tmp_path, monkeypatch
) -> None:
    app = make_app(tmp_path)
    digest = "a" * 64
    app.state.trace_store.payload_is_referenced = (
        lambda _ticket_id, _blob_ref, *, cancelled: True
    )
    app.state.audit_log = SimpleNamespace(
        log_required=lambda *_args: (_ for _ in ()).throw(
            TraceStoreWriteError("audit store unavailable")
        )
    )
    monkeypatch.setattr(
        traces_api,
        "PayloadBlobStore",
        lambda **_kwargs: SimpleNamespace(get=lambda *_args, **_kwargs: b"payload"),
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    try:
        response = await client.get(
            f"/api/v1/traces/PERF-1/payloads/{digest}",
            headers={"Authorization": "Bearer service"},
        )
    finally:
        await client.aclose()
        app.state.trace_store.close()

    assert response.status_code == 200
    assert response.content == b"payload"


async def test_payload_cancellation_records_audit(tmp_path) -> None:
    app = make_app(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    worker_done = threading.Event()
    audits: list[tuple] = []
    digest = "a" * 64

    def payload_is_referenced(_ticket_id, _blob_ref, *, cancelled):
        entered.set()
        if not release.wait(1):
            raise RuntimeError("payload worker was not released")
        worker_done.set()
        return True

    app.state.trace_store.payload_is_referenced = payload_is_referenced
    app.state.audit_log = SimpleNamespace(log=lambda *args: audits.append(args))
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    task = asyncio.create_task(
        client.get(
            f"/api/v1/traces/PERF-1/payloads/{digest}",
            headers={"Authorization": "Bearer service"},
        )
    )
    try:
        while not entered.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        await asyncio.to_thread(worker_done.wait, 1)
        await client.aclose()
        app.state.trace_store.close()

    assert audits == [
        (
            "trace_payload_read",
            "PERF-1",
            {
                "digest": digest,
                "outcome": "cancelled",
                "error": "request cancelled",
            },
        )
    ]


async def test_full_audit_queue_writes_strict_fallback_and_fails_closed(
    tmp_path, caplog
) -> None:
    class ObservedExecutor(ThreadPoolExecutor):
        def __init__(self) -> None:
            super().__init__(max_workers=1)
            self.submissions = 0

        def submit(self, *args, **kwargs):
            self.submissions += 1
            return super().submit(*args, **kwargs)

    app = make_app(tmp_path)
    audit_executor = ObservedExecutor()
    slots = threading.BoundedSemaphore(1)
    assert slots.acquire(blocking=False)
    app.state.trace_query_audit_executor = audit_executor
    app.state.trace_query_audit_slots = slots
    audits: list[tuple] = []
    audit_threads: list[str] = []
    app.state.audit_log = SimpleNamespace(
        log_required=lambda *args: (
            audits.append(args),
            audit_threads.append(threading.current_thread().name),
        )
    )
    request = SimpleNamespace(app=SimpleNamespace(state=app.state))
    try:
        with pytest.raises(HTTPException) as exc_info:
            await asyncio.wait_for(
                _record_query_audit(request, "trace_query", "PERF-1", "failed"),
                timeout=0.5,
            )
    finally:
        slots.release()
        audit_executor.shutdown(wait=True)
        app.state.trace_store.close()

    assert audit_executor.submissions == 0
    assert audit_threads[0].startswith("test-trace-audit-fallback")
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "trace audit capacity is full"
    assert audits == [
        (
            "trace_query",
            "PERF-1",
            {"outcome": "failed", "error": None, "ticket_scoped": True},
        )
    ]
    assert app.state.trace_health["audit_queue_fallbacks"] == 1
    assert "using dedicated strict fallback" in caplog.text


async def test_full_audit_queue_strict_fallback_failure_is_not_swallowed(
    tmp_path,
) -> None:
    app = make_app(tmp_path)
    slots = threading.BoundedSemaphore(1)
    assert slots.acquire(blocking=False)
    app.state.trace_query_audit_executor = ThreadPoolExecutor(max_workers=1)
    app.state.trace_query_audit_slots = slots

    def fail_required(*_args) -> None:
        raise TraceStoreWriteError("audit store unavailable")

    app.state.audit_log = SimpleNamespace(log_required=fail_required)
    request = SimpleNamespace(app=SimpleNamespace(state=app.state))
    try:
        with pytest.raises(TraceStoreWriteError, match="audit store unavailable"):
            await _record_query_audit(request, "trace_query", "PERF-1", "failed")
    finally:
        slots.release()
        app.state.trace_query_audit_executor.shutdown(wait=True)
        app.state.trace_store.close()


async def test_saturated_strict_fallback_fails_closed(tmp_path) -> None:
    app = make_app(tmp_path)
    primary_slots = threading.BoundedSemaphore(1)
    fallback_slots = threading.BoundedSemaphore(1)
    assert primary_slots.acquire(blocking=False)
    assert fallback_slots.acquire(blocking=False)
    app.state.trace_query_audit_slots = primary_slots
    app.state.trace_query_audit_fallback_slots = fallback_slots
    audits: list[tuple] = []
    app.state.audit_log = SimpleNamespace(
        log_required=lambda *args: audits.append(args)
    )
    request = SimpleNamespace(app=SimpleNamespace(state=app.state))
    try:
        with pytest.raises(HTTPException) as exc_info:
            await _record_query_audit(
                request, "trace_query", "PERF-1", "failed", "query failed"
            )
    finally:
        primary_slots.release()
        fallback_slots.release()
        app.state.trace_store.close()

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "trace audit capacity is full"
    assert audits == []
    assert app.state.trace_health["audit_queue_fallbacks"] == 1


@pytest.mark.parametrize(
    ("path", "operation"),
    [
        ("/api/v1/traces/query", "trace_query"),
        ("/api/v1/traces/export", "trace_export"),
    ],
)
async def test_trace_response_reserves_audit_slot_before_delivery(
    path, operation, tmp_path
) -> None:
    app = make_app(tmp_path)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="audit")
    slots = threading.BoundedSemaphore(1)
    assert slots.acquire(blocking=False)
    app.state.trace_query_audit_executor = executor
    app.state.trace_query_audit_slots = slots
    audits: list[tuple] = []
    app.state.audit_log = SimpleNamespace(
        log_required=lambda *args: audits.append(args)
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    try:
        response = await client.get(
            path,
            params={"ticket_id": "PERF-1"},
            headers={"Authorization": "Bearer service"},
        )
    finally:
        await client.aclose()
        executor.shutdown(wait=True)
        app.state.trace_store.close()

    assert response.status_code == 503
    assert response.json()["detail"] == "trace audit capacity is full"
    assert audits == [
        (
            operation,
            "PERF-1",
            {
                "outcome": "failed",
                "error": "trace audit capacity is full",
                "ticket_scoped": True,
            },
        )
    ]


async def test_trace_response_capacity_unavailable_fails_closed_without_delivery(
    tmp_path, monkeypatch
) -> None:
    app = make_app(tmp_path)
    audits: list[tuple] = []
    app.state.audit_log = SimpleNamespace(
        log_required=lambda *args: audits.append(args)
    )
    original_reserve = traces_api._reserve_trace_audit
    reserve_calls = 0

    def reserve_once_then_succeed(request):
        nonlocal reserve_calls
        reserve_calls += 1
        if reserve_calls == 1:
            return None
        return original_reserve(request)

    monkeypatch.setattr(traces_api, "_reserve_trace_audit", reserve_once_then_succeed)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    try:
        response = await client.get(
            "/api/v1/traces/query",
            params={"ticket_id": "PERF-1"},
            headers={"Authorization": "Bearer service"},
        )
    finally:
        await client.aclose()
        app.state.trace_store.close()

    assert response.status_code == 503
    assert response.json()["detail"] == "trace audit capacity is full"
    assert reserve_calls == 1
    assert audits == [
        (
            "trace_query",
            "PERF-1",
            {
                "outcome": "failed",
                "error": "trace audit capacity is full",
                "ticket_scoped": True,
            },
        )
    ]


async def test_absent_primary_audit_executor_falls_back_and_fails_closed() -> None:
    audits: list[tuple] = []
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                audit_log=SimpleNamespace(log=lambda *args: audits.append(args)),
                trace_query_audit_fallback_executor=_TEST_AUDIT_FALLBACK_EXECUTOR,
                trace_query_audit_fallback_slots=(_TEST_AUDIT_FALLBACK_EXECUTOR.slots),
            )
        )
    )

    with pytest.raises(HTTPException) as exc_info:
        await _record_query_audit(
            request, "trace_query", "PERF-1", "failed", "store failure"
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "trace audit capacity is full"

    assert audits == [
        (
            "trace_query",
            "PERF-1",
            {
                "outcome": "failed",
                "error": "store failure",
                "ticket_scoped": True,
            },
        )
    ]


async def test_trace_response_without_audit_log_delivers_without_reservation() -> None:
    sent: list[str] = []
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    response = _TraceAuditResponse(
        Response(b"ok"),
        request=request,
        operation="trace_query",
        ticket_id="PERF-1",
    )

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message["type"])

    await response({"type": "http"}, receive, send)

    assert sent == ["http.response.start", "http.response.body"]


async def test_trace_response_send_timeout_after_start_records_indeterminate_audit(
    monkeypatch,
) -> None:
    monkeypatch.setattr(traces_api, "TRACE_AUDIT_RESPONSE_SEND_TIMEOUT_SECONDS", 1.0)
    audits: list[tuple] = []
    sent: list[str] = []
    body_started = asyncio.Event()

    def log_audit(*args) -> None:
        audits.append(args)

    request = SimpleNamespace(
        app=SimpleNamespace(state=trace_audit_state(SimpleNamespace(log=log_audit)))
    )

    response = _TraceAuditResponse(
        Response(b"ok"),
        request=request,
        operation="trace_query",
        ticket_id="PERF-1",
    )

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message["type"])
        if message["type"] == "http.response.body":
            body_started.set()
            await asyncio.sleep(3600)

    task = asyncio.create_task(response({"type": "http"}, receive, send))
    await asyncio.wait_for(body_started.wait(), timeout=1)
    await asyncio.wait_for(task, timeout=2)

    assert sent == ["http.response.start", "http.response.body"]
    assert audits == [
        (
            "trace_query",
            "PERF-1",
            {
                "outcome": "indeterminate",
                "error": "trace audit response send timed out",
                "ticket_scoped": True,
            },
        )
    ]


async def test_trace_response_send_timeout_survives_fastapi_middleware(
    monkeypatch,
) -> None:
    monkeypatch.setattr(traces_api, "TRACE_AUDIT_RESPONSE_SEND_TIMEOUT_SECONDS", 1.0)
    audits: list[tuple] = []
    sent: list[str] = []
    body_started = asyncio.Event()
    app = FastAPI()

    class BlockingSendMiddleware:
        def __init__(self, downstream_app: object) -> None:
            self.app = downstream_app

        async def __call__(self, scope, receive, send) -> None:
            async def delayed_body_send(message: dict) -> None:
                await send(message)
                sent.append(message["type"])
                if message["type"] == "http.response.body" and message.get("body"):
                    body_started.set()
                    await asyncio.sleep(1.0)

            await self.app(scope, receive, delayed_body_send)

    app.add_middleware(BlockingSendMiddleware)

    @app.get("/")
    async def trace_query() -> Response:
        request = SimpleNamespace(
            app=SimpleNamespace(state=trace_audit_state(SimpleNamespace(log=audit)))
        )
        return _TraceAuditResponse(
            Response(b"ok"),
            request=request,
            operation="trace_query",
            ticket_id="PERF-1",
        )

    def audit(*args) -> None:
        audits.append(args)

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message: dict) -> None:
        pass

    task = asyncio.create_task(
        app(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "GET",
                "path": "/",
                "raw_path": b"/",
                "root_path": "",
                "scheme": "http",
                "query_string": b"",
                "headers": [],
                "client": ("127.0.0.1", 12345),
                "server": ("test", 80),
            },
            receive,
            send,
        )
    )
    await asyncio.wait_for(body_started.wait(), timeout=1)
    await asyncio.wait_for(task, timeout=2)

    assert sent == ["http.response.start", "http.response.body"]
    assert audits == [
        (
            "trace_query",
            "PERF-1",
            {
                "outcome": "indeterminate",
                "error": "trace audit response send timed out",
                "ticket_scoped": True,
            },
        )
    ]


async def test_cancellation_during_send_timeout_audit_preserves_failed_outcome(
    monkeypatch,
) -> None:
    monkeypatch.setattr(traces_api, "TRACE_AUDIT_RESPONSE_SEND_TIMEOUT_SECONDS", 0.1)
    audit_started = threading.Event()
    release = threading.Event()
    audit_done = threading.Event()
    audits: list[tuple] = []

    def log_audit(*args) -> None:
        audit_started.set()
        if not release.wait(1):
            raise RuntimeError("timeout audit was not released")
        audits.append(args)
        audit_done.set()

    request = SimpleNamespace(
        app=SimpleNamespace(state=trace_audit_state(SimpleNamespace(log=log_audit)))
    )
    response = _TraceAuditResponse(
        Response(b"ok"),
        request=request,
        operation="trace_query",
        ticket_id="PERF-1",
    )

    async def receive() -> dict:
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        if message["type"] == "http.response.start":
            await asyncio.sleep(3600)

    task = asyncio.create_task(response({"type": "http"}, receive, send))
    try:
        await asyncio.wait_for(asyncio.to_thread(audit_started.wait, 1), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        await asyncio.to_thread(audit_done.wait, 1)
        await asyncio.gather(task, return_exceptions=True)

    assert [audit[2]["outcome"] for audit in audits] == ["failed"]


async def test_success_audit_failure_preserves_delivered_response() -> None:
    sent: list[dict] = []

    def log_audit(*_args) -> None:
        raise RuntimeError("success audit failed")

    request = SimpleNamespace(
        app=SimpleNamespace(state=trace_audit_state(SimpleNamespace(log=log_audit)))
    )
    response = _TraceAuditResponse(
        Response(b"ok", status_code=200),
        request=request,
        operation="trace_query",
        ticket_id="PERF-1",
    )

    async def receive() -> dict:
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        sent.append(message)

    await response({"type": "http"}, receive, send)

    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 200
    assert sent[1] == {
        "type": "http.response.body",
        "body": b"ok",
    }


async def test_trace_response_send_timeout_before_start_records_failed_audit(
    monkeypatch,
) -> None:
    monkeypatch.setattr(traces_api, "TRACE_AUDIT_RESPONSE_SEND_TIMEOUT_SECONDS", 0.1)
    audits: list[tuple] = []
    sent: list[dict] = []

    def log_audit(*args) -> None:
        audits.append(args)

    request = SimpleNamespace(
        app=SimpleNamespace(state=trace_audit_state(SimpleNamespace(log=log_audit)))
    )
    response = _TraceAuditResponse(
        Response(b"ok"),
        request=request,
        operation="trace_query",
        ticket_id="PERF-1",
    )

    async def receive() -> dict:
        return {"type": "http.disconnect"}

    send_attempts = 0

    async def send(message: dict) -> None:
        nonlocal send_attempts
        send_attempts += 1
        if send_attempts == 1:
            await asyncio.sleep(3600)
        sent.append(message)

    await response({"type": "http"}, receive, send)

    assert sent[0]["status"] == 503
    assert sent[0]["type"] == "http.response.start"
    assert len(sent) == 2
    assert sent[1]["type"] == "http.response.body"
    assert audits[0][2]["outcome"] == "failed"


async def test_legacy_timestamp_cursor_continues_without_global_sequence() -> None:
    first = event().model_copy(
        update={
            "global_seq": None,
            "occurred_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
        }
    )
    second = event().model_copy(
        update={
            "global_seq": None,
            "occurred_at": datetime(2024, 1, 2, tzinfo=timezone.utc),
        }
    )
    selected, has_more, cursor = traces_api.page_events(
        [first, second], cursor=None, limit=1
    )
    continuation, continuation_has_more, next_cursor = traces_api.page_events(
        [first, second], cursor=cursor, limit=1
    )

    assert selected == [first]
    assert has_more
    assert traces_api._cursor_sequence(cursor) == 0
    assert continuation == [second]
    assert not continuation_has_more
    assert next_cursor is None


async def test_cancelled_query_audit_failure_preserves_cancellation() -> None:
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=trace_audit_state(
                SimpleNamespace(
                    log_required=lambda *_args: (_ for _ in ()).throw(
                        TraceStoreWriteError("audit store unavailable")
                    )
                )
            )
        )
    )

    await _record_cancelled_query(request, "trace_query", "PERF-1")


@pytest.mark.parametrize(
    ("endpoint", "operation"),
    [(query_trace, "trace_query"), (export_trace, "trace_export")],
)
async def test_handler_denied_audit_failure_preserves_403(endpoint, operation) -> None:
    request = SimpleNamespace(
        state=SimpleNamespace(
            principal=Principal(kind="user", username="other", is_admin=False)
        ),
        app=SimpleNamespace(
            state=SimpleNamespace(
                multi_user=True,
                store=SimpleNamespace(
                    get_ticket=lambda _ticket_id: SimpleNamespace(owners=["owner"])
                ),
                **{
                    "audit_log": SimpleNamespace(
                        log_required=lambda *_args: (_ for _ in ()).throw(
                            TraceStoreWriteError("audit store unavailable")
                        )
                    ),
                    "trace_query_audit_executor": _TEST_AUDIT_EXECUTOR,
                    "trace_query_audit_slots": _TEST_AUDIT_EXECUTOR.slots,
                    "trace_query_audit_fallback_executor": (
                        _TEST_AUDIT_FALLBACK_EXECUTOR
                    ),
                    "trace_query_audit_fallback_slots": (
                        _TEST_AUDIT_FALLBACK_EXECUTOR.slots
                    ),
                },
            )
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        await endpoint(request, ticket_id="PERF-1", limit=1)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "trace access requires ticket ownership"


@pytest.mark.parametrize(
    ("endpoint", "operation", "render_name"),
    [
        (query_trace, "trace_query", "_render_query_response"),
        (export_trace, "trace_export", "_render_export_response"),
    ],
)
@pytest.mark.parametrize("phase", ["store", "authorization", "rendering"])
async def test_unexpected_trace_failures_audit_once(
    endpoint, operation, render_name, phase, monkeypatch
) -> None:
    audits: list[tuple] = []

    async def is_disconnected() -> bool:
        return False

    def query_events(*_args, **_kwargs):
        if phase == "store":
            raise sqlite3.OperationalError("database is unavailable")
        return []

    def get_ticket(_ticket_id):
        raise RuntimeError("ticket store failed")

    if phase == "rendering":
        monkeypatch.setattr(
            f"state_store.api.traces.{render_name}",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("rendering failed")
            ),
        )
    request = SimpleNamespace(
        state=SimpleNamespace(
            principal=Principal(
                kind="user" if phase == "authorization" else "service",
                username="owner",
                is_admin=False,
            )
        ),
        app=SimpleNamespace(
            state=SimpleNamespace(
                multi_user=phase == "authorization",
                store=SimpleNamespace(get_ticket=get_ticket),
                trace_store=SimpleNamespace(query_events=query_events),
                trace_query_max_rss_bytes=0,
                trace_query_shutdown=threading.Event(),
                audit_log=SimpleNamespace(log=lambda *args: audits.append(args)),
                trace_query_audit_executor=_TEST_AUDIT_EXECUTOR,
                trace_query_audit_slots=_TEST_AUDIT_EXECUTOR.slots,
                trace_query_audit_fallback_executor=_TEST_AUDIT_FALLBACK_EXECUTOR,
                trace_query_audit_fallback_slots=(_TEST_AUDIT_FALLBACK_EXECUTOR.slots),
            )
        ),
        is_disconnected=is_disconnected,
    )

    with pytest.raises((sqlite3.OperationalError, RuntimeError)):
        await endpoint(request, ticket_id="PERF-1", limit=1)

    assert len(audits) == 1
    assert audits[0][0] == operation
    assert audits[0][2]["outcome"] == "failed"
    assert audits[0][2]["error"].startswith(
        "trace query failed" if operation == "trace_query" else "trace export failed"
    )


@pytest.mark.parametrize(
    ("endpoint", "operation"),
    [(query_trace, "trace_query"), (export_trace, "trace_export")],
)
async def test_trace_task_cancellation_records_audit(endpoint, operation) -> None:
    entered = threading.Event()
    stopped = threading.Event()
    audits: list[tuple] = []

    def query_events(_query, *, cancelled):
        entered.set()
        while not cancelled():
            stopped.wait(0.005)
        stopped.set()
        raise TraceQueryCancelled("trace query cancelled")

    def log(*args) -> None:
        audits.append(args)

    async def is_disconnected() -> bool:
        return False

    request = SimpleNamespace(
        state=SimpleNamespace(
            principal=Principal(kind="service", username="service", is_admin=False)
        ),
        app=SimpleNamespace(
            state=SimpleNamespace(
                trace_store=SimpleNamespace(query_events=query_events),
                audit_log=SimpleNamespace(log=log),
                trace_query_audit_executor=_TEST_AUDIT_EXECUTOR,
                trace_query_audit_slots=_TEST_AUDIT_EXECUTOR.slots,
                trace_query_audit_fallback_executor=_TEST_AUDIT_FALLBACK_EXECUTOR,
                trace_query_audit_fallback_slots=(_TEST_AUDIT_FALLBACK_EXECUTOR.slots),
            )
        ),
        is_disconnected=is_disconnected,
    )
    task = asyncio.create_task(endpoint(request, ticket_id="PERF-1", limit=1))
    try:
        while not entered.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.to_thread(stopped.wait, 1)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert audits == [
        (
            operation,
            "PERF-1",
            {
                "outcome": "cancelled",
                "error": "request cancelled",
                "ticket_scoped": True,
            },
        )
    ]


@pytest.mark.parametrize(
    ("endpoint", "operation", "render_name"),
    [
        (query_trace, "trace_query", "_render_query_response"),
        (export_trace, "trace_export", "_render_export_response"),
    ],
)
@pytest.mark.parametrize("phase", ["authorization", "rendering"])
async def test_trace_cancellation_outside_store_records_audit(
    endpoint, operation, render_name, phase, monkeypatch
) -> None:
    entered = threading.Event()
    release = threading.Event()
    worker_done = threading.Event()
    audits: list[tuple] = []

    def block(result):
        entered.set()
        if not release.wait(1):
            raise RuntimeError("cancelled worker was not released")
        worker_done.set()
        return result

    def get_ticket(_ticket_id):
        return block(SimpleNamespace(owners=["owner"]))

    def log(*args) -> None:
        audits.append(args)

    async def is_disconnected() -> bool:
        return False

    monkeypatch.setattr(
        f"state_store.api.traces.{render_name}",
        lambda *_args, **_kwargs: block(
            {} if endpoint is query_trace else ("{}", "application/json", {})
        ),
    )
    request = SimpleNamespace(
        state=SimpleNamespace(
            principal=Principal(
                kind="user" if phase == "authorization" else "service",
                username="owner",
                is_admin=False,
            )
        ),
        app=SimpleNamespace(
            state=SimpleNamespace(
                multi_user=True,
                store=SimpleNamespace(get_ticket=get_ticket),
                trace_store=SimpleNamespace(query_events=lambda *_args, **_kwargs: []),
                audit_log=SimpleNamespace(log=log),
                trace_query_audit_executor=_TEST_AUDIT_EXECUTOR,
                trace_query_audit_slots=_TEST_AUDIT_EXECUTOR.slots,
                trace_query_audit_fallback_executor=_TEST_AUDIT_FALLBACK_EXECUTOR,
                trace_query_audit_fallback_slots=(_TEST_AUDIT_FALLBACK_EXECUTOR.slots),
            )
        ),
        is_disconnected=is_disconnected,
    )
    task = asyncio.create_task(endpoint(request, ticket_id="PERF-1", limit=1))
    try:
        while not entered.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        await asyncio.to_thread(worker_done.wait, 1)
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert audits == [
        (
            operation,
            "PERF-1",
            {
                "outcome": "cancelled",
                "error": "request cancelled",
                "ticket_scoped": True,
            },
        )
    ]


@pytest.mark.parametrize(
    ("endpoint", "operation"),
    [(query_trace, "trace_query"), (export_trace, "trace_export")],
)
async def test_trace_cancellation_during_terminal_audit_is_recorded_once(
    endpoint, operation
) -> None:
    audit_entered = threading.Event()
    release = threading.Event()
    audit_done = threading.Event()
    audits: list[tuple] = []

    def log(*args) -> None:
        audit_entered.set()
        if not release.wait(1):
            raise RuntimeError("terminal audit was not released")
        audits.append(args)
        audit_done.set()

    async def is_disconnected() -> bool:
        return False

    request = SimpleNamespace(
        state=SimpleNamespace(
            principal=Principal(kind="service", username="service", is_admin=False)
        ),
        app=SimpleNamespace(state=trace_audit_state(SimpleNamespace(log=log))),
        is_disconnected=is_disconnected,
    )
    task = asyncio.create_task(
        endpoint(request, ticket_id="PERF-1", cursor="invalid", limit=1)
    )
    try:
        await asyncio.wait_for(asyncio.to_thread(audit_entered.wait, 1), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        await asyncio.to_thread(audit_done.wait, 1)
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert len(audits) == 1
    assert audits[0][0] == operation
    assert audits[0][2]["outcome"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["trace_query", "trace_export"])
async def test_cancellation_during_response_success_audit_is_recorded_once(
    operation,
) -> None:
    audit_started = threading.Event()
    release = threading.Event()
    audit_done = threading.Event()
    audits: list[tuple] = []

    def log(*args) -> None:
        audit_started.set()
        if not release.wait(1):
            raise RuntimeError("success audit was not released")
        audits.append(args)
        audit_done.set()

    request = SimpleNamespace(
        app=SimpleNamespace(state=trace_audit_state(SimpleNamespace(log=log)))
    )
    response = _TraceAuditResponse(
        Response(content=b"ok"),
        request=request,
        operation=operation,
        ticket_id="PERF-1",
    )

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        pass

    task = asyncio.create_task(
        response(
            {"type": "http", "method": "GET", "path": "/", "headers": []},
            receive,
            send,
        )
    )
    try:
        await asyncio.wait_for(asyncio.to_thread(audit_started.wait, 1), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        await asyncio.to_thread(audit_done.wait, 1)
        await asyncio.gather(task, return_exceptions=True)

    assert audits == [
        (
            operation,
            "PERF-1",
            {
                "outcome": "success",
                "error": None,
                "ticket_scoped": True,
            },
        )
    ]


@pytest.mark.parametrize("operation", ["trace_query", "trace_export"])
async def test_queued_audit_survives_request_cancellation(operation) -> None:
    audit_started = threading.Event()
    release = threading.Event()
    audit_done = threading.Event()
    audits: list[tuple] = []

    def log(*args) -> None:
        audit_started.set()
        if not release.wait(1):
            raise RuntimeError("audit worker was not released")
        audits.append(args)
        audit_done.set()

    async def is_disconnected() -> bool:
        return False

    request = SimpleNamespace(
        state=SimpleNamespace(),
        app=SimpleNamespace(state=trace_audit_state(SimpleNamespace(log=log))),
        is_disconnected=is_disconnected,
    )
    executor = BoundedThreadPoolExecutor(
        max_workers=1,
        queue_size=1,
        thread_name_prefix="test-trace-audit",
    )
    request.app.state.trace_query_audit_executor = executor
    request.app.state.trace_query_audit_slots = executor.slots
    task = asyncio.create_task(
        _record_query_audit(request, operation, "PERF-1", "success")
    )
    try:
        await asyncio.wait_for(asyncio.to_thread(audit_started.wait, 1), timeout=1)
        assert audit_started.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        await asyncio.to_thread(audit_done.wait, 1)
        executor.shutdown(wait=True)

    assert len(audits) == 1
    assert audits[0][0] == operation
    assert audits[0][2]["outcome"] == "success"


@pytest.mark.parametrize(
    ("endpoint", "operation"),
    [(query_trace, "trace_query"), (export_trace, "trace_export")],
)
async def test_trace_cursor_outside_sqlite_integer_is_rejected(
    endpoint, operation
) -> None:
    audits: list[tuple] = []
    cursor = base64.urlsafe_b64encode(
        json.dumps([0, 2**63, ""], separators=(",", ":")).encode()
    ).decode()

    async def is_disconnected() -> bool:
        return False

    request = SimpleNamespace(
        state=SimpleNamespace(
            principal=Principal(kind="service", username="service", is_admin=False)
        ),
        app=SimpleNamespace(
            state=SimpleNamespace(
                audit_log=SimpleNamespace(log=lambda *args: audits.append(args)),
                trace_query_audit_executor=_TEST_AUDIT_EXECUTOR,
                trace_query_audit_slots=_TEST_AUDIT_EXECUTOR.slots,
                trace_query_audit_fallback_executor=_TEST_AUDIT_FALLBACK_EXECUTOR,
                trace_query_audit_fallback_slots=(_TEST_AUDIT_FALLBACK_EXECUTOR.slots),
            )
        ),
        is_disconnected=is_disconnected,
    )

    with pytest.raises(HTTPException) as raised:
        await endpoint(request, ticket_id="PERF-1", cursor=cursor, limit=1)

    assert raised.value.status_code == 400
    assert audits == [
        (
            operation,
            "PERF-1",
            {
                "outcome": "failed",
                "error": "invalid trace cursor for stored events",
                "ticket_scoped": True,
            },
        )
    ]


async def test_trace_access_matrix_and_audit_outcomes(tmp_path) -> None:
    app = make_app(tmp_path)
    app.state.store = SimpleNamespace(
        get_ticket=lambda _ticket_id: SimpleNamespace(owners=["owner"])
    )
    app.state.trace_store.insert_event(event())
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    operator = await client.get(
        "/api/v1/traces/query",
        params={"ticket_id": "PERF-1", "include_payloads": "true"},
        headers={"Authorization": "Bearer service"},
    )
    assert operator.status_code == 200
    await client.aclose()


def test_owner_projection_and_access_decisions_are_redacted(tmp_path) -> None:
    request = SimpleNamespace(
        state=SimpleNamespace(
            principal=Principal(kind="user", username="owner", is_admin=False)
        ),
        app=SimpleNamespace(
            state=SimpleNamespace(
                multi_user=True,
                store=SimpleNamespace(
                    get_ticket=lambda _ticket_id: SimpleNamespace(owners=["owner"])
                ),
            )
        ),
    )
    assert _authorize_query(request, "PERF-1") is False
    hidden = _event_json(
        event().model_copy(update={"attributes": {"secret": "no"}}), False
    )
    assert hidden["attributes"] is None
    request.state.principal = Principal(kind="user", username="other", is_admin=False)
    try:
        _authorize_query(request, "PERF-1")
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("non-owner was authorized")


@pytest.mark.parametrize("endpoint", [query_trace, export_trace])
async def test_trace_authorization_runs_off_event_loop(endpoint) -> None:
    entered = threading.Event()
    release = threading.Event()

    def get_ticket(_ticket_id):
        entered.set()
        if not release.wait(1):
            raise RuntimeError("authorization thread was not released")
        return SimpleNamespace(owners=["owner"])

    async def is_disconnected() -> bool:
        return False

    request = SimpleNamespace(
        state=SimpleNamespace(
            principal=Principal(kind="user", username="owner", is_admin=False)
        ),
        app=SimpleNamespace(
            state=SimpleNamespace(
                multi_user=True,
                store=SimpleNamespace(get_ticket=get_ticket),
                trace_store=SimpleNamespace(query_events=lambda *_args, **_kwargs: []),
            )
        ),
        is_disconnected=is_disconnected,
    )
    task = asyncio.create_task(endpoint(request, ticket_id="PERF-1", limit=1))

    async def release_authorization() -> None:
        while not entered.is_set():
            await asyncio.sleep(0.001)
        release.set()

    try:
        await asyncio.wait_for(release_authorization(), timeout=0.5)
        await asyncio.wait_for(task, timeout=1)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_user_or_anonymous_cannot_spoof_producer(tmp_path) -> None:
    users = UserStore(tmp_path / "users.json")
    _, token = users.create_user("alice")
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=make_app(tmp_path, users)),
        base_url="http://test",
    )
    missing = await client.post(
        "/api/v1/traces/events",
        json=payload(event()),
        headers={"X-Trace-Instance": "bad"},
    )
    response = await client.post(
        "/api/v1/traces/events",
        json=payload(event()),
        headers={"Authorization": f"Bearer {token}", "X-Trace-Instance": "bad"},
    )
    await client.aclose()
    assert missing.status_code == 401
    assert response.status_code == 403


async def test_partial_duplicate_batch_has_stable_acknowledgements(tmp_path) -> None:
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=make_app(tmp_path)), base_url="http://test"
    )
    one, two = event(), event()
    headers = {"Authorization": "Bearer service", "X-Trace-Instance": "process-a"}
    first = await client.post(
        "/api/v1/traces/events", json=payload(one), headers=headers
    )
    assert first.status_code == 200
    response = await client.post(
        "/api/v1/traces/events/batch",
        json={"events": [payload(one), payload(two)]},
        headers=headers,
    )
    await client.aclose()
    assert response.status_code == 200
    acknowledgements = response.json()["acknowledgements"]
    assert acknowledgements == [
        {
            "event_id": str(one.event_id),
            "accepted": True,
            "status": "duplicate",
        },
        {
            "event_id": str(two.event_id),
            "accepted": True,
            "status": "stored",
        },
    ]


async def test_batch_ingest_returns_compact_bounded_acknowledgements(tmp_path) -> None:
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=make_app(tmp_path)), base_url="http://test"
    )
    large = event().model_copy(update={"attributes": {"payload": "x" * 1_000_000}})
    response = await client.post(
        "/api/v1/traces/events/batch",
        json={"events": [payload(event()), payload(large)]},
        headers={"Authorization": "Bearer service"},
    )
    await client.aclose()

    assert response.status_code == 200
    assert len(response.content) < 2_048
    acknowledgements = response.json()["acknowledgements"]
    assert len(acknowledgements) == 2
    assert all("event" not in acknowledgement for acknowledgement in acknowledgements)


async def test_batch_ingest_enforces_acknowledgement_response_budget(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(traces_api, "MAX_TRACE_BATCH_ACK_BYTES", 64)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=make_app(tmp_path)), base_url="http://test"
    )
    response = await client.post(
        "/api/v1/traces/events/batch",
        json={"events": [payload(event())]},
        headers={"Authorization": "Bearer service"},
    )
    await client.aclose()

    assert response.status_code == 503
    assert response.json()["detail"] == "trace batch acknowledgement is too large"


async def test_ingest_queue_saturation_returns_audited_503(tmp_path) -> None:
    app = make_app(tmp_path)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ingestion")
    slots = threading.BoundedSemaphore(1)
    assert slots.acquire(blocking=False)
    audits: list[tuple] = []
    app.state.trace_ingestion_executor = executor
    app.state.trace_ingestion_slots = slots
    app.state.audit_log = SimpleNamespace(log=lambda *args: audits.append(args))
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    try:
        response = await client.post(
            "/api/v1/traces/events",
            json=payload(event()),
            headers={"Authorization": "Bearer service"},
        )
    finally:
        slots.release()
        await client.aclose()
        executor.shutdown(wait=True)
        app.state.trace_store.close()

    assert response.status_code == 503
    assert response.json()["detail"] == "trace ingestion queue is full"
    assert app.state.trace_health["ingestion_queue_rejections"] == 1
    assert audits == [
        (
            "trace_ingest",
            "PERF-1",
            {
                "outcome": "failed",
                "error": "trace ingestion queue is full",
                "ticket_scoped": True,
            },
        )
    ]


async def test_ingest_saturation_audit_failure_preserves_503(tmp_path) -> None:
    app = make_app(tmp_path)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ingestion")
    slots = threading.BoundedSemaphore(1)
    assert slots.acquire(blocking=False)
    app.state.trace_ingestion_executor = executor
    app.state.trace_ingestion_slots = slots
    app.state.audit_log = SimpleNamespace(
        log_required=lambda *_args: (_ for _ in ()).throw(
            TraceStoreWriteError("audit store unavailable")
        )
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    try:
        response = await client.post(
            "/api/v1/traces/events",
            json=payload(event()),
            headers={"Authorization": "Bearer service"},
        )
    finally:
        slots.release()
        await client.aclose()
        executor.shutdown(wait=True)
        app.state.trace_store.close()

    assert response.status_code == 503
    assert response.json()["detail"] == "trace ingestion queue is full"
    assert app.state.trace_health["ingestion_queue_rejections"] == 1


async def test_batch_ingest_saturation_preserves_per_event_acknowledgements(
    tmp_path,
) -> None:
    app = make_app(tmp_path)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ingestion")
    slots = threading.BoundedSemaphore(1)
    assert slots.acquire(blocking=False)
    audits: list[tuple] = []
    app.state.trace_ingestion_executor = executor
    app.state.trace_ingestion_slots = slots
    app.state.audit_log = SimpleNamespace(log=lambda *args: audits.append(args))
    one, two = event(), event()
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    try:
        response = await client.post(
            "/api/v1/traces/events/batch",
            json={"events": [payload(one), payload(two)]},
            headers={"Authorization": "Bearer service"},
        )
    finally:
        slots.release()
        await client.aclose()
        executor.shutdown(wait=True)
        app.state.trace_store.close()

    assert response.status_code == 200
    acknowledgements = response.json()["acknowledgements"]
    assert [
        (ack["event_id"], ack["accepted"], ack["status"]) for ack in acknowledgements
    ] == [
        (str(one.event_id), False, 503),
        (str(two.event_id), False, 503),
    ]
    assert app.state.trace_health["ingestion_queue_rejections"] == 2
    assert [audit[0] for audit in audits] == [
        "trace_ingest_batch",
        "trace_ingest_batch",
    ]


async def test_invalid_trace_schema_increments_health_counter() -> None:
    app = create_app(initialize_immediately=True)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    try:
        response = await client.post(
            "/api/v1/traces/events",
            json={"ticket_id": "PERF-invalid"},
            headers={"Authorization": f"Bearer {app.state.api_token}"},
        )
        assert response.status_code == 422
        assert app.state.trace_health["schema_rejections"] == 1
    finally:
        await client.aclose()
        app.state.trace_store.close()


async def test_ingest_runs_sqlite_write_off_event_loop(tmp_path, monkeypatch) -> None:
    """_insert() must run in a thread so the event loop stays free for health probes."""
    import threading

    app = make_app(tmp_path)
    loop_thread = threading.current_thread()
    insert_threads: list[threading.Thread] = []

    real_insert = app.state.trace_store.insert_event_result

    def recording_insert(event):
        insert_threads.append(threading.current_thread())
        return real_insert(event)

    monkeypatch.setattr(app.state.trace_store, "insert_event_result", recording_insert)

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    headers = {"Authorization": "Bearer service"}
    response = await client.post(
        "/api/v1/traces/events", json=payload(event()), headers=headers
    )
    await client.aclose()

    assert response.status_code == 200
    assert len(insert_threads) == 1
    assert insert_threads[0] is not loop_thread, (
        "SQLite insert ran on the event loop thread — this blocks health probes"
    )


async def test_ingest_batch_runs_sqlite_writes_off_event_loop(
    tmp_path, monkeypatch
) -> None:
    """Each _insert() in ingest_batch must also run in a thread."""
    import threading

    app = make_app(tmp_path)
    loop_thread = threading.current_thread()
    insert_threads: list[threading.Thread] = []

    real_insert = app.state.trace_store.insert_event_result

    def recording_insert(event):
        insert_threads.append(threading.current_thread())
        return real_insert(event)

    monkeypatch.setattr(app.state.trace_store, "insert_event_result", recording_insert)

    one, two = event(), event().model_copy(update={"action_id": "2" * 16})
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    headers = {"Authorization": "Bearer service"}
    response = await client.post(
        "/api/v1/traces/events/batch",
        json={"events": [payload(one), payload(two)]},
        headers=headers,
    )
    await client.aclose()

    assert response.status_code == 200
    assert len(insert_threads) == 2
    for t in insert_threads:
        assert t is not loop_thread, (
            "SQLite insert ran on the event loop thread — this blocks health probes"
        )
