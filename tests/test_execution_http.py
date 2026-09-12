from __future__ import annotations

import asyncio

import httpx
import pytest

from providers.execution import (
    AmbiguousHTTPReplayError,
    AuditedAsyncHTTPClient,
    AuditedHTTPClient,
)
from providers.tracing import (
    TraceSpool,
    bind_trace_context,
    new_trace_context,
    reset_trace_context,
)
from state_store.trace_store import TraceStore


def _context():
    return bind_trace_context(new_trace_context(ticket_id="PERF-http", agent_id="test"))


async def _discard_event(_event) -> None:
    return None


@pytest.mark.asyncio
async def test_audited_get_redacts_secrets_and_records_provider_request_id() -> None:
    events = []

    async def emit(event):
        events.append(event)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["traceparent"]
        return httpx.Response(
            200, content=b"secret-response", headers={"x-request-id": "remote-1"}
        )

    token = _context()
    try:
        async with AuditedAsyncHTTPClient(
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), emit=emit
        ) as client:
            response = await client.get(
                "https://provider.example/a?token=secret",
                headers={"Authorization": "Bearer secret"},
            )
    finally:
        reset_trace_context(token)

    assert response.status_code == 200
    serialized = "\n".join(event.model_dump_json() for event in events)
    assert "secret" not in serialized
    terminal = events[-1]
    assert terminal.attributes["provider_request_id"] == "remote-1"
    assert terminal.output.size_bytes == len(b"secret-response")


@pytest.mark.asyncio
async def test_mutating_retry_keeps_idempotency_key() -> None:
    events = []
    seen = []

    async def emit(event):
        events.append(event)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["idempotency-key"])
        return httpx.Response(503 if len(seen) == 1 else 201)

    token = _context()
    try:
        async with AuditedAsyncHTTPClient(
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            emit=emit,
            retries=1,
            supports_idempotency=True,
        ) as client:
            response = await client.post(
                "https://provider.example/jobs", json={"password": "nope"}
            )
    finally:
        reset_trace_context(token)

    assert response.status_code == 201
    assert seen[0] == seen[1]
    assert [
        event.lifecycle.attempt
        for event in events
        if event.lifecycle.state.value == "requested"
    ] == [1, 1, 2]
    operation_id = events[0].action_id
    operation = [event for event in events if event.action_id == operation_id]
    assert [event.lifecycle.state.value for event in operation] == [
        "requested",
        "retry_scheduled",
        "completed",
    ]
    attempts = [event for event in events if event.action_id != operation_id]
    assert [event.lifecycle.state.value for event in attempts] == [
        "requested",
        "failed",
        "requested",
        "completed",
    ]
    assert all(event.parent_action_id == operation[0].action_id for event in attempts)
    assert len([event for event in operation if event.outcome is not None]) == 1


@pytest.mark.asyncio
async def test_ambiguous_mutating_timeout_is_not_replayed() -> None:
    events = []

    async def emit(event):
        events.append(event)

    async def fail(*_args, **_kwargs):
        raise httpx.ReadTimeout(
            "late response",
            request=httpx.Request("POST", "https://provider.example/jobs"),
        )

    token = _context()
    try:
        client = AuditedAsyncHTTPClient(
            client=type("Client", (), {"request": fail})(), emit=emit, retries=2
        )
        with pytest.raises(AmbiguousHTTPReplayError):
            await client.post(
                "https://provider.example/jobs", json={"api_key": "never-recorded"}
            )
    finally:
        reset_trace_context(token)

    assert [event.lifecycle.state.value for event in events] == [
        "requested",
        "requested",
        "timed_out",
        "indeterminate",
    ]


@pytest.mark.asyncio
async def test_pre_send_connect_error_retries_without_idempotency_support() -> None:
    events = []
    calls = 0

    async def emit(event):
        events.append(event)

    async def fail_then_succeed(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("dns unavailable")
        return httpx.Response(201)

    token = _context()
    try:
        client = AuditedAsyncHTTPClient(
            client=type("Client", (), {"request": fail_then_succeed})(),
            emit=emit,
            retries=1,
        )
        assert (await client.post("https://provider.example/jobs")).status_code == 201
    finally:
        reset_trace_context(token)
    assert calls == 2
    assert [event.lifecycle.state.value for event in events] == [
        "requested",
        "requested",
        "failed",
        "requested",
        "completed",
        "completed",
    ]


@pytest.mark.asyncio
async def test_exhausted_pre_send_connect_error_is_definite_failure() -> None:
    events = []

    async def emit(event):
        events.append(event)

    async def fail(*_args, **_kwargs):
        raise httpx.ConnectTimeout("connect timeout")

    token = _context()
    try:
        client = AuditedAsyncHTTPClient(
            client=type("Client", (), {"request": fail})(), emit=emit, retries=1
        )
        with pytest.raises(httpx.ConnectTimeout):
            await client.post("https://provider.example/jobs")
    finally:
        reset_trace_context(token)
    assert events[-1].lifecycle.state.value == "timed_out"
    assert events[-1].outcome.value == "timed_out"


@pytest.mark.asyncio
async def test_target_paths_and_automatic_redirects_are_safe() -> None:
    events = []

    async def emit(event):
        events.append(event)

    seen = []

    def redirect(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://other.example/next"})

    token = _context()
    try:
        client = AuditedAsyncHTTPClient(
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(redirect), follow_redirects=True
            ),
            emit=emit,
        )
        with pytest.raises(ValueError, match="automatic redirects"):
            await client.post("https://api.example/jobs", follow_redirects=True)
        response = await client.get(
            "https://api.example/webhooks/super-secret-token-1234567890"
        )
    finally:
        reset_trace_context(token)
    assert "super-secret-token-1234567890" not in events[-1].attributes["target"]
    assert response.status_code == 302
    assert seen == ["https://api.example/webhooks/super-secret-token-1234567890"]


@pytest.mark.asyncio
async def test_audited_http_secrets_never_reach_db_wal_spool_or_export(
    tmp_path,
) -> None:
    """Scan every persisted/auditable representation, including a live WAL."""
    events = []
    secrets = {
        "auth-cookie-791",
        "request-body-791",
        "query-secret-791",
        "webhook-secret-791-0123456789",
        "userinfo-secret-791",
    }

    async def emit(event):
        events.append(event)

    token = _context()
    try:
        async with AuditedAsyncHTTPClient(
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(lambda _: httpx.Response(201))
            ),
            emit=emit,
        ) as client:
            await client.post(
                "https://user:userinfo-secret-791@api.example/webhooks/"
                "webhook-secret-791-0123456789?token=query-secret-791",
                headers={"Cookie": "session=auth-cookie-791"},
                json={"password": "request-body-791"},
            )
    finally:
        reset_trace_context(token)

    spool = TraceSpool(tmp_path / "spool", name="audit")
    with TraceStore(tmp_path / "trace.db") as store:
        for event in events:
            store.insert_event(event)
            spool.append(event)
        surfaces = [event.model_dump_json() for event in events]  # export fixture
        for path in tmp_path.rglob("*"):
            if path.is_file():
                surfaces.append(path.read_bytes().decode(errors="ignore"))
        assert all(secret not in surface for secret in secrets for surface in surfaces)
    spool.close()


@pytest.mark.asyncio
async def test_unsupported_mutation_does_not_retry_server_failure() -> None:
    events = []
    calls = 0

    async def emit(event):
        events.append(event)

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    token = _context()
    try:
        async with AuditedAsyncHTTPClient(
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            emit=emit,
            retries=2,
        ) as client:
            response = await client.post("https://provider.example/jobs", json={"x": 1})
    finally:
        reset_trace_context(token)

    assert response.status_code == 503
    assert calls == 1
    assert "idempotency-key" not in response.request.headers


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 500])
async def test_http_error_status_is_terminal_without_secret_payload(
    status: int,
) -> None:
    events = []

    async def emit(event):
        events.append(event)

    token = _context()
    try:
        async with AuditedAsyncHTTPClient(
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(lambda _: httpx.Response(status))
            ),
            emit=emit,
        ) as client:
            response = await client.get("https://api.example/path?token=never")
    finally:
        reset_trace_context(token)
    assert response.status_code == status
    assert events[-1].lifecycle.state.value == "failed"


@pytest.mark.asyncio
async def test_mutation_without_audit_readiness_fails_before_send() -> None:
    called = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(201)

    token = _context()
    try:
        client = AuditedAsyncHTTPClient(
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        with pytest.raises(Exception, match="central trace readiness"):
            await client.post("https://api.example/jobs")
    finally:
        reset_trace_context(token)
    assert not called


@pytest.mark.asyncio
async def test_cancel_records_one_terminal_event() -> None:
    events = []
    entered = asyncio.Event()

    async def emit(event):
        events.append(event)

    async def wait(*_args, **_kwargs):
        entered.set()
        await asyncio.Event().wait()

    token = _context()
    try:
        client = AuditedAsyncHTTPClient(
            client=type("Client", (), {"request": wait})(), emit=emit
        )
        task = asyncio.create_task(client.get("https://provider.example/slow"))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        reset_trace_context(token)
    assert [event.lifecycle.state.value for event in events] == [
        "requested",
        "requested",
        "cancelled",
        "cancelled",
    ]


def test_sync_client_records_bounded_request() -> None:
    events = []

    async def emit(event):
        events.append(event)

    token = _context()
    try:
        client = AuditedHTTPClient(
            client=httpx.Client(
                transport=httpx.MockTransport(lambda _: httpx.Response(200))
            ),
            emit=emit,
        )
        assert client.get("https://provider.example/read?sig=secret").status_code == 200
    finally:
        reset_trace_context(token)
    assert events[-1].attributes["target"] == "https://provider.example/read"


def test_sync_client_disables_constructor_redirect_default() -> None:
    seen = []

    def redirect(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://other.example/next"})

    token = _context()
    try:
        client = AuditedHTTPClient(
            client=httpx.Client(
                transport=httpx.MockTransport(redirect), follow_redirects=True
            ),
            emit=_discard_event,
        )
        response = client.get("https://api.example/redirect")
    finally:
        reset_trace_context(token)
    assert response.status_code == 302
    assert seen == ["https://api.example/redirect"]
