"""Workload-group immutability and plan mv_params enforcement.

The kernel A/B epic requires identical benchmark configuration across
runs in the same workload group. PERF-E47D0F45 showed the LLM-built run
file dropping the planned mv-params in one of two runs (one ran the
sleep benchmark's 600s multiplex default instead of the planned 30s)
with nothing catching it. These tests pin the three layers: the plan
builder stamps the group, the step-results capture records the
fingerprint, and execute_benchmark rejects drift and plan mismatch.
"""

from __future__ import annotations

from types import SimpleNamespace

import agents.benchmark.server as benchmark_server
import agents.server_utils as server_utils
import orchestrator.main as orch_main
from providers.kernel_plan import build_kernel_comparison_plan

A = "5.14.0-503.14.1.el9_5.x86_64"
B = "5.14.0-503.16.1.el9_5.x86_64"
GOOD = {"baseline": A, "candidate": B}


def _default_plan():
    return {
        "current_step": 0,
        "run_ids": [],
        "steps": [
            {"id": 0, "agent_type": "resource", "params": {}},
            {"id": 1, "agent_type": "provision", "params": {"label": "harness"}},
            {"id": 2, "agent_type": "benchmark", "params": {"label": "sleep"}},
            {"id": 3, "agent_type": "review", "params": {}},
            {"id": 4, "agent_type": "teardown", "params": {}},
        ],
    }


# ---------------------------------------------------------------------------
# 1. Plan builder stamps the workload group
# ---------------------------------------------------------------------------


def test_plan_builder_stamps_workload_group_on_benchmark_copies():
    plan, err = build_kernel_comparison_plan(_default_plan(), dict(GOOD))
    assert err == ""
    benches = [s for s in plan["steps"] if s["agent_type"] == "benchmark"]
    assert len(benches) == 2
    for step in benches:
        assert step["params"]["workload_group"] == "kernel-ab"
    roles = [s["params"]["kernel_role"] for s in benches]
    assert roles == ["baseline", "candidate"]


# ---------------------------------------------------------------------------
# 2. Step-results capture records the executed runfile identity
# ---------------------------------------------------------------------------


def test_capture_step_results_records_runfile_fingerprint():
    cf = {
        "run_id": "run-1",
        "benchmark_status": "completed",
        "benchmark_validations": {
            "active_validation_id": "val-1",
            "records": {
                "val-1": {
                    "runfile_fingerprint": "f" * 64,
                    "run_file": {"benchmarks": [{"name": "sleep"}]},
                }
            },
        },
    }
    results = orch_main._capture_step_results("benchmark", cf)
    assert results["runfile_fingerprint"] == "f" * 64
    assert results["runfile"] == {"benchmarks": [{"name": "sleep"}]}
    assert results["run_id"] == "run-1"


def test_capture_step_results_tolerates_missing_validation():
    results = orch_main._capture_step_results("benchmark", {"run_id": "r"})
    assert results["runfile_fingerprint"] == ""
    assert results["runfile"] is None


# ---------------------------------------------------------------------------
# 3. execute_benchmark rejects group drift and plan mismatch
# ---------------------------------------------------------------------------


def _ticket_with_plan(monkeypatch, *, steps_results=None, mv_params=None):
    record = {
        "validation_id": "val-788",
        "attempt_id": "attempt-1",
        "execution_intent_id": "intent-1",
        "run_file": {
            "benchmarks": [
                {
                    "name": "sleep",
                    "mv-params": {
                        "schema": {"version": "2021.12.02"},
                        "sets": [{"params": [{"arg": "seconds", "vals": ["30"]}]}],
                    },
                }
            ],
            "tags": {},
        },
        "runfile_fingerprint": benchmark_server._runfile_fingerprint(
            {
                "benchmarks": [
                    {
                        "name": "sleep",
                        "mv-params": {
                            "schema": {"version": "2021.12.02"},
                            "sets": [{"params": [{"arg": "seconds", "vals": ["30"]}]}],
                        },
                    }
                ],
                "tags": {},
            }
        ),
        "state": "executable",
    }
    step3 = {
        "id": 3,
        "agent_type": "benchmark",
        "params": {
            "label": "bench-A",
            "workload_group": "kernel-ab",
            "kernel_role": "baseline",
        },
        "status": "completed",
        "results": steps_results or {},
    }
    step5 = {
        "id": 5,
        "agent_type": "benchmark",
        "params": {
            "label": "bench-B",
            "workload_group": "kernel-ab",
            "kernel_role": "candidate",
        },
        "status": "pending",
        "results": {},
    }
    if mv_params is not None:
        step5["params"]["mv_params"] = mv_params
    plan = {
        "current_step": 5,
        "steps": [
            {"id": 0, "agent_type": "resource", "params": {}},
            {"id": 1, "agent_type": "provision", "params": {}},
            step3,
            {"id": 4, "agent_type": "provision", "params": {"kernel": {"release": B}}},
            step5,
        ],
    }
    ticket = {
        "id": "T-WG",
        "status": "executing_benchmark",
        "custom_fields": {
            "execution_plan": plan,
            "benchmark_validations": {"records": {"val-788": record}},
        },
    }

    async def active_check(**kwargs):
        return ticket

    monkeypatch.setattr(server_utils, "assert_ticket_active", active_check)
    monkeypatch.setattr(benchmark_server, "_ticket", ticket)
    monkeypatch.setattr(
        benchmark_server,
        "_skill_provider",
        SimpleNamespace(default_harness="crucible"),
    )
    monkeypatch.setenv("EXECUTION_PLAN_STEP_ID", "5")
    return ticket, record


def test_workload_drift_is_detected(monkeypatch):
    ticket, _ = _ticket_with_plan(
        monkeypatch,
        steps_results={"runfile_fingerprint": "0" * 64},
    )
    drift = benchmark_server._check_workload_group_drift(ticket, "val-788")
    assert drift
    assert "workload group 'kernel-ab'" in drift
    assert "identical run file" in drift


def test_matching_fingerprint_passes(monkeypatch):
    record = _ticket_with_plan(
        monkeypatch,
        steps_results={"runfile_fingerprint": None},
    )[1]
    # Same fingerprint as this validation.
    ticket = _ticket_with_plan(
        monkeypatch,
        steps_results={
            "runfile_fingerprint": benchmark_server._runfile_fingerprint(
                record["run_file"]
            )
        },
    )[0]
    assert benchmark_server._check_workload_group_drift(ticket, "val-788") == ""


def test_no_prior_fingerprint_is_not_drift(monkeypatch):
    """Older tickets never recorded fingerprints — nothing to compare."""
    ticket, _ = _ticket_with_plan(monkeypatch, steps_results={})
    assert benchmark_server._check_workload_group_drift(ticket, "val-788") == ""


def test_group_check_skipped_without_step_env(monkeypatch):
    ticket, _ = _ticket_with_plan(
        monkeypatch, steps_results={"runfile_fingerprint": "0" * 64}
    )
    monkeypatch.delenv("EXECUTION_PLAN_STEP_ID", raising=False)
    assert benchmark_server._check_workload_group_drift(ticket, "val-788") == ""


def test_plan_mv_params_mismatch_detected(monkeypatch):
    """The PERF-E47D0F45 case: plan says seconds=30, the run file omits
    mv-params entirely (→ harness default 600s)."""
    ticket, _ = _ticket_with_plan(monkeypatch, mv_params={"seconds": "30"})
    # Strip mv-params from the validated run file, as the LLM did in the
    # observed failure.
    ticket["custom_fields"]["benchmark_validations"]["records"]["val-788"]["run_file"][
        "benchmarks"
    ][0]["mv-params"] = {}
    error = benchmark_server._check_plan_mv_params(ticket, "val-788", {"seconds": "30"})
    assert error
    assert "seconds" in error
    assert "['30']" in error


def test_plan_mv_params_match_passes(monkeypatch):
    ticket, _ = _ticket_with_plan(monkeypatch, mv_params={"seconds": "30"})
    assert (
        benchmark_server._check_plan_mv_params(ticket, "val-788", {"seconds": "30"})
        == ""
    )


def test_plan_mv_params_list_form_passes(monkeypatch):
    ticket, _ = _ticket_with_plan(monkeypatch, mv_params={"seconds": ["30"]})
    assert (
        benchmark_server._check_plan_mv_params(ticket, "val-788", {"seconds": ["30"]})
        == ""
    )


def test_plan_mv_params_ignored_when_step_has_none(monkeypatch):
    """Steps without mv_params (non-kernel-ab tickets) are unaffected."""
    ticket, _ = _ticket_with_plan(monkeypatch)
    assert benchmark_server._check_plan_mv_params(ticket, "val-788", None) == ""
