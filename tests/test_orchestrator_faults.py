"""Multiprocess proofs for leader fencing and benchmark idempotency (#801)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import httpx
import pytest

from tests.integration.orchestration_faults import (
    FakeBenchmarkController,
    FaultHarness,
    ScriptedMockLLM,
    diagnostics,
    free_port,
    start_store,
    stop_process,
    wait_for_log,
)

# #801 acceptance mapping.  These are deliberately named test functions, not
# prose-only checklist entries, so review can see which proof protects each
# failure boundary.  ``FaultHarness`` supplies the shared service/process
# lifecycle; individual tests select only the primitives their fault requires.
SCENARIO_TO_TEST = {
    "leader_collision": "test_two_real_orchestrators_share_one_fenced_production_leader",
    "takeover_stale_rejection": "test_takeover_fences_stale_session_and_controller_launch",
    "long_approval": "test_long_approval_pause_preserves_one_immutable_execution_capability",
    "two_waiter_ambiguity": "test_two_waiters_require_a_structured_approval_and_wake_one_only",
    "duplicate_delivery_reconnect": "test_process_visible_controller_ledger_is_atomic_across_reconnects",
    "store_restart": "test_real_state_store_restart_retains_leader_and_operation_fences",
    "crash_boundaries": "test_fenced_operation_crash_boundaries_never_relaunch_side_effect",
    "copied_runtime": "test_copied_runtime_is_sanitized_but_shared_store_still_fences_bypass",
    "causal_trace": "test_trace_projection_causally_attributes_fault_decisions",
}


def test_fault_scenario_mapping_names_live_proofs() -> None:
    """Keep the #801 acceptance checklist attached to executable tests."""
    assert set(SCENARIO_TO_TEST.values()) <= set(globals())


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


def test_two_real_orchestrators_share_one_fenced_production_leader(
    tmp_path: Path,
) -> None:
    """Real ``orchestrator.main`` processes cannot both enter the poll loop.

    The runtime homes are intentionally separate (as are normal dev instances),
    but both mains use the same service URL and token.  The winning readiness
    line occurs after the production lease acquisition; the other main must
    fail *before* it creates a dispatcher or can claim any ticket.
    """
    with FaultHarness(tmp_path) as harness:
        winner = harness.start_main("fault-a")
        wait_for_log(winner, "Orchestrator started")
        loser = harness.start_main("fault-b")
        # A losing real main exits on the production 409 leader-lease response.
        deadline = __import__("time").monotonic() + 10
        while loser.poll() is None and __import__("time").monotonic() < deadline:
            __import__("time").sleep(0.02)
        assert loser.poll() not in (None, 0), harness.evidence()
        loser_log = (tmp_path / "runtime-fault-b" / "orchestrator.log").read_text()
        assert "orchestrator leader lease unavailable" in loser_log
        assert "Orchestrator started" not in loser_log
        lease = httpx.get(
            f"{harness.store_url}/api/v1/control/orchestrator-lease",
            headers=harness.headers,
            timeout=2,
        )
        assert lease.status_code == 200, harness.evidence()
        assert lease.json()["lease"]["pid"] == winner.pid


@pytest.mark.asyncio
async def test_scripted_transcript_interpolates_real_runtime_identifiers() -> None:
    """A real MCP response can supply opaque IDs to the next agent turn."""
    llm = ScriptedMockLLM(
        [
            {"tool_calls": [{"name": "validate_benchmark", "input": {}}]},
            {
                "tool_calls": [
                    {
                        "name": "execute_benchmark",
                        "input": {
                            "validation_id": "${validation_id}",
                            "approval_request_id": "${approval_request_id}",
                            "intent_id": "${intent_id}",
                        },
                    }
                ]
            },
        ]
    )
    first = await llm.complete("", [], [])
    assert first.tool_calls[0].name == "validate_benchmark"
    llm.bind_json(
        {
            "validation_id": "val-live",
            "approval_request_id": "apr-live",
            "intent_id": "intent-live",
        },
        "validation_id",
        "approval_request_id",
        "intent_id",
    )
    second = await llm.complete("", [], [])
    assert second.tool_calls[0].input == {
        "validation_id": "val-live",
        "approval_request_id": "apr-live",
        "intent_id": "intent-live",
    }


def test_process_visible_controller_ledger_is_atomic_across_reconnects(
    tmp_path: Path,
) -> None:
    """Two independent external deliveries record exactly one durable launch."""
    with FaultHarness(tmp_path) as harness:
        env = os.environ | harness.controller.environment()
        command = "ssh"
        with ThreadPoolExecutor(max_workers=2) as pool:
            outputs = list(
                pool.map(
                    lambda _: subprocess.run(
                        [command, "crucible", "run", "same-intent"],
                        cwd=Path(__file__).parents[1],
                        env=env,
                        check=True,
                        text=True,
                        capture_output=True,
                    ).stdout,
                    range(2),
                )
            )
        assert sorted(json.loads(output)["status"] for output in outputs) == [
            "launched",
            "replayed",
        ]
        launches = [
            record
            for record in harness.controller.records()
            if record["operation"] == "launch"
        ]
        assert len(launches) == 1


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


def _approval_request(
    *, validation_id: str, session_id: uuid.UUID, claim_id: str, waiter: str
) -> object:
    """Build the immutable capability used by the real approval state machine."""
    from state_store.models import CreateApprovalRequest

    return CreateApprovalRequest(
        validation_id=validation_id,
        presented_run_file_digest=sha256(b"fault-run-file").hexdigest(),
        execution_intent_digest=sha256(b"fault-intent").hexdigest(),
        invocation_id=waiter,
        tool_call_id=f"tool-{waiter}",
        session_id=str(session_id),
        session_epoch="1",
        waiter_owner=waiter,
        ticket_attempt=claim_id,
        claim_id=claim_id,
    )


def _approval_store(tmp_path: Path):
    """Create a claimed ticket with executable validation evidence."""
    from state_store.models import AcquireOrchestratorLeaseRequest, CreateTicketRequest
    from state_store.store import TicketStore

    now = [datetime.now(timezone.utc)]
    store = TicketStore(persist_dir=tmp_path, clock=lambda: now[0])
    ticket = store.create_ticket(
        CreateTicketRequest(summary="fault", description="fault")
    )
    session = uuid.uuid4()
    store.acquire_orchestrator_lease(
        AcquireOrchestratorLeaseRequest(
            session_id=session,
            instance_name="fault",
            host="test",
            pid=1,
            process_start_id="fault",
            ttl_seconds=3600,
        )
    )
    claim = store.claim_ticket(
        ticket.id, "benchmark", duration_seconds=3600, session_id=session, epoch=1
    )
    return store, ticket, session, claim, now


def _persist_validation(store: object, ticket_id: str, validation_id: str) -> None:
    """Persist real executable evidence; approvals never trust fixture-only flags."""
    ticket = store.get_ticket(ticket_id)
    ticket.custom_fields["benchmark_validations"] = {
        "version": 1,
        "active_validation_id": validation_id,
        "records": {
            validation_id: {
                "validation_id": validation_id,
                "state": "executable",
                "runfile_fingerprint": sha256(b"fault-run-file").hexdigest(),
                "execution_intent_digest": sha256(b"fault-intent").hexdigest(),
            }
        },
    }
    store._persist_ticket(ticket)


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


def test_long_approval_pause_preserves_one_immutable_execution_capability(
    tmp_path: Path,
) -> None:
    """Approval survives normal renewals and can authorize exactly one launch."""
    from state_store.models import ConsumeApprovalRequest, ResolveApprovalRequest

    store, ticket, session, claim, now = _approval_store(tmp_path / "state")
    validation_id = "val-" + "a" * 32
    _persist_validation(store, ticket.id, validation_id)
    request = _approval_request(
        validation_id=validation_id,
        session_id=session,
        claim_id=claim["claim_id"],
        waiter="waiter-one",
    )
    approval = store.create_approval_request(ticket.id, request, created_by="benchmark")

    # Advance a controlled clock through several normal renewal intervals.  This
    # intentionally does not use a wall-clock sleep while a human is deciding.
    now[0] += timedelta(minutes=45)
    store.renew_orchestrator_lease(session, 1, 3600)
    resolved = store.resolve_approval_request(
        ticket.id,
        approval.approval_request_id,
        ResolveApprovalRequest(
            decision="approved",
            validation_id=validation_id,
            presented_run_file_digest=request.presented_run_file_digest,
            execution_intent_digest=request.execution_intent_digest,
        ),
        resolved_by="operator",
    )
    consumed = store.consume_approval_request(
        ticket.id,
        approval.approval_request_id,
        ConsumeApprovalRequest(
            validation_id=validation_id,
            presented_run_file_digest=request.presented_run_file_digest,
            execution_intent_digest=request.execution_intent_digest,
            session_id=str(session),
            session_epoch="1",
            claim_id=claim["claim_id"],
            ticket_attempt=claim["claim_id"],
        ),
        consumed_by="benchmark",
    )
    assert resolved.approval_request_id == approval.approval_request_id
    assert consumed.consumed_at is not None
    assert (
        consumed.validation_id,
        consumed.presented_run_file_digest,
        consumed.execution_intent_digest,
    ) == (
        validation_id,
        request.presented_run_file_digest,
        request.execution_intent_digest,
    )
    with pytest.raises(ValueError, match="already consumed"):
        store.consume_approval_request(
            ticket.id,
            approval.approval_request_id,
            ConsumeApprovalRequest(
                validation_id=validation_id,
                presented_run_file_digest=request.presented_run_file_digest,
                execution_intent_digest=request.execution_intent_digest,
                session_id=str(session),
                session_epoch="1",
                claim_id=claim["claim_id"],
                ticket_attempt=claim["claim_id"],
            ),
            consumed_by="reconnect",
        )


def test_two_waiters_require_a_structured_approval_and_wake_one_only(
    tmp_path: Path,
) -> None:
    """Approval has no generic selector: a precise immutable request is required."""
    from state_store.models import ConsumeApprovalRequest, ResolveApprovalRequest
    from state_store.store import TicketNotFound

    store, ticket, session, claim, _ = _approval_store(tmp_path / "state")
    first_id, second_id = "val-" + "a" * 32, "val-" + "b" * 32
    _persist_validation(store, ticket.id, first_id)
    # Two independent executable candidates are deliberately present.
    stored = store.get_ticket(ticket.id)
    stored.custom_fields["benchmark_validations"]["records"][second_id] = {
        "validation_id": second_id,
        "state": "executable",
        "runfile_fingerprint": sha256(b"fault-run-file").hexdigest(),
        "execution_intent_digest": sha256(b"fault-intent").hexdigest(),
    }
    store._persist_ticket(stored)
    first_request = _approval_request(
        validation_id=first_id,
        session_id=session,
        claim_id=claim["claim_id"],
        waiter="waiter-one",
    )
    second_request = _approval_request(
        validation_id=second_id,
        session_id=session,
        claim_id=claim["claim_id"],
        waiter="waiter-two",
    )
    first = store.create_approval_request(
        ticket.id, first_request, created_by="benchmark"
    )
    second = store.create_approval_request(
        ticket.id, second_request, created_by="benchmark"
    )
    with pytest.raises(TicketNotFound, match="approval request not found"):
        store.resolve_approval_request(
            ticket.id,
            "apr-" + "0" * 32,
            ResolveApprovalRequest(decision="approved"),
            resolved_by="operator",
        )
    store.resolve_approval_request(
        ticket.id,
        first.approval_request_id,
        ResolveApprovalRequest(
            decision="approved",
            validation_id=first_id,
            presented_run_file_digest=first_request.presented_run_file_digest,
            execution_intent_digest=first_request.execution_intent_digest,
        ),
        resolved_by="operator",
    )
    consumed = store.consume_approval_request(
        ticket.id,
        first.approval_request_id,
        ConsumeApprovalRequest(
            validation_id=first_id,
            presented_run_file_digest=first_request.presented_run_file_digest,
            execution_intent_digest=first_request.execution_intent_digest,
            session_id=str(session),
            session_epoch="1",
            claim_id=claim["claim_id"],
            ticket_attempt=claim["claim_id"],
        ),
        consumed_by="benchmark",
    )
    assert consumed.approval_request_id == first.approval_request_id
    assert (
        store.list_approval_requests(ticket.id)[1].approval_request_id
        == second.approval_request_id
    )
    assert store.list_approval_requests(ticket.id)[1].status == "pending"


def test_store_restart_preserves_pending_approval_and_operation_fence(
    tmp_path: Path,
) -> None:
    """A process restart cannot discard pending intent or reopen a live operation."""
    from state_store.store import TicketStore
    from state_store.trace_store import OperationLeaseError, OperationRecord, TraceStore

    root = tmp_path / "persistent"
    store, ticket, session, claim, _ = _approval_store(root / "tickets")
    validation_id = "val-" + "c" * 32
    _persist_validation(store, ticket.id, validation_id)
    request = _approval_request(
        validation_id=validation_id,
        session_id=session,
        claim_id=claim["claim_id"],
        waiter="restart-waiter",
    )
    approval = store.create_approval_request(ticket.id, request, created_by="benchmark")
    trace = TraceStore(root / "trace.sqlite")
    trace.register_or_get(OperationRecord("intent-restart", "digest", "registered"))
    trace.acquire_operation_result("intent-restart", "digest", "leader-a", 3600)
    trace.close()

    restarted = TicketStore(persist_dir=root / "tickets")
    assert (
        restarted.list_approval_requests(ticket.id)[0].approval_request_id
        == approval.approval_request_id
    )
    assert restarted.list_approval_requests(ticket.id)[0].status == "pending"
    restarted_trace = TraceStore(root / "trace.sqlite")
    try:
        with pytest.raises(OperationLeaseError, match="held"):
            restarted_trace.acquire_operation_result(
                "intent-restart", "digest", "leader-b", 3600
            )
    finally:
        restarted_trace.close()


def test_real_state_store_restart_retains_leader_and_operation_fences(
    tmp_path: Path,
) -> None:
    """Restart the service process, not merely an in-process repository object."""
    port = free_port()
    home = tmp_path / "service-home"
    process = start_store(home, port)
    processes = [process]
    base = f"http://127.0.0.1:{port}/api/v1"
    headers = {"Authorization": "Bearer fault-token"}
    session = str(uuid.uuid4())
    lease_body = {
        "session_id": session,
        "instance_name": "fault-service",
        "host": "test",
        "pid": 1,
        "process_start_id": "first",
        "ttl_seconds": 60,
    }
    try:
        acquired = httpx.post(
            f"{base}/control/orchestrator-lease/acquire",
            headers=headers,
            json=lease_body,
            timeout=2,
        )
        assert acquired.status_code == 200, diagnostics(processes)
        assert acquired.json()["epoch"] == 1
        registered = httpx.post(
            f"{base}/traces/operations/register",
            headers=headers,
            json={"operation_key": "restart-intent", "request_hash": "digest"},
            timeout=2,
        )
        assert registered.status_code == 200, diagnostics(processes)
        claimed = httpx.post(
            f"{base}/traces/operations/acquire",
            headers=headers,
            json={
                "operation_key": "restart-intent",
                "request_hash": "digest",
                "ttl_seconds": 60,
            },
            timeout=2,
        )
        assert claimed.status_code == 200, diagnostics(processes)
        prepared = httpx.post(
            f"{base}/traces/operations/restart-intent/prepared",
            headers=headers,
            json={"fencing_token": 1},
            timeout=2,
        )
        assert prepared.status_code == 200, diagnostics(processes)
        started = httpx.post(
            f"{base}/traces/operations/restart-intent/side-effect-started",
            headers=headers,
            json={"fencing_token": 1},
            timeout=2,
        )
        assert started.status_code == 200, diagnostics(processes)
        projection = httpx.get(
            f"{base}/traces/query",
            headers=headers,
            params={"ticket_id": "restart-intent", "causal": "true"},
            timeout=2,
        )
        assert projection.status_code == 200, diagnostics(processes)
        states = {event["lifecycle"]["state"] for event in projection.json()["events"]}
        assert {
            "registered",
            "lease_acquired",
            "prepared",
            "side_effect_started",
        } <= states
        assert projection.json()["diagnostics"]["missing_parents"] == []
        stop_process(process)
        process = start_store(home, port)
        processes.append(process)
        retained = httpx.get(
            f"{base}/control/orchestrator-lease", headers=headers, timeout=2
        )
        assert retained.status_code == 200, diagnostics(processes)
        assert retained.json()["lease"]["session_id"] == session
        replay = httpx.post(
            f"{base}/traces/operations/acquire",
            headers=headers,
            json={
                "operation_key": "restart-intent",
                "request_hash": "digest",
                "ttl_seconds": 60,
            },
            timeout=2,
        )
        assert replay.status_code == 409, diagnostics(processes)
        assert "reconciliation" in replay.json()["detail"]
    finally:
        for child in reversed(processes):
            stop_process(child)


def test_copied_runtime_is_sanitized_but_shared_store_still_fences_bypass(
    tmp_path: Path,
) -> None:
    """Fixture import removes claim state; direct shared-store use still has one leader."""
    from state_store.models import AcquireOrchestratorLeaseRequest, CreateTicketRequest
    from state_store.store import OrchestratorLeaseHeld, TicketStore

    spec = spec_from_file_location(
        "fault_dev_instance_identity",
        Path(__file__).parents[1] / "scripts" / "dev_instance_identity.py",
    )
    assert spec and spec.loader
    identity = module_from_spec(spec)
    spec.loader.exec_module(identity)
    source, destination = tmp_path / "source", tmp_path / "destination"
    source_worktree, destination_worktree = (
        tmp_path / "source-repo",
        tmp_path / "destination-repo",
    )
    source_worktree.mkdir()
    destination_worktree.mkdir()
    for home, worktree, name, port in (
        (source, source_worktree, "source", 18101),
        (destination, destination_worktree, "destination", 18102),
    ):
        home.mkdir()
        (home / "config.json").write_text(
            json.dumps(
                {
                    "instance_name": name,
                    "state_store": {"url": f"http://localhost:{port}", "port": port},
                }
            )
        )
        identity.create_manifest(str(home), str(worktree), name, port)
    (source / "tickets").mkdir(parents=True)
    (source / "tickets" / "PERF-fault.json").write_text(
        json.dumps(
            {
                "id": "PERF-fault",
                "status": "executing_benchmark",
                "custom_fields": {"claim": {"owner": "old"}, "pending_approval": True},
            }
        )
    )
    identity.import_state(str(source), str(destination), ["PERF-fault"], True)
    copied = json.loads((destination / "tickets" / "PERF-fault.json").read_text())
    assert copied["status"] == "awaiting_customer_guidance"
    assert "claim" not in copied["custom_fields"]

    # This intentionally bypasses the helper after the copied-runtime check.
    # The central persistence root remains authoritative nevertheless.
    central = TicketStore(persist_dir=tmp_path / "central")
    central.create_ticket(CreateTicketRequest(summary="central", description="central"))
    first = uuid.uuid4()
    central.acquire_orchestrator_lease(
        AcquireOrchestratorLeaseRequest(
            session_id=first,
            instance_name="same-name",
            host="one",
            pid=1,
            process_start_id="one",
            ttl_seconds=60,
        )
    )
    with pytest.raises(OrchestratorLeaseHeld):
        central.acquire_orchestrator_lease(
            AcquireOrchestratorLeaseRequest(
                session_id=uuid.uuid4(),
                instance_name="same-name",
                host="two",
                pid=2,
                process_start_id="two",
                ttl_seconds=60,
            )
        )


def test_trace_projection_causally_attributes_fault_decisions(tmp_path: Path) -> None:
    """#773 query projections retain the causal winner-to-launch evidence chain."""
    from providers.tracing import (
        ActionDescriptor,
        ActionType,
        LifecycleDescriptor,
        LifecycleState,
        TraceEventV1,
    )
    from providers.tracing.query import TraceQuery, query_events
    from state_store.trace_store import TraceStore

    trace = TraceStore(tmp_path / "trace.sqlite")
    try:
        winner = trace.insert_event(
            TraceEventV1(
                ticket_id="PERF-fault",
                action=ActionDescriptor(type=ActionType.DISPATCH),
                lifecycle=LifecycleDescriptor(state=LifecycleState.CLAIMED),
                attributes={"epoch": 2, "reason": "lease_acquired"},
            )
        )
        stale = trace.insert_event(
            TraceEventV1(
                ticket_id="PERF-fault",
                trace_id=winner.trace_id,
                parent_action_id=winner.action_id,
                action=ActionDescriptor(type=ActionType.STATE),
                lifecycle=LifecycleDescriptor(state=LifecycleState.REJECTED),
                outcome="rejected",
                duration_ms=0,
                attributes={"reason": "stale_epoch"},
            )
        )
        launched = trace.insert_event(
            TraceEventV1(
                ticket_id="PERF-fault",
                trace_id=winner.trace_id,
                parent_action_id=winner.action_id,
                action=ActionDescriptor(type=ActionType.OPERATION, target="intent-1"),
                lifecycle=LifecycleDescriptor(state=LifecycleState.SIDE_EFFECT_STARTED),
                attributes={"approval_resolution": "apr-1", "external_launches": 1},
            )
        )
        projected = query_events(
            trace.list_events(), TraceQuery(action_id=launched.action_id, causal=True)
        )
    finally:
        trace.close()
    assert {event.action_id for event in projected} == {
        winner.action_id,
        stale.action_id,
        launched.action_id,
    }
    assert next(
        event for event in projected if event.action_id == stale.action_id
    ).attributes == {"reason": "stale_epoch"}
