"""Tests for action_required hints in API responses."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from state_store.main import create_app
from state_store.store import TicketStore


@pytest.fixture
def client(tmp_path):
    app = create_app()
    app.state.store = TicketStore(persist_dir=tmp_path / "tickets")
    c = TestClient(app)
    c.headers["Authorization"] = f"Bearer {app.state.api_token}"
    return c


@pytest.fixture
def ticket_id(client):
    r = client.post(
        "/api/v1/tickets",
        json={"summary": "test", "description": "desc"},
    )
    assert r.status_code == 200
    return r.json()["id"]


class TestCreateTicketHint:
    def test_create_returns_action_required(self, client):
        r = client.post(
            "/api/v1/tickets",
            json={"summary": "test", "description": "desc"},
        )
        data = r.json()
        hint = data["action_required"]
        assert hint is not None
        assert hint["method"] == "POST"
        assert "/transition" in hint["path"]
        assert hint["body"]["status"] == "triage_pending"

    def test_hint_contains_ticket_id(self, client):
        r = client.post(
            "/api/v1/tickets",
            json={"summary": "test", "description": "desc"},
        )
        data = r.json()
        tid = data["id"]
        assert tid in data["action_required"]["path"]


class TestCommentHint:
    def _pause_ticket(self, client, ticket_id):
        """Transition ticket to awaiting_customer_guidance."""
        client.post(
            f"/api/v1/tickets/{ticket_id}/transition",
            json={"status": "triage_pending"},
        )
        client.post(
            f"/api/v1/tickets/{ticket_id}/transition",
            json={"status": "awaiting_customer_guidance"},
        )

    def test_user_comment_on_paused_ticket_returns_hint(self, client, ticket_id):
        self._pause_ticket(client, ticket_id)
        r = client.post(
            f"/api/v1/tickets/{ticket_id}/comments",
            json={"author": "user", "body": "approve"},
        )
        data = r.json()
        hint = data["action_required"]
        assert hint is not None
        assert hint["method"] == "POST"
        assert "/transition" in hint["path"]
        assert hint["body"]["status"] == "triage_pending"
        assert (
            "resume" in hint["reason"].lower() or "transition" in hint["reason"].lower()
        )

    def test_agent_comment_on_paused_ticket_no_hint(self, client, ticket_id):
        self._pause_ticket(client, ticket_id)
        r = client.post(
            f"/api/v1/tickets/{ticket_id}/comments",
            json={"author": "benchmark-agent", "body": "processing"},
        )
        data = r.json()
        assert data["action_required"] is None

    def test_comment_on_active_ticket_no_hint(self, client, ticket_id):
        client.post(
            f"/api/v1/tickets/{ticket_id}/transition",
            json={"status": "triage_pending"},
        )
        r = client.post(
            f"/api/v1/tickets/{ticket_id}/comments",
            json={"author": "user", "body": "hello"},
        )
        data = r.json()
        assert data["action_required"] is None

    def test_hint_points_to_previous_status(self, client, ticket_id):
        """When paused from executing_benchmark, hint says resume there."""
        for status in [
            "triage_pending",
            "awaiting_hardware",
            "preparing_platform",
            "awaiting_provision",
            "executing_benchmark",
            "awaiting_customer_guidance",
        ]:
            r = client.post(
                f"/api/v1/tickets/{ticket_id}/transition",
                json={"status": status},
            )
            assert r.status_code == 200, f"Failed to transition to {status}"
        r = client.post(
            f"/api/v1/tickets/{ticket_id}/comments",
            json={"author": "user", "body": "approve"},
        )
        hint = r.json()["action_required"]
        assert hint["body"]["status"] == "executing_benchmark"
