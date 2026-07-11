from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from agents.benchmark.mcp_server import create_benchmark_tool_handlers
from providers.skills.base import RunfileTemplate
from tests.conftest import MockSkillProvider


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
                    "default_userenv": "alma8",
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
    async def noop_clarification(q):
        pass

    h, ssh = create_benchmark_tool_handlers(
        skill_provider=mock_provider,
        request_clarification_fn=noop_clarification,
    )
    return h


@pytest.mark.asyncio
async def test_get_execution_config_crucible(handlers):
    result = await handlers["get_execution_config"](harness_name="crucible")
    assert result["found"] is True
    assert result["harness"] == "crucible"
    assert result["run_command"] == "crucible run"
    assert result["run_file_format"] == "json"
    assert result["default_userenv"] == "alma8"


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


# ── result-summary.json verification tests ──────────────────


@dataclass
class _FakeSSHResult:
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""


RUN_DIR = "/var/lib/crucible/run/uperf--abc12345-1234-5678-9abc-def012345678"
SUMMARY_JSON = json.dumps({"result": "pass", "primary_metric": "Gb_sec"})


async def _make_crucible_ssh(
    *,
    run_exit_code: int = 0,
    run_stdout: str = "",
    summary_found: bool = True,
    summary_content: str = SUMMARY_JSON,
    log_content: str = "some log output",
):
    """Build a mock SSH executor that simulates crucible run + result check."""

    async def _run(host, command, timeout=300):
        if "crucible-valkey" in command:
            return _FakeSSHResult(exit_code=0, stdout="OK")
        if "result-summary.json" in command:
            if summary_found:
                return _FakeSSHResult(exit_code=0, stdout=summary_content)
            return _FakeSSHResult(exit_code=1, stdout="")
        if "crucible.log" in command:
            return _FakeSSHResult(exit_code=0, stdout=log_content)
        return _FakeSSHResult(exit_code=0, stdout="ok")

    async def _run_with_progress(host, command, progress_callback=None):
        return _FakeSSHResult(
            exit_code=run_exit_code,
            stdout=f"run directory: {RUN_DIR}\n" + (run_stdout or ""),
        )

    async def _copy_to(host, local_path, remote_path, timeout=60):
        return _FakeSSHResult(exit_code=0)

    class _Mock:
        pass

    m = _Mock()
    m.run = _run
    m.run_with_progress = _run_with_progress
    m.copy_to = _copy_to
    return m


def _make_crucible_handlers(mock_ssh):
    """Create benchmark handlers with a mocked SSH executor."""
    provider = MockSkillProvider(
        private_config={
            "crucible": {
                "execution": {
                    "controller_required": True,
                    "run_command": "crucible run",
                },
            },
        },
        runfile_template=RunfileTemplate(
            benchmark="fio", template={"harness": "crucible"}
        ),
    )

    async def noop_clarification(q):
        pass

    h, real_ssh = create_benchmark_tool_handlers(
        skill_provider=provider,
        request_clarification_fn=noop_clarification,
    )
    real_ssh.run = mock_ssh.run
    real_ssh.run_with_progress = mock_ssh.run_with_progress
    real_ssh.copy_to = mock_ssh.copy_to
    return h


@pytest.mark.asyncio
async def test_crucible_missing_result_summary_marks_failed():
    """Exit code 0 but no result-summary.json → status must be 'failed'."""
    mock_ssh = await _make_crucible_ssh(
        run_exit_code=0,
        summary_found=False,
        log_content="indexing failed: connection refused",
    )
    h = _make_crucible_handlers(mock_ssh)

    result = await h["execute_benchmark"](
        controller="test-host",
        run_file={"benchmarks": []},
        harness="crucible",
        run_command="crucible run",
    )

    assert result["status"] == "failed"
    assert "result-summary.json" in result["message"]
    assert "run_log" in result
    assert "indexing failed" in result["run_log"]
    assert "result_summary" not in result


@pytest.mark.asyncio
async def test_crucible_with_result_summary_marks_completed():
    """Exit code 0 with result-summary.json → status must be 'completed'."""
    mock_ssh = await _make_crucible_ssh(
        run_exit_code=0,
        summary_found=True,
    )
    h = _make_crucible_handlers(mock_ssh)

    result = await h["execute_benchmark"](
        controller="test-host",
        run_file={"benchmarks": []},
        harness="crucible",
        run_command="crucible run",
    )

    assert result["status"] == "completed"
    assert "result_summary" in result
    assert result["result_summary"]["result"] == "pass"
