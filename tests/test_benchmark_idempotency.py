"""Durable benchmark execution intent and launch-claim tests."""

from __future__ import annotations

import asyncio
import json

import pytest

import agents.benchmark.server as benchmark_server
import agents.server_utils as server_utils
from agents.benchmark.server import _benchmark_intent_identity, _BenchmarkOperation


class _ControllerResult:
    def __init__(self, exit_code: int = 0, stdout: str = "", stderr: str = ""):
        self.exit_code, self.stdout, self.stderr = exit_code, stdout, stderr


def _crucible_fixture(
    tmp_path, monkeypatch, *, launch=None, run_result=None, run_error=None
):
    """Wire the real execute handler to a deterministic fake controller."""
    import paths

    monkeypatch.setattr(paths, "TRACE_DB_PATH", tmp_path / "trace.db")
    monkeypatch.delenv("AGENTIC_PERF_API_TOKEN", raising=False)
    monkeypatch.setenv("TICKET_ID", "T-788")
    record = {
        "validation_id": "val-788",
        "attempt_id": "attempt-1",
        "execution_intent_id": "intent-1",
        "run_file": {"benchmarks": [{"name": "uperf"}]},
        "runfile_fingerprint": benchmark_server._runfile_fingerprint(
            {"benchmarks": [{"name": "uperf"}]}
        ),
        "harness": "crucible",
        "controller": "controller",
        "params_fingerprint": "no-mv-params",
        "execution_plan_fingerprint": benchmark_server._execution_plan_fingerprint({}),
        "run_command": "crucible run",
        "state": "executable",
    }
    ticket = {
        "id": "T-788",
        "status": "executing_benchmark",
        "custom_fields": {"benchmark_validations": {"records": {"val-788": record}}},
    }
    calls = []

    class SSH:
        async def run(self, host, command, **kwargs):
            calls.append(("run", command))
            if "result-summary.json" in command:
                return _ControllerResult(stdout='{"result":"pass"}')
            if "crucible-opensearch" in command:
                return _ControllerResult(stdout="GONE")
            if "crucible start opensearch" in command:
                return _ControllerResult(stdout="Successfully started OpenSearch")
            return _ControllerResult(stdout="OK")

        async def copy_to(self, host, local_path, remote_path, **kwargs):
            calls.append(("copy_to", remote_path))
            return _ControllerResult()

        async def run_with_progress(self, host, command, **kwargs):
            calls.append(("launch", command))
            if launch:
                await launch()
            if run_error:
                raise run_error
            return run_result or _ControllerResult(
                stdout="Results stored in: "
                "/var/lib/crucible/run/uperf--12345678-1234-1234-1234-123456789abc\n"
            )

    async def ensure_init():
        benchmark_server._ssh = SSH()

    async def active_check(**kwargs):
        return ticket

    monkeypatch.setattr(benchmark_server, "_ensure_init", ensure_init)
    monkeypatch.setattr(server_utils, "assert_ticket_active", active_check)
    monkeypatch.setattr(
        benchmark_server,
        "_get_validated_runfile",
        lambda *_args: (record["run_file"], None),
    )

    class Approval:
        status_code = 200

        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, *args, **kwargs):
            if "/transition" in str(url):
                calls.append(("pause", kwargs.get("json", {})))
            return self

    monkeypatch.setattr("providers.execution.AuditedAsyncHTTPClient", Approval)
    return record, calls


async def _execute():
    return json.loads(
        await benchmark_server.execute_benchmark(
            "controller", validation_id="val-788", approval_request_id="approval-1"
        )
    )


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


@pytest.mark.asyncio
async def test_execute_path_duplicate_replay_does_not_launch(monkeypatch) -> None:
    """The actual server entrypoint returns the durable cached operation."""
    record = {
        "validation_id": "val-1",
        "run_file": {"benchmark": "demo"},
        "runfile_fingerprint": "fp",
        "harness": "crucible",
        "controller": "controller",
        "params_fingerprint": "no-mv-params",
        "execution_plan_fingerprint": "x",
        "run_command": "crucible run",
        "state": "executable",
    }
    active = {"id": "", "status": "executing_benchmark", "custom_fields": {}}
    calls: list[str] = []

    async def no_init() -> None:
        benchmark_server._ssh = object()

    async def active_check(**_kwargs: object) -> dict:
        return active

    monkeypatch.setattr(benchmark_server, "_ensure_init", no_init)
    monkeypatch.setattr(server_utils, "assert_ticket_active", active_check)
    monkeypatch.setattr(
        benchmark_server,
        "_get_validated_runfile",
        lambda *_: (record["run_file"], None),
    )
    monkeypatch.setattr(benchmark_server, "_validation_records", {"val-1": record})

    class DuplicateOperation:
        async def acquire(self):
            calls.append("acquire")
            return (
                {"result_descriptor": {"benchmark_result": {"status": "completed"}}},
                "terminal",
            )

        async def close(self):
            calls.append("close")

    monkeypatch.setattr(
        benchmark_server, "_BenchmarkOperation", lambda *_: DuplicateOperation()
    )
    result = await benchmark_server.execute_benchmark(
        "controller", validation_id="val-1"
    )
    assert '"existing_operation": true' in result
    assert calls == ["acquire", "close"]


@pytest.mark.asyncio
async def test_execute_sequential_duplicate_replays_terminal_without_second_launch(
    tmp_path, monkeypatch
):
    _, calls = _crucible_fixture(tmp_path, monkeypatch)
    first, second = await _execute(), await _execute()
    assert first["status"] == "completed"
    assert second["status"] == "completed"
    assert second["existing_operation"] is True
    assert len([c for c in calls if c[0] == "launch"]) == 1


@pytest.mark.asyncio
async def test_execute_concurrent_workers_only_one_launches(tmp_path, monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def launch():
        entered.set()
        await release.wait()

    _, calls = _crucible_fixture(tmp_path, monkeypatch, launch=launch)
    monkeypatch.setenv("AGENTIC_PERF_INSTANCE_NAME", "worker-1")
    first_task = asyncio.create_task(_execute())
    await entered.wait()
    monkeypatch.setenv("AGENTIC_PERF_INSTANCE_NAME", "worker-2")
    second = await _execute()
    assert second["status"] == "rejected"
    assert second["reason_code"] == "existing_operation"
    release.set()
    first = await first_task
    assert first["status"] == "completed"
    assert len([c for c in calls if c[0] == "launch"]) == 1


@pytest.mark.asyncio
async def test_execute_new_validation_attempt_launches_second_time(
    tmp_path, monkeypatch
):
    record, calls = _crucible_fixture(tmp_path, monkeypatch)
    first = await _execute()
    record["validation_id"] = "val-789"
    record["attempt_id"] = "attempt-2"
    record["execution_intent_id"] = "intent-2"
    record["runfile_fingerprint"] = benchmark_server._runfile_fingerprint(
        record["run_file"]
    )
    monkeypatch.setattr(
        benchmark_server,
        "_get_validated_runfile",
        lambda *_args: (record["run_file"], None),
    )
    # execute uses the ticket manifest only for resolution; swap its token too.
    active = {
        "id": "T-788",
        "status": "executing_benchmark",
        "custom_fields": {"benchmark_validations": {"records": {"val-789": record}}},
    }

    async def active_check(**kwargs):
        return active

    monkeypatch.setattr(server_utils, "assert_ticket_active", active_check)
    result = json.loads(
        await benchmark_server.execute_benchmark(
            "controller", validation_id="val-789", approval_request_id="approval-2"
        )
    )
    assert first["status"] == "completed"
    assert result["status"] == "completed"
    assert len([c for c in calls if c[0] == "launch"]) == 2


@pytest.mark.asyncio
async def test_execute_validation_rejection_has_zero_controller_mutation(
    tmp_path, monkeypatch
):
    _, calls = _crucible_fixture(tmp_path, monkeypatch)

    def invalid(*args):
        return None, "unknown validation token"

    monkeypatch.setattr(benchmark_server, "_get_validated_runfile", invalid)
    result = json.loads(
        await benchmark_server.execute_benchmark(
            "controller", validation_id="bad", approval_request_id="approval-1"
        )
    )
    assert result["status"] == "rejected"
    assert not calls


@pytest.mark.asyncio
async def test_execute_prelaunch_controller_crash_is_takeover_safe(
    tmp_path, monkeypatch
):
    _, calls = _crucible_fixture(tmp_path, monkeypatch)

    class CrashingSSH:
        async def copy_to(self, *args, **kwargs):
            calls.append(("copy_to", "crash"))
            raise ConnectionError("controller lost before launch")

        async def run(self, *args, **kwargs):
            return _ControllerResult(stdout="GONE")

        async def run_with_progress(self, *args, **kwargs):
            calls.append(("launch", "must-not-run"))
            return _ControllerResult()

    async def ensure():
        benchmark_server._ssh = CrashingSSH()

    monkeypatch.setattr(benchmark_server, "_ensure_init", ensure)
    result = await _execute()
    assert result["status"] == "indeterminate"
    assert not [c for c in calls if c[0] == "launch"]


@pytest.mark.asyncio
async def test_execute_postlaunch_crash_is_indeterminate_and_replay_never_relaunches(
    tmp_path, monkeypatch
):
    _, calls = _crucible_fixture(
        tmp_path, monkeypatch, run_error=ConnectionError("lost response after launch")
    )
    with pytest.raises(ConnectionError):
        await _execute()
    replay = await _execute()
    assert replay["status"] == "indeterminate"
    assert replay["reason_code"] == "indeterminate"
    assert replay["existing_operation"] is True
    assert replay["operation"]["terminal_outcome"] == "indeterminate"
    assert len([c for c in calls if c[0] == "launch"]) == 1
    assert any(
        c[0] == "pause" and c[1]["status"] == "awaiting_customer_guidance"
        for c in calls
    )


@pytest.mark.asyncio
async def test_execute_mcp_reconnect_same_correlation_replays_without_launch(
    tmp_path, monkeypatch
):
    _, calls = _crucible_fixture(tmp_path, monkeypatch)
    monkeypatch.setenv("AGENTIC_PERF_ORCHESTRATOR_SESSION_ID", "session-788")
    monkeypatch.setenv("AGENTIC_PERF_ORCHESTRATOR_EPOCH", "7")
    monkeypatch.setenv("AGENTIC_PERF_REQUEST_ID", "mcp-request-788")
    first = await _execute()
    # Simulate a new MCP delivery with the same causal correlation but a new worker.
    monkeypatch.setenv("AGENTIC_PERF_INSTANCE_NAME", "reconnected-worker")
    second = await _execute()
    assert first["status"] == second["status"] == "completed"
    assert second["existing_operation"] is True
    assert len([c for c in calls if c[0] == "launch"]) == 1


@pytest.mark.asyncio
async def test_execute_terminal_failure_survives_restart_and_external_identity_is_durable(
    tmp_path, monkeypatch
):
    run_result = _ControllerResult(
        exit_code=1,
        stdout="Results stored in: /var/lib/crucible/run/uperf--12345678-1234-1234-1234-123456789abc\n",
        stderr="benchmark failed",
    )
    _, calls = _crucible_fixture(tmp_path, monkeypatch, run_result=run_result)
    first = await _execute()
    assert first["status"] == "failed"
    assert first["run_id"] == "12345678-1234-1234-1234-123456789abc"
    assert first["run_dir"].startswith("/var/lib/crucible/run/")
    # A fresh server process (new module operation adapter) still gets cached failure.
    benchmark_server._initialized = False
    second = await _execute()
    assert second["status"] == "failed"
    assert second["existing_operation"] is True
    assert len([c for c in calls if c[0] == "launch"]) == 1
