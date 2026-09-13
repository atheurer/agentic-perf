"""Tests for dispatcher claim-based dedup.

Covers: claim survives restart, expired claim reclaimed, same-owner
re-claim, claim API endpoints via FastAPI test client.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from state_store.main import create_app
from state_store.models import AcquireOrchestratorLeaseRequest, CreateTicketRequest
from state_store.store import TicketStore

SESSION_ID = "00000000-0000-4000-8000-000000000001"


def claim_payload(owner: str, duration_seconds: int = 300, claim_id: str = "claim-1"):
    return {
        "owner": owner,
        "duration_seconds": duration_seconds,
        "session_id": SESSION_ID,
        "epoch": 1,
        "claim_id": claim_id,
        "instance_name": "orch-1",
    }


@pytest.fixture
def store(tmp_path):
    return TicketStore(persist_dir=tmp_path)


@pytest.fixture
def app(store):
    application = create_app(initialize_immediately=True)
    application.state.store = store
    store.acquire_orchestrator_lease(
        AcquireOrchestratorLeaseRequest(
            session_id=UUID(SESSION_ID),
            instance_name="orch-1",
            host="test",
            pid=1,
            process_start_id="test",
            ttl_seconds=300,
        )
    )
    return application


@pytest.fixture
def client(app):
    c = TestClient(app)
    c.headers["Authorization"] = f"Bearer {app.state.api_token}"
    return c


@pytest.fixture
def ticket(store):
    return store.create_ticket(CreateTicketRequest(summary="test", description="test"))


class TestClaimAPI:
    def test_claim_returns_200(self, client, ticket):
        r = client.post(
            f"/api/v1/tickets/{ticket.id}/claim",
            json=claim_payload("orch-1"),
        )
        assert r.status_code == 200
        assert r.json()["owner"] == "orch-1"

    def test_claim_conflict_returns_409(self, client, ticket):
        client.post(
            f"/api/v1/tickets/{ticket.id}/claim",
            json=claim_payload("orch-1"),
        )
        r = client.post(
            f"/api/v1/tickets/{ticket.id}/claim",
            json=claim_payload("orch-2", claim_id="claim-2"),
        )
        assert r.status_code == 409

    def test_release_claim(self, client, ticket, store):
        client.post(
            f"/api/v1/tickets/{ticket.id}/claim",
            json=claim_payload("orch-1"),
        )
        r = client.request(
            "DELETE",
            f"/api/v1/tickets/{ticket.id}/claim",
            json=claim_payload("orch-1"),
        )
        assert r.status_code == 200
        assert r.json()["released"] is True
        t = store.get_ticket(ticket.id)
        assert "claim" not in t.custom_fields

    def test_renew_claim(self, client, ticket):
        client.post(
            f"/api/v1/tickets/{ticket.id}/claim",
            json=claim_payload("orch-1", 60),
        )
        r = client.post(
            f"/api/v1/tickets/{ticket.id}/claim/renew",
            json=claim_payload("orch-1", 600),
        )
        assert r.status_code == 200

    def test_renew_wrong_owner_returns_409(self, client, ticket):
        client.post(
            f"/api/v1/tickets/{ticket.id}/claim",
            json=claim_payload("orch-1"),
        )
        r = client.post(
            f"/api/v1/tickets/{ticket.id}/claim/renew",
            json=claim_payload("orch-2", claim_id="claim-2"),
        )
        assert r.status_code == 409

    def test_claim_not_found(self, client):
        r = client.post(
            "/api/v1/tickets/PERF-NOSUCH/claim",
            json=claim_payload("orch-1"),
        )
        assert r.status_code == 404


class TestClaimSurvivesRestart:
    def test_claim_persisted_to_disk(self, store, ticket, tmp_path):
        store.claim_ticket(ticket.id, "orch-1", 300)
        store2 = TicketStore(persist_dir=tmp_path)
        t = store2.get_ticket(ticket.id)
        assert t.custom_fields["claim"]["owner"] == "orch-1"

    def test_new_instance_cannot_reclaim_unexpired(self, store, ticket):
        store.claim_ticket(ticket.id, "orch-1", 300)
        result = store.claim_ticket(ticket.id, "orch-2", 300)
        assert result is None

    def test_new_instance_reclaims_after_expiry(self, store, ticket):
        store.claim_ticket(ticket.id, "orch-1", 300)
        t = store._tickets[ticket.id]
        t.custom_fields["claim"]["expires"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat()
        result = store.claim_ticket(ticket.id, "orch-2", 300)
        assert result is not None
        assert result["owner"] == "orch-2"

    def test_same_instance_reclaims_own_after_restart(self, store, ticket):
        """Same instance name can re-claim even if unexpired (it's the same owner)."""
        store.claim_ticket(ticket.id, "orch-1", 300)
        result = store.claim_ticket(ticket.id, "orch-1", 300)
        assert result is not None
