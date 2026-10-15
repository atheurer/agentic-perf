"""Tests for trace database retention / pruning."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from providers.tracing import (
    ActionDescriptor,
    ActionType,
    LifecycleDescriptor,
    LifecycleState,
    ProducerIdentity,
    TraceEventV1,
)
from state_store.trace_store import TraceStore


def _make_event(
    ticket_id: str = "t1",
    occurred_at: datetime | None = None,
) -> TraceEventV1:
    return TraceEventV1(
        ticket_id=ticket_id,
        action=ActionDescriptor(type=ActionType.OPERATION, target="test"),
        lifecycle=LifecycleDescriptor(state=LifecycleState.STARTED),
        producer=ProducerIdentity(component="test"),
        occurred_at=occurred_at or datetime.now(timezone.utc),
    )


class TestPruneOldTraces:
    """prune_old_traces deletes old records and VACUUMs the database."""

    def test_old_events_are_deleted(self, tmp_path: Path) -> None:
        store = TraceStore(tmp_path / "trace.db", trace_retention_days=0)
        old = datetime.now(timezone.utc) - timedelta(days=30)
        store.insert_event(_make_event(ticket_id="old", occurred_at=old))
        store.insert_event(_make_event(ticket_id="new"))

        deleted = store.prune_old_traces(retention_days=14)

        assert deleted >= 1
        assert store.count_events("old") == 0
        assert store.count_events("new") == 1

    def test_no_deletion_when_nothing_is_old(self, tmp_path: Path) -> None:
        store = TraceStore(tmp_path / "trace.db", trace_retention_days=0)
        store.insert_event(_make_event())

        deleted = store.prune_old_traces(retention_days=14)
        assert deleted == 0

    def test_zero_retention_days_skips(self, tmp_path: Path) -> None:
        store = TraceStore(tmp_path / "trace.db", trace_retention_days=0)
        old = datetime.now(timezone.utc) - timedelta(days=30)
        store.insert_event(_make_event(occurred_at=old))

        deleted = store.prune_old_traces(retention_days=0)
        assert deleted == 0

    def test_startup_prunes_automatically(self, tmp_path: Path) -> None:
        """When trace_retention_days > 0, __init__ prunes old records."""
        db = tmp_path / "trace.db"
        # First: create a store with retention disabled and insert old data
        store = TraceStore(db, trace_retention_days=0)
        old = datetime.now(timezone.utc) - timedelta(days=30)
        store.insert_event(_make_event(ticket_id="old", occurred_at=old))
        store.insert_event(_make_event(ticket_id="new"))
        store.close()

        # Second: reopen with retention enabled — old data should be pruned
        store2 = TraceStore(db, trace_retention_days=14)
        assert store2.count_events("old") == 0
        assert store2.count_events("new") == 1
        store2.close()

    def test_default_retention_is_14_days(self, tmp_path: Path) -> None:
        store = TraceStore(tmp_path / "trace.db")
        assert store._retention_days == 14
        store.close()
