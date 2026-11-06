"""Tests for durable immutable trace storage."""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import get_context
from pathlib import Path

import pytest

from providers.tracing import (
    ActionDescriptor,
    ActionType,
    LifecycleDescriptor,
    LifecycleState,
    TraceEventV1,
)
from state_store.trace_store import (
    OperationRecord,
    TraceEventConflictError,
    TraceStore,
    TraceStoreWriteError,
)


def event(**updates: object) -> TraceEventV1:
    values = {
        "ticket_id": "PERF-1",
        "action": ActionDescriptor(type=ActionType.STATE),
        "lifecycle": LifecycleDescriptor(state=LifecycleState.STARTED),
    }
    values.update(updates)
    return TraceEventV1(**values)


def _process_insert(path: str) -> tuple[int, int]:
    """Spawn-safe worker used to prove SQLite sequencing across processes."""
    with TraceStore(Path(path)) as store:
        stored = store.insert_event(event())
    return stored.global_seq, stored.ticket_seq


def _cold_start_insert(
    path: str, ready: object, start: object, results: object
) -> None:
    """Open only after peers are ready to force a cold-start initialization race."""
    ready.put(None)
    start.wait()
    try:
        global_seq, ticket_seq = _process_insert(path)
        results.put((global_seq, ticket_seq, None))
    except Exception as exc:  # pragma: no cover - returned to parent for assertion
        results.put((None, None, repr(exc)))


def test_concurrent_writers_have_gap_free_sequences(tmp_path: Path) -> None:
    path = tmp_path / "trace.db"

    def insert(_: int) -> TraceEventV1:
        with TraceStore(path) as store:
            return store.insert_event(event())

    with ThreadPoolExecutor(max_workers=8) as executor:
        stored = list(executor.map(insert, range(24)))
    assert sorted(item.global_seq for item in stored) == list(range(1, 25))
    assert sorted(item.ticket_seq for item in stored) == list(range(1, 25))


def test_shared_store_serializes_concurrent_event_transactions(tmp_path: Path) -> None:
    """Concurrent ASGI handlers share a store instance without nested BEGINs."""
    with TraceStore(tmp_path / "trace.db") as store:
        with ThreadPoolExecutor(max_workers=8) as executor:
            stored = list(
                executor.map(lambda _: store.insert_event(event()), range(24))
            )
    assert sorted(item.global_seq for item in stored) == list(range(1, 25))


def test_shared_store_serializes_reads_with_write_transactions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TraceStore(tmp_path / "trace.db") as store:
        lease = store.acquire_operation("key", "hash", "owner", 60)
        entered = threading.Event()
        release = threading.Event()
        original_audit = store._audit

        def blocking_audit(connection, operation, reason):
            if reason == "prepared":
                entered.set()
                assert release.wait(5)
            return original_audit(connection, operation, reason)

        monkeypatch.setattr(store, "_audit", blocking_audit)
        with ThreadPoolExecutor(max_workers=2) as executor:
            writer = executor.submit(
                store.mark_prepared, "key", "owner", lease.fencing_generation
            )
            assert entered.wait(5)
            reader = executor.submit(store.get_operation, "key")
            assert not reader.done()
            release.set()
            assert writer.result().state == "prepared"
            assert reader.result().state == "prepared"


def test_multiprocess_cold_start_has_gap_free_sequences(tmp_path: Path) -> None:
    path = tmp_path / "trace.db"
    context = get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_cold_start_insert, args=(str(path), ready, start, results)
        )
        for _ in range(8)
    ]
    for process in processes:
        process.start()
    for _ in processes:
        ready.get(timeout=10)
    start.set()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0
    stored = [results.get(timeout=5) for _ in processes]
    assert [failure for _, _, failure in stored if failure] == []
    assert sorted(global_seq for global_seq, _, _ in stored) == list(range(1, 9))
    assert sorted(ticket_seq for _, ticket_seq, _ in stored) == list(range(1, 9))


def test_restart_is_idempotent_and_continues_sequences(tmp_path: Path) -> None:
    path = tmp_path / "trace.db"
    original = event()
    with TraceStore(path) as store:
        first = store.insert_event(original)
    with TraceStore(path) as store:
        assert store.insert_event(original) == first
        second = store.insert_event(event())
    assert second.global_seq == second.ticket_seq == 2


def test_conflicting_event_id_is_rejected(tmp_path: Path) -> None:
    with TraceStore(tmp_path / "trace.db") as store:
        original = event()
        store.insert_event(original)
        changed = original.model_copy(update={"agent_id": "different"})
        with pytest.raises(TraceEventConflictError):
            store.insert_event(changed)


def test_failed_insert_rolls_back_sequence_allocation(tmp_path: Path) -> None:
    with TraceStore(tmp_path / "trace.db") as store:

        def deny_event_insert(action: int, arg1: str, *_: object) -> int:
            if action == sqlite3.SQLITE_INSERT and arg1 == "trace_events":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        store._connection.set_authorizer(deny_event_insert)
        with pytest.raises(TraceStoreWriteError):
            store.insert_event(event())
        store._connection.set_authorizer(None)
        assert store.insert_event(event()).global_seq == 1


def test_busy_database_is_an_explicit_write_error(tmp_path: Path) -> None:
    path = tmp_path / "trace.db"
    with TraceStore(path):
        blocked = TraceStore(path, busy_timeout_ms=1, trace_retention_days=0)
        blocker = sqlite3.connect(path, isolation_level=None)
        try:
            blocker.execute("BEGIN EXCLUSIVE")
            with pytest.raises(TraceStoreWriteError):
                blocked.insert_event(event())
        finally:
            blocker.rollback()
            blocker.close()
            blocked.close()


def test_read_only_database_write_is_explicit(tmp_path: Path) -> None:
    with TraceStore(tmp_path / "trace.db") as store:
        store._connection.execute("PRAGMA query_only = ON")
        with pytest.raises(TraceStoreWriteError):
            store.insert_event(event())


def test_unserializable_event_content_is_explicit(tmp_path: Path) -> None:
    with TraceStore(tmp_path / "trace.db") as store:
        with pytest.raises(TraceStoreWriteError):
            store.insert_event(event(attributes={"not_json": object()}))


def test_operation_crud_preserves_empty_descriptors(tmp_path: Path) -> None:
    operation = OperationRecord(
        operation_key="operation-1",
        request_hash="hash-1",
        state="registered",
        result_descriptor={},
        external_ids={},
    )
    with TraceStore(tmp_path / "trace.db") as store:
        assert store.create_operation(operation) == operation
        assert store.get_operation(operation.operation_key) == operation
        updated = OperationRecord(**{**operation.__dict__, "state": "prepared"})
        assert store.update_operation(updated) == updated
        assert store.get_operation(operation.operation_key) == updated
        store.delete_operation(operation.operation_key)
        assert store.get_operation(operation.operation_key) is None


def test_closed_store_operation_methods_raise_typed_error(tmp_path: Path) -> None:
    store = TraceStore(tmp_path / "trace.db")
    store.close()
    operation = OperationRecord("key", "hash", "registered")
    with pytest.raises(TraceStoreWriteError):
        store.create_operation(operation)
    with pytest.raises(TraceStoreWriteError):
        store.update_operation(operation)
    with pytest.raises(TraceStoreWriteError):
        store.get_operation("key")
    with pytest.raises(TraceStoreWriteError):
        store.delete_operation("key")


def test_startup_configuration_and_close(tmp_path: Path) -> None:
    store = TraceStore(tmp_path / "trace.db", busy_timeout_ms=123)
    assert store._connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert store._connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert store._connection.execute("PRAGMA busy_timeout").fetchone()[0] == 123
    store.close()
    assert store._connection is None


def test_latest_event_time_returns_none_for_empty_store(tmp_path: Path) -> None:
    path = tmp_path / "trace.db"
    with TraceStore(path) as store:
        assert store.latest_event_time("PERF-NONE") is None


def test_latest_event_time_returns_timestamp_after_insert(tmp_path: Path) -> None:
    path = tmp_path / "trace.db"
    with TraceStore(path) as store:
        store.insert_event(event(ticket_id="PERF-T1"))
        ts = store.latest_event_time("PERF-T1")
        assert ts is not None
        assert isinstance(ts, float)
        # Should be recent (within last 5 seconds)
        import time

        assert abs(time.time() - ts) < 5.0


def test_latest_event_time_tracks_most_recent(tmp_path: Path) -> None:
    path = tmp_path / "trace.db"
    with TraceStore(path) as store:
        store.insert_event(event(ticket_id="PERF-T2"))
        ts1 = store.latest_event_time("PERF-T2")

        import time

        time.sleep(0.05)
        store.insert_event(event(ticket_id="PERF-T2"))
        ts2 = store.latest_event_time("PERF-T2")

        assert ts2 is not None
        assert ts1 is not None
        assert ts2 >= ts1


def test_latest_event_time_scoped_to_ticket(tmp_path: Path) -> None:
    path = tmp_path / "trace.db"
    with TraceStore(path) as store:
        store.insert_event(event(ticket_id="PERF-A"))
        store.insert_event(event(ticket_id="PERF-B"))
        assert store.latest_event_time("PERF-A") is not None
        assert store.latest_event_time("PERF-B") is not None
        assert store.latest_event_time("PERF-C") is None


def test_latest_event_time_skips_excluded_state_targets(tmp_path: Path) -> None:
    """Timer-driven store bookkeeping must not count as activity."""
    import time
    from datetime import datetime, timedelta, timezone

    path = tmp_path / "trace.db"
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    with TraceStore(path) as store:
        store.insert_event(
            event(
                ticket_id="PERF-T3",
                occurred_at=old,
                action=ActionDescriptor(type=ActionType.STATE, target="add_comment"),
            )
        )
        store.insert_event(
            event(
                ticket_id="PERF-T3",
                action=ActionDescriptor(type=ActionType.STATE, target="renew_claim"),
            )
        )

        unfiltered = store.latest_event_time("PERF-T3")
        filtered = store.latest_event_time(
            "PERF-T3", exclude_state_targets=("renew_claim",)
        )

    assert unfiltered is not None
    assert filtered is not None
    assert abs(time.time() - unfiltered) < 5.0
    assert abs(filtered - old.timestamp()) < 1.0
