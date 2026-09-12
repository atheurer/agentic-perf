"""State-store mutations inherit request context rather than request bodies."""

from __future__ import annotations

import pytest

from providers.tracing import (
    ActionType,
    LifecycleState,
    OperationOutcome,
    TraceRecorder,
    bind_trace_context,
    new_trace_context,
    reset_trace_context,
)
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


def test_request_payload_fields_cannot_spoof_bound_causal_identity(tmp_path) -> None:
    traces = TraceStore(tmp_path / "trace.db")
    store = TicketStore(persist_dir=tmp_path / "tickets", trace_store=traces)
    ticket = store.create_ticket(CreateTicketRequest(summary="x", description="x"))
    trusted = new_trace_context(ticket_id=ticket.id, agent_id="trusted-agent")
    token = bind_trace_context(trusted)
    try:
        store.update_fields(
            ticket.id,
            {
                "agent_id": "forged-agent",
                "invocation_id": "00000000-0000-0000-0000-000000000000",
                "parent_action_id": "0" * 16,
            },
        )
    finally:
        reset_trace_context(token)
    event = [
        e for e in traces.list_events(ticket.id) if e.action.target == "update_fields"
    ][0]
    assert event.agent_id == "trusted-agent"
    assert event.invocation_id == trusted.invocation_id
    assert event.parent_action_id == trusted.action_id
    traces.close()


def test_dispatch_llm_tool_state_chain_has_no_missing_parent(tmp_path) -> None:
    """A lightweight end-to-end causal reconstruction across the store boundary."""
    traces = TraceStore(tmp_path / "trace.db")
    store = TicketStore(persist_dir=tmp_path / "tickets", trace_store=traces)
    ticket = store.create_ticket(CreateTicketRequest(summary="x", description="x"))
    recorder = TraceRecorder()
    dispatch, _ = recorder.start(ActionType.DISPATCH)
    dispatch = dispatch.model_copy(
        update={"ticket_id": ticket.id, "agent_id": "triage"}
    )
    llm, timer = recorder.start(ActionType.LLM, parent=dispatch, iteration=1)
    recorder.record(
        llm,
        ActionType.LLM,
        LifecycleState.COMPLETED,
        duration_ms=timer.elapsed_ms(),
        outcome=OperationOutcome.SUCCESS,
    )
    from providers.tracing import child_context

    tool = child_context(llm, tool_call_id="call-1")
    token = bind_trace_context(tool)
    try:
        store.update_fields(ticket.id, {"from_tool": True})
    finally:
        reset_trace_context(token)
    state = [
        e for e in traces.list_events(ticket.id) if e.action.target == "update_fields"
    ][0]
    assert state.parent_action_id == tool.action_id
    assert tool.parent_action_id == llm.action_id
    assert llm.parent_action_id == dispatch.action_id
    traces.close()
