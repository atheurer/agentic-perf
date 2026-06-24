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
