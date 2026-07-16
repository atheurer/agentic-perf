"""Tests for SSE event stream endpoint.

Covers: event polling, multi-ticket mode, type filtering,
since cursor, zero emit() calls from state store, and
streaming via a real uvicorn server.
"""

from __future__ import annotations

import asyncio
import json
import socket

import httpx
import pytest
import uvicorn

from providers.events import EventBus
from state_store.main import create_app
from state_store.models import CreateTicketRequest, TransitionRequest
from state_store.store import TicketStore


@pytest.fixture
def store(tmp_path):
    return TicketStore(persist_dir=tmp_path)


@pytest.fixture
def event_bus(tmp_path):
    return EventBus(log_dir=tmp_path / "events")


@pytest.fixture
def app(store, event_bus):
    application = create_app()
    application.state.store = store
    application.state.event_bus = event_bus
    return application


@pytest.fixture
def ticket_with_events(store, event_bus):
    """Create a ticket and emit some events for it."""
    ticket = store.create_ticket(
        CreateTicketRequest(summary="test", description="test"),
    )
    tid = ticket.id
    event_bus.emit(tid, "triage", "agent_started", {"info": "starting"})
    event_bus.emit(tid, "triage", "tool_called", {"tool": "list_benchmarks"})
    event_bus.emit(tid, "triage", "tool_result", {"tool": "list_benchmarks"})
    event_bus.emit(tid, "triage", "agent_finished", {})
    return tid


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def live_server(app):
    """Start the app on a random port and yield the base URL."""
    port = _free_port()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    # Wait for the server to start
    for _ in range(50):
        try:
            async with httpx.AsyncClient() as c:
                r = await c.get(f"http://127.0.0.1:{port}/api/v1/health")
                if r.status_code == 200:
                    break
        except httpx.ConnectError:
            await asyncio.sleep(0.1)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    await task


async def _collect_sse_events(
    base_url: str,
    path: str,
    headers: dict,
    max_events: int = 10,
    timeout: float = 5.0,
) -> tuple[list[dict], list[str]]:
    """Connect to SSE and collect events + raw id: lines."""
    events: list[dict] = []
    id_lines: list[str] = []

    async with httpx.AsyncClient() as client:
        async with client.stream(
            "GET",
            f"{base_url}{path}",
            headers=headers,
            timeout=timeout,
        ) as response:
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
                elif line.startswith("id: "):
                    id_lines.append(line[4:])
                if len(events) >= max_events:
                    break

    return events, id_lines


class TestEventStreamLive:
    """Integration tests using a real uvicorn server."""

    async def test_streams_events(
        self,
        live_server,
        app,
        ticket_with_events,
    ):
        headers = {"Authorization": f"Bearer {app.state.api_token}"}
        tid = ticket_with_events
        events, _ = await _collect_sse_events(
            live_server,
            f"/api/v1/events/stream?ticket_id={tid}",
            headers,
            max_events=4,
        )

        assert len(events) == 4
        assert events[0]["event_type"] == "agent_started"
        assert events[1]["event_type"] == "tool_called"
        assert events[2]["event_type"] == "tool_result"
        assert events[3]["event_type"] == "agent_finished"

    async def test_event_id_format(
        self,
        live_server,
        app,
        ticket_with_events,
    ):
        headers = {"Authorization": f"Bearer {app.state.api_token}"}
        tid = ticket_with_events
        _, id_lines = await _collect_sse_events(
            live_server,
            f"/api/v1/events/stream?ticket_id={tid}",
            headers,
            max_events=4,
        )

        assert len(id_lines) >= 4
        assert id_lines[0].startswith(f"{tid}:")
        seq_part = id_lines[0].split(":")[-1]
        assert seq_part.strip().isdigit()

    async def test_type_filter(
        self,
        live_server,
        app,
        ticket_with_events,
    ):
        headers = {"Authorization": f"Bearer {app.state.api_token}"}
        tid = ticket_with_events
        events, _ = await _collect_sse_events(
            live_server,
            f"/api/v1/events/stream?ticket_id={tid}&event_type=tool_called",
            headers,
            max_events=1,
        )

        assert len(events) == 1
        assert events[0]["event_type"] == "tool_called"

    async def test_since_cursor(
        self,
        live_server,
        app,
        ticket_with_events,
    ):
        headers = {"Authorization": f"Bearer {app.state.api_token}"}
        tid = ticket_with_events
        events, _ = await _collect_sse_events(
            live_server,
            f"/api/v1/events/stream?ticket_id={tid}&since=2",
            headers,
            max_events=2,
        )

        assert len(events) == 2
        assert all(e["seq"] > 2 for e in events)

    async def test_content_type(
        self,
        live_server,
        app,
        ticket_with_events,
    ):
        headers = {"Authorization": f"Bearer {app.state.api_token}"}
        tid = ticket_with_events
        async with httpx.AsyncClient() as client:
            async with client.stream(
                "GET",
                f"{live_server}/api/v1/events/stream?ticket_id={tid}",
                headers=headers,
                timeout=5.0,
            ) as response:
                assert response.status_code == 200
                ct = response.headers["content-type"]
                assert "text/event-stream" in ct

    async def test_all_active_tickets(
        self,
        live_server,
        app,
        store,
        event_bus,
    ):
        headers = {"Authorization": f"Bearer {app.state.api_token}"}
        t1 = store.create_ticket(
            CreateTicketRequest(summary="t1", description="t1"),
        )
        t2 = store.create_ticket(
            CreateTicketRequest(summary="t2", description="t2"),
        )
        event_bus.emit(t1.id, "agent", "agent_started", {})
        event_bus.emit(t2.id, "agent", "agent_started", {})

        events, _ = await _collect_sse_events(
            live_server,
            "/api/v1/events/stream",
            headers,
            max_events=2,
        )

        ticket_ids = {e["ticket_id"] for e in events}
        assert t1.id in ticket_ids
        assert t2.id in ticket_ids


class TestPollEventsInternal:
    """Unit tests for the internal _poll_events function."""

    def test_poll_returns_events(self, event_bus, ticket_with_events):
        from state_store.api.stream import _poll_events

        tid = ticket_with_events
        cursors: dict[str, int] = {}
        events = _poll_events(event_bus, [tid], cursors, None)
        assert len(events) == 4
        assert cursors[tid] == 4

    def test_poll_respects_cursor(self, event_bus, ticket_with_events):
        from state_store.api.stream import _poll_events

        tid = ticket_with_events
        cursors: dict[str, int] = {tid: 2}
        events = _poll_events(event_bus, [tid], cursors, None)
        assert len(events) == 2
        assert all(e["seq"] > 2 for e in events)

    def test_poll_with_type_filter(self, event_bus, ticket_with_events):
        from state_store.api.stream import _poll_events

        tid = ticket_with_events
        cursors: dict[str, int] = {}
        events = _poll_events(
            event_bus,
            [tid],
            cursors,
            {"tool_called"},
        )
        assert len(events) == 1
        assert events[0]["event_type"] == "tool_called"
        assert cursors[tid] == 4

    def test_poll_none_event_bus(self):
        from state_store.api.stream import _poll_events

        events = _poll_events(None, ["PERF-000001"], {}, None)
        assert events == []

    def test_poll_multiple_tickets(self, store, event_bus):
        from state_store.api.stream import _poll_events

        t1 = store.create_ticket(
            CreateTicketRequest(summary="a", description="a"),
        )
        t2 = store.create_ticket(
            CreateTicketRequest(summary="b", description="b"),
        )
        event_bus.emit(t1.id, "a", "agent_started", {})
        event_bus.emit(t2.id, "b", "agent_started", {})

        cursors: dict[str, int] = {}
        events = _poll_events(
            event_bus,
            [t1.id, t2.id],
            cursors,
            None,
        )
        assert len(events) == 2
        tids = {e["ticket_id"] for e in events}
        assert t1.id in tids
        assert t2.id in tids


class TestActiveTicketIds:
    def test_excludes_closed(self, store):
        from state_store.api.stream import _active_ticket_ids

        t = store.create_ticket(
            CreateTicketRequest(summary="x", description="x"),
        )
        for status in [
            "triage_pending",
            "awaiting_hardware",
            "awaiting_provision",
            "executing_benchmark",
            "awaiting_review",
            "awaiting_teardown",
            "retrospective_pending",
            "closed",
        ]:
            store.transition_ticket(
                t.id,
                TransitionRequest(status=status),
            )

        ids = _active_ticket_ids(store)
        assert t.id not in ids

    def test_includes_active(self, store):
        from state_store.api.stream import _active_ticket_ids

        t = store.create_ticket(
            CreateTicketRequest(summary="x", description="x"),
        )
        ids = _active_ticket_ids(store)
        assert t.id in ids


class TestNoEmitGuardrail:
    def test_no_emit_from_stream_module(self):
        """Verify EventBus.emit is never called from stream.py."""
        import inspect

        import state_store.api.stream as stream_module

        source = inspect.getsource(stream_module)
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert ".emit(" not in stripped, (
                f"stream.py must never call emit(): {stripped}"
            )
