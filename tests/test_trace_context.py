"""Tests for task-local immutable trace context propagation."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from providers.tracing import (
    TraceContext,
    bind_trace_context,
    child_context,
    current_trace_context,
    new_trace_context,
    reset_trace_context,
)


async def test_nested_tasks_inherit_context_and_siblings_are_unique() -> None:
    root = new_trace_context(ticket_id="PERF-123", agent_id="triage")
    token = bind_trace_context(root)
    try:

        async def make_child() -> TraceContext:
            assert current_trace_context() == root
            return child_context(tool_call_id="call-1")

        first, second = await asyncio.gather(make_child(), make_child())
    finally:
        reset_trace_context(token)

    assert first.trace_id == second.trace_id == root.trace_id
    assert first.invocation_id == second.invocation_id == root.invocation_id
    assert first.parent_action_id == second.parent_action_id == root.action_id
    assert first.action_id != second.action_id
    assert root.tool_call_id is None
    assert current_trace_context() is None


def test_context_is_immutable_and_w3c_compatible() -> None:
    context = new_trace_context(ticket_id="PERF-123")

    with pytest.raises(ValidationError):
        TraceContext(trace_id="not-a-trace-id")
    with pytest.raises(ValidationError):
        context.action_id = "0123456789abcdef"

    assert len(context.trace_id) == 32
    assert len(context.action_id) == 16
