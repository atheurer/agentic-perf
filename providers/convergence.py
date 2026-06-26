"""Convergence criteria for investigation loops.

Defines what "converged" means for an investigation. The evaluating
agent checks these criteria after each benchmark iteration to decide
whether to loop back (refine parameters, re-provision) or advance
to synthesis.

Two evaluation modes work together:

1. **Deterministic criteria** — code-enforced checks that don't
   require an LLM call. Statistical thresholds, iteration bounds,
   and consecutive-pass requirements. These fire first as hard gates.

2. **LLM-driven evaluation** — the evaluate agent reasons about
   whether more data would change the conclusion. Used when no
   deterministic criteria are set, or when they haven't fired yet.

The criteria live on the ticket's custom_fields.convergence_criteria,
set during triage or planning. If omitted, the evaluate agent falls
back to pure LLM-driven evaluation with a default max_iterations
safety bound.

Usage:
    from providers.convergence import (
        ConvergenceCriteria,
        evaluate_deterministic,
        ConvergenceOutcome,
    )

    criteria = ConvergenceCriteria(**ticket["custom_fields"]["convergence_criteria"])
    outcome = evaluate_deterministic(criteria, iteration_results)
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ConvergenceOutcome(str, Enum):
    """Result of a convergence check."""

    NOT_CONVERGED = "not_converged"
    CONVERGED = "converged"
    MAX_ITERATIONS = "max_iterations"
    UNDETERMINED = "undetermined"  # no deterministic criteria fired


class ConvergenceCriteria(BaseModel):
    """User-configurable convergence criteria.

    All fields are optional. When set, they act as deterministic
    gates checked before the LLM evaluates. When omitted, the
    evaluate agent uses LLM reasoning.

    The evaluate agent checks in order:
    1. max_iterations — hard ceiling, always enforced
    2. statistical threshold — metric within N% for M runs
    3. comparative threshold — difference between runs < N%
    4. If none fired → LLM evaluation (undetermined)
    """

    # --- Hard safety bound ---
    max_iterations: int = Field(
        default=10,
        description=(
            "Maximum investigation iterations before forced "
            "synthesis. Set to 0 for unlimited (termination "
            "driven by other criteria or LLM judgment)."
        ),
    )

    # --- Statistical convergence ---
    metric: str = Field(
        default="",
        description=(
            "Metric name to evaluate (e.g., throughput_mpps, "
            "iops_4k_randread). If empty, statistical criteria "
            "are not used."
        ),
    )
    threshold_pct: float = Field(
        default=2.0,
        description=(
            "Results within this percentage of the target "
            "value are considered converged."
        ),
    )
    consecutive_passes: int = Field(
        default=3,
        description=(
            "Number of consecutive iterations that must meet "
            "the threshold before convergence is declared."
        ),
    )
    baseline_value: float | None = Field(
        default=None,
        description=(
            "Reference value to compare against. If None, "
            "the first iteration's result is used as baseline."
        ),
    )

    # --- Comparative convergence ---
    compare_metric: str = Field(
        default="",
        description=(
            "Metric to compare between the last two iterations. "
            "If the delta is below compare_threshold_pct, the "
            "investigation is converged. If empty, comparative "
            "criteria are not used."
        ),
    )
    compare_threshold_pct: float = Field(
        default=1.0,
        description=(
            "Maximum percentage difference between consecutive "
            "iterations for comparative convergence."
        ),
    )

    # --- Information gain ---
    min_info_gain: float = Field(
        default=0.0,
        description=(
            "Minimum information gain between iterations. If "
            "the last iteration's info_gain falls below this "
            "threshold, the investigation is converged (entropy "
            "stall). Set to 0.0 to disable."
        ),
    )


class IterationResult(BaseModel):
    """Summary of one investigation iteration's outcome.

    Populated by the benchmark/review agents and stored on the
    ticket. The evaluate agent reads these to check convergence.
    """

    iteration: int
    metric_value: float | None = None
    metric_name: str = ""
    info_gain: float = 0.0
    summary: str = ""


def evaluate_deterministic(
    criteria: ConvergenceCriteria,
    results: list[IterationResult],
) -> ConvergenceOutcome:
    """Check deterministic convergence criteria.

    Returns a ConvergenceOutcome indicating whether a hard gate
    has fired. Returns UNDETERMINED if no deterministic criteria
    matched — the caller should then use LLM evaluation.

    Args:
        criteria: The convergence criteria from the ticket.
        results: Iteration results in chronological order.
    """
    if not results:
        return ConvergenceOutcome.NOT_CONVERGED

    iteration_count = len(results)

    # 1. Max iterations — hard ceiling
    if criteria.max_iterations > 0 and iteration_count >= criteria.max_iterations:
        return ConvergenceOutcome.MAX_ITERATIONS

    # 2. Information gain stall
    if criteria.min_info_gain > 0 and iteration_count >= 2:
        last_gain = results[-1].info_gain
        if last_gain < criteria.min_info_gain:
            return ConvergenceOutcome.CONVERGED

    # 3. Statistical convergence — metric within threshold
    if criteria.metric:
        matching = [
            r
            for r in results
            if r.metric_name == criteria.metric and r.metric_value is not None
        ]
        if matching and criteria.consecutive_passes > 0:
            baseline = (
                criteria.baseline_value
                if criteria.baseline_value is not None
                else matching[0].metric_value
            )
            if baseline is not None and baseline != 0:
                # Count consecutive passes from the end
                passes = 0
                for r in reversed(matching):
                    assert r.metric_value is not None
                    pct_diff = abs(r.metric_value - baseline) / abs(baseline) * 100
                    if pct_diff <= criteria.threshold_pct:
                        passes += 1
                    else:
                        break
                if passes >= criteria.consecutive_passes:
                    return ConvergenceOutcome.CONVERGED

    # 4. Comparative convergence — delta between last two
    if criteria.compare_metric and iteration_count >= 2:
        matching = [
            r
            for r in results
            if r.metric_name == criteria.compare_metric and r.metric_value is not None
        ]
        if len(matching) >= 2:
            prev = matching[-2].metric_value
            curr = matching[-1].metric_value
            assert prev is not None and curr is not None
            if prev != 0:
                delta_pct = abs(curr - prev) / abs(prev) * 100
                if delta_pct <= criteria.compare_threshold_pct:
                    return ConvergenceOutcome.CONVERGED

    # No deterministic criteria fired — caller should use LLM
    return ConvergenceOutcome.UNDETERMINED


def criteria_from_custom_fields(
    custom_fields: dict[str, Any],
) -> ConvergenceCriteria:
    """Extract convergence criteria from ticket custom_fields.

    Returns default criteria if convergence_criteria is not set.
    """
    raw = custom_fields.get("convergence_criteria", {})
    if not raw:
        return ConvergenceCriteria()
    return ConvergenceCriteria(**raw)


def results_from_custom_fields(
    custom_fields: dict[str, Any],
) -> list[IterationResult]:
    """Extract iteration results from ticket custom_fields.

    Reads from custom_fields.iteration_results — a list of
    dicts populated by the benchmark/review agents after each
    iteration.
    """
    raw = custom_fields.get("iteration_results", [])
    return [IterationResult(**r) for r in raw]
