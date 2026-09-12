"""Focused causality tests for dispatch-to-agent runtime envelopes."""

from __future__ import annotations

from providers.tracing import (
    ActionType,
    LifecycleState,
    OperationOutcome,
    TraceRecorder,
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
