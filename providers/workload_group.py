"""Workload group immutability check.

Benchmark steps in the same workload_group must produce byte-identical
canonical run files. This module compares runfile_fingerprints across
steps in the same group.
"""

from __future__ import annotations

from typing import Any


def check_workload_group(
    plan: dict[str, Any],
    step_id: int,
    runfile_fingerprint: str,
) -> tuple[bool, str]:
    """Check that a runfile matches earlier steps in the same workload group.

    Returns (ok, reason). ok=True when this is the first step in
    the group or the fingerprint matches all completed predecessors.
    """
    steps = plan.get("steps", [])
    current_step = None
    for step in steps:
        if step.get("id") == step_id:
            current_step = step
            break
    if current_step is None:
        return True, ""

    group = current_step.get("params", {}).get("workload_group")
    if not group:
        return True, ""

    for step in steps:
        if step["id"] >= step_id:
            break
        if step.get("params", {}).get("workload_group") != group:
            continue
        results = step.get("results", {})
        prior_fp = results.get("runfile_fingerprint")
        if prior_fp and prior_fp != runfile_fingerprint:
            return False, (
                f"workload_drift: step {step['id']} fingerprint"
                f" {prior_fp[:12]}... != {runfile_fingerprint[:12]}..."
            )

    return True, ""
