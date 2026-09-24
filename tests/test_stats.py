"""Tests for providers/stats.py — descriptive stats, Welch's t, comparison."""

from __future__ import annotations

import math

from providers.stats import (
    compare_samples,
    describe,
    holm_adjust,
    student_t_cdf,
    student_t_ppf,
    welch,
)


class TestDescribe:
    def test_empty(self):
        d = describe([])
        assert d["n"] == 0
        assert d["mean"] is None

    def test_single(self):
        d = describe([5.0])
        assert d["n"] == 1
        assert d["mean"] == 5.0
        assert d["stddev"] is None

    def test_normal(self):
        d = describe([10.0, 12.0, 11.0, 13.0, 9.0])
        assert d["n"] == 5
        assert abs(d["mean"] - 11.0) < 0.01
        assert d["stddev"] > 0
        assert d["cv_pct"] is not None


class TestStudentT:
    def test_ppf_known_values(self):
        assert abs(student_t_ppf(0.975, 1) - 12.706) < 0.01
        assert abs(student_t_ppf(0.975, 5) - 2.571) < 0.01
        assert abs(student_t_ppf(0.975, 10) - 2.228) < 0.01
        assert abs(student_t_ppf(0.975, 30) - 2.042) < 0.01

    def test_ppf_large_df(self):
        assert abs(student_t_ppf(0.975, 1e6) - 1.960) < 0.01

    def test_cdf_roundtrip(self):
        t_val = student_t_ppf(0.975, 10)
        p = student_t_cdf(t_val, 10)
        assert abs(p - 0.975) < 0.001

    def test_fractional_df(self):
        t1 = student_t_ppf(0.975, 7.5)
        t2 = student_t_ppf(0.975, 7)
        t3 = student_t_ppf(0.975, 8)
        assert t2 > t1 > t3


class TestWelch:
    def test_identical_samples(self):
        a = [10.0, 11.0, 12.0, 10.0, 11.0]
        w = welch(a, a.copy())
        assert abs(w["t"]) < 0.001
        assert w["p"] > 0.99

    def test_different_samples(self):
        a = [10.0, 11.0, 10.5, 10.2, 10.8]
        b = [20.0, 21.0, 20.5, 20.2, 20.8]
        w = welch(a, b)
        assert w["p"] < 0.001
        assert w["t"] < 0

    def test_zero_variance_different_means(self):
        w = welch([1.0, 1.0], [2.0, 2.0])
        assert w["p"] == 0.0
        assert math.isinf(w["t"])

    def test_zero_variance_same_means(self):
        w = welch([5.0, 5.0], [5.0, 5.0])
        assert w["t"] == 0.0
        assert w["p"] == 1.0

    def test_insufficient_samples(self):
        w = welch([1.0], [2.0, 3.0])
        assert math.isnan(w["t"])


class TestCompareSamples:
    def test_throughput_improved(self):
        baseline = [100.0, 102.0, 98.0, 101.0, 99.0]
        candidate = [112.0, 110.0, 113.0, 111.0, 114.0]
        result = compare_samples(baseline, candidate, "higher")
        assert result["outcome"] == "improved"
        assert result["significant"]

    def test_latency_improved(self):
        baseline = [50.0, 52.0, 48.0, 51.0, 49.0]
        candidate = [42.0, 40.0, 43.0, 41.0, 44.0]
        result = compare_samples(baseline, candidate, "lower")
        assert result["outcome"] == "improved"

    def test_latency_regressed(self):
        baseline = [50.0, 52.0, 48.0, 51.0, 49.0]
        candidate = [62.0, 60.0, 63.0, 61.0, 64.0]
        result = compare_samples(baseline, candidate, "lower")
        assert result["outcome"] == "regressed"

    def test_insufficient(self):
        result = compare_samples([1.0], [2.0], "higher")
        assert result["outcome"] == "inconclusive"
        assert result["reason"] == "insufficient_samples"

    def test_unchanged(self):
        baseline = [100.0, 100.1, 99.9, 100.2, 99.8]
        candidate = [100.1, 99.9, 100.0, 100.2, 99.8]
        result = compare_samples(baseline, candidate, "higher")
        assert result["outcome"] == "unchanged"

    def test_unknown_direction(self):
        baseline = [100.0, 102.0, 98.0, 101.0, 99.0]
        candidate = [112.0, 110.0, 113.0, 111.0, 114.0]
        result = compare_samples(baseline, candidate, None)
        assert result["outcome"] == "changed"

    def test_high_variance_inconclusive(self):
        baseline = [50.0, 150.0, 25.0, 175.0, 75.0]
        candidate = [55.0, 145.0, 30.0, 170.0, 80.0]
        result = compare_samples(baseline, candidate, "higher")
        assert result["outcome"] in ("inconclusive", "unchanged")


class TestHolmAdjust:
    def test_basic(self):
        results = holm_adjust([0.01, 0.04, 0.03], alpha=0.05)
        assert results[0] is True
        assert results[1] is False
        assert results[2] is False

    def test_all_significant(self):
        results = holm_adjust([0.001, 0.002, 0.003], alpha=0.05)
        assert all(results)

    def test_none_significant(self):
        results = holm_adjust([0.5, 0.6, 0.7], alpha=0.05)
        assert not any(results)

    def test_empty(self):
        assert holm_adjust([]) == []
