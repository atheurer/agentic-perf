"""Durable benchmark execution intent and launch-claim tests."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import agents.benchmark.server as benchmark_server
import agents.server_utils as server_utils
from agents.benchmark.server import _benchmark_intent_identity, _BenchmarkOperation
from tests.test_benchmark_tools import BenchmarkMCP


def _set_crucible_context(monkeypatch, ticket):
    """Provide the ticket-bound harness context normally loaded by init."""
    monkeypatch.setattr(benchmark_server, "_ticket", ticket)
    monkeypatch.setattr(
        benchmark_server,
        "_skill_provider",
        SimpleNamespace(default_harness="crucible"),
    )


def _crucible_fixture(
    tmp_path,
    monkeypatch,
    *,
    launch=None,
    run_error=None,
    prepare_error=None,
    statuses=None,
):
    """Wire the real execute handler to a deterministic contract-v3 controller."""
    import paths

    monkeypatch.setattr(paths, "TRACE_DB_PATH", tmp_path / "trace.db")
    monkeypatch.delenv("AGENTIC_PERF_API_TOKEN", raising=False)
    monkeypatch.setenv("TICKET_ID", "T-788")
    record = {
        "validation_id": "val-788",
        "attempt_id": "attempt-1",
        "execution_intent_id": "intent-1",
        "run_file": {"benchmarks": [{"name": "uperf"}], "tags": {}},
        "runfile_fingerprint": benchmark_server._runfile_fingerprint(
            {"benchmarks": [{"name": "uperf"}], "tags": {}}
        ),
        "harness": "crucible",
        "controller": "controller",
        "params_fingerprint": "no-mv-params",
        "execution_plan_fingerprint": benchmark_server._execution_plan_fingerprint({}),
        "run_command": "crucible run",
        "execution_transport": "crucible_mcp",
        "mcp_contract_version": "3",
        "crucible_idempotency_key": "T-788:crucible:val-788",
        "state": "executable",
    }
    ticket = {
        "id": "T-788",
        "status": "executing_benchmark",
        "custom_fields": {"benchmark_validations": {"records": {"val-788": record}}},
    }
    _set_crucible_context(monkeypatch, ticket)
    calls = []

    class TicketBenchmarkMCP(BenchmarkMCP):
        async def call_tool(self, name: str, arguments: dict) -> str:
            if name == "start_run":
                calls.append(("launch", arguments))
            return await super().call_tool(name, arguments)

    adapter = TicketBenchmarkMCP(
        launch=launch,
        run_error=run_error,
        prepare_error=prepare_error,
        statuses=statuses,
    )
    monkeypatch.setattr(benchmark_server, "_crucible_mcp", adapter)

    async def ensure_init():
        benchmark_server._ssh = object()

    async def active_check(**kwargs):
        return ticket

    monkeypatch.setattr(benchmark_server, "_ensure_init", ensure_init)
    monkeypatch.setattr(server_utils, "assert_ticket_active", active_check)
    monkeypatch.setattr(server_utils, "_state_store_token", lambda: "")
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


async def _wait_for_event_or_producer(
    event: asyncio.Event,
    producer: asyncio.Task,
    *,
    operation: str,
    timeout: float = 5.0,
) -> None:
    """Wait for a signal, or fail promptly if its producer returns first."""
    event_waiter = asyncio.create_task(event.wait())
    done, _ = await asyncio.wait(
        {event_waiter, producer},
        timeout=timeout,
        return_when=asyncio.FIRST_COMPLETED,
    )
    if event.is_set():
        await asyncio.gather(event_waiter, return_exceptions=True)
        return

    event_waiter.cancel()
    await asyncio.gather(event_waiter, return_exceptions=True)
    if producer in done:
        result = await producer
        pytest.fail(
            f"{operation} producer completed before signaling its start event: "
            f"{result!r}"
        )

    producer.cancel()
    try:
        await asyncio.wait_for(producer, timeout=timeout)
    except asyncio.CancelledError:
        pass
    pytest.fail(f"{operation} did not signal its start event within {timeout}s")


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
    assert not await operation.terminalize({}, "complete", {"result": "ok"})
    assert calls == ["complete", "indeterminate"]


@pytest.mark.asyncio
async def test_execute_rejects_invalid_validation_without_controller_mutation(
    monkeypatch,
) -> None:
    ticket = {
        "id": "T-4",
        "status": "executing_benchmark",
        "custom_fields": {"benchmark_validations": {"records": {}}},
    }
    _set_crucible_context(monkeypatch, ticket)

    async def no_init() -> None:
        benchmark_server._ssh = type("Unused", (), {})()

    async def active(**_kwargs: object) -> dict:
        return ticket

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
        "run_file": {"benchmark": "demo", "tags": {}},
        "runfile_fingerprint": "fp",
        "harness": "crucible",
        "controller": "controller",
        "params_fingerprint": "no-mv-params",
        "execution_plan_fingerprint": "x",
        "run_command": "crucible run",
        "execution_transport": "crucible_mcp",
        "mcp_contract_version": "3",
        "crucible_idempotency_key": "external:crucible:val-1",
        "state": "executable",
    }
    active = {"id": "", "status": "executing_benchmark", "custom_fields": {}}
    calls: list[str] = []
    _set_crucible_context(monkeypatch, active)
    monkeypatch.setattr(benchmark_server, "_crucible_mcp", BenchmarkMCP())

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
        def __init__(self, _key, request_hash, _owner):
            self.request_hash = request_hash

        async def acquire(self):
            calls.append("acquire")
            return (
                {"result_descriptor": {"benchmark_result": {"status": "completed"}}},
                "terminal",
            )

        async def peek_terminal(self):
            return {
                "state": "terminal",
                "request_hash": self.request_hash,
                "result_descriptor": {"benchmark_result": {"status": "completed"}},
            }

        async def close(self):
            calls.append("close")

    monkeypatch.setattr(
        benchmark_server,
        "_BenchmarkOperation",
        lambda key, request_hash, owner: DuplicateOperation(key, request_hash, owner),
    )
    result = await benchmark_server.execute_benchmark(
        "controller", validation_id="val-1"
    )
    assert '"existing_operation": true' in result
    # The duplicate replay resolves through the read-only terminal peek
    # (#1425) — no acquire, no lease, no close.
    assert calls == []


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
    try:
        await _wait_for_event_or_producer(
            entered, first_task, operation="benchmark launch"
        )
        monkeypatch.setenv("AGENTIC_PERF_INSTANCE_NAME", "worker-2")
        second = await _execute()
        assert second["status"] == "rejected"
        assert second["reason_code"] == "existing_operation"
        release.set()
        first = await first_task
    finally:
        release.set()
        if not first_task.done():
            first_task.cancel()
        await asyncio.gather(first_task, return_exceptions=True)
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
    record["crucible_idempotency_key"] = "T-788:crucible:val-789"
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
    _, calls = _crucible_fixture(
        tmp_path,
        monkeypatch,
        prepare_error=ConnectionError("controller lost before planning"),
    )
    result = await _execute()
    assert result["status"] == "failed"
    assert result["reason_code"] == "mcp_execution_failed"
    assert not [c for c in calls if c[0] == "launch"]


@pytest.mark.asyncio
async def test_execute_postlaunch_crash_is_indeterminate_and_replay_never_relaunches(
    tmp_path, monkeypatch
):
    _, calls = _crucible_fixture(
        tmp_path, monkeypatch, run_error=ConnectionError("lost response after launch")
    )
    first = await _execute()
    assert first["status"] == "indeterminate"
    assert first["reason_code"] == "mcp_execution_outcome_unknown"
    replay = await _execute()
    assert replay["status"] == "indeterminate"
    assert replay["reason_code"] == "mcp_execution_outcome_unknown"
    assert replay["existing_operation"] is True
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
    _, calls = _crucible_fixture(
        tmp_path,
        monkeypatch,
        statuses=[
            {
                "state": "failed",
                "result_status": "available",
                "error_category": "benchmark_failed",
                "exit_code": 1,
                "cdm_run_id": "cdm-run-1",
                "rickshaw_run_id": "rickshaw-run-1",
                "run_directory": "/var/lib/crucible/run/one",
            }
        ],
    )
    first = await _execute()
    assert first["status"] == "failed"
    assert first["run_id"] == "cdm-run-1"
    assert first["crucible_mcp_job_id"] == "mcp-job-1"
    assert first["run_path"] == "/var/lib/crucible/run/one"
    # A fresh server process (new module operation adapter) still gets cached failure.
    benchmark_server._initialized = False
    second = await _execute()
    assert second["status"] == "failed"
    assert second["existing_operation"] is True
    assert len([c for c in calls if c[0] == "launch"]) == 1


class TestControllerMcpHealthCheck:
    """#1425: the controller's Crucible MCP service is health-checked
    before validation (read-only) and before execution (start-if-stopped
    with a clear remediation error)."""

    @staticmethod
    def _wire_health(monkeypatch, health_call):
        class _Adapter:
            controller = "controller"
            contract_info = {}

            def endpoint(self):
                return "http://controller:8889/mcp"

            async def health_check(self):
                await health_call()

            async def call_tool(self, name, arguments):
                raise AssertionError("health check should gate tool calls")

        monkeypatch.setattr(benchmark_server, "_crucible_mcp", _Adapter())
        benchmark_server._ticket.setdefault("custom_fields", {})["ssh_hardware_ips"] = {
            "controller": "controller"
        }

    @pytest.mark.asyncio
    async def test_validation_reports_unreachable_service_without_starting_it(
        self, tmp_path, monkeypatch
    ):
        _crucible_fixture(tmp_path, monkeypatch)
        ssh_calls: list[str] = []

        class _NoStartSSH:
            async def run(self, host, command, **kw):
                ssh_calls.append(command)
                raise AssertionError("validation must not start services")

        benchmark_server._ssh = _NoStartSSH()

        async def unreachable():
            raise ConnectionError("connection refused")

        self._wire_health(monkeypatch, unreachable)
        result = json.loads(
            await benchmark_server.validate_benchmark(
                "controller", {"tags": {}, "benchmarks": [{"name": "uperf"}]}
            )
        )
        assert result["status"] == "failed"
        assert result["reason_code"] == "controller_mcp_unavailable"
        assert "crucible start mcp-server" in result["message"]
        assert ssh_calls == []

    @pytest.mark.asyncio
    async def test_configuration_failure_is_not_restarted(self, tmp_path, monkeypatch):
        from agents.crucible_mcp import CrucibleMCPError

        _crucible_fixture(tmp_path, monkeypatch)

        class _NoStartSSH:
            async def run(self, host, command, **kw):
                raise AssertionError("configuration errors must not restart")

        benchmark_server._ssh = _NoStartSSH()

        async def bad_config():
            raise CrucibleMCPError("invalid_configuration", "bad config")

        self._wire_health(monkeypatch, bad_config)
        result = json.loads(
            await benchmark_server.validate_benchmark(
                "controller", {"tags": {}, "benchmarks": [{"name": "uperf"}]}
            )
        )
        assert result["reason_code"] == "controller_mcp_unavailable"
        assert "configuration" in result["message"]

    @pytest.mark.asyncio
    async def test_execution_starts_stopped_service_and_proceeds(
        self, tmp_path, monkeypatch
    ):
        _crucible_fixture(tmp_path, monkeypatch)

        async def _keep_ssh():
            # The fixture's ensure_init replaces _ssh with a bare object.
            return None

        monkeypatch.setattr(benchmark_server, "_ensure_init", _keep_ssh)
        start_calls: list[tuple[str, str]] = []

        class _StartSSH:
            async def run(self, host, command, timeout=300, mutating=None, **kw):
                start_calls.append((host, command))
                from types import SimpleNamespace as _NS

                return _NS(exit_code=0, stdout="", stderr="")

        benchmark_server._ssh = _StartSSH()

        # Simulate: first health check fails, the start succeeds, and the
        # second health check passes.
        check_count = {"n": 0}

        async def flaky_health():
            check_count["n"] += 1
            if check_count["n"] == 1:
                raise ConnectionError("connection refused")
            # subsequent checks: the adapter is healthy

        class _Adapter:
            controller = "controller"
            contract_info = {"version": "3"}

            def endpoint(self):
                return "http://controller:8889/mcp"

            async def health_check(self):
                await flaky_health()

            async def call_tool(self, name, arguments):
                # After remediation the run proceeds through the fixture
                # adapter; this stand-in only proves the gate opened.
                raise ConnectionError("should use fixture adapter")

        # Keep the fixture's MCP adapter for execution; only the health
        # stand-in is needed until remediation succeeds. Simplest: patch
        # health on the real flow via the stand-in, then restore.
        monkeypatch.setattr(benchmark_server, "_crucible_mcp", _Adapter())
        result = json.loads(
            await benchmark_server.execute_benchmark(
                "controller", validation_id="val-788", approval_request_id="apr-1"
            )
        )
        assert start_calls == [("controller", "crucible start mcp-server")]
        # The second health check passed, so the preflight returned None
        # and execution continued (it fails later only because the
        # stand-in adapter cannot run tools — not with the preflight
        # error).
        assert result.get("reason_code") != "controller_mcp_unavailable"

    @pytest.mark.asyncio
    async def test_execution_reports_clear_error_when_start_fails(
        self, tmp_path, monkeypatch
    ):
        _crucible_fixture(tmp_path, monkeypatch)

        async def _keep_ssh():
            return None

        monkeypatch.setattr(benchmark_server, "_ensure_init", _keep_ssh)

        class _FailingStartSSH:
            async def run(self, host, command, timeout=300, mutating=None, **kw):
                from types import SimpleNamespace as _NS

                return _NS(exit_code=1, stdout="", stderr="unit is masked")

        benchmark_server._ssh = _FailingStartSSH()

        async def unreachable():
            raise ConnectionError("connection refused")

        self._wire_health(monkeypatch, unreachable)
        result = json.loads(
            await benchmark_server.execute_benchmark(
                "controller", validation_id="val-788", approval_request_id="apr-1"
            )
        )
        assert result["reason_code"] == "controller_mcp_unavailable"
        assert "crucible start mcp-server" in result["message"]
        assert "unit is masked" in result["message"]

    @pytest.mark.asyncio
    async def test_http_auth_failure_is_not_restarted(self, tmp_path, monkeypatch):
        import httpx

        _crucible_fixture(tmp_path, monkeypatch)

        class _NoStartSSH:
            async def run(self, host, command, **kw):
                raise AssertionError("auth failures must not restart the service")

        benchmark_server._ssh = _NoStartSSH()

        async def unauthorized():
            request = httpx.Request("POST", "http://controller:8889/mcp")
            raise httpx.HTTPStatusError(
                "401", request=request, response=httpx.Response(401)
            )

        self._wire_health(monkeypatch, unauthorized)
        result = json.loads(
            await benchmark_server.execute_benchmark(
                "controller", validation_id="val-788", approval_request_id="apr-1"
            )
        )
        assert result["reason_code"] == "controller_mcp_unavailable"
        # The service is running (it answered 401); the error points at
        # configuration, not at starting the service.
        assert "retry" not in result["message"]

    @pytest.mark.asyncio
    async def test_endpoint_override_never_auto_starts(self, tmp_path, monkeypatch):
        _crucible_fixture(tmp_path, monkeypatch)
        start_calls: list[str] = []

        class _NoStartSSH:
            async def run(self, host, command, **kw):
                start_calls.append(command)
                from types import SimpleNamespace as _NS

                return _NS(exit_code=0, stdout="", stderr="")

        benchmark_server._ssh = _NoStartSSH()

        class _OverrideAdapter:
            controller = "controller"
            contract_info = {}

            def endpoint(self):
                # An override pointing elsewhere: the assigned controller
                # is not where this service lives.
                return "http://mcp-proxy.example:8889/mcp"

            async def health_check(self):
                raise ConnectionError("connection refused")

        monkeypatch.setattr(benchmark_server, "_crucible_mcp", _OverrideAdapter())
        result = json.loads(
            await benchmark_server.execute_benchmark(
                "controller", validation_id="val-788", approval_request_id="apr-1"
            )
        )
        assert result["reason_code"] == "controller_mcp_unavailable"
        assert start_calls == []


async def test_terminal_replay_requires_matching_request_hash(tmp_path, monkeypatch):
    """#1425: the read-only peek must enforce the request-hash binding the
    acquire path enforces — a different controller or command must not
    replay an old terminal result."""
    _crucible_fixture(tmp_path, monkeypatch)

    class _MismatchedOperation:
        def __init__(self, key, request_hash, owner):
            self.key = key
            self.request_hash = request_hash

        async def peek_terminal(self):
            return {
                "state": "terminal",
                "request_hash": "a-different-hash-entirely",
                "result_descriptor": {"benchmark_result": {"status": "completed"}},
            }

        async def acquire(self):
            return ({"external_ids": {}}, "acquired")

        async def transition(self, _action, record, **kwargs):
            return record

        async def terminalize(self, record, _outcome, descriptor=None):
            return True

        async def start_renewal(self, _record):
            return None

        async def close(self):
            return None

    monkeypatch.setattr(benchmark_server, "_BenchmarkOperation", _MismatchedOperation)
    result = json.loads(
        await benchmark_server.execute_benchmark(
            "controller", validation_id="val-788", approval_request_id="apr-1"
        )
    )
    # No replay of the mismatched terminal record; execution continued
    # down its normal validation path (which rejects in the fixture).
    assert result.get("existing_operation") is not True
