"""Shared identities and validation for durable iteration allowances."""

from __future__ import annotations

from typing import Any

ITERATION_BUDGET_FIELD = "iteration_budget"
DEFAULT_AGENT_ITERATIONS = 20

# Keep grant targeting and dispatch targeting identical. Resource creation and
# teardown remain distinct phases because their configured limits may differ.
STATUS_AGENT_MAP = {
    "triage_pending": "triage",
    "awaiting_hardware": "resource_create",
    "preparing_platform": "platform",
    "awaiting_provision": "provisioning",
    "executing_benchmark": "benchmark",
    "awaiting_review": "review",
    "awaiting_teardown": "resource_teardown",
    "retrospective_pending": "retrospective",
    "analyzing": "analyze",
    "building_image": "image_builder",
    "gathering_context": "gathering_context",
    "planning_investigation": "planning_investigation",
    "evaluating_convergence": "evaluating_convergence",
    "coordinating_fleet": "fleet_coordinator",
    "synthesizing_results": "synthesizing_results",
}


def iteration_limit(value: Any, name: str) -> int:
    """Reject coerced booleans, negative values, and ambiguous numeric strings."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value
