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
from typing import Callable, Iterable

from .models import TraceEventV1

MAX_SEQUENCE_GAPS = 10_000


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

    def __post_init__(self) -> None:
        """Normalize request timestamps so naive CLI values are UTC-safe."""
        object.__setattr__(self, "since", _as_utc(self.since))
        object.__setattr__(self, "until", _as_utc(self.until))


class TraceQueryTooBroad(ValueError):
    """A trace query would require materializing an unsafe amount of data."""


class TraceQueryCancelled(RuntimeError):
    """The caller abandoned a trace query before it completed."""


class TraceQueryResourceExhausted(RuntimeError):
    """The state store cannot safely admit another trace query right now."""


def _check_cancelled(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise TraceQueryCancelled("trace query cancelled")


def _json_dumps(
    value: object,
    *,
    indent: int | None = None,
    separators: tuple[str, str] | None = None,
    cancelled: Callable[[], bool] | None = None,
    max_bytes: int | None = None,
) -> str:
    encoder = json.JSONEncoder(indent=indent, separators=separators)
    fragments: list[str] = []
    total_bytes = 0
    for fragment in encoder.iterencode(value):
        _check_cancelled(cancelled)
        total_bytes += len(fragment.encode("utf-8"))
        if max_bytes is not None and total_bytes > max_bytes:
            raise TraceQueryTooBroad(
                "trace response is too broad; narrow the query or reduce its limit"
            )
        fragments.append(fragment)
    return "".join(fragments)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return datetime(
            value.year,
            value.month,
            value.day,
            value.hour,
            value.minute,
            value.second,
            value.microsecond,
            tzinfo=timezone.utc,
            fold=value.fold,
        )
    return value.astimezone(timezone.utc)


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
    events: Iterable[TraceEventV1],
    *,
    cursor: str | None,
    limit: int,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[list[TraceEventV1], bool, str | None]:
    """Page a complete ordered scope without ordinal insert races."""
    ordered: list[TraceEventV1] = []
    for event in events:
        _check_cancelled(cancelled)
        ordered.append(event)
    ordered.sort(key=_ordering_key)
    _check_cancelled(cancelled)
    marker = decode_cursor(cursor)
    selected: list[TraceEventV1] = []
    has_more = False
    for event in ordered:
        _check_cancelled(cancelled)
        if marker is not None and _ordering_key(event) <= marker:
            continue
        if len(selected) < limit:
            selected.append(event)
        else:
            has_more = True
            break
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
        scopes = {(event.ticket_id, event.trace_id) for event in direct}
        scoped = [
            event for event in values if (event.ticket_id, event.trace_id) in scopes
        ]
        direct = expand_causal_events(
            direct,
            scoped,
            limit=query.limit,
            cursor=query.cursor,
        )
    direct.sort(key=lambda event: (event.global_seq is None, event.global_seq or 0))
    if query.cursor and not query.causal:
        direct = [event for event in direct if (event.global_seq or 0) > query.cursor]
    return direct[: query.limit]


def expand_causal_events(
    direct: Iterable[TraceEventV1],
    scoped: Iterable[TraceEventV1],
    *,
    limit: int,
    cursor: int = 0,
    cancelled: Callable[[], bool] | None = None,
) -> list[TraceEventV1]:
    """Expand selected actions to their ancestors and descendants in linear time."""
    direct_values = list(direct)
    if not direct_values:
        return []

    parents: dict[str, set[str]] = {}
    children: dict[str, set[str]] = {}
    selected = {event.action_id for event in direct_values}
    scoped_values = list(scoped)
    for event in scoped_values:
        if cancelled is not None and cancelled():
            raise TraceQueryCancelled("trace query cancelled")
        if event.parent_action_id:
            parents.setdefault(event.action_id, set()).add(event.parent_action_id)
            children.setdefault(event.parent_action_id, set()).add(event.action_id)

    pending = list(selected)
    while pending:
        if cancelled is not None and cancelled():
            raise TraceQueryCancelled("trace query cancelled")
        action_id = pending.pop()
        related = parents.get(action_id, set()) | children.get(action_id, set())
        for related_id in related:
            if related_id not in selected:
                selected.add(related_id)
                pending.append(related_id)

    expanded = [event for event in scoped_values if event.action_id in selected]
    if cursor:
        expanded = [event for event in expanded if (event.global_seq or 0) > cursor]
    expanded.sort(key=lambda event: (event.global_seq is None, event.global_seq or 0))
    return expanded[:limit]


def diagnostics(
    events: Iterable[TraceEventV1],
    *,
    cancelled: Callable[[], bool] | None = None,
    max_sequence_gaps: int = MAX_SEQUENCE_GAPS,
) -> dict[str, object]:
    """Report incomplete causal data without inventing relationships."""
    if max_sequence_gaps < 0:
        raise ValueError("max_sequence_gaps must be non-negative")
    values: list[TraceEventV1] = []
    for event in events:
        _check_cancelled(cancelled)
        values.append(event)
    ids: set[str] = set()
    parents: dict[str, str] = {}
    seqs: list[int] = []
    for event in values:
        _check_cancelled(cancelled)
        ids.add(event.action_id)
        if event.parent_action_id:
            parents[event.action_id] = event.parent_action_id
        if event.ticket_seq is not None:
            seqs.append(event.ticket_seq)
    missing = sorted({parent for parent in parents.values() if parent not in ids})
    seqs.sort()
    gaps, gaps_truncated = _sequence_gaps(
        seqs, max_gaps=max_sequence_gaps, cancelled=cancelled
    )
    return {
        "missing_parents": missing,
        "cycles": _causal_cycles(parents, cancelled=cancelled),
        "sequence_gaps": gaps,
        "sequence_gaps_truncated": gaps_truncated,
        "unmatched_lifecycle_pairs": _unmatched_pairs(values, cancelled=cancelled),
        "indeterminate_operations": sorted(
            event.action_id
            for event in values
            if event.action.type.value == "operation"
            and event.outcome is not None
            and event.outcome.value == "indeterminate"
        ),
    }


def _sequence_gaps(
    seqs: list[int],
    *,
    max_gaps: int,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[list[int], bool]:
    gaps: list[int] = []
    for left, right in zip(seqs, seqs[1:]):
        _check_cancelled(cancelled)
        for number in range(left + 1, right):
            if len(gaps) >= max_gaps:
                return gaps, True
            gaps.append(number)
    return gaps, False


def _causal_cycles(
    parents: dict[str, str], *, cancelled: Callable[[], bool] | None = None
) -> list[str]:
    """Return every action in a parent cycle using one pass over the graph."""
    state: dict[str, int] = {}
    cycles: set[str] = set()
    for start in parents:
        _check_cancelled(cancelled)
        if state.get(start) is not None:
            continue
        path: list[str] = []
        positions: dict[str, int] = {}
        current = start
        while current in parents and state.get(current) is None:
            state[current] = 1
            positions[current] = len(path)
            path.append(current)
            current = parents[current]
        if current in positions:
            cycles.update(path[positions[current] :])
        for action_id in path:
            state[action_id] = 2
    return sorted(cycles)


def _unmatched_pairs(
    values: list[TraceEventV1], *, cancelled: Callable[[], bool] | None = None
) -> list[str]:
    starts: set[str] = set()
    terminals: set[str] = set()
    for event in values:
        _check_cancelled(cancelled)
        if event.lifecycle.state.value in {"started", "requested", "claimed"}:
            starts.add(event.action_id)
        if event.lifecycle.state.value in {
            "completed",
            "failed",
            "cancelled",
            "aborted",
            "rejected",
            "indeterminate",
        }:
            terminals.add(event.action_id)
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


def export_events(
    events: Iterable[TraceEventV1],
    format: str = "json",
    *,
    cancelled: Callable[[], bool] | None = None,
    max_bytes: int | None = None,
) -> str:
    """Serialize events as JSON, newline-delimited JSON, or a compact CSV."""
    if format == "json":
        rows: list[dict[str, object]] = []
        for event in events:
            _check_cancelled(cancelled)
            rows.append(event.model_dump(mode="json"))
        return _json_dumps(rows, indent=2, cancelled=cancelled, max_bytes=max_bytes)
    if format == "jsonl":
        fragments: list[str] = []
        total_bytes = 0
        for event in events:
            _check_cancelled(cancelled)
            row = event.model_dump(mode="json")
            remaining = None if max_bytes is None else max_bytes - total_bytes
            encoded = _json_dumps(
                row,
                separators=(",", ":"),
                cancelled=cancelled,
                max_bytes=remaining,
            )
            total_bytes = _append_export_fragment(
                fragments, encoded, total_bytes, max_bytes, cancelled=cancelled
            )
            total_bytes = _append_export_fragment(
                fragments, "\n", total_bytes, max_bytes, cancelled=cancelled
            )
        return "".join(fragments)
    if format == "csv":
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
        fragments: list[str] = []
        header = io.StringIO()
        writer = csv.DictWriter(header, fieldnames=fields)
        writer.writeheader()
        total_bytes = _append_export_fragment(
            fragments, header.getvalue(), 0, max_bytes, cancelled=cancelled
        )
        for event in events:
            _check_cancelled(cancelled)
            row = event.model_dump(mode="json")
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=fields)
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
            total_bytes = _append_export_fragment(
                fragments,
                output.getvalue(),
                total_bytes,
                max_bytes,
                cancelled=cancelled,
            )
        return "".join(fragments)
    raise ValueError("format must be json, jsonl, or csv")


def _append_export_fragment(
    fragments: list[str],
    fragment: str,
    total_bytes: int,
    max_bytes: int | None,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> int:
    _check_cancelled(cancelled)
    next_total = total_bytes + len(fragment.encode("utf-8"))
    if max_bytes is not None and next_total > max_bytes:
        raise TraceQueryTooBroad(
            "trace export is too broad; narrow the query or reduce its limit"
        )
    fragments.append(fragment)
    return next_total


def export_manifest(
    events: Iterable[TraceEventV1],
    content: str,
    *,
    verified_blob_refs: Iterable[str] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, object]:
    """Return integrity metadata for an export without including payload bytes."""
    values: list[TraceEventV1] = []
    sequences: list[int] = []
    schema_versions: set[str] = set()
    for event in events:
        _check_cancelled(cancelled)
        values.append(event)
        if event.global_seq is not None:
            sequences.append(event.global_seq)
        schema_versions.add(event.schema_version)
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
    digests: set[str] = set()
    for ref in candidates:
        _check_cancelled(cancelled)
        if re.fullmatch(r"sha256:[0-9a-f]{64}", ref):
            digests.add(ref)
    return {
        "manifest_version": "trace-export-v1",
        "schema_versions": sorted(schema_versions),
        "first_seq": min(sequences) if sequences else None,
        "last_seq": max(sequences) if sequences else None,
        "count": len(values),
        "blob_digests": sorted(digests),
        "content_digest_algorithm": "sha256-utf8",
        "content_digest": hashlib.sha256(content.encode()).hexdigest(),
        # Kept as an alias for consumers of the initial PR contract.
        "event_content_digest": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "digest_scope": "canonical event body excluding manifest",
    }
