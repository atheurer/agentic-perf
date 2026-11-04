"""Stable identities for durable benchmark execution and comparison."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def benchmark_intent_identity(
    ticket_id: str,
    validation_id: str,
    record: dict[str, Any],
    controller: str,
    harness: str,
    run_command: str,
) -> tuple[str, str, dict[str, Any]]:
    """Reconstruct the durable operation identity from immutable validation."""
    attempt_id = str(record.get("attempt_id") or validation_id)
    intent_id = str(record.get("execution_intent_id") or validation_id)
    operation_key = f"benchmark-execution:{ticket_id or 'external'}:{intent_id}"
    immutable = {
        "ticket_id": ticket_id,
        "execution_plan_step_id": record.get("execution_plan_step_id", "current"),
        "attempt_id": attempt_id,
        "execution_intent_id": intent_id,
        "validation_id": validation_id,
        "validation_fingerprint": record.get("runfile_fingerprint"),
        "harness": harness,
        "controller": controller,
        "run_command": run_command,
    }
    return operation_key, _digest(immutable), immutable


def comparison_fingerprint(record: dict[str, Any]) -> str:
    """Fingerprint runfile and execution details that must match across steps."""
    return _digest(
        {
            "runfile": record.get("runfile_fingerprint"),
            "harness": record.get("harness"),
            "controller": record.get("controller"),
            "run_command": record.get("run_command") or "crucible run",
        }
    )


def comparison_requested(ticket: dict[str, Any]) -> bool:
    """Recognize comparison tickets even when the current step is malformed."""
    fields = ticket.get("custom_fields") or {}
    directives = fields.get("directives") or {}
    if isinstance(directives, dict) and "kernel_comparison" in directives:
        return True
    plan = fields.get("execution_plan") or {}
    steps = plan.get("steps") if isinstance(plan, dict) else []
    return any(
        isinstance(step, dict) and (step.get("params") or {}).get("workload_group")
        for step in (steps or [])
    )


def comparison_step_requested(
    ticket: dict[str, Any], step: dict[str, Any] | None = None
) -> bool:
    """Identify a comparison benchmark without gating unrelated plan steps."""
    fields = ticket.get("custom_fields") or {}
    plan = fields.get("execution_plan") or {}
    if step is None and isinstance(plan, dict):
        steps = plan.get("steps") or []
        current = plan.get("current_step")
        if isinstance(current, int) and not isinstance(current, bool):
            step = next(
                (
                    candidate
                    for candidate in steps
                    if isinstance(candidate, dict) and candidate.get("id") == current
                ),
                None,
            )
            if step is None and 0 <= current < len(steps):
                step = steps[current]
    if step is None and comparison_requested(ticket):
        return True
    step = step or {}
    if step.get("agent_type") != "benchmark":
        return False
    params = step.get("params") or {}
    if params.get("workload_group") or params.get("kernel_role"):
        return True

    directives = fields.get("directives") or {}
    if not isinstance(directives, dict) or "kernel_comparison" not in directives:
        return False
    steps = plan.get("steps") if isinstance(plan, dict) else []
    marked = any(
        isinstance(candidate, dict)
        and candidate.get("agent_type") == "benchmark"
        and (
            (candidate.get("params") or {}).get("workload_group")
            or (candidate.get("params") or {}).get("kernel_role")
        )
        for candidate in (steps or [])
    )
    # If a directive is present but the plan lost every comparison marker,
    # keep failing closed instead of treating the malformed plan as ordinary.
    return not marked
