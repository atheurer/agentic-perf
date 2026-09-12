"""State-store mutations inherit request context rather than request bodies."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from providers.tracing import (
    bind_trace_context,
    new_trace_context,
    reset_trace_context,
    trace_headers,
)
from state_store.main import create_app
from state_store.models import CreateTicketRequest, TicketStatus, TransitionRequest
from state_store.store import InvalidTransition, TicketStore
from state_store.trace_store import TraceStore


def test_transition_and_rejection_have_old_new_state_and_parent(tmp_path) -> None:
    traces = TraceStore(tmp_path / "trace.db")
    store = TicketStore(persist_dir=tmp_path / "tickets", trace_store=traces)
    ticket = store.create_ticket(CreateTicketRequest(summary="x", description="x"))
    context = new_trace_context(ticket_id=ticket.id, agent_id="triage")
    token = bind_trace_context(context)
    try:
        store.transition_ticket(
            ticket.id, TransitionRequest(status=TicketStatus.TRIAGE_PENDING)
        )
        with pytest.raises(InvalidTransition):
            store.transition_ticket(
                ticket.id, TransitionRequest(status=TicketStatus.CLOSED)
            )
    finally:
        reset_trace_context(token)
    events = traces.list_events(ticket_id=ticket.id)
    transition = [e for e in events if e.action.target == "transition_ticket"]
    assert transition[-2].attributes == {
        "old_state": "new",
        "new_state": "triage_pending",
    }
    assert transition[-2].parent_action_id == context.action_id
    assert transition[-1].attributes == {
        "old_state": "triage_pending",
        "new_state": "closed",
    }
    assert transition[-1].lifecycle.state.value == "rejected"
    traces.close()


def test_interject_and_abort_endpoint_mutations_inherit_headers(tmp_path) -> None:
    app = create_app()
    app.state.store = TicketStore(
        persist_dir=tmp_path / "tickets", trace_store=app.state.trace_store
    )
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {app.state.api_token}"
    ticket = app.state.store.create_ticket(
        CreateTicketRequest(summary="x", description="x")
    )
    app.state.store.transition_ticket(
        ticket.id, TransitionRequest(status=TicketStatus.TRIAGE_PENDING)
    )
    context = new_trace_context(ticket_id=ticket.id, agent_id="triage")
    client.headers.update(trace_headers(context))
    assert (
        client.post(
            f"/api/v1/tickets/{ticket.id}/interject", json={"message": "wait"}
        ).status_code
        == 200
    )
    # Abort is intentionally only exposed from a paused ticket; seed that
    # state here because the test is exercising endpoint causality, not the
    # unrelated multi-step workflow that led to the pause.
    app.state.store._tickets[ticket.id].status = TicketStatus.AWAITING_CUSTOMER_GUIDANCE
    assert (
        client.post(
            f"/api/v1/tickets/{ticket.id}/abort", json={"reason": "stop"}
        ).status_code
        == 200
    )
    events = app.state.trace_store.list_events(ticket_id=ticket.id)
    assert any(event.action.target == "add_comment" for event in events)
    assert any(event.action.target == "update_fields" for event in events)
    app.state.trace_store.close()
