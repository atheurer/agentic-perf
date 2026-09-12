from __future__ import annotations

import asyncio

import httpx
import pytest

from providers.execution import (
    AmbiguousHTTPReplayError,
    AuditedAsyncHTTPClient,
    AuditedHTTPClient,
)
from providers.tracing import bind_trace_context, new_trace_context, reset_trace_context


def _context():
    return bind_trace_context(new_trace_context(ticket_id="PERF-http", agent_id="test"))


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
    ] == [1, 2]
    terminal = [event for event in events if event.outcome is not None]
    assert len({event.action_id for event in terminal}) == len(terminal)


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

    assert (
        len([event for event in events if event.lifecycle.state.value == "requested"])
        == 1
    )
    assert events[-1].lifecycle.state.value == "timed_out"


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
