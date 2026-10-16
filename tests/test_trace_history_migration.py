"""Tests for closed ticket trace migration and history storage."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

from providers.events import EventBus
from providers.tracing import (
    ActionDescriptor,
    ActionType,
    LifecycleDescriptor,
    LifecycleState,
    TraceEventV1,
)
from state_store.api.events import get_usage
from state_store.audit import AuditLog
from state_store.models import CreateTicketRequest, TicketStatus, TransitionRequest
from state_store.store import TicketStore
from state_store.trace_store import TraceStore


def make_event(ticket_id: str, **updates: object) -> TraceEventV1:
    values = {
        "ticket_id": ticket_id,
        "action": ActionDescriptor(type=ActionType.STATE),
        "lifecycle": LifecycleDescriptor(state=LifecycleState.STARTED),
    }
    values.update(updates)
    return TraceEventV1(**values)


def test_migrate_ticket_traces_moves_events(tmp_path: Path) -> None:
    db_path = tmp_path / "trace.db"
    history_db_path = tmp_path / "trace-history.db"

    with TraceStore(db_path, history_db_path=history_db_path) as store:
        e1 = store.insert_event(make_event("PERF-1"))
        e2 = store.insert_event(make_event("PERF-1"))
        e3 = store.insert_event(make_event("PERF-2"))

        assert store.count_events("PERF-1") == 2
        assert store.count_events("PERF-2") == 1

        # Check active db before migration
        active_p1 = (
            store._open_connection()
            .execute("SELECT COUNT(*) FROM trace_events WHERE ticket_id = 'PERF-1'")
            .fetchone()[0]
        )
        assert active_p1 == 2

        # Migrate PERF-1
        migrated = store.migrate_ticket_traces("PERF-1")
        assert migrated == 2

        # Check active db after migration: PERF-1 deleted, PERF-2 remains
        active_p1_after = (
            store._open_connection()
            .execute("SELECT COUNT(*) FROM trace_events WHERE ticket_id = 'PERF-1'")
            .fetchone()[0]
        )
        assert active_p1_after == 0
        active_p2_after = (
            store._open_connection()
            .execute("SELECT COUNT(*) FROM trace_events WHERE ticket_id = 'PERF-2'")
            .fetchone()[0]
        )
        assert active_p2_after == 1

        # Check history db: PERF-1 exists in history
        history_p1 = (
            store._open_history_connection()
            .execute("SELECT COUNT(*) FROM trace_events WHERE ticket_id = 'PERF-1'")
            .fetchone()[0]
        )
        assert history_p1 == 2

        # Transparent count_events
        assert store.count_events("PERF-1") == 2
        assert store.count_events("PERF-2") == 1

        # Transparent list_events for single ticket
        p1_events = store.list_events("PERF-1")
        assert len(p1_events) == 2
        assert [e.global_seq for e in p1_events] == [e1.global_seq, e2.global_seq]

        # Transparent list_events across multiple tickets
        multi_events = store.list_events(ticket_ids=["PERF-1", "PERF-2"])
        assert len(multi_events) == 3
        assert [e.global_seq for e in multi_events] == [
            e1.global_seq,
            e2.global_seq,
            e3.global_seq,
        ]


def test_history_readers_deduplicate_interrupted_migration(tmp_path: Path) -> None:
    db_path = tmp_path / "trace.db"
    history_db_path = tmp_path / "trace-history.db"

    with TraceStore(db_path, history_db_path=history_db_path) as store:
        event = store.insert_event(make_event("PERF-3"))
        assert store.migrate_ticket_traces("PERF-3") == 1

        history_row = (
            store._open_history_connection()
            .execute(
                "SELECT * FROM trace_events WHERE event_id = ?",
                (str(event.event_id),),
            )
            .fetchone()
        )
        store._open_connection().execute(
            "INSERT INTO trace_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            tuple(history_row),
        )

        assert store.count_events("PERF-3") == 1
        assert [item.event_id for item in store.list_events("PERF-3")] == [
            event.event_id
        ]


def test_migration_snapshot_does_not_drop_concurrent_insert(tmp_path: Path) -> None:
    db_path = tmp_path / "trace.db"
    history_db_path = tmp_path / "trace-history.db"
    store = TraceStore(db_path, history_db_path=history_db_path)
    writer = TraceStore(db_path, history_db_path=history_db_path)
    ticket_id = "PERF-4"
    initial_event = store.insert_event(make_event(ticket_id))
    concurrent_event = make_event(ticket_id)

    snapshot_started = threading.Event()
    continue_migration = threading.Event()
    writer_started = threading.Event()
    writer_finished = threading.Event()
    active_connection = store._open_connection()

    class PausingConnection:
        def execute(self, sql: str, parameters: tuple[str, ...] = ()):
            cursor = active_connection.execute(sql, parameters)
            if sql.startswith("SELECT event_id, content_hash"):
                snapshot_started.set()
                if not continue_migration.wait(timeout=5):
                    raise TimeoutError("migration snapshot was not released")
            return cursor

        def __getattr__(self, name: str):
            return getattr(active_connection, name)

    def insert_concurrent_event() -> None:
        writer_started.set()
        try:
            writer.insert_event(concurrent_event)
        finally:
            writer_finished.set()

    try:
        with patch.object(
            store,
            "_open_connection",
            return_value=PausingConnection(),
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                migration = executor.submit(store.migrate_ticket_traces, ticket_id)
                assert snapshot_started.wait(timeout=5)
                insert = executor.submit(insert_concurrent_event)
                assert writer_started.wait(timeout=5)
                try:
                    assert not writer_finished.wait(timeout=1)
                finally:
                    continue_migration.set()

                assert migration.result(timeout=5) == 1
                insert.result(timeout=5)

        assert store.count_events(ticket_id) == 2
        assert {event.event_id for event in store.list_events(ticket_id)} == {
            initial_event.event_id,
            concurrent_event.event_id,
        }
    finally:
        continue_migration.set()
        writer.close()
        store.close()


def test_transition_ticket_to_closed_triggers_migration(tmp_path: Path) -> None:
    db_path = tmp_path / "trace.db"
    history_db_path = tmp_path / "trace-history.db"
    ticket_dir = tmp_path / "tickets"

    trace_store = TraceStore(db_path, history_db_path=history_db_path)
    event_bus = EventBus(log_dir=tmp_path / "logs", trace_store=trace_store)
    audit = AuditLog(path=tmp_path / "audit.jsonl", trace_store=trace_store)
    store = TicketStore(
        persist_dir=ticket_dir,
        event_bus=event_bus,
        audit_log=audit,
        trace_store=trace_store,
    )

    try:
        # Create a ticket in store
        ticket = store.create_ticket(
            CreateTicketRequest(summary="Test Ticket", description="Test Description")
        )
        tid = ticket.id

        # Advance ticket to awaiting_teardown
        for status in [
            "triage_pending",
            "awaiting_hardware",
            "awaiting_provision",
            "executing_benchmark",
            "awaiting_teardown",
        ]:
            store.transition_ticket(tid, TransitionRequest(status=status))

        # Emit an event on event bus
        event_bus.emit(tid, "agent", "tool_called", {"tool": "bash"})

        # Verify event in active db
        active_count = (
            trace_store._open_connection()
            .execute(
                "SELECT COUNT(*) FROM trace_events WHERE ticket_id = ?",
                (tid,),
            )
            .fetchone()[0]
        )
        assert active_count > 0

        # Close ticket via transition_ticket
        store.transition_ticket(
            tid,
            TransitionRequest(status=TicketStatus.CLOSED, comment="done"),
            triggered_by="operator",
        )

        # Traces should now be migrated out of active db
        active_count_after = (
            trace_store._open_connection()
            .execute(
                "SELECT COUNT(*) FROM trace_events WHERE ticket_id = ?",
                (tid,),
            )
            .fetchone()[0]
        )
        assert active_count_after == 0

        # And into history db
        history_count = (
            trace_store._open_history_connection()
            .execute(
                "SELECT COUNT(*) FROM trace_events WHERE ticket_id = ?",
                (tid,),
            )
            .fetchone()[0]
        )
        assert history_count > 0

        # Transparent reading works via event_bus and trace_store
        events = event_bus.get_events(tid)
        assert len(events) > 0
        event_types = [e["event_type"] for e in events]
        assert "status_change" in event_types

        # Audit read works
        audit_entries = audit.read(ticket_id=tid)
        assert len(audit_entries) > 0

    finally:
        event_bus.close()
        audit.close()
        trace_store.close()


def test_force_close_triggers_migration(tmp_path: Path) -> None:
    db_path = tmp_path / "trace.db"
    history_db_path = tmp_path / "trace-history.db"
    ticket_dir = tmp_path / "tickets"

    trace_store = TraceStore(db_path, history_db_path=history_db_path)
    store = TicketStore(
        persist_dir=ticket_dir,
        trace_store=trace_store,
    )

    try:
        ticket = store.create_ticket(
            CreateTicketRequest(
                summary="Test Ticket 20", description="Test Description 20"
            )
        )
        tid = ticket.id
        trace_store.insert_event(make_event(tid))

        assert (
            trace_store._open_connection()
            .execute(
                "SELECT COUNT(*) FROM trace_events WHERE ticket_id = ?",
                (tid,),
            )
            .fetchone()[0]
            > 0
        )

        # Force close
        store.force_close(tid, comment="forced")

        # Events migrated
        active_count = (
            trace_store._open_connection()
            .execute(
                "SELECT COUNT(*) FROM trace_events WHERE ticket_id = ?",
                (tid,),
            )
            .fetchone()[0]
        )
        assert active_count == 0

        history_count = (
            trace_store._open_history_connection()
            .execute(
                "SELECT COUNT(*) FROM trace_events WHERE ticket_id = ?",
                (tid,),
            )
            .fetchone()[0]
        )
        assert history_count > 0

        assert trace_store.count_events(tid) == history_count
        assert len(trace_store.list_events(tid)) == history_count

    finally:
        trace_store.close()


def test_ticket_usage_includes_late_events_after_trace_migration(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "trace.db"
    history_db_path = tmp_path / "trace-history.db"
    trace_store = TraceStore(db_path, history_db_path=history_db_path)
    event_bus = EventBus(log_dir=tmp_path / "logs", trace_store=trace_store)
    store = TicketStore(
        persist_dir=tmp_path / "tickets",
        event_bus=event_bus,
        trace_store=trace_store,
    )

    try:
        ticket = store.create_ticket(
            CreateTicketRequest(summary="pipeline usage", description="d")
        )
        event_bus.emit(
            ticket.id,
            "triage-agent",
            "llm_usage",
            {
                "input_tokens": 120,
                "output_tokens": 30,
                "model": "claude-sonnet-4-6",
            },
        )
        store.force_close(ticket.id)

        event_bus.emit(
            ticket.id,
            "introspection-agent",
            "llm_usage",
            {
                "input_tokens": 12,
                "output_tokens": 3,
                "model": "claude-haiku-4-5",
            },
        )

        request = MagicMock()
        request.app.state.event_bus = event_bus
        result = get_usage(ticket.id, request)

        assert result["usage"]["total_tokens"] == 165
        assert set(result["by_agent"]) == {"triage-agent", "introspection-agent"}
        assert result["by_agent"]["triage-agent"]["total_tokens"] == 150
        assert result["by_agent"]["introspection-agent"]["total_tokens"] == 15
    finally:
        event_bus.close()
        trace_store.close()


def test_backfill_script(tmp_path: Path) -> None:
    import subprocess
    import sys

    ticket_dir = tmp_path / "tickets"
    ticket_dir.mkdir()
    db_path = tmp_path / "trace.db"
    history_db_path = tmp_path / "trace-history.db"

    # Create a closed ticket and an open ticket file
    closed_ticket = {
        "id": "PERF-30",
        "summary": "Closed Ticket",
        "status": "closed",
        "status_trail": ["open", "closed"],
    }
    (ticket_dir / "PERF-30.json").write_text(
        json.dumps(closed_ticket), encoding="utf-8"
    )

    open_ticket = {
        "id": "PERF-31",
        "summary": "Open Ticket",
        "status": "open",
        "status_trail": ["open"],
    }
    (ticket_dir / "PERF-31.json").write_text(json.dumps(open_ticket), encoding="utf-8")

    with TraceStore(db_path, history_db_path=history_db_path) as store:
        store.insert_event(make_event("PERF-30"))
        store.insert_event(make_event("PERF-31"))

    script_path = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "migrate-closed-ticket-traces.py"
    )
    # Run dry-run
    cmd_dry = [
        sys.executable,
        str(script_path),
        "--ticket-dir",
        str(ticket_dir),
        "--trace-db",
        str(db_path),
        "--history-db",
        str(history_db_path),
    ]
    res_dry = subprocess.run(cmd_dry, capture_output=True, text=True, check=True)
    assert (
        "Dry run completed" in res_dry.stdout or "Dry run completed" in res_dry.stderr
    )

    # Active DB still has events for both
    with TraceStore(db_path, history_db_path=history_db_path) as store:
        assert (
            store._open_connection()
            .execute("SELECT COUNT(*) FROM trace_events WHERE ticket_id = 'PERF-30'")
            .fetchone()[0]
            == 1
        )

    # Run with --apply and --vacuum
    cmd_apply = cmd_dry + ["--apply", "--vacuum"]
    res_apply = subprocess.run(cmd_apply, capture_output=True, text=True, check=True)
    assert res_apply.returncode == 0

    # Active DB now has 0 for PERF-30 and 1 for PERF-31
    with TraceStore(db_path, history_db_path=history_db_path) as store:
        assert (
            store._open_connection()
            .execute("SELECT COUNT(*) FROM trace_events WHERE ticket_id = 'PERF-30'")
            .fetchone()[0]
            == 0
        )
        assert (
            store._open_connection()
            .execute("SELECT COUNT(*) FROM trace_events WHERE ticket_id = 'PERF-31'")
            .fetchone()[0]
            == 1
        )
        assert store.count_events("PERF-30") == 1
