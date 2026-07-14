"""Tests for ticket claim/lease mechanism in the state store."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from state_store.models import CreateTicketRequest
from state_store.store import TicketNotFound, TicketStore


@pytest.fixture
def store(tmp_path):
    return TicketStore(persist_dir=tmp_path)


@pytest.fixture
def ticket(store):
    return store.create_ticket(CreateTicketRequest(summary="test", description="test"))


class TestClaimTicket:
    def test_claim_success(self, store, ticket):
        result = store.claim_ticket(ticket.id, "orch-1", 300)
        assert result is not None
        assert result["owner"] == "orch-1"
        assert "expires" in result

    def test_claim_persists_to_custom_fields(self, store, ticket):
        store.claim_ticket(ticket.id, "orch-1", 300)
        t = store.get_ticket(ticket.id)
        assert "claim" in t.custom_fields
        assert t.custom_fields["claim"]["owner"] == "orch-1"

    def test_claim_rejected_when_held_by_other(self, store, ticket):
        store.claim_ticket(ticket.id, "orch-1", 300)
        result = store.claim_ticket(ticket.id, "orch-2", 300)
        assert result is None

    def test_same_owner_can_reclaim(self, store, ticket):
        store.claim_ticket(ticket.id, "orch-1", 300)
        result = store.claim_ticket(ticket.id, "orch-1", 300)
        assert result is not None
        assert result["owner"] == "orch-1"

    def test_expired_claim_can_be_taken(self, store, ticket):
        store.claim_ticket(ticket.id, "orch-1", 300)
        t = store._tickets[ticket.id]
        t.custom_fields["claim"]["expires"] = (
            datetime.now(timezone.utc) - timedelta(seconds=10)
        ).isoformat()
        result = store.claim_ticket(ticket.id, "orch-2", 300)
        assert result is not None
        assert result["owner"] == "orch-2"

    def test_claim_nonexistent_ticket(self, store):
        with pytest.raises(TicketNotFound):
            store.claim_ticket("PERF-NOSUCH", "orch-1", 300)

    def test_claim_records_status(self, store, ticket):
        result = store.claim_ticket(ticket.id, "orch-1", 300)
        assert result["status"] == "new"

    def test_claim_survives_reload(self, store, ticket, tmp_path):
        store.claim_ticket(ticket.id, "orch-1", 300)
        store2 = TicketStore(persist_dir=tmp_path)
        t = store2.get_ticket(ticket.id)
        assert t.custom_fields["claim"]["owner"] == "orch-1"


class TestReleaseClaim:
    def test_release_own_claim(self, store, ticket):
        store.claim_ticket(ticket.id, "orch-1", 300)
        assert store.release_claim(ticket.id, "orch-1") is True
        t = store.get_ticket(ticket.id)
        assert "claim" not in t.custom_fields

    def test_release_other_owner_noop(self, store, ticket):
        store.claim_ticket(ticket.id, "orch-1", 300)
        assert store.release_claim(ticket.id, "orch-2") is False
        t = store.get_ticket(ticket.id)
        assert t.custom_fields["claim"]["owner"] == "orch-1"

    def test_release_unclaimed_returns_false(self, store, ticket):
        assert store.release_claim(ticket.id, "orch-1") is False

    def test_release_nonexistent_ticket(self, store):
        with pytest.raises(TicketNotFound):
            store.release_claim("PERF-NOSUCH", "orch-1")


class TestRenewClaim:
    def test_renew_extends_expiry(self, store, ticket):
        store.claim_ticket(ticket.id, "orch-1", 60)
        t1 = store.get_ticket(ticket.id)
        exp1 = datetime.fromisoformat(t1.custom_fields["claim"]["expires"])

        result = store.renew_claim(ticket.id, "orch-1", 600)
        assert result is not None
        exp2 = datetime.fromisoformat(result["expires"])
        assert exp2 > exp1

    def test_renew_wrong_owner_returns_none(self, store, ticket):
        store.claim_ticket(ticket.id, "orch-1", 300)
        assert store.renew_claim(ticket.id, "orch-2", 300) is None

    def test_renew_unclaimed_returns_none(self, store, ticket):
        assert store.renew_claim(ticket.id, "orch-1", 300) is None

    def test_renew_nonexistent_ticket(self, store):
        with pytest.raises(TicketNotFound):
            store.renew_claim("PERF-NOSUCH", "orch-1", 300)
