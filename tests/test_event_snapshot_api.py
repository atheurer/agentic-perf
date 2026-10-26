"""Regression tests for cursor-safe activity and snapshot event pages."""

from __future__ import annotations

from types import SimpleNamespace

from state_store.api.events import (
    get_events as get_ticket_events,
)
from state_store.api.events import (
    get_ticket_snapshot,
)


def _activity_event(seq: int) -> dict:
    return {"seq": seq, "event_source": "activity", "event_type": "agent_started"}


def _successful_audit_event(seq: int) -> dict:
    return {
        "seq": seq,
        "event_source": "audit",
        "audit": {"lifecycle_state": "completed", "outcome": "success"},
    }


class _MemoryEventBus:
    def __init__(self, events: list[dict]) -> None:
        self.events = events

    def get_events(self, ticket_id: str, *, since: int, limit: int) -> list[dict]:
        del ticket_id
        return [event for event in self.events if event["seq"] > since][:limit]

    def get_events_before(
        self, ticket_id: str, *, before: int, limit: int
    ) -> list[dict]:
        del ticket_id
        return [event for event in self.events if event["seq"] < before][-limit:]

    def get_terminal_events(self, ticket_id: str) -> list[dict]:
        del ticket_id
        return []

    def get_event_count(self, ticket_id: str) -> int:
        del ticket_id
        return len(self.events)

    def get_latest_seq(self, ticket_id: str) -> int:
        del ticket_id
        return max((event["seq"] for event in self.events), default=0)

    def get_usage_events(self, ticket_id: str) -> list[dict]:
        del ticket_id
        return []


def _request(event_bus: _MemoryEventBus) -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(event_bus=event_bus))
    )


def test_activity_forward_page_scans_hidden_records_and_advances_safe_cursor() -> None:
    bus = _MemoryEventBus(
        [
            _successful_audit_event(1),
            _successful_audit_event(2),
            _successful_audit_event(3),
            _successful_audit_event(4),
            _activity_event(5),
            _successful_audit_event(6),
            _activity_event(7),
            _successful_audit_event(8),
            _successful_audit_event(9),
            _successful_audit_event(10),
            _successful_audit_event(11),
        ]
    )

    first = get_ticket_events(
        "PERF-1", _request(bus), since=0, limit=2, view="activity", before=None
    )
    assert [event["seq"] for event in first["events"]] == [5, 7]
    # The page cursor must stop before records that did not fit.
    assert first["latest_seq"] == 7

    hidden_page = get_ticket_events(
        "PERF-1", _request(bus), since=7, limit=2, view="activity", before=None
    )
    assert hidden_page["events"] == []
    assert hidden_page["latest_seq"] == 11

    bus.events.append(_activity_event(12))

    next_page = get_ticket_events(
        "PERF-1",
        _request(bus),
        since=hidden_page["latest_seq"],
        limit=2,
        view="activity",
        before=None,
    )
    assert [event["seq"] for event in next_page["events"]] == [12]
    assert next_page["latest_seq"] == 12


def test_activity_backward_page_scans_past_audit_only_ranges() -> None:
    events = [_activity_event(1)]
    events.extend(_successful_audit_event(seq) for seq in range(2, 100))
    events.extend([_activity_event(100), _activity_event(101)])
    bus = _MemoryEventBus(events)

    page = get_ticket_events(
        "PERF-2", _request(bus), since=0, limit=2, view="activity", before=101
    )
    assert [event["seq"] for event in page["events"]] == [1, 100]

    previous = get_ticket_events(
        "PERF-2", _request(bus), since=0, limit=2, view="activity", before=100
    )
    assert [event["seq"] for event in previous["events"]] == [1]


def test_audit_view_filters_snapshot_forward_and_backward_pages() -> None:
    bus = _MemoryEventBus(
        [
            _activity_event(1),
            _successful_audit_event(2),
            _activity_event(3),
            _successful_audit_event(4),
            _activity_event(5),
            _activity_event(6),
        ]
    )
    request = _request(bus)

    snapshot = get_ticket_snapshot("PERF-AUDIT", request, recent=2, view="audit")
    assert [event["seq"] for event in snapshot["recent_events"]] == [2, 4]
    assert all(event["event_source"] == "audit" for event in snapshot["recent_events"])

    first = get_ticket_events(
        "PERF-AUDIT", request, since=0, limit=1, view="audit", before=None
    )
    assert [event["seq"] for event in first["events"]] == [2]
    assert first["latest_seq"] == 2

    second = get_ticket_events(
        "PERF-AUDIT", request, since=2, limit=1, view="audit", before=None
    )
    assert [event["seq"] for event in second["events"]] == [4]
    assert second["latest_seq"] == 4

    hidden = get_ticket_events(
        "PERF-AUDIT", request, since=4, limit=1, view="audit", before=None
    )
    assert hidden["events"] == []
    assert hidden["latest_seq"] == 6

    older = get_ticket_events(
        "PERF-AUDIT", request, since=0, limit=1, view="audit", before=5
    )
    assert [event["seq"] for event in older["events"]] == [4]
    all_view = get_ticket_events(
        "PERF-AUDIT", request, since=0, limit=2, view="all", before=None
    )
    assert [event["seq"] for event in all_view["events"]] == [1, 2]
