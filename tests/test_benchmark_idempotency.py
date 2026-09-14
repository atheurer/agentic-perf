"""Durable benchmark execution intent and launch-claim tests."""

from __future__ import annotations

import pytest

import agents.benchmark.server as benchmark_server
import agents.server_utils as server_utils
from agents.benchmark.server import _benchmark_intent_identity, _BenchmarkOperation


def test_execution_intent_identity_is_stable_for_replay() -> None:
    record = {"runfile_fingerprint": "abc", "attempt_id": "attempt-1"}
    first = _benchmark_intent_identity(
        "T-1", "val-1", record, "ctrl", "crucible", "crucible run"
    )
    second = _benchmark_intent_identity(
        "T-1", "val-1", record, "ctrl", "crucible", "crucible run"
    )
    assert first == second
    assert first[0] == "benchmark-execution:T-1:val-1"


@pytest.mark.asyncio
async def test_operation_claims_once_and_caches_terminal_result(
    tmp_path, monkeypatch
) -> None:
    import paths

    monkeypatch.setattr(paths, "TRACE_DB_PATH", tmp_path / "trace.db")
    monkeypatch.delenv("AGENTIC_PERF_API_TOKEN", raising=False)
    first = _BenchmarkOperation("benchmark-execution:T-1:val-1", "hash", "worker-1")
    record, status = await first.acquire()
    assert status == "acquired"
    await first.transition("prepared", record, descriptor={"intent": {}})
    await first.transition("side-effect-started", record)
    await first.transition(
        "complete", record, descriptor={"benchmark_result": {"status": "completed"}}
    )
    await first.close()

    second = _BenchmarkOperation("benchmark-execution:T-1:val-1", "hash", "worker-2")
    cached, status = await second.acquire()
    assert status == "terminal"
    assert cached["result_descriptor"]["benchmark_result"]["status"] == "completed"
    await second.close()


@pytest.mark.asyncio
async def test_terminal_write_failure_is_reclassified_indeterminate(
    monkeypatch,
) -> None:
    operation = _BenchmarkOperation("benchmark-execution:T-3:val-1", "hash", "worker")
    calls: list[str] = []

    async def transition(action: str, _record: dict, **_kwargs: object) -> dict:
        calls.append(action)
        if action == "complete":
            raise RuntimeError("lost terminal acknowledgement")
        return {}

    monkeypatch.setattr(operation, "transition", transition)
    assert await operation.terminalize({}, "complete", {"result": "ok"})
    assert calls == ["complete", "indeterminate"]


@pytest.mark.asyncio
async def test_execute_rejects_invalid_validation_without_controller_mutation(
    monkeypatch,
) -> None:
    async def no_init() -> None:
        benchmark_server._ssh = type("Unused", (), {})()

    async def active(**_kwargs: object) -> dict:
        return {
            "id": "T-4",
            "status": "executing_benchmark",
            "custom_fields": {"benchmark_validations": {"records": {}}},
        }

    monkeypatch.setattr(benchmark_server, "_ensure_init", no_init)
    monkeypatch.setattr(server_utils, "assert_ticket_active", active)
    result = await benchmark_server.execute_benchmark(
        "controller", validation_id="missing", approval_request_id="approval"
    )
    assert '"status": "rejected"' in result
