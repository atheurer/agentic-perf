"""Kernel comparison advances only with the actual durable benchmark result."""

from __future__ import annotations

import copy
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import Depends, FastAPI

import agents.benchmark.arcaflow_plugin_server as plugin_server
import agents.benchmark.server as benchmark_server
import agents.server_utils as server_utils
import orchestrator.main as orchestrator
from providers.benchmark_identity import benchmark_intent_identity
from state_store.api.router import api_router
from state_store.auth import make_auth_dependency
from state_store.models import AcquireOrchestratorLeaseRequest, CreateTicketRequest
from state_store.store import (
    ComparisonEvidenceChangedError,
    ExecutionPlanChangedError,
    TicketStore,
    ValidationSupersededError,
)


def _digest(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _example():
    params = {"workload_group": "kernel-ab", "kernel_role": "baseline"}
    step = {
        "id": 0,
        "status": "in_progress",
        "agent_type": "benchmark",
        "params": params,
    }
    runfile = {
        "benchmarks": [{"name": "sleep", "mv-params": {"sets": [{"params": []}]}}]
    }
    record = {
        "validation_id": "val-a",
        "attempt_id": "attempt-a",
        "execution_intent_id": "intent-a",
        "execution_plan_step_id": "0",
        "execution_plan_fingerprint": _digest(params),
        "run_file": runfile,
        "runfile_fingerprint": _digest(runfile),
        "controller": "controller",
        "harness": "crucible",
        "run_command": "crucible run",
    }
    other = {
        **record,
        "validation_id": "val-b",
        "runfile_fingerprint": "f" * 64,
    }
    cf = {
        "execution_plan": {
            "current_step": 0,
            "steps": [step, {"id": 1, "agent_type": "review", "params": {}}],
            "run_ids": [],
        },
        "benchmark_validations": {
            "active_validation_id": "val-b",
            "records": {"val-a": record, "val-b": other},
        },
        "benchmark_execution_validation_id": "val-a",
        "run_id": "run-a",
        "benchmark_status": "completed",
    }
    key, request_hash, _ = benchmark_intent_identity(
        "PERF-TEST", "val-a", record, "controller", "crucible", "crucible run"
    )
    operation = {
        "operation_key": key,
        "state": "terminal",
        "terminal_outcome": "success",
        "request_hash": request_hash,
        "external_ids": {"run_id": "run-a"},
        "result_descriptor": {
            "benchmark_result": {
                "validation_id": "val-a",
                "run_id": "run-a",
                "status": "completed",
            }
        },
    }
    return cf, record, operation


class _Response:
    def __init__(self, value, status_code=200):
        self.value = value
        self.status_code = status_code

    def json(self):
        return self.value

    def raise_for_status(self):
        assert self.status_code < 300


class _Client:
    def __init__(
        self,
        ticket,
        operation=None,
        supersede_on_plan_write=False,
        change_plan_on_write=False,
        change_evidence_on_write=None,
    ):
        self.ticket = ticket
        self.operation = operation
        self.supersede_on_plan_write = supersede_on_plan_write
        self.change_plan_on_write = change_plan_on_write
        self.change_evidence_on_write = change_evidence_on_write
        self.patches = []
        self.patch_attempts = []
        self.posts = []
        self.operation_reads = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, url, **kwargs):
        if url.endswith("/traces/operations"):
            self.operation_reads += 1
            return _Response({"operation": self.operation} if self.operation else {})
        return _Response(self.ticket)

    async def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return _Response({})

    async def patch(self, url, **kwargs):
        self.patch_attempts.append((url, copy.deepcopy(kwargs)))
        if self.supersede_on_plan_write and kwargs.get("json", {}).get(
            "require_unsuperseded_validation_id"
        ):
            return _Response(
                {
                    "detail": (
                        "the executed benchmark validation was superseded before "
                        "the comparison step could be committed"
                    )
                },
                status_code=409,
            )
        if self.change_plan_on_write and kwargs.get("json", {}).get(
            "expected_execution_plan"
        ):
            return _Response(
                {"detail": "execution plan changed before the comparison step"},
                status_code=409,
            )
        expected_evidence = kwargs.get("json", {}).get("expected_comparison_evidence")
        if self.change_evidence_on_write and expected_evidence:
            self.ticket["custom_fields"][self.change_evidence_on_write] = (
                "concurrent-value"
            )
            current_evidence = {
                name: self.ticket["custom_fields"].get(name)
                for name in (
                    "benchmark_execution_validation_id",
                    "run_id",
                    "benchmark_status",
                )
            }
            if current_evidence != expected_evidence:
                return _Response(
                    {
                        "detail": (
                            "comparison execution evidence changed after its "
                            "durable receipt was read"
                        )
                    },
                    status_code=409,
                )
        self.patches.append((url, copy.deepcopy(kwargs)))
        return _Response({})


async def _advance(
    monkeypatch,
    cf,
    operation,
    ticket_id="PERF-TEST",
    supersede_on_plan_write=False,
    change_plan_on_write=False,
    change_evidence_on_write=None,
):
    ticket = {"status": "awaiting_review", "custom_fields": cf}
    client = _Client(
        ticket,
        operation,
        supersede_on_plan_write,
        change_plan_on_write,
        change_evidence_on_write,
    )
    monkeypatch.setattr(orchestrator, "AuditedAsyncHTTPClient", lambda **_: client)
    await orchestrator._advance_plan("http://store", ticket_id, "executing_benchmark")
    return client


def test_receipt_uses_executed_validation_not_active_validation():
    cf, record, operation = _example()
    identity = orchestrator._comparison_operation_identity(
        "PERF-TEST", cf["execution_plan"]["steps"][0], cf
    )
    assert identity is not None
    key, request_hash, actual_record = identity
    receipt = orchestrator._comparison_receipt(
        actual_record, cf, key, request_hash, operation
    )
    assert receipt is not None
    assert receipt["runfile"] == record["run_file"]
    captured = orchestrator._capture_step_results("benchmark", cf, receipt)
    assert captured["runfile"] == record["run_file"]
    assert captured["runfile_fingerprint"] != record["runfile_fingerprint"]


def test_ordinary_step_capture_prefers_the_validation_that_actually_ran():
    cf = {
        "run_id": "run-a",
        "benchmark_status": "completed",
        "benchmark_execution_validation_id": "val-a",
        "benchmark_validations": {
            "active_validation_id": "val-b",
            "records": {
                "val-a": {
                    "runfile_fingerprint": "a" * 64,
                    "run_file": {"benchmarks": [{"name": "actual"}]},
                },
                "val-b": {
                    "runfile_fingerprint": "b" * 64,
                    "run_file": {"benchmarks": [{"name": "later"}]},
                },
            },
        },
    }

    results = orchestrator._capture_step_results("benchmark", cf)

    assert results["runfile_fingerprint"] == "a" * 64
    assert results["runfile"]["benchmarks"][0]["name"] == "actual"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["failed", "pending", "wrong_key", "wrong_hash", "wrong_run"]
)
async def test_comparison_does_not_advance_without_matching_success(monkeypatch, fault):
    cf, _, operation = _example()
    if fault == "failed":
        operation["terminal_outcome"] = "failure"
    elif fault == "pending":
        operation["state"] = "side_effect_started"
    elif fault == "wrong_key":
        operation["operation_key"] = "other-operation"
    elif fault == "wrong_hash":
        operation["request_hash"] = "invalid"
    elif fault == "wrong_run":
        operation["external_ids"]["run_id"] = "different-run"
    client = await _advance(monkeypatch, cf, operation)
    assert not client.patches
    assert client.posts[0][1]["json"]["status"] == "awaiting_customer_guidance"


@pytest.mark.asyncio
async def test_successful_receipt_captures_actual_runfile(monkeypatch):
    cf, record, operation = _example()
    expected_plan = copy.deepcopy(cf["execution_plan"])
    client = await _advance(monkeypatch, cf, operation)
    plan = client.patches[0][1]["json"]["fields"]["execution_plan"]
    captured = plan["steps"][0]["results"]
    assert captured["runfile"] == record["run_file"]
    assert (
        client.patch_attempts[0][1]["json"]["require_unsuperseded_validation_id"]
        == "val-a"
    )
    assert captured["runfile_fingerprint"] == orchestrator.comparison_fingerprint(
        record
    )
    assert (
        client.patch_attempts[0][1]["json"]["expected_execution_plan"] == expected_plan
    )
    assert client.patch_attempts[0][1]["json"]["expected_comparison_evidence"] == {
        "benchmark_execution_validation_id": "val-a",
        "run_id": "run-a",
        "benchmark_status": "completed",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("completion_path", ["stop_after", "last_step"])
async def test_every_comparison_plan_write_is_fenced(monkeypatch, completion_path):
    cf, _, operation = _example()
    if completion_path == "stop_after":
        cf["stop_after_step"] = "benchmark"
    else:
        cf["execution_plan"]["steps"] = cf["execution_plan"]["steps"][:1]

    client = await _advance(monkeypatch, cf, operation)

    assert client.patch_attempts
    assert all(
        attempt[1]["json"]["require_unsuperseded_validation_id"] == "val-a"
        for attempt in client.patch_attempts
    )


@pytest.mark.asyncio
async def test_supersession_after_receipt_pauses_without_committing_or_advancing(
    monkeypatch,
):
    cf, _, operation = _example()
    cf["scoped_context"] = {"review": "stale review context"}
    client = await _advance(monkeypatch, cf, operation, supersede_on_plan_write=True)

    assert (
        client.patch_attempts[0][1]["json"]["require_unsuperseded_validation_id"]
        == "val-a"
    )
    assert client.patch_attempts[0][1]["json"]["fields"]["scoped_context"] == {}
    assert client.patches == []
    assert len(client.posts) == 1
    assert client.posts[0][1]["json"]["status"] == "awaiting_customer_guidance"
    assert "validation was superseded" in client.posts[0][1]["json"]["comment"]


@pytest.mark.asyncio
async def test_concurrent_plan_edit_pauses_without_committing_comparison(monkeypatch):
    cf, _, operation = _example()
    expected_plan = copy.deepcopy(cf["execution_plan"])
    client = await _advance(monkeypatch, cf, operation, change_plan_on_write=True)

    assert (
        client.patch_attempts[0][1]["json"]["expected_execution_plan"] == expected_plan
    )
    assert client.patches == []
    assert len(client.posts) == 1
    assert client.posts[0][1]["json"]["status"] == "awaiting_customer_guidance"
    assert "execution plan changed" in client.posts[0][1]["json"]["comment"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed_field",
    [
        "benchmark_execution_validation_id",
        "run_id",
        "benchmark_status",
    ],
)
async def test_concurrent_comparison_evidence_edit_pauses_before_commit(
    monkeypatch, changed_field
):
    cf, _, operation = _example()
    client = await _advance(
        monkeypatch, cf, operation, change_evidence_on_write=changed_field
    )

    expected = client.patch_attempts[0][1]["json"]["expected_comparison_evidence"]
    assert expected == {
        "benchmark_execution_validation_id": "val-a",
        "run_id": "run-a",
        "benchmark_status": "completed",
    }
    assert client.patches == []
    assert len(client.posts) == 1
    assert client.posts[0][1]["json"]["status"] == "awaiting_customer_guidance"
    assert (
        "evidence changed after the durable receipt was read"
        in client.posts[0][1]["json"]["comment"]
    )


@pytest.mark.asyncio
async def test_mixed_plan_ordinary_benchmark_needs_no_comparison_receipt(monkeypatch):
    cf, _, _ = _example()
    cf["execution_plan"]["steps"][0]["params"].pop("workload_group")
    cf["execution_plan"]["steps"][0]["params"].pop("kernel_role")
    cf["execution_plan"]["steps"][1]["agent_type"] = "benchmark"
    cf["execution_plan"]["steps"][1]["params"] = {
        "workload_group": "kernel-ab",
        "kernel_role": "candidate",
    }
    cf["directives"] = {"kernel_comparison": {"baseline": "A", "candidate": "B"}}
    cf["benchmark_validations"]["active_validation_id"] = "val-b"
    client = await _advance(monkeypatch, cf, None)
    assert client.operation_reads == 0
    assert client.patches
    assert (
        client.patches[0][1]["json"]["fields"]["execution_plan"]["steps"][0]["results"][
            "runfile_fingerprint"
        ]
        == cf["benchmark_validations"]["records"]["val-a"]["runfile_fingerprint"]
    )


@pytest.mark.parametrize("fault", ["wrong_step", "missing_execution_id"])
def test_identity_requires_the_current_step_and_execution_validation(fault):
    cf, _, _ = _example()
    if fault == "wrong_step":
        cf["benchmark_validations"]["records"]["val-a"]["execution_plan_step_id"] = "1"
    else:
        cf["benchmark_execution_validation_id"] = ""
    assert (
        orchestrator._comparison_operation_identity(
            "PERF-TEST", cf["execution_plan"]["steps"][0], cf
        )
        is None
    )


def test_superseded_validation_cannot_prove_a_comparison_run(tmp_path):
    cf, record, _ = _example()
    store = TicketStore(persist_dir=tmp_path)
    ticket = store.create_ticket(
        CreateTicketRequest(
            summary="comparison receipt",
            description="comparison receipt",
            custom_fields={"execution_plan": cf["execution_plan"]},
        )
    )

    params_digest = benchmark_server._execution_plan_fingerprint(cf)
    runfile_digest = benchmark_server._runfile_fingerprint(record["run_file"])
    for validation_id in ("val-a", "val-b"):
        validation = {
            **record,
            "validation_id": validation_id,
            "runfile_fingerprint": runfile_digest,
            "execution_plan_fingerprint": params_digest,
            "execution_intent_digest": benchmark_server._execution_intent_digest(
                runfile_digest,
                params_digest,
                "crucible",
                "controller",
                "crucible run",
            ),
            "params_fingerprint": "no-plan",
            "validator_command": "crucible validate",
            "validator_version": "test",
            "validation_output": {
                "size_bytes": 2,
                "original_size_bytes": 2,
                "redacted_size_bytes": 2,
                "media_type": "text/plain",
                "digest": "a" * 64,
                "digest_kind": "sha256",
                "preview": "ok",
                "truncated": False,
                "redaction_applied": False,
            },
        }
        _, conflict = store.create_validation(
            ticket.id, validation, 0 if validation_id == "val-a" else 1
        )
        assert conflict is None

    # A newer validation may exist without retiring the one that actually ran.
    # The conditional plan write remains valid in that ordering.
    store.update_fields(
        ticket.id,
        {
            "execution_plan": cf["execution_plan"],
            "comparison_commit_marker": "accepted-before-retirement",
        },
        require_unsuperseded_validation_id="val-a",
    )
    assert (
        store.get_ticket(ticket.id).custom_fields["comparison_commit_marker"]
        == "accepted-before-retirement"
    )

    _, conflict = store.supersede_validation(
        ticket.id, "val-a", "val-b", "comparison parameters changed", 2
    )
    assert conflict is None

    # Supersession and the conditional field write share the store lock. If
    # retirement wins first, the comparison plan update cannot be committed.
    with pytest.raises(ValidationSupersededError, match="was superseded"):
        store.update_fields(
            ticket.id,
            {"execution_plan": cf["execution_plan"]},
            require_unsuperseded_validation_id="val-a",
        )
    manifest = store.get_ticket(ticket.id).custom_fields["benchmark_validations"]
    supersession = next(
        value
        for value in manifest["records"].values()
        if value.get("record_type") == "supersession"
    )
    assert supersession["supersedes_validation_id"] == "val-a"

    cf["benchmark_validations"] = manifest
    assert (
        orchestrator._comparison_operation_identity(
            "PERF-TEST", cf["execution_plan"]["steps"][0], cf
        )
        is None
    )


def test_stale_comparison_write_cannot_overwrite_a_concurrent_plan_edit(tmp_path):
    cf, _, _ = _example()
    expected_plan = copy.deepcopy(cf["execution_plan"])
    store = TicketStore(persist_dir=tmp_path)
    ticket = store.create_ticket(
        CreateTicketRequest(
            summary="comparison plan race",
            description="comparison plan race",
            custom_fields={"execution_plan": expected_plan},
        )
    )

    concurrently_edited = copy.deepcopy(expected_plan)
    concurrently_edited["steps"][0]["params"]["kernel_role"] = "candidate"
    store.update_fields(ticket.id, {"execution_plan": concurrently_edited})
    with pytest.raises(ExecutionPlanChangedError, match="execution plan changed"):
        store.update_fields(
            ticket.id,
            {"comparison_receipt": {"run_id": "run-a"}},
            expected_execution_plan=expected_plan,
        )

    current = store.get_ticket(ticket.id).custom_fields
    assert current["execution_plan"] == concurrently_edited
    assert "comparison_receipt" not in current


def test_ticket_store_atomically_rejects_stale_comparison_evidence(tmp_path):
    cf, _, _ = _example()
    plan = copy.deepcopy(cf["execution_plan"])
    store = TicketStore(persist_dir=tmp_path)
    ticket = store.create_ticket(
        CreateTicketRequest(
            summary="comparison evidence race",
            description="comparison evidence race",
            custom_fields={"execution_plan": plan},
        )
    )
    expected_evidence = {
        "benchmark_execution_validation_id": "val-a",
        "run_id": "run-a",
        "benchmark_status": "completed",
    }
    store.update_fields(ticket.id, expected_evidence)
    store.update_fields(ticket.id, {"run_id": "run-b"})

    with pytest.raises(
        ComparisonEvidenceChangedError,
        match="comparison execution evidence changed",
    ):
        store.update_fields(
            ticket.id,
            {"comparison_receipt": {"run_id": "run-a"}},
            expected_execution_plan=plan,
            expected_comparison_evidence=expected_evidence,
        )

    current = store.get_ticket(ticket.id).custom_fields
    assert current["run_id"] == "run-b"
    assert "comparison_receipt" not in current


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed_field",
    [
        "benchmark_execution_validation_id",
        "run_id",
        "benchmark_status",
    ],
)
async def test_ticket_fields_api_maps_evidence_cas_conflict_to_409(
    tmp_path, monkeypatch, changed_field
):
    from orchestrator.main import _mutation_headers

    cf, record, _ = _example()
    plan = copy.deepcopy(cf["execution_plan"])
    store = TicketStore(persist_dir=tmp_path)
    ticket = store.create_ticket(
        CreateTicketRequest(
            summary="comparison API evidence race",
            description="comparison API evidence race",
            custom_fields={"execution_plan": plan},
        )
    )
    runfile_digest = _digest(record["run_file"])
    params_digest = _digest(plan["steps"][0]["params"])
    validation = {
        **record,
        "validation_id": "val-a",
        "runfile_fingerprint": runfile_digest,
        "execution_plan_fingerprint": params_digest,
        "execution_intent_digest": benchmark_server._execution_intent_digest(
            runfile_digest, params_digest, "crucible", "controller", "crucible run"
        ),
        "validator_command": "crucible validate",
        "validator_version": "test",
        "validation_output": {
            "size_bytes": 2,
            "original_size_bytes": 2,
            "redacted_size_bytes": 2,
            "media_type": "text/plain",
            "digest": "a" * 64,
            "digest_kind": "sha256",
            "preview": "ok",
            "truncated": False,
            "redaction_applied": False,
        },
    }
    _, conflict = store.create_validation(ticket.id, validation, 0)
    assert conflict is None

    expected_evidence = {
        "benchmark_execution_validation_id": "val-a",
        "run_id": "run-a",
        "benchmark_status": "completed",
    }
    store.update_fields(ticket.id, expected_evidence)
    changed_values = {
        "benchmark_execution_validation_id": "val-b",
        "run_id": "run-b",
        "benchmark_status": "failed",
    }
    store.update_fields(ticket.id, {changed_field: changed_values[changed_field]})

    session_id = uuid4()
    lease = store.acquire_orchestrator_lease(
        AcquireOrchestratorLeaseRequest(
            session_id=session_id,
            instance_name="comparison-cas-test",
            host="host-a",
            pid=123,
            process_start_id="boot:123",
            ttl_seconds=30,
        )
    )
    monkeypatch.setenv("AGENTIC_PERF_API_TOKEN", "service")
    monkeypatch.setenv("AGENTIC_PERF_ORCHESTRATOR_SESSION_ID", str(session_id))
    monkeypatch.setenv("AGENTIC_PERF_ORCHESTRATOR_EPOCH", str(lease.epoch))
    app = FastAPI()
    app.state.store = store
    app.include_router(
        api_router, dependencies=[Depends(make_auth_dependency("service"))]
    )
    path = f"/api/v1/tickets/{ticket.id}/fields"
    body = {
        "fields": {"comparison_receipt": {"run_id": "run-a"}},
        "require_unsuperseded_validation_id": "val-a",
        "expected_execution_plan": plan,
        "expected_comparison_evidence": expected_evidence,
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://store"
    ) as client:
        response = await client.patch(path, headers=_mutation_headers(None), json=body)
        assert response.status_code == 409
        assert "comparison execution evidence changed" in response.json()["detail"]

        # An ordinary ticket edit without this optional precondition remains valid.
        ordinary = await client.patch(
            path,
            headers=_mutation_headers(None),
            json={"fields": {"ordinary_note": "still allowed"}},
        )
        assert ordinary.status_code == 200, ordinary.text

    current = store.get_ticket(ticket.id).custom_fields
    assert current[changed_field] == changed_values[changed_field]
    assert "comparison_receipt" not in current
    assert current["ordinary_note"] == "still allowed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed_field",
    [
        "benchmark_execution_validation_id",
        "run_id",
        "benchmark_status",
    ],
)
async def test_ticket_fields_api_atomically_rejects_changed_comparison_evidence(
    tmp_path, monkeypatch, changed_field
):
    from orchestrator.main import _mutation_headers

    cf, record, _ = _example()
    plan = copy.deepcopy(cf["execution_plan"])
    store = TicketStore(persist_dir=tmp_path)
    ticket = store.create_ticket(
        CreateTicketRequest(
            summary="comparison evidence race",
            description="comparison evidence race",
            custom_fields={"execution_plan": plan},
        )
    )
    runfile_digest = _digest(record["run_file"])
    params_digest = _digest(plan["steps"][0]["params"])
    validation = {
        **record,
        "validation_id": "val-a",
        "runfile_fingerprint": runfile_digest,
        "execution_plan_fingerprint": params_digest,
        "execution_intent_digest": benchmark_server._execution_intent_digest(
            runfile_digest, params_digest, "crucible", "controller", "crucible run"
        ),
        "validator_command": "crucible validate",
        "validator_version": "test",
        "validation_output": {
            "size_bytes": 2,
            "original_size_bytes": 2,
            "redacted_size_bytes": 2,
            "media_type": "text/plain",
            "digest": "a" * 64,
            "digest_kind": "sha256",
            "preview": "ok",
            "truncated": False,
            "redaction_applied": False,
        },
    }
    _, conflict = store.create_validation(ticket.id, validation, 0)
    assert conflict is None

    original_evidence = {
        "benchmark_execution_validation_id": "val-a",
        "run_id": "run-a",
        "benchmark_status": "completed",
    }
    store.update_fields(ticket.id, original_evidence)
    concurrent_value = {
        "benchmark_execution_validation_id": "val-b",
        "run_id": "run-b",
        "benchmark_status": "failed",
    }[changed_field]
    store.update_fields(ticket.id, {changed_field: concurrent_value})

    session_id = uuid4()
    lease = store.acquire_orchestrator_lease(
        AcquireOrchestratorLeaseRequest(
            session_id=session_id,
            instance_name="comparison-cas-test",
            host="host-a",
            pid=123,
            process_start_id="boot:123",
            ttl_seconds=30,
        )
    )
    monkeypatch.setenv("AGENTIC_PERF_API_TOKEN", "service")
    monkeypatch.setenv("AGENTIC_PERF_ORCHESTRATOR_SESSION_ID", str(session_id))
    monkeypatch.setenv("AGENTIC_PERF_ORCHESTRATOR_EPOCH", str(lease.epoch))
    app = FastAPI()
    app.state.store = store
    app.include_router(
        api_router, dependencies=[Depends(make_auth_dependency("service"))]
    )
    path = f"/api/v1/tickets/{ticket.id}/fields"
    body = {
        "fields": {"comparison_receipt": {"run_id": "run-a"}},
        "require_unsuperseded_validation_id": "val-a",
        "expected_execution_plan": plan,
        "expected_comparison_evidence": original_evidence,
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://store"
    ) as client:
        response = await client.patch(path, headers=_mutation_headers(None), json=body)
        assert response.status_code == 409
        assert "comparison execution evidence changed" in response.json()["detail"]

        # Generic ticket edits remain unaffected by the optional comparison CAS.
        ordinary = await client.patch(
            path,
            headers=_mutation_headers(None),
            json={"fields": {"ordinary_note": "still allowed"}},
        )
        assert ordinary.status_code == 200, ordinary.text

    current = store.get_ticket(ticket.id).custom_fields
    assert current[changed_field] == concurrent_value
    assert "comparison_receipt" not in current
    assert current["ordinary_note"] == "still allowed"


def test_crucible_validation_from_another_comparison_step_is_rejected():
    cf, record, _ = _example()
    record.update(
        {
            "state": "executable",
            "params_fingerprint": benchmark_server._compute_params_fingerprint(cf),
            "execution_plan_fingerprint": benchmark_server._execution_plan_fingerprint(
                cf
            ),
        }
    )
    record["execution_plan_step_id"] = "1"
    runfile, error = benchmark_server._get_validated_runfile(
        "val-a", "controller", "crucible", {"custom_fields": cf}
    )
    assert runfile is None
    assert "different execution plan step" in error


def _benchmark(name, sets):
    return {
        "name": name,
        "mv-params": {"sets": [{"params": params} for params in sets]},
    }


def test_mv_params_check_covers_every_benchmark_and_set():
    runfile = {
        "benchmarks": [
            _benchmark("first", [[{"arg": "seconds", "vals": ["30"]}]]),
            _benchmark(
                "second",
                [
                    [{"arg": "seconds", "vals": ["30"]}],
                    [{"arg": "seconds", "vals": ["31"]}],
                ],
            ),
        ]
    }
    error = benchmark_server._check_plan_mv_params(
        {"custom_fields": {}}, runfile, {"seconds": "30"}
    )
    assert "seconds" in error
    assert "31" in error


def test_mv_params_check_rejects_duplicate_and_extraneous_identity():
    duplicate = {
        "benchmarks": [
            _benchmark(
                "sleep",
                [
                    [
                        {"arg": "seconds", "vals": ["30"]},
                        {"arg": "seconds", "vals": ["30"]},
                    ]
                ],
            )
        ]
    }
    extra = {
        "benchmarks": [
            _benchmark(
                "sleep",
                [
                    [
                        {"arg": "seconds", "vals": ["30"]},
                        {"arg": "cpu", "vals": ["0-3"]},
                    ]
                ],
            )
        ]
    }
    assert "duplicate" in benchmark_server._check_plan_mv_params(
        {"custom_fields": {}}, duplicate, {"seconds": "30"}
    )
    assert "cpu" in benchmark_server._check_plan_mv_params(
        {"custom_fields": {}}, extra, {"seconds": "30"}
    )


def test_large_result_uses_bounded_audited_blob_reference(monkeypatch, tmp_path):
    class _BlobStore:
        def __init__(self):
            self.payloads = {}

        def put(self, payload, *, max_bytes):
            assert len(payload) <= max_bytes
            self.payloads["sha256:test"] = payload
            return "sha256:test"

        def get(self, reference, *, max_bytes):
            assert reference == "sha256:test"
            return self.payloads[reference]

    store = _BlobStore()
    monkeypatch.setattr(
        benchmark_server, "_execution_result_blob_store", lambda _ticket: store
    )
    original = {
        "status": "completed",
        "exit_code": 0,
        "harness": "crucible",
        "run_id": "run-a",
        "run_dir": "/run/a",
        "validation_id": "val-a",
        "result_summary": {"value": "x" * 10000},
    }
    compact, safe = benchmark_server._prepare_operation_result("PERF-TEST", original)
    assert len(json.dumps(compact).encode()) <= 3000
    assert benchmark_server._replay_operation_result("PERF-TEST", compact) == safe


@pytest.mark.asyncio
async def test_generic_comparison_rejected_before_adapter_support_check(monkeypatch):
    ticket = {
        "id": "PERF-TEST",
        "custom_fields": {
            "execution_plan": {
                "current_step": 0,
                "steps": [
                    {
                        "id": 0,
                        "agent_type": "benchmark",
                        "params": {"workload_group": "kernel-ab"},
                    }
                ],
            }
        },
    }

    async def active(**kwargs):
        return ticket

    async def initialized():
        return None

    monkeypatch.setattr(benchmark_server, "_ensure_init", initialized)
    monkeypatch.setattr(benchmark_server, "_ticket", ticket)
    monkeypatch.setattr(
        benchmark_server,
        "_skill_provider",
        SimpleNamespace(
            default_harness="kube-burner", generic_execution_supported=False
        ),
    )
    monkeypatch.setattr(server_utils, "assert_ticket_active", active)
    result = json.loads(
        await benchmark_server.execute_benchmark("controller", run_file={})
    )
    assert result["reason_code"] == "workload_group_harness_unsupported"

    ticket["custom_fields"]["execution_plan"]["steps"][0]["params"] = {}
    assert not benchmark_server.comparison_step_requested(ticket)


@pytest.mark.asyncio
async def test_specialized_comparison_tools_reject_before_launch(monkeypatch):
    cf, _, _ = _example()
    ticket = {"custom_fields": cf}

    async def active(**kwargs):
        return ticket

    async def owned(*args):
        return None

    monkeypatch.setattr(server_utils, "assert_ticket_active", active)
    monkeypatch.setattr(benchmark_server, "_ticket", ticket)
    monkeypatch.setattr(plugin_server, "_ticket", ticket)
    monkeypatch.setattr(benchmark_server, "_require_tool_owner", owned)
    monkeypatch.setattr(plugin_server, "_require_tool_owner", owned)
    boot = json.loads(await benchmark_server.execute_boot_time_test("192.0.2.1"))
    plugin = json.loads(
        await plugin_server.execute_arcaflow_plugin("image", "step", {})
    )
    assert boot["reason_code"] == "workload_group_harness_unsupported"
    assert plugin["reason_code"] == "workload_group_harness_unsupported"


@pytest.mark.asyncio
async def test_arcaflow_workflow_comparison_rejected_before_engine_call(monkeypatch):
    from agents.benchmark.agent import BenchmarkAgent

    cf, _, _ = _example()
    agent = BenchmarkAgent.__new__(BenchmarkAgent)
    agent._ticket_id = "PERF-TEST"

    async def get_ticket(_):
        return {"custom_fields": cf}

    monkeypatch.setattr(agent, "_get_ticket", get_ticket)
    result = json.loads(await agent._execute_arcaflow_workflow("workflow", {}))
    assert result["reason_code"] == "workload_group_harness_unsupported"
