from __future__ import annotations

import base64
import json

from providers.tracing import (
    ActionDescriptor,
    ActionType,
    LifecycleDescriptor,
    LifecycleState,
    TraceEventV1,
)
from providers.tracing.models import PayloadDescriptor
from providers.tracing.query import (
    TraceQuery,
    decode_cursor,
    diagnostics,
    export_events,
    export_manifest,
    page_events,
    query_events,
)


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
    content = export_events([event], "jsonl")
    manifest = export_manifest([event], content)
    assert manifest["count"] == 1
    assert len(manifest["event_content_digest"]) == 64


def test_manifest_only_lists_resolvable_blob_references() -> None:
    event = TraceEventV1(
        ticket_id="PERF-1",
        action=ActionDescriptor(type=ActionType.API),
        lifecycle=LifecycleDescriptor(state=LifecycleState.REQUESTED),
        input=PayloadDescriptor(digest="hmac-secret", digest_kind="hmac-sha256"),
        output=PayloadDescriptor(
            digest="sha256-content",
            digest_kind="sha256",
            blob_ref="sha256:" + "a" * 64,
        ),
    )
    manifest = export_manifest([event], export_events([event], "json"))
    assert manifest["blob_digests"] == ["sha256:" + "a" * 64]


def test_causal_diagnostics_are_available_before_page_slicing() -> None:
    root = TraceEventV1(
        ticket_id="PERF-1",
        action=ActionDescriptor(type=ActionType.STATE),
        lifecycle=LifecycleDescriptor(state=LifecycleState.STARTED),
        global_seq=1,
    )
    child = TraceEventV1(
        ticket_id="PERF-1",
        trace_id=root.trace_id,
        parent_action_id=root.action_id,
        action=ActionDescriptor(type=ActionType.STATE),
        lifecycle=LifecycleDescriptor(state=LifecycleState.STARTED),
        global_seq=2,
    )
    selected = query_events(
        [root, child], TraceQuery(action_id=child.action_id, causal=True, limit=1)
    )
    assert len(selected) == 1
    assert diagnostics([root, child])["missing_parents"] == []


def test_query_scope_can_exceed_ten_thousand_events() -> None:
    events = [
        TraceEventV1(
            ticket_id="PERF-large",
            action_id=f"{index + 1:016x}",
            action=ActionDescriptor(type=ActionType.STATE),
            lifecycle=LifecycleDescriptor(state=LifecycleState.STARTED),
        )
        for index in range(10_001)
    ]
    result = query_events(events, TraceQuery(limit=20_000))
    assert len(result) == 10_001
    assert result[-1].global_seq is None


def test_cursor_continuation_survives_interleaved_insert_and_legacy_events() -> None:
    first = TraceEventV1(
        ticket_id="PERF-page",
        global_seq=1,
        action=ActionDescriptor(type=ActionType.STATE),
        lifecycle=LifecycleDescriptor(state=LifecycleState.STARTED),
    )
    second = first.model_copy(update={"global_seq": 2, "action_id": "2" * 16})
    legacy = first.model_copy(update={"global_seq": None, "action_id": "3" * 16})
    page, more, cursor = page_events([first, second], cursor=None, limit=1)
    assert more and cursor
    inserted = first.model_copy(update={"global_seq": 3, "action_id": "4" * 16})
    continuation, _, _ = page_events(
        [first, second, inserted, legacy], cursor=cursor, limit=10
    )
    assert [event.action_id for event in continuation] == [
        second.action_id,
        inserted.action_id,
        legacy.action_id,
    ]


def test_cursor_decoder_rejects_malformed_shapes() -> None:
    malformed = base64.urlsafe_b64encode(json.dumps(["bad", {}, 1]).encode()).decode()
    try:
        decode_cursor(malformed)
    except ValueError:
        pass
    else:
        raise AssertionError("malformed cursor was accepted")
