"""Tests for convergence criteria evaluation.

Tests deterministic convergence gates: max iterations, statistical
thresholds, comparative thresholds, and information gain stalls.
Also tests the custom_fields extraction helpers.
"""

from __future__ import annotations

from providers.convergence import (
    ConvergenceCriteria,
    ConvergenceOutcome,
    IterationResult,
    criteria_from_custom_fields,
    evaluate_deterministic,
    results_from_custom_fields,
)

# --- ConvergenceCriteria model ---


class TestConvergenceCriteriaDefaults:
    """Default criteria should be safe and reasonable."""

    def test_default_max_iterations(self):
        c = ConvergenceCriteria()
        assert c.max_iterations == 10

    def test_default_threshold(self):
        c = ConvergenceCriteria()
        assert c.threshold_pct == 2.0

    def test_default_consecutive_passes(self):
        c = ConvergenceCriteria()
        assert c.consecutive_passes == 3

    def test_default_no_metric(self):
        """No metric means statistical criteria are inactive."""
        c = ConvergenceCriteria()
        assert c.metric == ""

    def test_unlimited_iterations(self):
        """max_iterations=0 means no iteration limit."""
        c = ConvergenceCriteria(max_iterations=0)
        assert c.max_iterations == 0


# --- Max iterations ---


class TestMaxIterations:
    """Hard ceiling on iteration count."""

    def test_hits_max(self):
        criteria = ConvergenceCriteria(max_iterations=3)
        results = [IterationResult(iteration=i) for i in range(3)]
        assert (
            evaluate_deterministic(criteria, results)
            == ConvergenceOutcome.MAX_ITERATIONS
        )

    def test_below_max(self):
        criteria = ConvergenceCriteria(max_iterations=5)
        results = [IterationResult(iteration=i) for i in range(3)]
        assert (
            evaluate_deterministic(criteria, results)
            != ConvergenceOutcome.MAX_ITERATIONS
        )

    def test_unlimited(self):
        """max_iterations=0 never fires."""
        criteria = ConvergenceCriteria(max_iterations=0)
        results = [IterationResult(iteration=i) for i in range(100)]
        outcome = evaluate_deterministic(criteria, results)
        assert outcome != ConvergenceOutcome.MAX_ITERATIONS

    def test_empty_results(self):
        criteria = ConvergenceCriteria(max_iterations=3)
        assert evaluate_deterministic(criteria, []) == ConvergenceOutcome.NOT_CONVERGED


# --- Statistical convergence ---


class TestStatisticalConvergence:
    """Metric within threshold for N consecutive runs."""

    def test_converges_when_stable(self):
        criteria = ConvergenceCriteria(
            metric="throughput",
            threshold_pct=5.0,
            consecutive_passes=3,
            baseline_value=100.0,
        )
        results = [
            IterationResult(
                iteration=0,
                metric_name="throughput",
                metric_value=95.0,
            ),
            IterationResult(
                iteration=1,
                metric_name="throughput",
                metric_value=99.0,
            ),
            IterationResult(
                iteration=2,
                metric_name="throughput",
                metric_value=101.0,
            ),
            IterationResult(
                iteration=3,
                metric_name="throughput",
                metric_value=100.5,
            ),
        ]
        # Last 3 are within 5% of 100.0
        assert evaluate_deterministic(criteria, results) == ConvergenceOutcome.CONVERGED

    def test_not_enough_consecutive(self):
        criteria = ConvergenceCriteria(
            metric="throughput",
            threshold_pct=5.0,
            consecutive_passes=3,
            baseline_value=100.0,
        )
        results = [
            IterationResult(
                iteration=0,
                metric_name="throughput",
                metric_value=99.0,
            ),
            IterationResult(
                iteration=1,
                metric_name="throughput",
                metric_value=80.0,  # outlier breaks streak
            ),
            IterationResult(
                iteration=2,
                metric_name="throughput",
                metric_value=100.0,
            ),
        ]
        assert evaluate_deterministic(criteria, results) != ConvergenceOutcome.CONVERGED

    def test_uses_first_result_as_baseline(self):
        """When no baseline_value is set, use first iteration."""
        criteria = ConvergenceCriteria(
            metric="iops",
            threshold_pct=3.0,
            consecutive_passes=2,
        )
        results = [
            IterationResult(
                iteration=0,
                metric_name="iops",
                metric_value=1000.0,
            ),
            IterationResult(
                iteration=1,
                metric_name="iops",
                metric_value=1020.0,
            ),
            IterationResult(
                iteration=2,
                metric_name="iops",
                metric_value=990.0,
            ),
        ]
        # Last 2 within 3% of 1000 (first result)
        assert evaluate_deterministic(criteria, results) == ConvergenceOutcome.CONVERGED

    def test_no_metric_means_no_statistical_check(self):
        """Empty metric string skips statistical evaluation."""
        criteria = ConvergenceCriteria(
            metric="",
            threshold_pct=1.0,
            consecutive_passes=1,
        )
        results = [
            IterationResult(
                iteration=0,
                metric_name="throughput",
                metric_value=100.0,
            ),
        ]
        # Should be UNDETERMINED, not CONVERGED
        assert (
            evaluate_deterministic(criteria, results) == ConvergenceOutcome.UNDETERMINED
        )

    def test_ignores_non_matching_metrics(self):
        """Only considers results with the specified metric name."""
        criteria = ConvergenceCriteria(
            metric="throughput",
            threshold_pct=5.0,
            consecutive_passes=2,
            baseline_value=100.0,
        )
        results = [
            IterationResult(
                iteration=0,
                metric_name="latency",
                metric_value=50.0,
            ),
            IterationResult(
                iteration=1,
                metric_name="throughput",
                metric_value=99.0,
            ),
        ]
        # Only 1 throughput result, need 2 consecutive
        assert evaluate_deterministic(criteria, results) != ConvergenceOutcome.CONVERGED


# --- Comparative convergence ---


class TestComparativeConvergence:
    """Delta between consecutive iterations below threshold."""

    def test_converges_when_stable(self):
        criteria = ConvergenceCriteria(
            compare_metric="latency_p99",
            compare_threshold_pct=2.0,
            max_iterations=0,
        )
        results = [
            IterationResult(
                iteration=0,
                metric_name="latency_p99",
                metric_value=120.0,
            ),
            IterationResult(
                iteration=1,
                metric_name="latency_p99",
                metric_value=121.0,
            ),
        ]
        # Delta = 0.83%, below 2.0%
        assert evaluate_deterministic(criteria, results) == ConvergenceOutcome.CONVERGED

    def test_not_converged_when_volatile(self):
        criteria = ConvergenceCriteria(
            compare_metric="latency_p99",
            compare_threshold_pct=2.0,
            max_iterations=0,
        )
        results = [
            IterationResult(
                iteration=0,
                metric_name="latency_p99",
                metric_value=100.0,
            ),
            IterationResult(
                iteration=1,
                metric_name="latency_p99",
                metric_value=110.0,
            ),
        ]
        # Delta = 10%, above 2.0%
        assert evaluate_deterministic(criteria, results) != ConvergenceOutcome.CONVERGED

    def test_needs_two_results(self):
        criteria = ConvergenceCriteria(
            compare_metric="latency_p99",
            compare_threshold_pct=2.0,
            max_iterations=0,
        )
        results = [
            IterationResult(
                iteration=0,
                metric_name="latency_p99",
                metric_value=100.0,
            ),
        ]
        assert (
            evaluate_deterministic(criteria, results) == ConvergenceOutcome.UNDETERMINED
        )


# --- Information gain stall ---


class TestInfoGainStall:
    """Entropy stall when info_gain drops below threshold."""

    def test_stall_detected(self):
        criteria = ConvergenceCriteria(
            min_info_gain=0.1,
            max_iterations=0,
        )
        results = [
            IterationResult(iteration=0, info_gain=0.5),
            IterationResult(iteration=1, info_gain=0.05),
        ]
        assert evaluate_deterministic(criteria, results) == ConvergenceOutcome.CONVERGED

    def test_no_stall_when_gaining(self):
        criteria = ConvergenceCriteria(
            min_info_gain=0.1,
            max_iterations=0,
        )
        results = [
            IterationResult(iteration=0, info_gain=0.5),
            IterationResult(iteration=1, info_gain=0.3),
        ]
        assert evaluate_deterministic(criteria, results) != ConvergenceOutcome.CONVERGED

    def test_disabled_by_default(self):
        """min_info_gain=0.0 disables the check."""
        criteria = ConvergenceCriteria(
            min_info_gain=0.0,
            max_iterations=0,
        )
        results = [
            IterationResult(iteration=0, info_gain=0.5),
            IterationResult(iteration=1, info_gain=0.0),
        ]
        assert evaluate_deterministic(criteria, results) != ConvergenceOutcome.CONVERGED

    def test_needs_two_iterations(self):
        criteria = ConvergenceCriteria(
            min_info_gain=0.1,
            max_iterations=0,
        )
        results = [
            IterationResult(iteration=0, info_gain=0.0),
        ]
        # Only 1 iteration, can't determine stall
        assert evaluate_deterministic(criteria, results) != ConvergenceOutcome.CONVERGED


# --- Priority order ---


class TestEvaluationOrder:
    """Max iterations fires before other criteria."""

    def test_max_iterations_beats_statistical(self):
        """Even if metric is stable, max_iterations wins."""
        criteria = ConvergenceCriteria(
            max_iterations=2,
            metric="throughput",
            threshold_pct=5.0,
            consecutive_passes=2,
            baseline_value=100.0,
        )
        results = [
            IterationResult(
                iteration=0,
                metric_name="throughput",
                metric_value=100.0,
            ),
            IterationResult(
                iteration=1,
                metric_name="throughput",
                metric_value=100.5,
            ),
        ]
        # Both criteria met, but max_iterations checked first
        assert (
            evaluate_deterministic(criteria, results)
            == ConvergenceOutcome.MAX_ITERATIONS
        )

    def test_undetermined_when_no_criteria_set(self):
        """Default criteria with no metric → UNDETERMINED."""
        criteria = ConvergenceCriteria(max_iterations=0)
        results = [
            IterationResult(iteration=0),
            IterationResult(iteration=1),
        ]
        assert (
            evaluate_deterministic(criteria, results) == ConvergenceOutcome.UNDETERMINED
        )


# --- Custom fields helpers ---


class TestCustomFieldsExtraction:
    """Extract criteria and results from ticket custom_fields."""

    def test_criteria_from_custom_fields(self):
        cf = {
            "convergence_criteria": {
                "metric": "iops",
                "threshold_pct": 3.0,
                "max_iterations": 5,
            },
        }
        c = criteria_from_custom_fields(cf)
        assert c.metric == "iops"
        assert c.threshold_pct == 3.0
        assert c.max_iterations == 5

    def test_criteria_defaults_when_missing(self):
        c = criteria_from_custom_fields({})
        assert c.max_iterations == 10
        assert c.metric == ""

    def test_results_from_custom_fields(self):
        cf = {
            "iteration_results": [
                {
                    "iteration": 0,
                    "metric_value": 100.0,
                    "metric_name": "throughput",
                    "info_gain": 0.5,
                },
                {
                    "iteration": 1,
                    "metric_value": 99.5,
                    "metric_name": "throughput",
                    "info_gain": 0.3,
                },
            ],
        }
        results = results_from_custom_fields(cf)
        assert len(results) == 2
        assert results[0].metric_value == 100.0
        assert results[1].info_gain == 0.3

    def test_results_empty_when_missing(self):
        results = results_from_custom_fields({})
        assert results == []
