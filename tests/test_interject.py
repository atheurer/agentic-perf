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
    application = create_app()
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


class TestAgentInterjectPickup:
    """Test the agent-side pickup of pending_interject."""

    async def test_pickup_clears_field_and_emits(self, store, event_bus):
        from agents.base import AgentBase

        ticket = store.create_ticket(
            CreateTicketRequest(summary="t", description="t"),
        )
        tid = ticket.id
        store.update_fields(
            tid,
            {
                "pending_interject": {
                    "message": "focus on latency",
                    "timestamp": "2024-01-01T00:00:00Z",
                },
            },
        )

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
