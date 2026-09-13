from __future__ import annotations

from providers.tracing import (
    ActionDescriptor,
    ActionType,
    LifecycleDescriptor,
    LifecycleState,
    TraceEventV1,
)
from providers.tracing.query import TraceQuery, export_events, query_events


def test_causal_query_includes_ancestors_and_descendants() -> None:
    root = TraceEventV1(
        ticket_id="PERF-1",
        action=ActionDescriptor(type=ActionType.STATE),
        lifecycle=LifecycleDescriptor(state=LifecycleState.STARTED),
    )
    child = TraceEventV1(
        ticket_id="PERF-1",
        trace_id=root.trace_id,
        parent_action_id=root.action_id,
        action=ActionDescriptor(type=ActionType.STATE),
        lifecycle=LifecycleDescriptor(state=LifecycleState.STARTED),
    )
    result = query_events(
        [root, child], TraceQuery(action_id=child.action_id, causal=True)
    )
    assert [event.action_id for event in result] == [root.action_id, child.action_id]


def test_export_formats_are_stable() -> None:
    event = TraceEventV1(
        ticket_id="PERF-1",
        action=ActionDescriptor(type=ActionType.API),
        lifecycle=LifecycleDescriptor(state=LifecycleState.REQUESTED),
    )
    assert '"action_id"' in export_events([event], "json")
    assert export_events([event], "jsonl").endswith("\n")
    assert "global_seq,ticket_id" in export_events([event], "csv")
