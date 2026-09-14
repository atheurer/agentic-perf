"""Read-only filtering, causal expansion, and export of trace events."""

from __future__ import annotations

import base64
import binascii
import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
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
    retry_kind: str | None = None
    idempotency_outcome: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    causal: bool = False
    limit: int = 1000
    cursor: int = 0


def _ordering_key(event: TraceEventV1) -> tuple[int, int | str, str]:
    """Return an immutable ordering key, including a legacy-event fallback."""
    if event.global_seq is not None:
        return (0, event.global_seq, str(event.event_id))
    occurred = event.occurred_at.astimezone(timezone.utc).isoformat()
    return (1, occurred, str(event.event_id))


def encode_cursor(event: TraceEventV1) -> str:
    """Encode the last ordering key as an opaque continuation cursor."""
    key = _ordering_key(event)
    raw = json.dumps(key, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str | None) -> tuple[int, int | str, str] | None:
    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if not isinstance(value, list) or len(value) != 3:
            raise ValueError
        if value[0] not in (0, 1) or not isinstance(value[2], str):
            raise ValueError
        if value[0] == 0 and (
            isinstance(value[1], bool) or not isinstance(value[1], int)
        ):
            raise ValueError
        if value[0] == 1 and not isinstance(value[1], str):
            raise ValueError
        return (value[0], value[1], value[2])
    except (
        ValueError,
        TypeError,
        json.JSONDecodeError,
        UnicodeError,
        binascii.Error,
    ) as exc:
        raise ValueError("invalid trace cursor") from exc


def page_events(
    events: Iterable[TraceEventV1], *, cursor: str | None, limit: int
) -> tuple[list[TraceEventV1], bool, str | None]:
    """Page a complete ordered scope without ordinal insert races."""
    ordered = sorted(events, key=_ordering_key)
    marker = decode_cursor(cursor)
    if marker is not None:
        ordered = [event for event in ordered if _ordering_key(event) > marker]
    selected = ordered[:limit]
    has_more = len(ordered) > len(selected)
    return (
        selected,
        has_more,
        encode_cursor(selected[-1]) if has_more and selected else None,
    )


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
        scope_traces = {event.trace_id for event in direct}
        scoped = [
            event
            for event in values
            if event.ticket_id in scope_tickets and event.trace_id in scope_traces
        ]
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
    if query.cursor:
        direct = [event for event in direct if (event.global_seq or 0) > query.cursor]
    return direct[: query.limit]


def diagnostics(events: Iterable[TraceEventV1]) -> dict[str, object]:
    """Report incomplete causal data without inventing relationships."""
    values = list(events)
    ids = {event.action_id for event in values}
    missing = sorted(
        {
            event.parent_action_id
            for event in values
            if event.parent_action_id and event.parent_action_id not in ids
        }
    )
    cycles: list[str] = []
    for event in values:
        seen: set[str] = set()
        current = event
        while current.parent_action_id:
            if current.action_id in seen:
                cycles.append(event.action_id)
                break
            seen.add(current.action_id)
            parent = next(
                (item for item in values if item.action_id == current.parent_action_id),
                None,
            )
            if parent is None:
                break
            current = parent
    seqs = sorted(item.ticket_seq for item in values if item.ticket_seq is not None)
    gaps = [
        number
        for left, right in zip(seqs, seqs[1:])
        for number in range(left + 1, right)
    ]
    return {
        "missing_parents": missing,
        "cycles": sorted(set(cycles)),
        "sequence_gaps": gaps,
        "unmatched_lifecycle_pairs": _unmatched_pairs(values),
        "indeterminate_operations": sorted(
            event.action_id
            for event in values
            if event.action.type.value == "operation"
            and event.outcome is not None
            and event.outcome.value == "indeterminate"
        ),
    }


def _unmatched_pairs(values: list[TraceEventV1]) -> list[str]:
    starts = {
        event.action_id
        for event in values
        if event.lifecycle.state.value in {"started", "requested", "claimed"}
    }
    terminals = {
        event.action_id
        for event in values
        if event.lifecycle.state.value
        in {"completed", "failed", "cancelled", "aborted", "rejected", "indeterminate"}
    }
    return sorted(starts - terminals)


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
        (query.retry_kind, event.lifecycle.retry_kind.value),
        (query.idempotency_outcome, event.idempotency.outcome.value),
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


def export_manifest(
    events: Iterable[TraceEventV1],
    content: str,
    *,
    verified_blob_refs: Iterable[str] | None = None,
) -> dict[str, object]:
    """Return integrity metadata for an export without including payload bytes."""
    values = list(events)
    sequences = [event.global_seq for event in values if event.global_seq is not None]
    # A descriptor digest may be HMAC'd metadata and is not an address for a
    # stored blob.  Exports must only claim references that can be resolved by
    # the payload endpoint.
    candidates = (
        verified_blob_refs
        if verified_blob_refs is not None
        else (
            descriptor.blob_ref
            for event in values
            for descriptor in (event.input, event.output)
            if descriptor and descriptor.blob_ref
        )
    )
    digests = sorted(
        {ref for ref in candidates if re.fullmatch(r"sha256:[0-9a-f]{64}", ref)}
    )
    return {
        "manifest_version": "trace-export-v1",
        "schema_versions": sorted({event.schema_version for event in values}),
        "first_seq": min(sequences) if sequences else None,
        "last_seq": max(sequences) if sequences else None,
        "count": len(values),
        "blob_digests": digests,
        "content_digest_algorithm": "sha256-utf8",
        "content_digest": hashlib.sha256(content.encode()).hexdigest(),
        # Kept as an alias for consumers of the initial PR contract.
        "event_content_digest": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "digest_scope": "canonical event body excluding manifest",
    }
