"""Tests for state store transition logic.

Covers: double request_clarification, previous_status preservation.
"""

from __future__ import annotations

import pytest

from state_store.models import TicketStatus, TransitionRequest
from state_store.store import TicketStore


@pytest.fixture
def store(tmp_path):
    return TicketStore(persist_dir=tmp_path)


@pytest.fixture
def ticket_in_benchmark(store):
    """Create a ticket and advance it to executing_benchmark."""
    from state_store.models import CreateTicketRequest

    ticket = store.create_ticket(
        CreateTicketRequest(summary="test", description="test")
    )
    tid = ticket.id
    for status in [
        "triage_pending",
        "awaiting_hardware",
        "awaiting_provision",
        "executing_benchmark",
    ]:
        store.transition_ticket(tid, TransitionRequest(status=status))
    return store.get_ticket(tid)


class TestDoubleRequestClarification:
    def test_preparing_platform_can_return_to_resource_reconciliation(self, store):
        from state_store.models import CreateTicketRequest

        ticket = store.create_ticket(
            CreateTicketRequest(summary="test", description="test")
        )
        for status in (
            "triage_pending",
            "awaiting_hardware",
            "preparing_platform",
            "awaiting_customer_guidance",
            "preparing_platform",
            "awaiting_hardware",
        ):
            store.transition_ticket(ticket.id, TransitionRequest(status=status))

        assert store.get_ticket(ticket.id).status == TicketStatus.AWAITING_HARDWARE

    def test_single_pause_and_resume(self, store, ticket_in_benchmark):
        """Normal case: pause once, resume back to executing_benchmark."""
        tid = ticket_in_benchmark.id
        store.transition_ticket(
            tid, TransitionRequest(status="awaiting_customer_guidance")
        )
        t = store.get_ticket(tid)
        assert t.status == TicketStatus.AWAITING_CUSTOMER_GUIDANCE
        assert t.previous_status == TicketStatus.EXECUTING_BENCHMARK

        store.transition_ticket(tid, TransitionRequest(status="executing_benchmark"))
        t = store.get_ticket(tid)
        assert t.status == TicketStatus.EXECUTING_BENCHMARK
        assert t.previous_status is None

    def test_double_pause_preserves_original_status(self, store, ticket_in_benchmark):
        """Double request_clarification must not clobber previous_status."""
        tid = ticket_in_benchmark.id

        store.transition_ticket(
            tid, TransitionRequest(status="awaiting_customer_guidance")
        )
        t = store.get_ticket(tid)
        assert t.previous_status == TicketStatus.EXECUTING_BENCHMARK

        store.transition_ticket(
            tid, TransitionRequest(status="awaiting_customer_guidance")
        )
        t = store.get_ticket(tid)
        assert t.status == TicketStatus.AWAITING_CUSTOMER_GUIDANCE
        assert t.previous_status == TicketStatus.EXECUTING_BENCHMARK

        store.transition_ticket(tid, TransitionRequest(status="executing_benchmark"))
        t = store.get_ticket(tid)
        assert t.status == TicketStatus.EXECUTING_BENCHMARK

    def test_triple_pause_preserves_original_status(self, store, ticket_in_benchmark):
        """Even three consecutive pauses preserve the original status."""
        tid = ticket_in_benchmark.id

        store.transition_ticket(
            tid, TransitionRequest(status="awaiting_customer_guidance")
        )
        store.transition_ticket(
            tid, TransitionRequest(status="awaiting_customer_guidance")
        )
        store.transition_ticket(
            tid, TransitionRequest(status="awaiting_customer_guidance")
        )
        t = store.get_ticket(tid)
        assert t.previous_status == TicketStatus.EXECUTING_BENCHMARK

        store.transition_ticket(tid, TransitionRequest(status="executing_benchmark"))
        t = store.get_ticket(tid)
        assert t.status == TicketStatus.EXECUTING_BENCHMARK

    def test_abort_from_double_pause(self, store, ticket_in_benchmark):
        """Abort (awaiting_teardown) works even after double pause."""
        tid = ticket_in_benchmark.id

        store.transition_ticket(
            tid, TransitionRequest(status="awaiting_customer_guidance")
        )
        store.transition_ticket(
            tid, TransitionRequest(status="awaiting_customer_guidance")
        )

        store.transition_ticket(tid, TransitionRequest(status="awaiting_teardown"))
        t = store.get_ticket(tid)
        assert t.status == TicketStatus.AWAITING_TEARDOWN


class TestExecutionIntentMinting:
    """#1378: server-owned, idempotent per-authorization attempt identity."""

    def test_mint_is_idempotent_and_numbers_attempts(self, store):
        from state_store.models import CreateTicketRequest

        ticket = store.create_ticket(CreateTicketRequest(summary="s", description="d"))
        first = store.mint_benchmark_execution_intent(ticket.id, "val-1", "apr-first")
        # A duplicate resume of the same authorization receives the
        # canonical entry — it must not mint a second intent.
        duplicate = store.mint_benchmark_execution_intent(
            ticket.id, "val-1", "apr-first"
        )
        assert duplicate == first
        assert first["attempt_no"] == 1
        assert first["execution_intent_id"] == "val-1:apr-first"

        second = store.mint_benchmark_execution_intent(ticket.id, "val-1", "apr-second")
        assert second["attempt_no"] == 2
        assert second["previous_execution_intent_id"] == first["execution_intent_id"]

    def test_generic_fields_patch_cannot_write_intents(self, store):
        from state_store.models import CreateTicketRequest

        ticket = store.create_ticket(CreateTicketRequest(summary="s", description="d"))
        with pytest.raises(ValueError, match="benchmark execution intents"):
            store.update_fields(
                ticket.id, {"benchmark_execution_intents": {"val-1": {}}}
            )

    def test_mint_requires_identifiers(self, store):
        from state_store.models import CreateTicketRequest

        ticket = store.create_ticket(CreateTicketRequest(summary="s", description="d"))
        with pytest.raises(ValueError, match="validation_id and"):
            store.mint_benchmark_execution_intent(ticket.id, "", "apr-1")


def test_execution_intents_api_is_service_only_and_idempotent(tmp_path):
    from fastapi.testclient import TestClient

    from state_store.main import create_app
    from state_store.models import CreateTicketRequest
    from state_store.store import TicketStore

    application = create_app(initialize_immediately=True)
    application.state.store = TicketStore(persist_dir=tmp_path)
    client = TestClient(application)
    token = application.state.api_token
    store = application.state.store
    ticket = store.create_ticket(CreateTicketRequest(summary="s", description="d"))
    path = f"/api/v1/tickets/{ticket.id}/execution-intents"
    body = {"validation_id": "val-1", "authorization_id": "apr-1"}

    unauthenticated = client.post(path, json=body)
    assert unauthenticated.status_code == 401

    first = client.post(path, json=body, headers={"Authorization": f"Bearer {token}"})
    assert first.status_code == 200, first.text
    duplicate = client.post(
        path, json=body, headers={"Authorization": f"Bearer {token}"}
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["entry"] == first.json()["entry"]
    assert first.json()["entry"]["attempt_no"] == 1
