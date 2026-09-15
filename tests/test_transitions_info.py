"""Tests for valid-transitions info endpoint.

Covers: correct values from VALID_TRANSITIONS, dynamic
awaiting_customer_guidance handling, 404 on unknown ticket.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

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
def app(store):
    application = create_app()
    application.state.store = store
    return application


@pytest.fixture
def client(app):
    c = TestClient(app)
    c.headers["Authorization"] = f"Bearer {app.state.api_token}"
    return c


class TestTransitionsInfo:
    def test_new_ticket_transitions(self, client, store):
        ticket = store.create_ticket(
            CreateTicketRequest(summary="t", description="t"),
        )
        r = client.get(f"/api/v1/tickets/{ticket.id}/transitions")
        assert r.status_code == 200
        data = r.json()
        assert data["current"] == "new"
        assert "triage_pending" in data["valid"]

    def test_executing_benchmark_transitions(self, client, store):
        ticket = store.create_ticket(
            CreateTicketRequest(summary="t", description="t"),
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

        r = client.get(f"/api/v1/tickets/{ticket.id}/transitions")
        assert r.status_code == 200
        data = r.json()
        assert data["current"] == "executing_benchmark"
        assert "awaiting_review" in data["valid"]
        assert "awaiting_customer_guidance" in data["valid"]

    def test_guidance_returns_previous_status(self, client, store):
        ticket = store.create_ticket(
            CreateTicketRequest(summary="t", description="t"),
        )
        for status in [
            "triage_pending",
            "awaiting_hardware",
            "awaiting_provision",
            "executing_benchmark",
            "awaiting_customer_guidance",
        ]:
            store.transition_ticket(
                ticket.id,
                TransitionRequest(status=status),
            )

        r = client.get(f"/api/v1/tickets/{ticket.id}/transitions")
        data = r.json()
        assert data["current"] == "awaiting_customer_guidance"
        assert data["valid"] == ["executing_benchmark"]

    def test_closed_ticket_no_transitions(self, client, store):
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

        r = client.get(f"/api/v1/tickets/{ticket.id}/transitions")
        data = r.json()
        assert data["current"] == "closed"
        assert data["valid"] == []

    def test_404_unknown_ticket(self, client):
        r = client.get("/api/v1/tickets/PERF-NONEXISTENT/transitions")
        assert r.status_code == 404
