from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from state_store.models import AcquireOrchestratorLeaseRequest, CreateTicketRequest
from state_store.store import ClaimFenceError, TicketStore


def _lease(session_id, *, ttl_seconds=30):
    return AcquireOrchestratorLeaseRequest(
        session_id=session_id,
        instance_name="same-hostname",
        host="host-a",
        pid=123,
        process_start_id="boot:123",
        ttl_seconds=ttl_seconds,
    )


def test_ticket_claim_is_session_and_epoch_fenced(tmp_path):
    now = [datetime.now(timezone.utc)]
    store = TicketStore(persist_dir=tmp_path, clock=lambda: now[0])
    ticket = store.create_ticket(CreateTicketRequest(summary="x", description="x"))
    first_session = uuid4()
    first_lease = store.acquire_orchestrator_lease(_lease(first_session))
    claim = store.claim_ticket(
        ticket.id,
        "same-hostname",
        session_id=first_session,
        epoch=first_lease.epoch,
    )
    assert claim["session_id"] == str(first_session)
    assert claim["epoch"] == first_lease.epoch
    now[0] += timedelta(seconds=31)
    second_session = uuid4()
    second_lease = store.acquire_orchestrator_lease(_lease(second_session))
    assert second_lease.epoch > first_lease.epoch
    with pytest.raises(ClaimFenceError, match="stale") as error:
        store.renew_claim(
            ticket.id,
            "same-hostname",
            session_id=first_session,
            epoch=first_lease.epoch,
            claim_id=claim["claim_id"],
        )
    assert error.value.reason == "stale_epoch"


def test_claim_id_prevents_cross_session_release(tmp_path):
    store = TicketStore(persist_dir=tmp_path)
    ticket = store.create_ticket(CreateTicketRequest(summary="x", description="x"))
    session = uuid4()
    lease = store.acquire_orchestrator_lease(_lease(session))
    claim = store.claim_ticket(
        ticket.id, "same-hostname", session_id=session, epoch=lease.epoch
    )
    assert claim["claim_id"]
    with pytest.raises(ClaimFenceError) as error:
        store.release_claim(
            ticket.id,
            "same-hostname",
            session_id=session,
            epoch=lease.epoch,
            claim_id=str(uuid4()),
        )
    assert error.value.reason == "claim_owned_by_other_session"


def test_expired_claim_renewal_is_structured_and_audited(tmp_path):
    class Audit:
        def __init__(self):
            self.entries = []

        def log(self, mutation, ticket_id, data):
            self.entries.append((mutation, ticket_id, data))

    audit = Audit()
    store = TicketStore(persist_dir=tmp_path, audit_log=audit)
    ticket = store.create_ticket(CreateTicketRequest(summary="x", description="x"))
    session = uuid4()
    lease = store.acquire_orchestrator_lease(_lease(session))
    claim = store.claim_ticket(
        ticket.id, "same-hostname", session_id=session, epoch=lease.epoch
    )
    store._tickets[ticket.id].custom_fields["claim"]["expires"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    with pytest.raises(ClaimFenceError) as error:
        store.renew_claim(
            ticket.id,
            "same-hostname",
            session_id=session,
            epoch=lease.epoch,
            claim_id=claim["claim_id"],
        )
    assert error.value.reason == "claim_expired"
    assert any(
        data.get("reason") == "claim_expired"
        for mutation, _, data in audit.entries
        if mutation == "claim_fence_rejected"
    )


def test_expired_claim_rejects_fenced_mutation_even_with_active_lease(tmp_path):
    store = TicketStore(persist_dir=tmp_path)
    ticket = store.create_ticket(CreateTicketRequest(summary="x", description="x"))
    session = uuid4()
    lease = store.acquire_orchestrator_lease(_lease(session))
    claim = store.claim_ticket(
        ticket.id, "same-hostname", session_id=session, epoch=lease.epoch
    )
    store._tickets[ticket.id].custom_fields["claim"]["expires"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    with pytest.raises(ClaimFenceError) as error:
        store.update_fields(
            ticket.id,
            {"note": "must be fenced"},
            session_id=session,
            epoch=lease.epoch,
            claim_id=claim["claim_id"],
        )
    assert error.value.reason == "claim_expired"


def test_malformed_claim_rejects_fenced_mutation_with_audit(tmp_path):
    class Audit:
        def __init__(self):
            self.entries = []

        def log(self, mutation, ticket_id, data):
            self.entries.append((mutation, ticket_id, data))

    audit = Audit()
    store = TicketStore(persist_dir=tmp_path, audit_log=audit)
    ticket = store.create_ticket(CreateTicketRequest(summary="x", description="x"))
    session = uuid4()
    lease = store.acquire_orchestrator_lease(_lease(session))
    claim = store.claim_ticket(
        ticket.id, "same-hostname", session_id=session, epoch=lease.epoch
    )
    store._tickets[ticket.id].custom_fields["claim"]["epoch"] = "bad"
    with pytest.raises(ClaimFenceError) as error:
        store.update_fields(
            ticket.id,
            {"note": "must be fenced"},
            session_id=session,
            epoch=lease.epoch,
            claim_id=claim["claim_id"],
        )
    assert error.value.reason == "claim_malformed"
    assert any(
        data.get("reason") == "claim_malformed"
        for mutation, _, data in audit.entries
        if mutation == "claim_fence_rejected"
    )


def test_claim_conflict_is_audited_before_fenced_rejection(tmp_path):
    class Audit:
        def __init__(self):
            self.entries = []

        def log(self, mutation, ticket_id, data):
            self.entries.append((mutation, ticket_id, data))

    audit = Audit()
    store = TicketStore(persist_dir=tmp_path, audit_log=audit)
    ticket = store.create_ticket(CreateTicketRequest(summary="x", description="x"))
    first = uuid4()
    lease = store.acquire_orchestrator_lease(_lease(first))
    store.claim_ticket(ticket.id, "same-hostname", session_id=first, epoch=lease.epoch)
    with pytest.raises(ClaimFenceError) as error:
        store.claim_ticket(
            ticket.id,
            "other",
            session_id=first,
            epoch=lease.epoch,
            claim_id="two",
        )
    assert error.value.reason == "claim_owned_by_other_session"
    assert any(
        data.get("reason") == "claim_owned_by_other_session"
        for mutation, _, data in audit.entries
        if mutation == "claim_fence_rejected"
    )
