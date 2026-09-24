"""Tests for providers/kernel_plan.py — plan builder and helpers."""

from __future__ import annotations

import pytest

from providers.kernel_plan import (
    build_kernel_comparison_plan,
    kernel_steps,
    preceding_kernel_step,
)

BASELINE = "5.14.0-503.14.1.el9_5.x86_64"
CANDIDATE = "5.14.0-503.16.1.el9_5.x86_64"


class TestBuildPlan:
    def test_ab_plan_shape(self):
        directive = {"baseline": BASELINE, "candidate": CANDIDATE}
        steps = build_kernel_comparison_plan(
            directive, [{"roles": ["controller"]}, {"roles": ["server"]}]
        )
        assert steps[0]["agent_type"] == "resource"
        assert steps[1]["agent_type"] == "provision"
        assert steps[1]["params"]["label"] == "harness-install"
        assert steps[2]["agent_type"] == "provision"
        assert steps[2]["params"]["kernel"]["release"] == BASELINE
        assert steps[2]["params"]["kernel"]["role"] == "baseline"
        assert steps[3]["agent_type"] == "benchmark"
        assert steps[3]["params"]["kernel_role"] == "baseline"
        assert steps[3]["params"]["workload_group"] == "kernel-ab"
        assert steps[4]["agent_type"] == "provision"
        assert steps[4]["params"]["kernel"]["release"] == CANDIDATE
        assert steps[5]["agent_type"] == "benchmark"
        assert steps[5]["params"]["kernel_role"] == "candidate"
        assert steps[6]["agent_type"] == "review"
        assert steps[6]["params"]["comparison"]["kind"] == "kernel_ab"
        assert steps[6]["params"]["comparison"]["baseline_step"] == 3
        assert steps[6]["params"]["comparison"]["candidate_step"] == 5
        assert steps[7]["agent_type"] == "teardown"

    def test_ids_sequential(self):
        directive = {"baseline": BASELINE, "candidate": CANDIDATE}
        steps = build_kernel_comparison_plan(directive, [])
        ids = [s["id"] for s in steps]
        assert ids == list(range(len(steps)))

    def test_abba_plan(self):
        directive = {
            "baseline": BASELINE,
            "candidate": CANDIDATE,
            "order": "ABBA",
        }
        steps = build_kernel_comparison_plan(directive, [])
        kernel_provisions = [s for s in steps if s.get("params", {}).get("kernel")]
        assert len(kernel_provisions) == 4
        roles = [s["params"]["kernel"]["role"] for s in kernel_provisions]
        assert roles == ["baseline", "candidate", "candidate", "baseline"]

        benchmarks = [s for s in steps if s["agent_type"] == "benchmark"]
        assert len(benchmarks) == 4

        review = next(s for s in steps if s["agent_type"] == "review")
        assert len(review["params"]["comparison"]["baseline_steps"]) == 2
        assert len(review["params"]["comparison"]["candidate_steps"]) == 2

    def test_baab_plan(self):
        directive = {
            "baseline": BASELINE,
            "candidate": CANDIDATE,
            "order": "BAAB",
        }
        steps = build_kernel_comparison_plan(directive, [])
        kernel_provisions = [s for s in steps if s.get("params", {}).get("kernel")]
        roles = [s["params"]["kernel"]["role"] for s in kernel_provisions]
        assert roles == ["candidate", "baseline", "baseline", "candidate"]

    def test_invalid_order(self):
        with pytest.raises(ValueError, match="unsupported order"):
            build_kernel_comparison_plan(
                {"baseline": BASELINE, "candidate": CANDIDATE, "order": "XYZ"},
                [],
            )

    def test_invalid_release(self):
        with pytest.raises(ValueError):
            build_kernel_comparison_plan(
                {"baseline": "bad; rm -rf /", "candidate": CANDIDATE},
                [],
            )

    def test_kernel_rt_package(self):
        directive = {
            "baseline": BASELINE,
            "candidate": CANDIDATE,
            "package": "kernel-rt",
        }
        steps = build_kernel_comparison_plan(directive, [])
        kernel_step = next(s for s in steps if s.get("params", {}).get("kernel"))
        assert kernel_step["params"]["kernel"]["package"] == "kernel-rt"


class TestHelpers:
    def test_kernel_steps(self):
        plan = {
            "steps": [
                {"id": 0, "agent_type": "resource", "params": {}},
                {"id": 1, "agent_type": "provision", "params": {"label": "harness"}},
                {
                    "id": 2,
                    "agent_type": "provision",
                    "params": {"kernel": {"release": BASELINE}},
                },
                {"id": 3, "agent_type": "benchmark", "params": {}},
                {
                    "id": 4,
                    "agent_type": "provision",
                    "params": {"kernel": {"release": CANDIDATE}},
                },
            ],
        }
        ks = kernel_steps(plan)
        assert len(ks) == 2
        assert ks[0]["id"] == 2
        assert ks[1]["id"] == 4

    def test_preceding_kernel_step(self):
        plan = {
            "steps": [
                {
                    "id": 0,
                    "agent_type": "provision",
                    "params": {"kernel": {"release": BASELINE}},
                },
                {"id": 1, "agent_type": "benchmark", "params": {}},
                {
                    "id": 2,
                    "agent_type": "provision",
                    "params": {"kernel": {"release": CANDIDATE}},
                },
                {"id": 3, "agent_type": "benchmark", "params": {}},
            ],
        }
        assert preceding_kernel_step(plan, 1)["id"] == 0
        assert preceding_kernel_step(plan, 3)["id"] == 2

    def test_no_preceding_kernel_step(self):
        plan = {
            "steps": [
                {"id": 0, "agent_type": "benchmark", "params": {}},
            ],
        }
        assert preceding_kernel_step(plan, 0) is None
