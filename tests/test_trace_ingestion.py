from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException

from providers.tracing import (
    ActionDescriptor,
    ActionType,
    LifecycleDescriptor,
    LifecycleState,
    ProducerIdentity,
    TraceEventV1,
)
from providers.tracing.query import TraceQuery, TraceQueryCancelled
from state_store.api.router import api_router
from state_store.api.traces import (
    _authorize_query,
    _event_json,
    _query_store,
    _record_query_audit,
)
from state_store.api.traces import (
    export as export_trace,
)
from state_store.api.traces import (
    query as query_trace,
)
from state_store.auth import Principal, make_auth_dependency
from state_store.identity import UserStore
from state_store.main import _set_audit_actor, create_app
from state_store.trace_store import TraceStore


def payload(event: TraceEventV1) -> dict:
    return event.model_dump(mode="json")


def make_app(tmp_path, user_store: UserStore | None = None) -> FastAPI:
    app = FastAPI()
    app.state.trace_store = TraceStore(tmp_path / "trace.db")
    app.state.trace_instance_id = "server-instance"
    app.state.trace_health = {
        "ingested": 0,
        "ingestion_failures": 0,
        "schema_rejections": 0,
        "quarantined_frames": 0,
    }
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
        app=SimpleNamespace(state=SimpleNamespace(audit_log=SimpleNamespace(log=log))),
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


@pytest.mark.parametrize("operation", ["trace_query", "trace_export"])
async def test_queued_audit_survives_request_cancellation(operation) -> None:
    audit_started = threading.Event()
    release_worker = threading.Event()
    audit_done = threading.Event()
    audits: list[tuple] = []

    class ObservedExecutor(ThreadPoolExecutor):
        def __init__(self):
            super().__init__(max_workers=1)
            self.submissions = 0

        def submit(self, *args, **kwargs):
            future = super().submit(*args, **kwargs)
            self.submissions += 1
            if self.submissions > 1:
                audit_started.set()
            return future

    def log(*args) -> None:
        audits.append(args)
        audit_done.set()

    async def is_disconnected() -> bool:
        return False

    request = SimpleNamespace(
        state=SimpleNamespace(),
        app=SimpleNamespace(state=SimpleNamespace(audit_log=SimpleNamespace(log=log))),
        is_disconnected=is_disconnected,
    )
    loop = asyncio.get_running_loop()
    executor = ObservedExecutor()
    loop.set_default_executor(executor)
    blocker = executor.submit(release_worker.wait, 1)
    task = asyncio.create_task(
        _record_query_audit(request, operation, "PERF-1", "success")
    )
    try:
        deadline = loop.time() + 1
        while not audit_started.is_set() and loop.time() < deadline:
            await asyncio.sleep(0.001)
        assert audit_started.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release_worker.set()
        await asyncio.to_thread(audit_done.wait, 1)
        await asyncio.to_thread(blocker.result, 1)
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
                audit_log=SimpleNamespace(log=lambda *args: audits.append(args))
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
    assert [ack["accepted"] for ack in acknowledgements] == [True, True]
    assert [ack["status"] for ack in acknowledgements] == ["duplicate", "stored"]
    assert (
        acknowledgements[0]["event"]["global_seq"]
        == first.json()["event"]["global_seq"]
    )


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
