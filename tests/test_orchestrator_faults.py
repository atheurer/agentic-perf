"""Multiprocess proofs for leader fencing and benchmark idempotency (#801)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
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
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from tests.integration.orchestration_faults import lease_worker; lease_worker()",
        ],
        cwd=Path(__file__).parents[1],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


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
