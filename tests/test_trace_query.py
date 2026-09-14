from __future__ import annotations

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
    diagnostics,
    export_events,
    export_manifest,
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
    assert len(manifest["export_digest"]) == 64


def test_manifest_only_lists_resolvable_blob_references() -> None:
    event = TraceEventV1(
        ticket_id="PERF-1",
        action=ActionDescriptor(type=ActionType.API),
        lifecycle=LifecycleDescriptor(state=LifecycleState.REQUESTED),
        input=PayloadDescriptor(digest="hmac-secret", digest_kind="hmac-sha256"),
        output=PayloadDescriptor(
            digest="sha256-content", digest_kind="sha256", blob_ref="sha256:stored"
        ),
    )
    manifest = export_manifest([event], export_events([event], "json"))
    assert manifest["blob_digests"] == ["sha256:stored"]


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
