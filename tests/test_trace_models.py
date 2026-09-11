"""Tests for the versioned trace event contract."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from providers.tracing import (
    ActionDescriptor,
    ActionType,
    LifecycleDescriptor,
    LifecycleState,
    MCPIdentity,
    OperationOutcome,
    TraceEventV1,
)


def _event(**updates: object) -> TraceEventV1:
    values = {
        "ticket_id": "PERF-123",
        "action": ActionDescriptor(type=ActionType.AGENT),
        "lifecycle": LifecycleDescriptor(state=LifecycleState.STARTED),
        "invocation_id": "be0e45d2-c60d-43b2-b9d7-2431e29c38e4",
    }
    values.update(updates)
    return TraceEventV1(**values)


def test_json_round_trip_is_stable() -> None:
    event = _event()

    assert TraceEventV1.model_validate_json(event.model_dump_json()) == event


def test_checked_in_schema_matches_model() -> None:
    schema_path = Path("docs/schemas/trace-event-v1.json")

    assert json.loads(schema_path.read_text()) == TraceEventV1.model_json_schema()


def test_serialization_keeps_null_fields() -> None:
    data = _event().model_dump(mode="json")

    assert "recorded_at" in data and data["recorded_at"] is None
    assert "tool_call_id" in data and data["tool_call_id"] is None
    assert "input" in data and data["input"] is None
    assert data["producer"]["host"] is None
    assert data["mcp"]["session_id"] is None


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"ticket_id": ""}, "ticket_id"),
        (
            {
                "action": ActionDescriptor(type=ActionType.AGENT),
                "invocation_id": None,
            },
            "invocation_id",
        ),
        ({"action": ActionDescriptor(type=ActionType.TOOL)}, "tool_call_id"),
        (
            {
                "action": ActionDescriptor(type=ActionType.MCP),
                "mcp": MCPIdentity(session_id="session"),
            },
            "correlation_request_id",
        ),
        (
            {"lifecycle": LifecycleDescriptor(state=LifecycleState.COMPLETED)},
            "terminal events",
        ),
    ],
)
def test_layer_specific_required_fields_are_validated(
    updates: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        _event(**updates)


def test_terminal_helpers_create_valid_events() -> None:
    action = ActionDescriptor(type=ActionType.AGENT)
    lifecycle = LifecycleDescriptor(state=LifecycleState.STARTED)

    failed = TraceEventV1.from_exception(
        ticket_id="PERF-123",
        invocation_id="be0e45d2-c60d-43b2-b9d7-2431e29c38e4",
        action=action,
        lifecycle=lifecycle,
        exception=RuntimeError("broken"),
        duration_ms=3.5,
    )
    cancelled = TraceEventV1.from_cancellation(
        ticket_id="PERF-123",
        invocation_id="be0e45d2-c60d-43b2-b9d7-2431e29c38e4",
        action=action,
        lifecycle=lifecycle,
        duration_ms=3.5,
    )

    assert failed.outcome == OperationOutcome.FAILURE
    assert failed.error is not None and failed.error.type == "RuntimeError"
    assert cancelled.outcome == OperationOutcome.CANCELLED


def test_w3c_compatible_ids_are_created() -> None:
    event = _event()

    assert len(event.trace_id) == 32
    assert len(event.action_id) == 16
    assert int(event.trace_id, 16) >= 0
    assert int(event.action_id, 16) >= 0


def test_timestamps_must_be_utc() -> None:
    event = _event(occurred_at=datetime.now(timezone.utc))

    assert event.occurred_at.tzinfo == timezone.utc
    with pytest.raises(ValidationError, match="UTC"):
        _event(occurred_at=datetime.now())
