"""Single compatibility mapping between legacy event APIs and TraceEventV1."""

from __future__ import annotations

from datetime import timezone
from typing import Any

from providers.tracing import (
    ActionDescriptor,
    ActionType,
    LifecycleDescriptor,
    LifecycleState,
    TraceEventV1,
)


def legacy_to_trace(
    ticket_id: str, agent: str, event_type: str, data: dict[str, Any]
) -> TraceEventV1:
    """Map a public EventBus write to an immutable trace envelope."""
    return TraceEventV1(
        ticket_id=ticket_id,
        agent_id=agent or None,
        action=ActionDescriptor(type=ActionType.STATE, phase=event_type),
        lifecycle=LifecycleDescriptor(state=LifecycleState.STARTED),
        producer={"component": "legacy-event-adapter"},
        attributes={
            "legacy_event": {"agent": agent, "event_type": event_type, "data": data}
        },
    )


def trace_to_legacy(event: TraceEventV1) -> dict[str, Any]:
    """Project a v1 trace record into the established EventBus response shape."""
    legacy = (event.attributes or {}).get("legacy_event", {})
    return {
        "seq": event.ticket_seq or 0,
        "timestamp": event.occurred_at.astimezone(timezone.utc).isoformat(),
        "ticket_id": event.ticket_id or "",
        "agent": legacy.get("agent", event.agent_id or ""),
        "event_type": legacy.get(
            "event_type", event.action.phase or event.action.type.value
        ),
        "data": legacy.get("data", {}),
        "schema_version": "v1",
        "trace_id": event.trace_id,
        "action_id": event.action_id,
    }


def legacy_record(record: dict[str, Any], line: int) -> dict[str, Any]:
    """Mark an immutable schema-0 JSONL record without rewriting its file."""
    projected = dict(record)
    projected["seq"] = line
    projected["schema_version"] = "legacy_uncorrelated"
    return projected


def audit_to_trace(
    ticket_id: str, mutation: str, actor: dict[str, str], data: dict[str, Any]
) -> TraceEventV1:
    """Map a legacy state mutation to the same canonical event stream."""
    return TraceEventV1(
        ticket_id=ticket_id,
        action=ActionDescriptor(type=ActionType.STATE, phase=mutation),
        lifecycle=LifecycleDescriptor(state=LifecycleState.COMPLETED),
        duration_ms=0,
        outcome="success",
        producer={"component": "legacy-audit-adapter"},
        attributes={
            "legacy_audit": {"mutation": mutation, "actor": actor, "data": data}
        },
    )


def trace_to_audit(event: TraceEventV1) -> dict[str, Any] | None:
    """Project only state-mutation trace records into the public audit shape."""
    audit = (event.attributes or {}).get("legacy_audit")
    if not isinstance(audit, dict):
        return None
    return {
        "seq": event.global_seq or 0,
        "timestamp": event.occurred_at.isoformat(),
        "ticket_id": event.ticket_id,
        **audit,
    }


def event_order_key(event: dict[str, Any]) -> tuple[int, int]:
    """Use immutable source identity and sequence, never mutable wall time.

    Historical JSONL precedes canonical records. Within each source, the
    persisted line/ticket sequence is immutable, so later backdated events
    cannot move an SSE cursor that has already been consumed.
    """
    return (
        0 if event.get("schema_version") == "legacy_uncorrelated" else 1,
        event.get("seq", 0),
    )
