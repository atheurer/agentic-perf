"""Tests for providers/metrics.py — metric direction classification."""

from __future__ import annotations

from providers.metrics import metric_direction


class TestMetricDirection:
    def test_latency_lower(self):
        assert metric_direction("p99_latency") == "lower"
        assert metric_direction("avg_latency") == "lower"
        assert metric_direction("ttft") == "lower"
        assert metric_direction("tpot") == "lower"
        assert metric_direction("elapsed_time") == "lower"
        assert metric_direction("jitter") == "lower"

    def test_throughput_higher(self):
        assert metric_direction("throughput") == "higher"
        assert metric_direction("iops") == "higher"
        assert metric_direction("bandwidth") == "higher"
        assert metric_direction("gbps") == "higher"
        assert metric_direction("rps") == "higher"
        assert metric_direction("tokens_per_second") == "higher"

    def test_unknown(self):
        assert metric_direction("custom_metric") is None
        assert metric_direction("foobar") is None

    def test_override(self):
        assert (
            metric_direction(
                "custom_metric",
                overrides={"custom_metric": "lower"},
            )
            == "lower"
        )

    def test_override_wins(self):
        assert (
            metric_direction(
                "throughput",
                overrides={"throughput": "lower"},
            )
            == "lower"
        )
