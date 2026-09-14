"""Durable operation fencing and replay contract."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from state_store.trace_store import (
    OperationConflictError,
    OperationLeaseError,
    OperationRecord,
    OperationTransitionError,
    TraceStore,
    TraceStoreWriteError,
)


def test_same_key_replays_and_conflicts_are_audited(tmp_path: Path) -> None:
    with TraceStore(tmp_path / "trace.db") as store:
        first, existed = store.register_or_get(
            OperationRecord("key", "one", "registered")
        )
        assert not existed
        assert (
            store.register_or_get(OperationRecord("key", "one", "registered"))[0]
            == first
        )
        with pytest.raises(OperationConflictError):
            store.register_or_get(OperationRecord("key", "two", "registered"))
        assert (
            store.operation_history("key")[-1]["reason"]
            == "rejected:request_hash_conflict"
        )
        assert (
            store._connection.execute("SELECT count(*) FROM trace_events").fetchone()[0]
            == 2
        )


def test_takeover_only_before_launch_and_stale_writes_audit(tmp_path: Path) -> None:
    with TraceStore(tmp_path / "trace.db") as store:
        one = store.acquire_operation("key", "hash", "one", 1)
        store._connection.execute(
            "UPDATE operations SET lease_expires_at=?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),),
        )
        two = store.acquire_operation("key", "hash", "two", 1)
        assert two.fencing_generation > one.fencing_generation
        with pytest.raises(OperationLeaseError):
            store.mark_prepared("key", "one", one.fencing_generation)
        prepared = store.mark_prepared("key", "two", two.fencing_generation)
        launched = store.mark_side_effect_started(
            "key", "two", prepared.fencing_generation
        )
        store._connection.execute(
            "UPDATE operations SET lease_expires_at=?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),),
        )
        with pytest.raises(OperationTransitionError):
            store.acquire_operation("key", "hash", "three", 1)
        assert any(
            item["reason"] == "rejected:post_launch_takeover"
            for item in store.operation_history("key")
        )
        assert launched.state == "side_effect_started"
        with pytest.raises(OperationLeaseError):
            store.complete("key", "one", one.fencing_generation, {"outcome": "x"})
        assert any(
            item["reason"] == "rejected:stale_fence"
            for item in store.operation_history("key")
        )


def test_transitions_descriptors_and_restart(tmp_path: Path) -> None:
    path = tmp_path / "trace.db"
    with TraceStore(path) as store:
        lease = store.acquire_operation("key", "hash", "owner", 60)
        with pytest.raises(OperationTransitionError):
            store.mark_side_effect_started("key", "owner", lease.fencing_generation)
        lease = store.mark_prepared("key", "owner", lease.fencing_generation)
        lease = store.mark_side_effect_started("key", "owner", lease.fencing_generation)
        done = store.complete(
            "key", "owner", lease.fencing_generation, {"outcome": "succeeded"}
        )
        assert done.result_descriptor == {"outcome": "succeeded"}
        with pytest.raises(OperationTransitionError):
            store.complete("key", "owner", lease.fencing_generation, {"x": "z" * 5000})
    with TraceStore(path) as store:
        assert store.get_operation("key") == done
        assert len(store.operation_history("key")) >= 4


def test_audit_fault_rolls_back_operation_state_and_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TraceStore(tmp_path / "trace.db") as store:
        lease = store.acquire_operation("key", "hash", "owner", 60)
        history = store.operation_history("key")
        events = store._connection.execute(
            "SELECT count(*) FROM trace_events"
        ).fetchone()[0]
        monkeypatch.setattr(
            store,
            "_audit",
            lambda *_: (_ for _ in ()).throw(sqlite3.OperationalError("fault")),
        )
        with pytest.raises(TraceStoreWriteError):
            store.mark_prepared("key", "owner", lease.fencing_generation)
        assert not store._connection.in_transaction
        assert store.get_operation("key") == lease
        assert store.operation_history("key") == history
        assert (
            store._connection.execute("SELECT count(*) FROM trace_events").fetchone()[0]
            == events
        )
        monkeypatch.undo()
        assert (
            store.mark_prepared("key", "owner", lease.fencing_generation).state
            == "prepared"
        )


def test_restart_preserves_operation_state_matrix(tmp_path: Path) -> None:
    path = tmp_path / "trace.db"
    with TraceStore(path) as store:
        store.register_or_get(OperationRecord("registered", "h", "registered"))
        leased = store.acquire_operation("leased", "h", "owner", 60)
        lease = store.acquire_operation("prepared", "h", "owner", 60)
        store.mark_prepared("prepared", "owner", lease.fencing_generation)
        launched = store.acquire_operation("launched", "h", "owner", 60)
        launched = store.mark_prepared("launched", "owner", launched.fencing_generation)
        launched = store.mark_side_effect_started(
            "launched", "owner", launched.fencing_generation
        )
        store.attach_external_id(
            "launched", "owner", launched.fencing_generation, {"id": "safe"}
        )
        reconciled = store.acquire_operation("reconciled", "h", "owner", 60)
        reconciled = store.mark_prepared(
            "reconciled", "owner", reconciled.fencing_generation
        )
        reconciled = store.mark_side_effect_started(
            "reconciled", "owner", reconciled.fencing_generation
        )
        store._connection.execute(
            "UPDATE operations SET lease_expires_at=? WHERE operation_key='reconciled'",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),),
        )
        store.reconcile(
            "reconciled",
            "owner",
            reconciled.fencing_generation,
            {"reason": "unknown"},
        )
        for outcome, method in [
            ("success", store.complete),
            ("failure", store.fail),
            ("rejected", store.reject),
            ("indeterminate", store.mark_indeterminate),
        ]:
            item = store.acquire_operation(f"terminal-{outcome}", "h", "owner", 60)
            item = store.mark_prepared(
                item.operation_key, "owner", item.fencing_generation
            )
            method(
                item.operation_key,
                "owner",
                item.fencing_generation,
                {"outcome": outcome},
            )
    with TraceStore(path) as store:
        assert store.get_operation("registered").state == "registered"
        restored_lease = store.get_operation("leased")
        assert restored_lease.state == "lease_acquired"
        assert restored_lease.fencing_generation == leased.fencing_generation
        assert restored_lease.lease_expires_at == leased.lease_expires_at
        assert store.get_operation("prepared").state == "prepared"
        assert store.get_operation("launched").external_ids == {"id": "safe"}
        assert store.get_operation("reconciled").terminal_outcome == "indeterminate"
        assert store.operation_history("reconciled")[-1]["reason"] == "reconciled"
        for outcome in ("success", "failure", "rejected", "indeterminate"):
            assert (
                store.get_operation(f"terminal-{outcome}").terminal_outcome == outcome
            )
