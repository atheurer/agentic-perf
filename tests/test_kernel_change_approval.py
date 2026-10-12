"""State-store contract for approvals of kind ``kernel_change``.

A kernel change approval binds to the intent record the provisioning server
persisted under ``custom_fields.kernel_change_intents``; the shared
resolve/consume paths then work unchanged, and a benchmark approval can
never be spent as a kernel approval or vice versa.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from state_store.models import (
    AcquireOrchestratorLeaseRequest,
    ConsumeApprovalRequest,
    CreateApprovalRequest,
    CreateTicketRequest,
    ResolveApprovalRequest,
)
from state_store.store import TicketStore

INTENT_ID = "kci-" + "a" * 32
DIGEST = hashlib.sha256(b"kernel-intent").hexdigest()


def _claimed_store(tmp_path, clock=None):
    store = (
        TicketStore(persist_dir=tmp_path, clock=clock)
        if clock
        else TicketStore(persist_dir=tmp_path)
    )
    ticket = store.create_ticket(CreateTicketRequest(summary="s", description="d"))
    session_id = uuid4()
    store.acquire_orchestrator_lease(
        AcquireOrchestratorLeaseRequest(
            session_id=session_id,
            instance_name="test",
            host="localhost",
            pid=1,
            process_start_id="test",
            ttl_seconds=60,
        )
    )
    claim = store.claim_ticket(
        ticket.id, "provisioning", session_id=session_id, epoch=1
    )
    return store, ticket, str(session_id), claim["claim_id"]


def _seed_intent(store, ticket, digest=DIGEST):
    ticket.custom_fields["kernel_change_intents"] = {
        "records": {
            INTENT_ID: {
                "intent_id": INTENT_ID,
                "intent_digest": digest,
                "hosts": ["10.0.0.1", "10.0.0.2"],
                "actions_required": ["reboot"],
            }
        }
    }
    store._persist_ticket(ticket)


def _request(session_id, claim_id, **overrides) -> CreateApprovalRequest:
    body = dict(
        kind="kernel_change",
        validation_id=INTENT_ID,
        presented_run_file_digest=DIGEST,
        execution_intent_digest=DIGEST,
        summary="kernel change",
        invocation_id="invocation-1",
        tool_call_id="tool-call-1",
        session_id=session_id,
        session_epoch="1",
        waiter_owner="invocation-1",
        ticket_attempt=claim_id,
        claim_id=claim_id,
    )
    body.update(overrides)
    return CreateApprovalRequest(**body)


def _consume(session_id, claim_id, **overrides) -> ConsumeApprovalRequest:
    body = dict(
        kind="kernel_change",
        validation_id=INTENT_ID,
        presented_run_file_digest=DIGEST,
        execution_intent_digest=DIGEST,
        session_id=session_id,
        session_epoch="1",
        claim_id=claim_id,
        ticket_attempt=claim_id,
    )
    body.update(overrides)
    return ConsumeApprovalRequest(**body)


def test_kernel_change_binds_to_persisted_intent(tmp_path):
    store, ticket, session_id, claim_id = _claimed_store(tmp_path)
    _seed_intent(store, ticket)
    approval = store.create_approval_request(
        ticket.id, _request(session_id, claim_id), created_by="provisioning"
    )
    assert approval.kind == "kernel_change"
    assert approval.validation_id == INTENT_ID
    resolved = store.resolve_approval_request(
        ticket.id,
        approval.approval_request_id,
        ResolveApprovalRequest(decision="approved", comment_id="c-1"),
        resolved_by="alice",
    )
    assert resolved.status == "approved"
    consumed = store.consume_approval_request(
        ticket.id,
        approval.approval_request_id,
        _consume(session_id, claim_id),
        consumed_by="provisioning",
    )
    assert consumed.consumed_at is not None
    with pytest.raises(ValueError, match="already consumed"):
        store.consume_approval_request(
            ticket.id,
            approval.approval_request_id,
            _consume(session_id, claim_id),
            consumed_by="provisioning",
        )


def test_kernel_change_without_intent_is_rejected(tmp_path):
    store, ticket, session_id, claim_id = _claimed_store(tmp_path)
    with pytest.raises(ValueError, match="persisted kernel change intent"):
        store.create_approval_request(
            ticket.id, _request(session_id, claim_id), created_by="provisioning"
        )


def test_kernel_change_digest_mismatch_is_rejected(tmp_path):
    store, ticket, session_id, claim_id = _claimed_store(tmp_path)
    _seed_intent(store, ticket, digest=hashlib.sha256(b"other").hexdigest())
    with pytest.raises(ValueError, match="persisted kernel change intent"):
        store.create_approval_request(
            ticket.id, _request(session_id, claim_id), created_by="provisioning"
        )


def test_kernel_change_does_not_need_a_benchmark_validation(tmp_path):
    store, ticket, session_id, claim_id = _claimed_store(tmp_path)
    _seed_intent(store, ticket)
    assert "benchmark_validations" not in ticket.custom_fields
    approval = store.create_approval_request(
        ticket.id, _request(session_id, claim_id), created_by="provisioning"
    )
    assert approval.status == "pending"


def test_cross_kind_consumption_is_rejected(tmp_path):
    store, ticket, session_id, claim_id = _claimed_store(tmp_path)
    _seed_intent(store, ticket)
    approval = store.create_approval_request(
        ticket.id, _request(session_id, claim_id), created_by="provisioning"
    )
    store.resolve_approval_request(
        ticket.id,
        approval.approval_request_id,
        ResolveApprovalRequest(decision="approved", comment_id="c-1"),
        resolved_by="alice",
    )
    with pytest.raises(ValueError, match="kind mismatch"):
        store.consume_approval_request(
            ticket.id,
            approval.approval_request_id,
            _consume(session_id, claim_id, kind="benchmark_run_file"),
            consumed_by="benchmark",
        )


def test_approved_but_expired_cannot_be_consumed(tmp_path):
    now = [datetime.now(timezone.utc)]
    store, ticket, session_id, claim_id = _claimed_store(tmp_path, clock=lambda: now[0])
    _seed_intent(store, ticket)
    approval = store.create_approval_request(
        ticket.id,
        _request(session_id, claim_id, expires_at=now[0] + timedelta(minutes=5)),
        created_by="provisioning",
    )
    store.resolve_approval_request(
        ticket.id,
        approval.approval_request_id,
        ResolveApprovalRequest(decision="approved", comment_id="c-1"),
        resolved_by="alice",
    )
    # Expiry is judged against wall-clock time at consumption, so set the
    # approval's expiry into the past directly.
    raw = store.get_ticket(ticket.id).custom_fields["approval_requests"]
    record = raw[approval.approval_request_id]
    record["expires_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    stored = store.get_ticket(ticket.id)
    stored.custom_fields["approval_requests"][approval.approval_request_id] = record
    store._persist_ticket(stored)
    with pytest.raises(ValueError, match="expired"):
        store.consume_approval_request(
            ticket.id,
            approval.approval_request_id,
            _consume(session_id, claim_id),
            consumed_by="provisioning",
        )


def test_benchmark_kind_still_requires_validation_record(tmp_path):
    store, ticket, session_id, claim_id = _claimed_store(tmp_path)
    _seed_intent(store, ticket)
    with pytest.raises(ValueError, match="executable validation record"):
        store.create_approval_request(
            ticket.id,
            _request(session_id, claim_id, kind="benchmark_run_file"),
            created_by="benchmark",
        )
