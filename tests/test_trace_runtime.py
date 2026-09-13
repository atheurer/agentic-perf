"""Focused causality tests for dispatch-to-agent runtime envelopes."""

from __future__ import annotations

from providers.tracing import (
    ActionType,
    LifecycleState,
    OperationOutcome,
    RetryKind,
    TraceRecorder,
    child_context,
    new_trace_context,
    trace_headers,
)


class _Sink:
    def __init__(self) -> None:
        self.events = []

    def record(self, event) -> None:
        self.events.append(event)


def test_llm_tool_tree_preserves_invocation_and_parentage() -> None:
    sink = _Sink()
    root = new_trace_context(ticket_id="PERF-1", agent_id="triage")
    recorder = TraceRecorder(root, sink)
    llm, timer = recorder.start(ActionType.LLM, iteration=1)
    recorder.record(
        llm,
        ActionType.LLM,
        LifecycleState.COMPLETED,
        duration_ms=timer.elapsed_ms(),
        outcome=OperationOutcome.SUCCESS,
    )
    tool, timer = recorder.start(
        ActionType.TOOL, parent=llm, tool_call_id="toolu_123", iteration=1
    )
    recorder.record(
        tool,
        ActionType.TOOL,
        LifecycleState.COMPLETED,
        duration_ms=timer.elapsed_ms(),
        outcome=OperationOutcome.SUCCESS,
    )

    assert tool.invocation_id == llm.invocation_id == root.invocation_id
    assert tool.parent_action_id == llm.action_id
    assert sink.events[-1].tool_call_id == "toolu_123"


def test_trace_headers_have_w3c_and_agentic_perf_ids() -> None:
    context = new_trace_context(ticket_id="PERF-1", agent_id="triage")
    headers = trace_headers(context)
    assert headers["traceparent"] == f"00-{context.trace_id}-{context.action_id}-01"
    assert headers["X-Agentic-Perf-Invocation-Id"] == str(context.invocation_id)
    assert headers["X-Agentic-Perf-Causal-Context"] == "v1"


def test_proposed_tool_reuses_one_action_id_through_terminal_result() -> None:
    sink = _Sink()
    root = new_trace_context(ticket_id="PERF-1", agent_id="triage")
    recorder = TraceRecorder(root, sink)
    llm, _ = recorder.start(ActionType.LLM, iteration=1)
    tool = child_context(llm, tool_call_id="call-1", iteration=1)
    recorder.record(tool, ActionType.TOOL, LifecycleState.PROPOSED, phase="lookup")
    recorder.record(tool, ActionType.TOOL, LifecycleState.STARTED, phase="lookup")
    recorder.record(
        tool,
        ActionType.TOOL,
        LifecycleState.CANCELLED,
        phase="lookup",
        duration_ms=1,
        outcome=OperationOutcome.CANCELLED,
    )
    tool_events = [
        event for event in sink.events if event.action.type == ActionType.TOOL
    ]
    assert {event.action_id for event in tool_events} == {tool.action_id}
    assert tool_events[-1].outcome == OperationOutcome.CANCELLED


def test_retry_attempt_is_explicit_and_not_a_transport_replay() -> None:
    sink = _Sink()
    recorder = TraceRecorder(new_trace_context(ticket_id="PERF-1"), sink)
    attempt, _ = recorder.start(
        ActionType.LLM, retry_kind=RetryKind.INTENTIONAL_AGENT_RETRY
    )
    assert sink.events[-1].lifecycle.retry_kind == RetryKind.INTENTIONAL_AGENT_RETRY
    assert sink.events[-1].lifecycle.replay_of_action_id is None


def test_primary_and_introspection_use_distinct_invocation_branches() -> None:
    primary = new_trace_context(ticket_id="PERF-1", agent_id="triage")
    observer = new_trace_context(ticket_id="PERF-1", agent_id="introspection-agent")
    assert primary.invocation_id != observer.invocation_id
    assert primary.trace_id != observer.trace_id
