"""Kernel A/B comparison plan builder.

Builds execution plans from a kernel_comparison directive. The plan
shape reuses existing provision/benchmark steps — no new agent types
or status machine edges.
"""

from __future__ import annotations

from typing import Any

from providers.rhel_kernel import KernelSpec


def build_kernel_comparison_plan(
    directive: dict[str, Any],
    required_hosts: list[dict[str, Any]],
    harness_directives: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build the step list for a kernel A/B comparison.

    The directive must contain baseline and candidate release strings.
    Returns a list of step dicts ready for execution_plan.steps.
    """
    baseline_release = directive["baseline"]
    candidate_release = directive["candidate"]
    package = directive.get("package", "kernel")
    order = directive.get("order", "AB")
    reboot_strategy = directive.get("reboot_strategy", "serial")
    reconnect_timeout = directive.get("reconnect_timeout_seconds", 900)

    baseline_spec = KernelSpec.parse({"release": baseline_release, "package": package})
    candidate_spec = KernelSpec.parse(
        {"release": candidate_release, "package": package}
    )

    if order not in ("AB", "ABBA", "BAAB"):
        raise ValueError(f"unsupported order: {order!r}")

    kernel_a = baseline_spec.to_dict()
    kernel_a.update(
        {
            "role": "baseline",
            "hosts": None,
            "reboot_strategy": reboot_strategy,
            "verification": "strict",
            "reconnect_timeout_seconds": reconnect_timeout,
        }
    )

    kernel_b = candidate_spec.to_dict()
    kernel_b.update(
        {
            "role": "candidate",
            "hosts": None,
            "reboot_strategy": reboot_strategy,
            "verification": "strict",
            "reconnect_timeout_seconds": reconnect_timeout,
        }
    )

    steps: list[dict[str, Any]] = []
    step_id = 0

    steps.append(
        {
            "id": step_id,
            "agent_type": "resource",
            "params": {"required_hosts": required_hosts},
        }
    )
    step_id += 1

    steps.append(
        {
            "id": step_id,
            "agent_type": "provision",
            "params": {"label": "harness-install"},
        }
    )
    step_id += 1

    if order == "AB":
        sequence = [
            ("baseline", kernel_a, "kernel-A"),
            ("candidate", kernel_b, "kernel-B"),
        ]
    elif order == "ABBA":
        sequence = [
            ("baseline", kernel_a, "kernel-A-1"),
            ("candidate", kernel_b, "kernel-B-1"),
            ("candidate", kernel_b, "kernel-B-2"),
            ("baseline", kernel_a, "kernel-A-2"),
        ]
    else:  # BAAB
        sequence = [
            ("candidate", kernel_b, "kernel-B-1"),
            ("baseline", kernel_a, "kernel-A-1"),
            ("baseline", kernel_a, "kernel-A-2"),
            ("candidate", kernel_b, "kernel-B-2"),
        ]

    baseline_benchmark_steps: list[int] = []
    candidate_benchmark_steps: list[int] = []

    for role, kernel, label in sequence:
        steps.append(
            {
                "id": step_id,
                "agent_type": "provision",
                "params": {"label": label, "kernel": dict(kernel)},
            }
        )
        step_id += 1

        benchmark_step_id = step_id
        steps.append(
            {
                "id": step_id,
                "agent_type": "benchmark",
                "params": {
                    "label": label,
                    "workload_group": "kernel-ab",
                    "kernel_role": role,
                },
            }
        )
        step_id += 1

        if role == "baseline":
            baseline_benchmark_steps.append(benchmark_step_id)
        else:
            candidate_benchmark_steps.append(benchmark_step_id)

    comparison_params: dict[str, Any] = {
        "comparison": {
            "kind": "kernel_ab",
            "baseline_steps": baseline_benchmark_steps,
            "candidate_steps": candidate_benchmark_steps,
        },
    }
    if len(baseline_benchmark_steps) == 1:
        comparison_params["comparison"]["baseline_step"] = baseline_benchmark_steps[0]
        comparison_params["comparison"]["candidate_step"] = candidate_benchmark_steps[0]

    steps.append(
        {
            "id": step_id,
            "agent_type": "review",
            "params": comparison_params,
        }
    )
    step_id += 1

    steps.append(
        {
            "id": step_id,
            "agent_type": "teardown",
            "params": {},
        }
    )

    return steps


def kernel_steps(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Return all provision steps that have a kernel param."""
    return [
        step for step in plan.get("steps", []) if step.get("params", {}).get("kernel")
    ]


def preceding_kernel_step(
    plan: dict[str, Any],
    benchmark_step_id: int,
) -> dict[str, Any] | None:
    """Find the most recent kernel provision step before a benchmark step."""
    steps = plan.get("steps", [])
    result = None
    for step in steps:
        if step["id"] >= benchmark_step_id:
            break
        if step.get("params", {}).get("kernel"):
            result = step
    return result
