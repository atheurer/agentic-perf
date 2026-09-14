"""Cross-connection operation claim and replay regression tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from state_store.trace_store import (
    OperationLeaseError,
    OperationRecord,
    OperationTransitionError,
    TraceStore,
)


def _claim(path: Path, owner: str):
    try:
        with TraceStore(path) as store:
            return store.acquire_operation("shared", "hash", owner, 60)
    except OperationLeaseError:
        return None


def test_concurrent_first_claim_has_one_fence_owner(tmp_path: Path) -> None:
    path = tmp_path / "trace.db"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda owner: _claim(path, owner), ["one", "two"]))
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert winners[0].fencing_generation == 1


def test_same_owner_concurrent_claim_has_one_acquired(tmp_path: Path) -> None:
    path = tmp_path / "trace.db"

    def claim():
        with TraceStore(path) as store:
            return store.acquire_operation_result("same", "hash", "service", 60)[1]

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _: claim(), range(2)))
    assert statuses.count("acquired") == 1
    assert statuses.count("existing") == 1


def test_registered_and_expired_takeover_are_acquired(tmp_path: Path) -> None:
    with TraceStore(tmp_path / "trace.db") as store:
        store.register_or_get(OperationRecord("key", "hash", "registered"))
        first, status = store.acquire_operation_result("key", "hash", "one", 60)
        assert status == "acquired"
        store._connection.execute(
            "UPDATE operations SET lease_expires_at=?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),),
        )
        second, status = store.acquire_operation_result("key", "hash", "two", 60)
        assert status == "acquired"
        assert second.fencing_generation > first.fencing_generation


def test_duplicate_terminal_is_cached_and_never_reclaimed(tmp_path: Path) -> None:
    with TraceStore(tmp_path / "trace.db") as store:
        lease = store.acquire_operation("key", "hash", "owner", 60)
        lease = store.mark_prepared("key", "owner", lease.fencing_generation)
        lease = store.mark_side_effect_started("key", "owner", lease.fencing_generation)
        done = store.complete("key", "owner", lease.fencing_generation, {"id": "safe"})
        assert store.acquire_operation("key", "hash", "other", 60) == done


def test_expired_launched_operation_reconciles_without_takeover(tmp_path: Path) -> None:
    with TraceStore(tmp_path / "trace.db") as store:
        lease = store.acquire_operation("key", "hash", "owner", 60)
        lease = store.mark_prepared("key", "owner", lease.fencing_generation)
        lease = store.mark_side_effect_started("key", "owner", lease.fencing_generation)
        store._connection.execute(
            "UPDATE operations SET lease_expires_at=?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),),
        )
        with pytest.raises(OperationTransitionError):
            store.acquire_operation("key", "hash", "other", 60)
        done = store.reconcile(
            "key", "owner", lease.fencing_generation, {"reason": "unknown"}
        )
        assert done.terminal_outcome == "indeterminate"
        assert store.operation_history("key")[-1]["reason"] == "reconciled"


def test_expired_launched_operation_can_reconcile_confirmed_success(
    tmp_path: Path,
) -> None:
    with TraceStore(tmp_path / "trace.db") as store:
        lease = store.acquire_operation("key", "hash", "owner", 60)
        lease = store.mark_prepared("key", "owner", lease.fencing_generation)
        lease = store.mark_side_effect_started("key", "owner", lease.fencing_generation)
        store._connection.execute(
            "UPDATE operations SET lease_expires_at=?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),),
        )
        done = store.reconcile(
            "key", "owner", lease.fencing_generation, {"id": "safe"}, "success"
        )
        assert done.terminal_outcome == "success"


def test_stale_mutations_are_rejected_and_audited(tmp_path: Path) -> None:
    with TraceStore(tmp_path / "trace.db") as store:
        lease = store.acquire_operation("key", "hash", "owner", 60)
        with pytest.raises(OperationLeaseError):
            store.renew_operation("key", "other", lease.fencing_generation, 60)
        with pytest.raises(OperationLeaseError):
            store.attach_external_id(
                "key", "other", lease.fencing_generation, {"id": "x"}
            )
        assert (
            len(store._connection.execute("SELECT * FROM trace_events").fetchall()) >= 3
        )
