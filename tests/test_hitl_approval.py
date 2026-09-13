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


def _approval_request(*, session_id: str, claim_id: str) -> CreateApprovalRequest:
    return CreateApprovalRequest(
        validation_id="val-" + "a" * 32,
        presented_run_file_digest=hashlib.sha256(b"run-file").hexdigest(),
        execution_intent_digest=hashlib.sha256(b"intent").hexdigest(),
        summary="benchmark approval",
        invocation_id="invocation-1",
        tool_call_id="tool-call-1",
        session_id=session_id,
        session_epoch="1",
        waiter_owner="invocation-1",
        ticket_attempt=claim_id,
        claim_id=claim_id,
    )


def test_approval_is_persisted_and_resolved_once(tmp_path):
    store = TicketStore(persist_dir=tmp_path)
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
    store.claim_ticket(ticket.id, "benchmark")
    claim_id = ticket.custom_fields["claim"]["claim_id"]
    approval_body = _approval_request(session_id=str(session_id), claim_id=claim_id)
    ticket.custom_fields["validated_run_file"] = {
        "validation_id": approval_body.validation_id,
        "state": "executable",
        "runfile_fingerprint": approval_body.presented_run_file_digest,
        "execution_intent_digest": approval_body.execution_intent_digest,
    }
    store._persist_ticket(ticket)
    approval = store.create_approval_request(
        ticket.id, approval_body, created_by="benchmark"
    )
    resolved = store.resolve_approval_request(
        ticket.id,
        approval.approval_request_id,
        ResolveApprovalRequest(decision="approved", comment_id="comment-1"),
        resolved_by="alice",
    )
    assert resolved.status == "approved"
    with pytest.raises(ValueError, match="already approved"):
        store.resolve_approval_request(
            ticket.id,
            approval.approval_request_id,
            ResolveApprovalRequest(decision="rejected"),
            resolved_by="bob",
        )
    restarted = TicketStore(persist_dir=tmp_path)
    assert restarted.list_approval_requests(ticket.id)[0].status == "approved"

    consumed = restarted.consume_approval_request(
        ticket.id,
        approval.approval_request_id,
        ConsumeApprovalRequest(
            validation_id=approval_body.validation_id,
            presented_run_file_digest=approval_body.presented_run_file_digest,
            execution_intent_digest=approval_body.execution_intent_digest,
            session_id=str(session_id),
            session_epoch="1",
            claim_id=ticket.custom_fields["claim"]["claim_id"],
            ticket_attempt=ticket.custom_fields["claim"]["claim_id"],
        ),
        consumed_by="benchmark",
    )
    assert consumed.consumed_at is not None
    with pytest.raises(ValueError, match="already consumed"):
        restarted.consume_approval_request(
            ticket.id,
            approval.approval_request_id,
            ConsumeApprovalRequest(
                validation_id=approval_body.validation_id,
                presented_run_file_digest=approval_body.presented_run_file_digest,
                execution_intent_digest=approval_body.execution_intent_digest,
                session_id=str(session_id),
                session_epoch="1",
                claim_id=ticket.custom_fields["claim"]["claim_id"],
                ticket_attempt=ticket.custom_fields["claim"]["claim_id"],
            ),
            consumed_by="benchmark",
        )


def test_approval_resolution_rejects_intent_mismatch(tmp_path):
    store = TicketStore(persist_dir=tmp_path)
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
    store.claim_ticket(ticket.id, "benchmark")
    approval_body = _approval_request(
        session_id=str(session_id), claim_id=ticket.custom_fields["claim"]["claim_id"]
    )
    ticket.custom_fields["validated_run_file"] = {
        "validation_id": approval_body.validation_id,
        "state": "executable",
        "runfile_fingerprint": approval_body.presented_run_file_digest,
        "execution_intent_digest": approval_body.execution_intent_digest,
    }
    store._persist_ticket(ticket)
    approval = store.create_approval_request(
        ticket.id, approval_body, created_by="benchmark"
    )
    with pytest.raises(ValueError, match="execution_intent_digest"):
        store.resolve_approval_request(
            ticket.id,
            approval.approval_request_id,
            ResolveApprovalRequest(
                decision="approved", execution_intent_digest="b" * 64
            ),
            resolved_by="alice",
        )


def test_approval_creation_is_idempotent_and_expiry_is_durable(tmp_path):
    now = datetime.now(timezone.utc)
    store = TicketStore(persist_dir=tmp_path, clock=lambda: now)
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
    store.claim_ticket(ticket.id, "benchmark")
    body = _approval_request(
        session_id=str(session_id), claim_id=ticket.custom_fields["claim"]["claim_id"]
    )
    ticket.custom_fields["validated_run_file"] = {
        "validation_id": body.validation_id,
        "state": "executable",
        "runfile_fingerprint": body.presented_run_file_digest,
        "execution_intent_digest": body.execution_intent_digest,
    }
    store._persist_ticket(ticket)
    first = store.create_approval_request(ticket.id, body, created_by="benchmark")
    second = store.create_approval_request(ticket.id, body, created_by="benchmark")
    assert first.approval_request_id == second.approval_request_id
    distinct_waiter = body.model_copy(update={"tool_call_id": "tool-call-2"})
    third = store.create_approval_request(
        ticket.id, distinct_waiter, created_by="benchmark"
    )
    assert third.approval_request_id != first.approval_request_id

    expiring = body.model_copy(update={"expires_at": now + timedelta(seconds=1)})
    ticket.custom_fields["validated_run_file"]["validation_id"] = "val-" + "b" * 32
    expiring = expiring.model_copy(update={"validation_id": "val-" + "b" * 32})
    store._persist_ticket(ticket)
    expired = store.create_approval_request(ticket.id, expiring, created_by="benchmark")
    store._clock = lambda: now + timedelta(seconds=2)
    assert store.list_approval_requests(ticket.id)[-1].status == "expired"
    assert expired.approval_request_id != first.approval_request_id


def test_approval_creation_rejects_missing_fence(tmp_path):
    store = TicketStore(persist_dir=tmp_path)
    ticket = store.create_ticket(CreateTicketRequest(summary="s", description="d"))
    body = _approval_request(session_id=str(uuid4()), claim_id="claim-missing")
    ticket.custom_fields["validated_run_file"] = {
        "validation_id": body.validation_id,
        "state": "executable",
        "runfile_fingerprint": body.presented_run_file_digest,
        "execution_intent_digest": body.execution_intent_digest,
    }
    store._persist_ticket(ticket)
    with pytest.raises(ValueError, match="active matching session"):
        store.create_approval_request(ticket.id, body, created_by="benchmark")
