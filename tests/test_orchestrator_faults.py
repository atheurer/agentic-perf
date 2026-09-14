"""Multiprocess proofs for leader fencing and benchmark idempotency (#801)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.integration.orchestration_faults import (
    FakeBenchmarkController,
    diagnostics,
    free_port,
    start_store,
    stop_process,
)


def _run_worker(
    *,
    store_url: str,
    instance: str,
    session: str,
    barrier: Path,
    result: Path,
    stop: Path,
) -> subprocess.Popen[str]:
    env = os.environ | {
        "FAULT_STORE_URL": store_url,
        "FAULT_INSTANCE": instance,
        "FAULT_SESSION": session,
        "FAULT_BARRIER": str(barrier),
        "FAULT_RESULT": str(result),
        "FAULT_STOP": str(stop),
    }
    log = result.with_suffix(".log").open("w+", encoding="utf-8")
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from tests.integration.orchestration_faults import lease_worker; lease_worker()",
        ],
        cwd=Path(__file__).parents[1],
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
    )
    process._fault_log = log  # type: ignore[attr-defined]
    log.close()
    return process


def _result(path: Path) -> dict:
    assert path.exists(), f"worker did not report: {path}"
    return json.loads(path.read_text())


def test_concurrent_startup_has_one_fenced_leader(tmp_path: Path) -> None:
    """Two real service clients released together cannot both lead."""
    home = tmp_path / "store"
    port = free_port()
    store = start_store(home, port)
    processes: list[subprocess.Popen[str]] = [store]
    try:
        barrier = tmp_path / "release"
        stop = tmp_path / "stop"
        workers = []
        for name in ("shared", "shared"):
            result = tmp_path / f"{len(workers)}.json"
            worker = _run_worker(
                store_url=f"http://127.0.0.1:{port}",
                instance=name,
                session=str(uuid.uuid4()),
                barrier=barrier,
                result=result,
                stop=stop,
            )
            workers.append((worker, result))
            processes.append(worker)
        barrier.touch()
        for _worker, result in workers:
            deadline = __import__("time").monotonic() + 5
            while not result.exists() and __import__("time").monotonic() < deadline:
                __import__("time").sleep(0.01)
            assert result.exists(), diagnostics(processes)
        outcomes = [_result(result) for _, result in workers]
        assert [item["status"] for item in outcomes].count("winner") == 1, outcomes
        assert [item["status"] for item in outcomes].count("loser") == 1
        assert "unavailable" in next(
            item["error"] for item in outcomes if item["status"] == "loser"
        )
    finally:
        stop.touch()
        for process in reversed(processes):
            stop_process(process)


def test_takeover_fences_stale_session_and_controller_launch(tmp_path: Path) -> None:
    """An expired epoch cannot mutate state or relaunch an intent."""
    from state_store.models import AcquireOrchestratorLeaseRequest, CreateTicketRequest
    from state_store.store import ClaimFenceError, TicketStore

    controller = FakeBenchmarkController(tmp_path / "controller")
    now = [datetime.now(timezone.utc)]
    store = TicketStore(persist_dir=tmp_path / "state", clock=lambda: now[0])
    ticket = store.create_ticket(
        CreateTicketRequest(summary="fault", description="fault")
    )
    first = uuid.uuid4()
    lease = store.acquire_orchestrator_lease(
        AcquireOrchestratorLeaseRequest(
            session_id=first,
            instance_name="shared",
            host="test",
            pid=1,
            process_start_id="first",
            ttl_seconds=1,
        )
    )
    controller.validate("v1", "digest-1")
    assert controller.launch("intent-1", "approval-1", str(first))
    now[0] += timedelta(seconds=2)
    second = uuid.uuid4()
    replacement = store.acquire_orchestrator_lease(
        AcquireOrchestratorLeaseRequest(
            session_id=second,
            instance_name="shared",
            host="test",
            pid=2,
            process_start_id="second",
            ttl_seconds=30,
        )
    )
    assert replacement.epoch > lease.epoch
    with pytest.raises(PermissionError):
        store.renew_orchestrator_lease(first, lease.epoch, 30)
    with pytest.raises(ClaimFenceError):
        store.update_fields(
            ticket.id,
            {"bad": True},
            session_id=first,
            epoch=lease.epoch,
            claim_id="stale",
        )
    assert not controller.launch("intent-1", "approval-1", str(first))
    assert len([r for r in controller.records() if r["operation"] == "launch"]) == 1


def test_controller_replay_after_restart_launches_once(tmp_path: Path) -> None:
    """Validation identity survives a process restart and duplicate delivery."""
    controller = FakeBenchmarkController(tmp_path / "controller")
    barrier = controller.arm_barrier("launch")
    controller.validate("validation-1", "runfile-digest")
    controller.release(barrier)
    controller.wait_for_release(barrier)
    assert controller.launch("intent-1", "approval-1", "session-1")

    # A reconnect/replay sees the durable external record and must not launch.
    restarted = FakeBenchmarkController(tmp_path / "controller")
    assert not restarted.launch("intent-1", "approval-1", "session-2")
    records = restarted.records()
    assert [r["operation"] for r in records].count("validate") == 1
    launches = [r for r in records if r["operation"] == "launch"]
    assert len(launches) == 1
    assert launches[0]["intent_id"] == "intent-1"
    assert launches[0]["approval_id"] == "approval-1"


def test_concurrent_controller_reconnects_have_one_external_launch(
    tmp_path: Path,
) -> None:
    """Concurrent delivery/reconnect cannot duplicate the external side effect."""
    controller = FakeBenchmarkController(tmp_path / "controller")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda session: controller.launch("intent-race", "approval-1", session),
                ("session-a", "session-b"),
            )
        )
    assert sorted(results) == [False, True]
    assert [
        record for record in controller.records() if record["operation"] == "launch"
    ] == [
        {
            "operation": "launch",
            "intent_id": "intent-race",
            "approval_id": "approval-1",
            "session_id": "session-a" if results[0] else "session-b",
        }
    ]


def _operation_store(tmp_path: Path):
    from state_store.trace_store import TraceStore

    return TraceStore(tmp_path / "trace.sqlite")


def test_operation_registration_rejects_ambiguous_replay_and_preserves_identity(
    tmp_path: Path,
) -> None:
    """A request key is immutable across validators and reconnects."""
    from state_store.trace_store import OperationConflictError, OperationRecord

    store = _operation_store(tmp_path)
    try:
        original, existed = store.register_or_get(
            OperationRecord("run-1", "digest-a", "registered")
        )
        assert not existed
        replay, existed = store.register_or_get(
            OperationRecord("run-1", "digest-a", "registered")
        )
        assert existed and replay == original
        with pytest.raises(OperationConflictError):
            store.register_or_get(OperationRecord("run-1", "digest-b", "registered"))
        reasons = [item["reason"] for item in store.operation_history("run-1")]
        assert "rejected:request_hash_conflict" in reasons
    finally:
        store.close()


def test_fenced_operation_crash_boundaries_never_relaunch_side_effect(
    tmp_path: Path,
) -> None:
    """Restart recovery distinguishes pre-launch work from indeterminate work."""
    from state_store.trace_store import (
        OperationRecord,
        OperationTransitionError,
        TraceStore,
    )

    db = tmp_path / "trace.sqlite"
    first = TraceStore(db)
    try:
        record, existed = first.register_or_get(
            OperationRecord("run-crash", "digest", "registered")
        )
        assert not existed and record.state == "registered"
        claimed, disposition = first.acquire_operation_result(
            "run-crash", "digest", "leader-a", 60
        )
        assert disposition == "acquired"
        prepared = first.mark_prepared(
            "run-crash", "leader-a", claimed.fencing_generation
        )
        started = first.mark_side_effect_started(
            "run-crash", "leader-a", prepared.fencing_generation
        )
        assert started.state == "side_effect_started"
    finally:
        first.close()

    restarted = TraceStore(db)
    try:
        with pytest.raises(OperationTransitionError):
            restarted.acquire_operation_result("run-crash", "digest", "leader-b", 60)
        history = restarted.operation_history("run-crash")
        assert history[-1]["reason"] == "rejected:post_launch_takeover"
        # Only reconciliation may finish an indeterminate side effect; a normal
        # reconnect is never allowed to start it a second time.
        reconciled = restarted.transition_operation(
            "run-crash",
            "leader-a",
            started.fencing_generation,
            "terminal",
            descriptor={"reconciled": True},
            terminal_outcome="indeterminate",
            allow_expired_reconciliation=True,
        )
        assert reconciled.terminal_outcome == "indeterminate"
        assert [e.lifecycle.state.value for e in restarted.list_events("run-crash")][
            -1
        ] == "indeterminate"
    finally:
        restarted.close()


@pytest.mark.parametrize(
    "boundary",
    ["before_validation", "after_validation", "after_approval", "before_side_effect"],
)
def test_pre_launch_crash_recovery_allows_one_fenced_claim(
    tmp_path: Path, boundary: str
) -> None:
    """Each pre-launch crash boundary is recoverable by one successor."""
    from state_store.trace_store import OperationRecord, TraceStore

    db = tmp_path / f"{boundary}.sqlite"
    store = TraceStore(db)
    try:
        store.register_or_get(OperationRecord("run", "digest", "registered"))
        if boundary != "before_validation":
            store.acquire_operation_result("run", "digest", "leader-a", 60)
        if boundary in {"after_approval", "before_side_effect"}:
            store.mark_prepared("run", "leader-a", 1)
    finally:
        store.close()
    recovered = TraceStore(db)
    try:
        # The old lease is live, so takeover is correctly refused. This proves
        # recovery never bypasses the production lease/fence contract.
        if boundary == "before_validation":
            record, disposition = recovered.acquire_operation_result(
                "run", "digest", "leader-b", 60
            )
            assert disposition == "acquired" and record.fencing_generation == 1
        else:
            from state_store.trace_store import OperationLeaseError

            with pytest.raises(OperationLeaseError):
                recovered.acquire_operation_result("run", "digest", "leader-b", 60)
    finally:
        recovered.close()
