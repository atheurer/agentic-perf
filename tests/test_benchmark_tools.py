from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import pytest

from agents.benchmark.server import (
    _apply_runfile_safety_directives,
    _validate_run_command,
)
from agents.mcp_client import MCPToolCallError
from providers.skills.base import RunfileTemplate
from tests.conftest import MockSkillProvider, MockSSHExecutor, make_benchmark_handlers


@pytest.fixture(autouse=True)
def _crucible_ticket_context(tmp_path, monkeypatch):
    import agents.benchmark.server as server
    import paths

    monkeypatch.setattr(paths, "TRACE_DB_PATH", tmp_path / "trace.db")
    monkeypatch.delenv("AGENTIC_PERF_API_TOKEN", raising=False)
    monkeypatch.delenv("TICKET_ID", raising=False)

    monkeypatch.setattr(
        server,
        "_ticket",
        {"custom_fields": {"directives": {"harness": "crucible"}}},
    )


@pytest.fixture
def mock_provider() -> MockSkillProvider:
    return MockSkillProvider(
        private_config={
            "crucible": {
                "execution": {
                    "controller_required": True,
                    "run_command": "crucible run",
                    "endpoint_type": "remotehosts",
                    "endpoint_user": "root",
                    # A stale value must be ignored for Crucible.
                    "default_userenv": "fedora42",
                    "default_osruntime": "podman",
                    "pre_run": ["ssh_key_setup"],
                    "run_file_format": "json",
                    "results_dir_pattern": "/var/lib/crucible/run/*",
                },
            },
            "zathras": {
                "execution": {
                    "controller_required": True,
                    "run_command": "/opt/zathras/bin/burden",
                    "endpoint_type": "local",
                    "endpoint_user": "root",
                    "pre_run": ["ssh_key_setup"],
                    "run_file_format": "yaml_scenario",
                    "results_dir_pattern": "/tmp/results_*",
                },
            },
        },
        runfile_template=RunfileTemplate(
            benchmark="fio", template={"harness": "crucible"}
        ),
    )


@pytest.fixture
def handlers(mock_provider):
    return make_benchmark_handlers(
        ssh=MockSSHExecutor(),
        skill_provider=mock_provider,
    )


def test_no_host_mounts_directive_removes_the_key_from_nested_runfile(monkeypatch):
    import agents.benchmark.server as srv

    monkeypatch.setattr(
        srv,
        "_ticket",
        {"custom_fields": {"directives": {"no_host_mounts": True}}},
    )
    original = {
        "remotes": [
            {"config": {"settings": {"host-mounts": [], "disable-tools": True}}}
        ]
    }

    sanitized = _apply_runfile_safety_directives(original)

    assert "host-mounts" not in sanitized["remotes"][0]["config"]["settings"]
    assert sanitized["remotes"][0]["config"]["settings"]["disable-tools"] is True
    assert original["remotes"][0]["config"]["settings"]["host-mounts"] == []


def test_controller_host_prefers_ssh_address_to_assigned_address(monkeypatch):
    import agents.benchmark.server as srv

    monkeypatch.setattr(
        srv,
        "_ticket",
        {
            "custom_fields": {
                "ssh_hardware_ips": {"controller": "public-controller"},
                "assigned_hardware_ips": {"controller": "private-controller"},
            }
        },
    )

    assert srv._controller_host() == "public-controller"


@pytest.mark.asyncio
async def test_get_execution_config_crucible(handlers):
    result = await handlers["get_execution_config"](harness_name="crucible")
    assert result["found"] is True
    assert result["controller_required"] is True
    assert result["endpoint_type"] == "remotehosts"
    assert result["endpoint_user"] == "root"
    assert result["default_userenv"] == "fedora42"
    assert result["default_osruntime"] == "podman"
    assert result["run_file_format"] == "json"
    assert result["results_dir_pattern"] == "/var/lib/crucible/run/*"
    assert "run_command" not in result
    assert "pre_run" not in result


@pytest.mark.asyncio
async def test_get_execution_config_zathras(handlers):
    result = await handlers["get_execution_config"](harness_name="zathras")
    assert result["found"] is True
    assert result["harness"] == "zathras"
    assert result["run_command"] == "/opt/zathras/bin/burden"
    assert result["run_file_format"] == "yaml_scenario"


@pytest.mark.asyncio
async def test_get_execution_config_not_found(handlers):
    result = await handlers["get_execution_config"](harness_name="unknown_harness")
    assert result["found"] is False


@pytest.mark.asyncio
async def test_no_generate_run_file_tool(handlers):
    assert "generate_run_file" not in handlers


@pytest.mark.asyncio
async def test_no_validate_run_file_tool(handlers):
    assert "validate_run_file" not in handlers


@dataclass
class _CrucibleMCPSettings:
    result_readiness_timeout_seconds: int = 0


class BenchmarkMCP:
    contract_info = {
        "mcp_contract_version": "3",
        "execution_supported": True,
    }
    settings = _CrucibleMCPSettings()

    def __init__(
        self,
        *,
        valid: bool = True,
        errors: list[str] | None = None,
        statuses: list[dict] | None = None,
        expire_first_start: bool = False,
        validate_too_large: bool = False,
        prepare_too_large: bool = False,
        launch=None,
        run_error=None,
        prepare_error=None,
    ) -> None:
        self.valid = valid
        self.errors = errors or []
        self.statuses = statuses or [
            {
                "state": "completed",
                "result_status": "available",
                "cdm_run_id": "cdm-run-1",
                "rickshaw_run_id": "rickshaw-run-1",
                "run_directory": "/var/lib/crucible/run/one",
            }
        ]
        self.expire_first_start = expire_first_start
        self.validate_too_large = validate_too_large
        self.prepare_too_large = prepare_too_large
        self.launch = launch
        self.run_error = run_error
        self.prepare_error = prepare_error
        self.calls: list[tuple[str, dict]] = []
        self.prepare_count = 0
        self.start_count = 0
        self.jobs_by_key: dict[str, dict] = {}

    async def call_tool(self, name: str, arguments: dict) -> str:
        self.calls.append((name, arguments))
        if name == "validate_run":
            if self.validate_too_large:
                raise MCPToolCallError(
                    json.dumps({"code": "too_large"}), "request_too_large"
                )
            return json.dumps({"valid": self.valid, "errors": self.errors})
        if name == "prepare_run":
            if self.prepare_error is not None:
                raise self.prepare_error
            if self.prepare_too_large:
                raise MCPToolCallError(
                    json.dumps({"code": "too_large"}), "request_too_large"
                )
            self.prepare_count += 1
            return json.dumps(
                {
                    "validation": {"valid": True},
                    "plan_handle": f"plan-{self.prepare_count}",
                }
            )
        if name == "start_run":
            if self.launch is not None:
                await self.launch()
            if self.run_error is not None:
                raise self.run_error
            self.start_count += 1
            if self.expire_first_start and self.start_count == 1:
                raise MCPToolCallError(
                    json.dumps({"code": "expired_plan_handle"}),
                    "ambiguous_after_send",
                )
            key = arguments["idempotency_key"]
            job = self.jobs_by_key.get(key)
            if job is None:
                job = {"mcp_job_id": "mcp-job-1"}
                self.jobs_by_key[key] = job
            return json.dumps(
                {"created": job is not self.jobs_by_key.get(key), "job": job}
            )
        if name == "get_run_status":
            index = min(
                sum(1 for tool, _ in self.calls if tool == "get_run_status") - 1,
                len(self.statuses) - 1,
            )
            return json.dumps(self.statuses[index])
        if name == "get_run_logs":
            return json.dumps(
                {
                    "text": "progress line",
                    "next_offset": None,
                }
            )
        if name == "get_run_summary":
            return json.dumps({"summary": {"result": "pass"}})
        raise AssertionError(f"unexpected MCP tool {name}")


class ReusableOperationGuard:
    records: dict[str, dict] = {}

    def __init__(self, key: str, _request_hash: str, _owner: str) -> None:
        self.key = key

    async def acquire(self) -> tuple[dict, str]:
        return self.records.get(self.key, {"external_ids": {}}), "acquired"

    async def transition(self, _action: str, record: dict, **kwargs: object) -> dict:
        updated = dict(record)
        external_ids = kwargs.get("external_ids")
        if isinstance(external_ids, dict):
            updated["external_ids"] = {
                **(updated.get("external_ids") or {}),
                **external_ids,
            }
        self.records[self.key] = updated
        return updated

    async def terminalize(
        self, record: dict, _outcome: str, descriptor: dict | None = None
    ) -> bool:
        updated = dict(record)
        if descriptor is not None:
            updated["result_descriptor"] = descriptor
        self.records[self.key] = updated
        return True

    async def start_renewal(self, _record: dict) -> None:
        return None

    async def close(self) -> None:
        return None


@pytest.fixture
def benchmark_mcp(tmp_path, monkeypatch):
    import agents.benchmark.server as server
    import paths

    monkeypatch.setattr(paths, "TRACE_DB_PATH", tmp_path / "trace.db")
    monkeypatch.delenv("AGENTIC_PERF_API_TOKEN", raising=False)
    monkeypatch.delenv("TICKET_ID", raising=False)

    adapter = BenchmarkMCP()
    monkeypatch.setattr(server, "_crucible_mcp", adapter)
    monkeypatch.setattr(
        server,
        "_ticket",
        {
            "custom_fields": {
                "directives": {"harness": "crucible"},
                "assigned_hardware_ips": {"controller": "controller.example"},
            }
        },
    )
    return adapter


def _benchmark_handlers_with_mcp():
    return make_benchmark_handlers(
        ssh=MockSSHExecutor(),
        skill_provider=MockSkillProvider(),
    )


@pytest.mark.asyncio
async def test_validate_benchmark_uses_mcp_without_execution(benchmark_mcp):
    handlers = _benchmark_handlers_with_mcp()
    run_file = {
        "benchmarks": [{"name": "uperf", "mv-params": {}}],
        "tags": {},
    }

    result = await handlers["validate_benchmark"](
        controller="controller.example",
        run_file=run_file,
        harness="crucible",
    )

    assert result["status"] == "valid"
    assert result["valid"] is True
    assert result["validation_id"].startswith("val-")
    assert benchmark_mcp.calls == [("validate_run", {"document": run_file})]


@pytest.mark.asyncio
async def test_validate_benchmark_maps_structured_mcp_errors(monkeypatch):
    import agents.benchmark.server as server

    adapter = BenchmarkMCP(valid=False, errors=["invalid endpoint schema"])
    monkeypatch.setattr(server, "_crucible_mcp", adapter)
    monkeypatch.setattr(
        server,
        "_ticket",
        {
            "custom_fields": {
                "directives": {"harness": "crucible"},
                "assigned_hardware_ips": {"controller": "controller.example"},
            }
        },
    )
    handlers = _benchmark_handlers_with_mcp()

    result = await handlers["validate_benchmark"](
        controller="controller.example",
        run_file={"bad": True, "tags": {}},
        harness="crucible",
    )

    assert result["status"] == "invalid"
    assert result["valid"] is False
    assert result["errors"] == ["invalid endpoint schema"]
    assert adapter.calls[0][0] == "validate_run"


@pytest.mark.asyncio
async def test_validate_benchmark_rejects_missing_tags_before_controller_io():
    ssh = MockSSHExecutor()
    handlers = make_benchmark_handlers(
        ssh=ssh,
        skill_provider=MockSkillProvider(),
    )

    result = await handlers["validate_benchmark"](
        controller="10.0.0.1",
        run_file={"benchmarks": [{"name": "uperf", "mv-params": {}}]},
        harness="crucible",
    )

    assert result["status"] == "invalid"
    assert result["valid"] is False
    assert result["errors"] == ["missing required top-level key: tags"]
    assert ssh.calls == []


@pytest.mark.asyncio
async def test_validate_benchmark_rejects_unsupported_harness(handlers):
    result = await handlers["validate_benchmark"](
        controller="10.0.0.1", run_file={}, harness="zathras"
    )

    assert result["status"] == "unsupported"
    assert result["valid"] is False


@pytest.mark.asyncio
async def test_crucible_execution_uses_prepare_start_and_distinct_ids(benchmark_mcp):
    handlers = _benchmark_handlers_with_mcp()
    run_file = {
        "benchmarks": [{"name": "uperf", "mv-params": {}}],
        "tags": {},
    }
    validation_id = (
        await handlers["validate_benchmark"](
            controller="controller.example",
            run_file=run_file,
            harness="crucible",
        )
    )["validation_id"]
    result = await handlers["execute_benchmark"](
        controller="controller.example",
        validation_id=validation_id,
        harness="crucible",
    )

    assert result["status"] == "completed"
    assert result["execution_transport"] == "crucible_mcp"
    assert result["mcp_contract_version"] == "3"
    assert result["crucible_mcp_job_id"] == "mcp-job-1"
    assert result["crucible_idempotency_key"].endswith(f":crucible:{validation_id}")
    assert result["run_id"] == "cdm-run-1"
    assert result["rickshaw_run_id"] == "rickshaw-run-1"
    assert result["run_path"] == "/var/lib/crucible/run/one"
    assert result["result_summary"] == {"result": "pass"}
    tool_names = [name for name, _ in benchmark_mcp.calls]
    assert tool_names == [
        "validate_run",
        "prepare_run",
        "start_run",
        "get_run_status",
        "get_run_summary",
    ]
    prepare = benchmark_mcp.calls[1]
    start = benchmark_mcp.calls[2]
    assert prepare[1] == {"document": run_file, "persist": True}
    assert start[1] == {
        "idempotency_key": result["crucible_idempotency_key"],
        "document": run_file,
        "plan_handle": "plan-1",
    }


@pytest.mark.asyncio
async def test_crucible_execution_persists_top_level_status_identifiers(
    monkeypatch, benchmark_mcp
):
    import agents.benchmark.server as server

    class CountingOperationGuard(ReusableOperationGuard):
        external_id_transitions = 0

        async def transition(self, action: str, record: dict, **kwargs: object):
            if action == "external-id":
                CountingOperationGuard.external_id_transitions += 1
            return await super().transition(action, record, **kwargs)

    ReusableOperationGuard.records = {}
    CountingOperationGuard.external_id_transitions = 0
    benchmark_mcp.statuses = [
        {
            "state": "running",
            "result_status": "not_available",
            "cdm_run_id": "cdm-run-1",
            "rickshaw_run_id": "rickshaw-run-1",
            "run_directory": "/var/lib/crucible/run/one",
        },
        {
            "state": "completed",
            "result_status": "available",
            "cdm_run_id": "cdm-run-1",
            "rickshaw_run_id": "rickshaw-run-1",
            "run_directory": "/var/lib/crucible/run/one",
        },
    ]

    async def immediate_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(server.asyncio, "sleep", immediate_sleep)
    monkeypatch.setattr(server, "_BenchmarkOperation", CountingOperationGuard)
    handlers = _benchmark_handlers_with_mcp()
    validation_id = (
        await handlers["validate_benchmark"](
            controller="controller.example",
            run_file={
                "benchmarks": [{"name": "uperf", "mv-params": {}}],
                "tags": {},
            },
            harness="crucible",
        )
    )["validation_id"]

    result = await handlers["execute_benchmark"](
        controller="controller.example",
        validation_id=validation_id,
        harness="crucible",
    )

    assert result["status"] == "completed"
    assert result["run_id"] == "cdm-run-1"
    assert result["cdm_run_id"] == "cdm-run-1"
    assert result["rickshaw_run_id"] == "rickshaw-run-1"
    assert result["run_path"] == "/var/lib/crucible/run/one"
    assert len(ReusableOperationGuard.records) == 1
    external_ids = next(iter(ReusableOperationGuard.records.values()))["external_ids"]
    assert external_ids["crucible_mcp_job_id"] == "mcp-job-1"
    assert external_ids["cdm_run_id"] == "cdm-run-1"
    assert external_ids["run_id"] == "cdm-run-1"
    assert external_ids["rickshaw_run_id"] == "rickshaw-run-1"
    assert external_ids["run_path"] == "/var/lib/crucible/run/one"
    assert CountingOperationGuard.external_id_transitions == 3


@pytest.mark.asyncio
async def test_crucible_execution_rejects_substituted_run_document(benchmark_mcp):
    handlers = _benchmark_handlers_with_mcp()
    validation_id = (
        await handlers["validate_benchmark"](
            controller="controller.example",
            run_file={
                "benchmarks": [{"name": "uperf", "mv-params": {}}],
                "tags": {},
            },
            harness="crucible",
        )
    )["validation_id"]

    result = await handlers["execute_benchmark"](
        controller="controller.example",
        validation_id=validation_id,
        harness="crucible",
        run_file={
            "benchmarks": [{"name": "uperf", "mv-params": {"changed": "1"}}],
            "tags": {},
        },
    )

    assert result["status"] == "rejected"
    assert "does not accept a runfile" in result["message"]
    assert [name for name, _ in benchmark_mcp.calls] == ["validate_run"]


@pytest.mark.asyncio
async def test_crucible_execution_rejects_legacy_validation(benchmark_mcp, monkeypatch):
    import agents.benchmark.server as server

    handlers = _benchmark_handlers_with_mcp()
    validation_id = (
        await handlers["validate_benchmark"](
            controller="controller.example",
            run_file={
                "benchmarks": [{"name": "uperf", "mv-params": {}}],
                "tags": {},
            },
            harness="crucible",
        )
    )["validation_id"]
    record = server._validation_records[validation_id]
    record["execution_transport"] = ""
    record["crucible_idempotency_key"] = ""

    result = await handlers["execute_benchmark"](
        controller="controller.example",
        validation_id=validation_id,
        harness="crucible",
    )

    assert result["status"] == "rejected"
    assert result["reason_code"] == "mcp_revalidation_required"
    assert [name for name, _ in benchmark_mcp.calls] == ["validate_run"]


@pytest.mark.asyncio
async def test_in_flight_legacy_cli_run_is_not_relaunched(benchmark_mcp):
    import agents.benchmark.server as server

    handlers = _benchmark_handlers_with_mcp()
    run_file = {
        "benchmarks": [{"name": "uperf", "mv-params": {}}],
        "tags": {},
    }
    validation_id = "val-legacy-active"
    record = {
        "validation_id": validation_id,
        "run_file": run_file,
        "runfile_fingerprint": server._runfile_fingerprint(run_file),
        "harness": "crucible",
        "controller": "controller.example",
        "params_fingerprint": server._compute_params_fingerprint({}),
        "execution_plan_fingerprint": server._execution_plan_fingerprint({}),
        "run_command": "crucible run",
        "state": "executable",
    }
    server._validation_records[validation_id] = record
    intent_key, intent_hash, _ = server._benchmark_intent_identity(
        "external",
        validation_id,
        record,
        "controller.example",
        "crucible",
        "crucible run",
    )
    operation = server._BenchmarkOperation(intent_key, intent_hash, "legacy-worker")
    operation_record, acquisition = await operation.acquire()
    assert acquisition == "acquired"
    operation_record = await operation.transition("prepared", operation_record)
    operation_record = await operation.transition(
        "side-effect-started", operation_record
    )
    await operation.close()

    result = await handlers["execute_benchmark"](
        controller="controller.example",
        validation_id=validation_id,
        harness="crucible",
    )

    assert result["status"] == "existing_operation"
    assert result["legacy_execution"] is True
    assert result["operation_id"] == intent_key
    assert "was not relaunched" in result["message"]
    assert benchmark_mcp.calls == []


@pytest.mark.asyncio
async def test_expired_plan_handle_retry_preserves_idempotency_key(monkeypatch):
    import agents.benchmark.server as server

    adapter = BenchmarkMCP(expire_first_start=True)
    monkeypatch.setattr(server, "_crucible_mcp", adapter)
    monkeypatch.setattr(
        server,
        "_ticket",
        {
            "custom_fields": {
                "directives": {"harness": "crucible"},
                "assigned_hardware_ips": {"controller": "controller.example"},
            }
        },
    )
    handlers = _benchmark_handlers_with_mcp()
    run_file = {
        "benchmarks": [{"name": "uperf", "mv-params": {}}],
        "tags": {},
    }
    validation_id = (
        await handlers["validate_benchmark"](
            controller="controller.example",
            run_file=run_file,
            harness="crucible",
        )
    )["validation_id"]

    result = await handlers["execute_benchmark"](
        controller="controller.example",
        validation_id=validation_id,
        harness="crucible",
    )

    assert result["status"] == "completed"
    starts = [arguments for name, arguments in adapter.calls if name == "start_run"]
    assert len(starts) == 2
    assert {item["idempotency_key"] for item in starts} == {
        result["crucible_idempotency_key"]
    }
    assert all(item["document"] == run_file for item in starts)
    assert [item["plan_handle"] for item in starts] == ["plan-1", "plan-2"]


@pytest.mark.asyncio
async def test_duplicate_start_returns_existing_job(monkeypatch, benchmark_mcp):
    import agents.benchmark.server as server

    ReusableOperationGuard.records = {}
    monkeypatch.setattr(
        server,
        "_BenchmarkOperation",
        ReusableOperationGuard,
    )
    handlers = _benchmark_handlers_with_mcp()
    run_file = {
        "benchmarks": [{"name": "uperf", "mv-params": {}}],
        "tags": {},
    }
    validation_id = (
        await handlers["validate_benchmark"](
            controller="controller.example",
            run_file=run_file,
            harness="crucible",
        )
    )["validation_id"]

    first = await handlers["execute_benchmark"](
        controller="controller.example",
        validation_id=validation_id,
        harness="crucible",
    )
    second = await handlers["execute_benchmark"](
        controller="controller.example",
        validation_id=validation_id,
        harness="crucible",
    )

    assert first["crucible_mcp_job_id"] == second["crucible_mcp_job_id"]
    starts = [
        arguments for name, arguments in benchmark_mcp.calls if name == "start_run"
    ]
    assert len(starts) == 2
    assert starts[0] == starts[1]


@pytest.mark.asyncio
async def test_crucible_polling_fetches_bounded_logs(monkeypatch, benchmark_mcp):
    import agents.benchmark.server as server

    benchmark_mcp.statuses = [
        {"state": "running", "result_status": "not_available"},
        {"state": "completed", "result_status": "available"},
    ]

    async def immediate_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(server.asyncio, "sleep", immediate_sleep)
    handlers = _benchmark_handlers_with_mcp()
    validation_id = (
        await handlers["validate_benchmark"](
            controller="controller.example",
            run_file={
                "benchmarks": [{"name": "uperf", "mv-params": {}}],
                "tags": {},
            },
            harness="crucible",
        )
    )["validation_id"]

    result = await handlers["execute_benchmark"](
        controller="controller.example",
        validation_id=validation_id,
        harness="crucible",
    )

    assert result["status"] == "completed"
    logs = [
        arguments for name, arguments in benchmark_mcp.calls if name == "get_run_logs"
    ]
    assert logs == [
        {
            "mcp_job_id": "mcp-job-1",
            "offset": 0,
            "limit": 65_536,
        }
    ]


@pytest.mark.asyncio
async def test_validate_benchmark_maps_inline_document_too_large(monkeypatch):
    import agents.benchmark.server as server

    adapter = BenchmarkMCP(validate_too_large=True)
    monkeypatch.setattr(server, "_crucible_mcp", adapter)
    monkeypatch.setattr(
        server,
        "_ticket",
        {
            "custom_fields": {
                "directives": {"harness": "crucible"},
                "assigned_hardware_ips": {"controller": "controller.example"},
            }
        },
    )
    handlers = _benchmark_handlers_with_mcp()

    result = await handlers["validate_benchmark"](
        controller="controller.example",
        run_file={
            "benchmarks": [{"name": "uperf", "mv-params": {}}],
            "tags": {},
        },
        harness="crucible",
    )

    assert result["status"] == "failed"
    assert result["valid"] is False
    assert result["reason_code"] == "run_document_too_large"
    assert [name for name, _ in adapter.calls] == ["validate_run"]


@pytest.mark.asyncio
async def test_execute_maps_inline_document_too_large(monkeypatch):
    import agents.benchmark.server as server

    adapter = BenchmarkMCP(prepare_too_large=True)
    monkeypatch.setattr(server, "_crucible_mcp", adapter)
    monkeypatch.setattr(
        server,
        "_ticket",
        {
            "custom_fields": {
                "directives": {"harness": "crucible"},
                "assigned_hardware_ips": {"controller": "controller.example"},
            }
        },
    )
    handlers = _benchmark_handlers_with_mcp()
    validation_id = (
        await handlers["validate_benchmark"](
            controller="controller.example",
            run_file={
                "benchmarks": [{"name": "uperf", "mv-params": {}}],
                "tags": {},
            },
            harness="crucible",
        )
    )["validation_id"]

    result = await handlers["execute_benchmark"](
        controller="controller.example",
        validation_id=validation_id,
        harness="crucible",
    )

    assert result["status"] == "failed"
    assert result["reason_code"] == "run_document_too_large"
    assert [name for name, _ in adapter.calls if name == "start_run"] == []


@pytest.mark.asyncio
async def test_ticket_cancellation_stops_polling_without_stopping_crucible(
    monkeypatch, benchmark_mcp
):
    import agents.benchmark.server as server

    benchmark_mcp.statuses = [{"state": "running", "result_status": "not_available"}]

    async def cancel_during_first_sleep(_delay: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(server.asyncio, "sleep", cancel_during_first_sleep)
    ReusableOperationGuard.records = {}
    monkeypatch.setattr(server, "_BenchmarkOperation", ReusableOperationGuard)
    handlers = _benchmark_handlers_with_mcp()
    validation_id = (
        await handlers["validate_benchmark"](
            controller="controller.example",
            run_file={
                "benchmarks": [{"name": "uperf", "mv-params": {}}],
                "tags": {},
            },
            harness="crucible",
        )
    )["validation_id"]

    with pytest.raises(asyncio.CancelledError):
        await handlers["execute_benchmark"](
            controller="controller.example",
            validation_id=validation_id,
            harness="crucible",
        )

    assert [name for name, _ in benchmark_mcp.calls].count("get_run_status") == 1
    assert [name for name, _ in benchmark_mcp.calls].count("get_run_logs") == 1
    record = next(iter(ReusableOperationGuard.records.values()))
    descriptor = record["result_descriptor"]
    assert descriptor["reason_code"] == "ticket_cancelled_during_polling"
    assert descriptor["polling_stopped"] is True
    assert descriptor["crucible_stopped"] is False


@pytest.mark.asyncio
async def test_crucible_run_paths_do_not_fall_back_to_ssh(monkeypatch, benchmark_mcp):
    handlers = _benchmark_handlers_with_mcp()

    result = await handlers["get_run_logs"](
        controller="controller.example",
        run_id="/var/lib/crucible/run/one",
        harness="crucible",
    )

    assert result["status"] == "error"
    assert result["reason"] == "invalid_run_id"
    assert benchmark_mcp.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state,result_status,expected,reason",
    [
        ("failed", "not_available", "failed", "crucible_failed"),
        ("recovery_required", "not_available", "indeterminate", "recovery_required"),
        ("completed", "partial", "partial", "result_partial"),
        ("completed", "unavailable", "unavailable", "result_unavailable"),
        ("completed", "pending", "indeterminate", "result_readiness_timeout"),
    ],
)
async def test_crucible_terminal_and_readiness_states(
    monkeypatch, state, result_status, expected, reason
):
    import agents.benchmark.server as server

    adapter = BenchmarkMCP(statuses=[{"state": state, "result_status": result_status}])
    monkeypatch.setattr(server, "_crucible_mcp", adapter)
    monkeypatch.setattr(
        server,
        "_ticket",
        {
            "custom_fields": {
                "directives": {"harness": "crucible"},
                "assigned_hardware_ips": {"controller": "controller.example"},
            }
        },
    )
    handlers = _benchmark_handlers_with_mcp()
    validation_id = (
        await handlers["validate_benchmark"](
            controller="controller.example",
            run_file={
                "benchmarks": [{"name": "uperf", "mv-params": {}}],
                "tags": {},
            },
            harness="crucible",
        )
    )["validation_id"]

    result = await handlers["execute_benchmark"](
        controller="controller.example",
        validation_id=validation_id,
        harness="crucible",
    )

    assert result["status"] == expected
    assert result["reason_code"] == reason
    assert [name for name, _ in adapter.calls].count("start_run") == 1


class TestValidateRunCommand:
    """Tests for _validate_run_command — issue #140."""

    @pytest.mark.parametrize(
        "run_command,harness",
        [
            ("crucible run", "crucible"),
            ("crucible", "crucible"),
            ("/usr/local/bin/crucible", "crucible"),
            ("burden", "zathras"),
            ("/opt/zathras/bin/burden", "zathras"),
            ("kube-burner init", "kube-burner"),
            ("vstorm", "vstorm"),
            ("/opt/vstorm/vstorm", "vstorm"),
            ("run_cli", "forge"),
            ("/opt/forge/bin/run_cli", "forge"),
            ("clusterbuster", "clusterbuster"),
            ("k8s-netperf", "k8s-netperf"),
        ],
    )
    def test_allowed_harness_binaries(self, run_command, harness):
        allowed, reason = _validate_run_command(run_command, harness)
        assert allowed, f"Should be allowed: {run_command!r} for {harness}: {reason}"

    @pytest.mark.parametrize(
        "run_command,harness",
        [
            ("bash -c 'rm -rf /'", "crucible"),
            ("ssh root@host 'ip addr'", "crucible"),
            ("rm -rf /tmp", "zathras"),
            ("python3 -c 'print(1)'", "kube-burner"),
            ("curl http://evil.com", "vstorm"),
        ],
    )
    def test_reject_arbitrary_binaries(self, run_command, harness):
        allowed, reason = _validate_run_command(run_command, harness)
        assert not allowed
        assert "not allowed" in reason

    @pytest.mark.parametrize(
        "run_command",
        [
            "crucible run; rm -rf /",
            "crucible run && curl evil.com",
            "crucible run || bash",
            "crucible run $(whoami)",
            "crucible run `id`",
            "crucible run | tee /etc/passwd",
        ],
    )
    def test_reject_shell_injection(self, run_command):
        allowed, reason = _validate_run_command(run_command, "crucible")
        assert not allowed
        assert "shell metacharacters" in reason

    def test_reject_unknown_harness(self):
        allowed, reason = _validate_run_command("some-binary", "unknown-harness")
        assert not allowed
        assert "Unknown harness" in reason

    def test_reject_empty_command(self):
        allowed, reason = _validate_run_command("", "crucible")
        assert not allowed

    def test_cross_harness_binary_rejected(self):
        allowed, _ = _validate_run_command("burden", "crucible")
        assert not allowed
        allowed, _ = _validate_run_command("crucible", "zathras")
        assert not allowed


@pytest.mark.asyncio
async def test_get_tool_params_found():
    provider = MockSkillProvider(
        tool_params={
            "sysstat": {
                "presets": {"defaults": {"interval": "3", "subtools": "mpstat,sar"}},
                "validations": {"interval": {"type": "positive_integer"}},
            }
        },
        tool_metadata={
            "sysstat": {
                "description": "Wrapper for sar, mpstat, iostat, pidstat",
            }
        },
    )
    h = make_benchmark_handlers(ssh=MockSSHExecutor(), skill_provider=provider)
    res = await h["get_tool_params"](tool="sysstat", harness="crucible")
    assert res["found"] is False
    assert "controller-sourced context" in res["message"]


@pytest.mark.asyncio
async def test_get_tool_params_not_found():
    provider = MockSkillProvider()
    h = make_benchmark_handlers(ssh=MockSSHExecutor(), skill_provider=provider)
    res = await h["get_tool_params"](tool="nonexistent_tool", harness="crucible")
    assert res["found"] is False
    assert "controller-sourced context" in res["message"]
