from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest

from agents.benchmark.agent import BenchmarkAgent
from agents.benchmark.mcp_server import (
    _compute_params_fingerprint,
    create_benchmark_tool_handlers,
)
from providers.skills.base import RunfileTemplate
from tests.conftest import MockSkillProvider

# ── Fixtures ──────────────────────────────────────────────


@dataclass
class _FakeSSHResult:
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""


RUN_DIR = "/var/lib/crucible/run/uperf--abc12345-1234-5678-9abc-def012345678"
SUMMARY_JSON = json.dumps({"result": "pass", "primary_metric": "Gb_sec"})


def _make_provider(
    *,
    validation_result: dict[str, Any] | None = None,
) -> MockSkillProvider:
    return MockSkillProvider(
        private_config={
            "crucible": {
                "execution": {
                    "controller_required": True,
                    "run_command": "crucible run",
                },
            },
        },
        runfile_template=RunfileTemplate(
            benchmark="fio",
            template={"harness": "crucible"},
        ),
        validation_result=validation_result,
    )


def _make_handlers(provider: MockSkillProvider):
    async def noop_clarification(q):
        pass

    h, ssh = create_benchmark_tool_handlers(
        skill_provider=provider,
        request_clarification_fn=noop_clarification,
    )
    return h, ssh


async def _make_crucible_ssh(
    *,
    run_exit_code: int = 0,
    summary_found: bool = True,
    summary_content: str = SUMMARY_JSON,
):
    async def _run(host, command, timeout=300):
        if "crucible-valkey" in command:
            return _FakeSSHResult(exit_code=0, stdout="OK")
        if "result-summary.json" in command:
            if summary_found:
                return _FakeSSHResult(exit_code=0, stdout=summary_content)
            return _FakeSSHResult(exit_code=1, stdout="")
        if "crucible.log" in command:
            return _FakeSSHResult(exit_code=0, stdout="log output")
        return _FakeSSHResult(exit_code=0, stdout="ok")

    async def _run_with_progress(host, command, progress_callback=None):
        return _FakeSSHResult(
            exit_code=run_exit_code,
            stdout=f"run directory: {RUN_DIR}\nrun complete",
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


# ── Fingerprint tests ────────────────────────────────────


class TestComputeParamsFingerprint:
    def test_no_plan(self):
        assert _compute_params_fingerprint({}) == "no-plan"

    def test_no_mv_params(self):
        cf = {
            "execution_plan": {
                "current_step": 0,
                "steps": [{"params": {"label": "baseline"}}],
            }
        }
        assert _compute_params_fingerprint(cf) == "no-mv-params"

    def test_deterministic(self):
        mv = {"test_type": "stream", "num_threads": 4}
        cf = {
            "execution_plan": {
                "current_step": 0,
                "steps": [{"params": {"mv_params": mv}}],
            }
        }
        expected = hashlib.sha256(json.dumps(mv, sort_keys=True).encode()).hexdigest()
        assert _compute_params_fingerprint(cf) == expected

    def test_key_order_independent(self):
        cf_a = {
            "execution_plan": {
                "current_step": 0,
                "steps": [{"params": {"mv_params": {"b": 2, "a": 1}}}],
            }
        }
        cf_b = {
            "execution_plan": {
                "current_step": 0,
                "steps": [{"params": {"mv_params": {"a": 1, "b": 2}}}],
            }
        }
        assert _compute_params_fingerprint(cf_a) == _compute_params_fingerprint(cf_b)

    def test_step_out_of_range(self):
        cf = {
            "execution_plan": {
                "current_step": 5,
                "steps": [{"params": {}}],
            }
        }
        assert _compute_params_fingerprint(cf) == "no-plan"


# ── Validation rejection tests ────────────────────────────


@pytest.mark.asyncio
async def test_invalid_runfile_rejected():
    """validate_runfile returning errors should reject before SSH."""
    provider = _make_provider(
        validation_result={"valid": False, "errors": ["missing field: benchmarks"]},
    )
    h, ssh = _make_handlers(provider)

    result = await h["execute_benchmark"](
        controller="test-host",
        run_file={"bad": "data"},
        harness="crucible",
    )

    assert result["status"] == "rejected"
    assert "schema validation" in result["message"]
    assert "missing field" in result["message"]


@pytest.mark.asyncio
async def test_valid_runfile_proceeds():
    """validate_runfile returning valid should proceed to execution."""
    provider = _make_provider(
        validation_result={"valid": True, "errors": []},
    )
    h, ssh = _make_handlers(provider)

    mock_ssh = await _make_crucible_ssh()
    ssh.run = mock_ssh.run
    ssh.run_with_progress = mock_ssh.run_with_progress
    ssh.copy_to = mock_ssh.copy_to

    result = await h["execute_benchmark"](
        controller="test-host",
        run_file={"benchmarks": []},
        harness="crucible",
        run_command="crucible run",
    )

    assert result["status"] in ("completed", "failed")


# ── Persist tests ────────────────────────────────────────


@pytest.mark.asyncio
async def test_persist_no_ticket_id():
    """No TICKET_ID env var should skip persist, execution proceeds."""
    provider = _make_provider()
    h, ssh = _make_handlers(provider)

    mock_ssh = await _make_crucible_ssh()
    ssh.run = mock_ssh.run
    ssh.run_with_progress = mock_ssh.run_with_progress
    ssh.copy_to = mock_ssh.copy_to

    with patch.dict("os.environ", {}, clear=True):
        result = await h["execute_benchmark"](
            controller="test-host",
            run_file={"benchmarks": []},
            harness="crucible",
            run_command="crucible run",
        )

    assert result["status"] in ("completed", "failed")


# ── _build_messages injection tests ──────────────────────


def _make_ticket(
    *,
    validated_run_file: dict[str, Any] | None = None,
    execution_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cf: dict[str, Any] = {}
    if validated_run_file:
        cf["validated_run_file"] = validated_run_file
    if execution_plan:
        cf["execution_plan"] = execution_plan
    return {
        "id": "PERF-TEST123",
        "summary": "test benchmark",
        "description": "run fio",
        "custom_fields": cf,
    }


def _build_messages_from_ticket(ticket: dict[str, Any]) -> str:
    """Call _build_messages via BenchmarkAgent.__new__ to avoid __init__."""
    agent = BenchmarkAgent.__new__(BenchmarkAgent)
    agent._repo_cache = None
    msgs = agent._build_messages(ticket)
    return msgs[0]["content"]


class TestBuildMessagesInjection:
    def test_no_validated_runfile(self):
        ticket = _make_ticket()
        content = _build_messages_from_ticket(ticket)
        assert "Previously Validated Run-File" not in content
        assert "does not match" not in content

    def test_matching_fingerprint_injects_runfile(self):
        mv_params = {"test_type": "stream", "num_threads": 8}
        fp = hashlib.sha256(json.dumps(mv_params, sort_keys=True).encode()).hexdigest()
        ticket = _make_ticket(
            validated_run_file={
                "run_file": {"benchmarks": [{"name": "fio"}]},
                "harness": "crucible",
                "params_fingerprint": fp,
            },
            execution_plan={
                "current_step": 0,
                "steps": [{"params": {"mv_params": mv_params}}],
            },
        )
        content = _build_messages_from_ticket(ticket)
        assert "Previously Validated Run-File" in content
        assert '"benchmarks"' in content
        assert "crucible" in content

    def test_mismatched_fingerprint_flags_stale(self):
        ticket = _make_ticket(
            validated_run_file={
                "run_file": {"benchmarks": [{"name": "fio"}]},
                "harness": "crucible",
                "params_fingerprint": "old-stale-hash",
            },
            execution_plan={
                "current_step": 0,
                "steps": [{"params": {"mv_params": {"test_type": "stream"}}}],
            },
        )
        content = _build_messages_from_ticket(ticket)
        assert "does not match" in content
        assert "Previously Validated Run-File" not in content

    def test_no_plan_fingerprint_matches_no_plan(self):
        """When there's no execution plan, fingerprint is 'no-plan' on both sides."""
        ticket = _make_ticket(
            validated_run_file={
                "run_file": {"benchmarks": [{"name": "fio"}]},
                "harness": "crucible",
                "params_fingerprint": "no-plan",
            },
        )
        content = _build_messages_from_ticket(ticket)
        assert "Previously Validated Run-File" in content
