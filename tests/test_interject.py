"""Tests for interject endpoint and agent pickup.

Covers: POST interject stores comment + field, 404/409 error
conditions, agent pickup clears field and emits event.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from providers.events import EventBus
from state_store.main import create_app
from state_store.models import (
    CreateTicketRequest,
    TransitionRequest,
)
from state_store.store import TicketStore


@pytest.fixture
def store(tmp_path):
    return TicketStore(persist_dir=tmp_path)


@pytest.fixture
def event_bus(tmp_path):
    return EventBus(log_dir=tmp_path / "events")


@pytest.fixture
def app(store, event_bus):
    application = create_app(initialize_immediately=True)
    application.state.store = store
    application.state.event_bus = event_bus
    return application


@pytest.fixture
def client(app):
    c = TestClient(app)
    c.headers["Authorization"] = f"Bearer {app.state.api_token}"
    return c


@pytest.fixture
def active_ticket(store):
    """Create a ticket in executing_benchmark status."""
    ticket = store.create_ticket(
        CreateTicketRequest(summary="test", description="test"),
    )
    for status in [
        "triage_pending",
        "awaiting_hardware",
        "awaiting_provision",
        "executing_benchmark",
    ]:
        store.transition_ticket(
            ticket.id,
            TransitionRequest(status=status),
        )
    return store.get_ticket(ticket.id)


class TestInterjectEndpoint:
    def test_interject_stores_comment_and_field(
        self,
        client,
        store,
        active_ticket,
    ):
        tid = active_ticket.id
        r = client.post(
            f"/api/v1/tickets/{tid}/interject",
            json={"message": "try a different approach"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "queued"
        assert data["ticket_id"] == tid

        ticket = store.get_ticket(tid)
        assert ticket.custom_fields["pending_interject"]["message"] == (
            "try a different approach"
        )
        assert "timestamp" in ticket.custom_fields["pending_interject"]

        user_comments = [c for c in ticket.comments if c.author == "user"]
        assert len(user_comments) == 1
        assert user_comments[0].body == "try a different approach"

    def test_interject_emits_user_interjection_event(
        self,
        client,
        event_bus,
        active_ticket,
    ):
        """POST /interject emits a user_interjection event immediately."""
        tid = active_ticket.id
        r = client.post(
            f"/api/v1/tickets/{tid}/interject",
            json={"message": "switch to latency mode"},
        )
        assert r.status_code == 200

        events = event_bus.get_events(tid, since=0, limit=100)
        interjection_events = [
            e for e in events if e.get("event_type") == "user_interjection"
        ]
        assert len(interjection_events) == 1
        assert interjection_events[0]["data"]["message"] == ("switch to latency mode")
        assert interjection_events[0]["agent"] == "chat-agent"

    def test_interject_event_failure_does_not_queue_message(
        self,
        client,
        store,
        event_bus,
        active_ticket,
        monkeypatch,
    ):
        def fail_emit(*_args, **_kwargs):
            raise OSError("event store unavailable")

        monkeypatch.setattr(event_bus, "emit", fail_emit)
        failing_client = TestClient(client.app, raise_server_exceptions=False)
        ticket_id = active_ticket.id

        response = failing_client.post(
            f"/api/v1/tickets/{ticket_id}/interject",
            json={"message": "switch to latency mode"},
            headers=client.headers,
        )

        assert response.status_code == 500
        ticket = store.get_ticket(ticket_id)
        assert ticket.custom_fields.get("pending_interject") is None
        assert not any(comment.author == "user" for comment in ticket.comments)

    def test_interject_404_unknown_ticket(self, client):
        r = client.post(
            "/api/v1/tickets/PERF-NONEXISTENT/interject",
            json={"message": "hello"},
        )
        assert r.status_code == 404

    def test_interject_409_terminal_status(self, client, store):
        ticket = store.create_ticket(
            CreateTicketRequest(summary="t", description="t"),
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
                ticket.id,
                TransitionRequest(status=status),
            )

        r = client.post(
            f"/api/v1/tickets/{ticket.id}/interject",
            json={"message": "hello"},
        )
        assert r.status_code == 409
        assert "terminal" in r.json()["detail"]

    def test_interject_409_guidance_status(
        self,
        client,
        store,
        active_ticket,
    ):
        tid = active_ticket.id
        store.transition_ticket(
            tid,
            TransitionRequest(status="awaiting_customer_guidance"),
        )

        r = client.post(
            f"/api/v1/tickets/{tid}/interject",
            json={"message": "hello"},
        )
        assert r.status_code == 409
        assert "HITL" in r.json()["detail"]

    def test_interject_no_status_change(
        self,
        client,
        store,
        active_ticket,
    ):
        """Interject must not change the ticket status."""
        tid = active_ticket.id
        client.post(
            f"/api/v1/tickets/{tid}/interject",
            json={"message": "guidance"},
        )
        ticket = store.get_ticket(tid)
        assert ticket.status.value == "executing_benchmark"


class TestUserReplyEndpoint:
    """Tests for the POST /user-reply endpoint."""

    def test_user_reply_emits_event(
        self,
        client,
        event_bus,
        active_ticket,
    ):
        tid = active_ticket.id
        r = client.post(
            f"/api/v1/tickets/{tid}/user-reply",
            json={"message": "approved, proceed"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "recorded"

        events = event_bus.get_events(tid, since=0, limit=100)
        reply_events = [e for e in events if e.get("event_type") == "user_reply"]
        assert len(reply_events) == 1
        assert reply_events[0]["data"]["message"] == "approved, proceed"
        assert reply_events[0]["agent"] == "chat-agent"

    def test_user_reply_404_unknown_ticket(self, client):
        r = client.post(
            "/api/v1/tickets/PERF-NONEXISTENT/user-reply",
            json={"message": "hello"},
        )
        assert r.status_code == 404


class TestAgentInterjectPickup:
    """Test the agent-side pickup of pending_interject."""

    async def test_pickup_clears_field_without_duplicate_event(
        self,
        client,
        store,
        event_bus,
        active_ticket,
    ):
        from agents.base import AgentBase

        tid = active_ticket.id
        response = client.post(
            f"/api/v1/tickets/{tid}/interject",
            json={"message": "focus on latency"},
        )
        assert response.status_code == 200

        class TestAgent(AgentBase):
            def _system_prompt(self, ticket):
                return ""

            def _build_messages(self, ticket):
                return []

            async def _handle_completion(self, ticket_id, response):
                pass

        agent = TestAgent(
            agent_name="test",
            llm_provider=AsyncMock(),
            state_store_url="http://localhost:8090",
            event_bus=event_bus,
        )
        agent._client = AsyncMock()

        async def mock_get(url):
            t = store.get_ticket(tid)
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "id": t.id,
                "status": t.status.value,
                "custom_fields": dict(t.custom_fields),
                "comments": [],
            }
            return resp

        async def mock_patch(url, json=None):
            if json and "fields" in json:
                store.update_fields(tid, json["fields"])
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {}
            return resp

        agent._client.get = AsyncMock(side_effect=mock_get)
        agent._client.patch = AsyncMock(side_effect=mock_patch)

        result = await agent._check_interject(tid)
        assert result == "focus on latency"

        updated = store.get_ticket(tid)
        assert updated.custom_fields.get("pending_interject") is None

        events = event_bus.get_events(tid, since=0, limit=100)
        interjection_events = [
            e for e in events if e.get("event_type") == "user_interjection"
        ]
        assert len(interjection_events) == 1
        assert interjection_events[0]["data"]["message"] == ("focus on latency")

    async def test_no_interject_returns_none(self, store, event_bus):
        from agents.base import AgentBase

        ticket = store.create_ticket(
            CreateTicketRequest(summary="t", description="t"),
        )
        tid = ticket.id

        class TestAgent(AgentBase):
            def _system_prompt(self, ticket):
                return ""

            def _build_messages(self, ticket):
                return []

            async def _handle_completion(self, ticket_id, response):
                pass

        agent = TestAgent(
            agent_name="test",
            llm_provider=AsyncMock(),
            state_store_url="http://localhost:8090",
            event_bus=event_bus,
        )
        agent._client = AsyncMock()

        async def mock_get(url):
            t = store.get_ticket(tid)
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "id": t.id,
                "status": t.status.value,
                "custom_fields": dict(t.custom_fields),
                "comments": [],
            }
            return resp

        agent._client.get = AsyncMock(side_effect=mock_get)

        result = await agent._check_interject(tid)
        assert result is None


async def test_approval_reply_is_emitted_before_resume() -> None:
    import json

    import httpx

    from agents.chat.tools import _reply_to_guidance

    ticket_id = "PERF-APPROVAL-1"
    approval_id = "apr-123"
    calls: list[tuple[str, str, dict | None]] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, payload))
        if request.url.path.endswith("/approvals"):
            return httpx.Response(
                200,
                json={
                    "approvals": [
                        {"approval_request_id": approval_id, "status": "pending"}
                    ]
                },
            )
        if request.url.path.endswith(f"/approvals/{approval_id}/resolve"):
            return httpx.Response(200, json={"status": "approved"})
        if request.method == "GET" and request.url.path.endswith(ticket_id):
            return httpx.Response(
                200,
                json={"previous_status": "executing_benchmark"},
            )
        if request.url.path.endswith("/transition"):
            return httpx.Response(200, json={"status": "executing_benchmark"})
        if request.url.path.endswith("/user-reply"):
            return httpx.Response(200, json={"status": "recorded"})
        return httpx.Response(404)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle_request),
    ) as client:
        result = await _reply_to_guidance(
            client,
            "http://state-store",
            {"Authorization": "Bearer service-token"},
            {"ticket_id": ticket_id, "message": "approved"},
        )

    assert json.loads(result) == {"status": "approval_resolved", "decision": "approved"}
    paths = [call[1] for call in calls]
    resolve_index = next(i for i, path in enumerate(paths) if path.endswith("/resolve"))
    reply_index = next(
        i for i, path in enumerate(paths) if path.endswith("/user-reply")
    )
    transition_index = next(
        i for i, path in enumerate(paths) if path.endswith("/transition")
    )
    assert resolve_index < reply_index < transition_index
    assert calls[reply_index][2] == {"message": "approved"}


async def test_approval_reply_event_survives_failed_resume() -> None:
    import json

    import httpx

    from agents.chat.tools import _reply_to_guidance

    ticket_id = "PERF-APPROVAL-2"
    approval_id = "apr-456"
    calls: list[tuple[str, str, dict | None]] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, payload))
        if request.url.path.endswith("/approvals"):
            return httpx.Response(
                200,
                json={
                    "approvals": [
                        {"approval_request_id": approval_id, "status": "pending"}
                    ]
                },
            )
        if request.url.path.endswith(f"/approvals/{approval_id}/resolve"):
            return httpx.Response(200, json={"status": "approved"})
        if request.method == "GET" and request.url.path.endswith(ticket_id):
            return httpx.Response(
                200,
                json={"previous_status": "executing_benchmark"},
            )
        if request.url.path.endswith("/transition"):
            return httpx.Response(409, json={"detail": "cannot resume"})
        if request.url.path.endswith("/user-reply"):
            return httpx.Response(200, json={"status": "recorded"})
        return httpx.Response(404)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle_request),
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await _reply_to_guidance(
                client,
                "http://state-store",
                {"Authorization": "Bearer service-token"},
                {"ticket_id": ticket_id, "message": "approved"},
            )

    paths = [call[1] for call in calls]
    reply_index = next(
        i for i, path in enumerate(paths) if path.endswith("/user-reply")
    )
    transition_index = next(
        i for i, path in enumerate(paths) if path.endswith("/transition")
    )
    assert reply_index < transition_index
    assert calls[reply_index][2] == {"message": "approved"}


async def test_failed_guidance_comment_does_not_emit_reply_event() -> None:
    import httpx

    from agents.chat.tools import _reply_to_guidance

    ticket_id = "PERF-GUIDANCE-1"
    calls: list[str] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/approvals"):
            return httpx.Response(200, json={"approvals": []})
        if request.method == "GET" and request.url.path.endswith(ticket_id):
            return httpx.Response(200, json={"comments": [], "status_trail": []})
        if request.url.path.endswith("/comments"):
            return httpx.Response(503, json={"detail": "store unavailable"})
        if request.url.path.endswith("/user-reply"):
            return httpx.Response(200, json={"status": "recorded"})
        return httpx.Response(404)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle_request),
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await _reply_to_guidance(
                client,
                "http://state-store",
                {"Authorization": "Bearer service-token"},
                {"ticket_id": ticket_id, "message": "please continue"},
            )

    assert calls[-1].endswith("/comments")
    assert all(not path.endswith("/user-reply") for path in calls)
