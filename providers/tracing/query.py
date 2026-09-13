"""Read-only filtering, causal expansion, and export of trace events."""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from .models import TraceEventV1


@dataclass(frozen=True)
class TraceQuery:
    """Bounded, composable filters for a trace projection."""

    ticket_id: str | None = None
    trace_id: str | None = None
    invocation_id: str | None = None
    action_id: str | None = None
    parent_action_id: str | None = None
    action_type: str | None = None
    lifecycle_state: str | None = None
    outcome: str | None = None
    producer_component: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    causal: bool = False
    limit: int = 1000


def query_events(
    events: Iterable[TraceEventV1], query: TraceQuery
) -> list[TraceEventV1]:
    """Filter events and, when requested, include their complete causal tree.

    Causal expansion is deliberately performed after the direct filters: a
    caller can select one action and receive all ancestors and descendants
    without exposing events from another ticket or trace.
    """
    values = list(events)
    direct = [event for event in values if _matches(event, query)]
    if query.causal and direct:
        selected = {event.action_id for event in direct}
        scope_tickets = {event.ticket_id for event in direct}
        scoped = [event for event in values if event.ticket_id in scope_tickets]
        changed = True
        while changed:
            changed = False
            for event in scoped:
                if (
                    event.parent_action_id in selected
                    and event.action_id not in selected
                ):
                    selected.add(event.action_id)
                    changed = True
                if (
                    any(item.parent_action_id == event.action_id for item in values)
                    and event.action_id in selected
                ):
                    continue
            for event in scoped:
                if event.action_id in selected and event.parent_action_id:
                    if event.parent_action_id not in selected:
                        selected.add(event.parent_action_id)
                        changed = True
        direct = [event for event in scoped if event.action_id in selected]
    direct.sort(key=lambda event: (event.global_seq is None, event.global_seq or 0))
    return direct[: query.limit]


def _matches(event: TraceEventV1, query: TraceQuery) -> bool:
    checks = (
        (query.ticket_id, event.ticket_id),
        (query.trace_id, event.trace_id),
        (
            query.invocation_id,
            str(event.invocation_id) if event.invocation_id else None,
        ),
        (query.action_id, event.action_id),
        (query.parent_action_id, event.parent_action_id),
        (query.action_type, event.action.type.value),
        (query.lifecycle_state, event.lifecycle.state.value),
        (query.outcome, event.outcome.value if event.outcome else None),
        (query.producer_component, event.producer.component),
    )
    if any(expected is not None and expected != actual for expected, actual in checks):
        return False
    if query.since and event.occurred_at < query.since:
        return False
    if query.until and event.occurred_at > query.until:
        return False
    return True


def export_events(events: Iterable[TraceEventV1], format: str = "json") -> str:
    """Serialize events as JSON, newline-delimited JSON, or a compact CSV."""
    rows = [event.model_dump(mode="json") for event in events]
    if format == "json":
        return json.dumps(rows, indent=2)
    if format == "jsonl":
        return "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows)
    if format == "csv":
        output = io.StringIO()
        fields = [
            "global_seq",
            "ticket_id",
            "trace_id",
            "action_id",
            "parent_action_id",
            "action_type",
            "lifecycle_state",
            "outcome",
            "occurred_at",
        ]
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "global_seq": row.get("global_seq"),
                    "ticket_id": row.get("ticket_id"),
                    "trace_id": row.get("trace_id"),
                    "action_id": row.get("action_id"),
                    "parent_action_id": row.get("parent_action_id"),
                    "action_type": row.get("action", {}).get("type"),
                    "lifecycle_state": row.get("lifecycle", {}).get("state"),
                    "outcome": row.get("outcome"),
                    "occurred_at": row.get("occurred_at"),
                }
            )
        return output.getvalue()
    raise ValueError("format must be json, jsonl, or csv")
