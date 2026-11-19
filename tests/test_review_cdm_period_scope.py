"""Tests for the deterministic per-period metric tool in the review server.

Crucible records tool metrics (e.g. mpstat CPU) continuously across a run, but
a benchmark is only actively executing inside its periods. Averaging a metric
over the whole run span would include the idle gaps between periods and report
an inaccurate figure, so ``get_run_metric_by_period`` resolves each indexed
primary period through the Crucible MCP adapter and queries the metric once
per period. These tests cover the pure aggregation helpers plus the
adapter-backed tool path.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import agents.review.server as review_server
from agents.review.server import (
    _aggregate_across_samples,
    _discovered_period_ids,
    _weighted_point_mean,
)


class _FakeAdapter:
    """Minimal stand-in for ``CrucibleMCPAdapter`` that records tool calls."""

    def __init__(
        self,
        *,
        periods: tuple[str, ...] = ("p1", "p2"),
        metrics: dict[str, Any] | None = None,
        discovery_error: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.periods = list(periods)
        self.metrics = metrics or {}
        self.discovery_error = discovery_error

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        self.calls.append((name, arguments))
        if name == "list_indexed_periods":
            if self.discovery_error is not None:
                raise self.discovery_error
            return json.dumps(
                {"periods": [{"primary_period_id": p} for p in self.periods]}
            )
        if name == "get_indexed_metric":
            outcome = self.metrics.get(arguments["period"])
            if isinstance(outcome, Exception):
                raise outcome
            if outcome is None:
                outcome = {"values": {}}
            return outcome if isinstance(outcome, str) else json.dumps(outcome)
        raise AssertionError(f"unexpected MCP tool {name}")


@pytest.fixture
def install_adapter(monkeypatch):
    """Install a fake initialized adapter bound to ``controller.example``."""

    def install(adapter: _FakeAdapter | None, controller: str = "controller.example"):
        monkeypatch.setattr(review_server, "_initialized", False)
        monkeypatch.setattr(review_server, "_cdm_initialized", True)
        monkeypatch.setattr(review_server, "_skill_provider", None)
        monkeypatch.setattr(
            review_server,
            "_ticket",
            {
                "custom_fields": {
                    "assigned_hardware_ips": {"controller": "controller.example"}
                }
            },
        )
        monkeypatch.setattr(review_server, "_crucible_mcp", adapter)
        return adapter

    return install


# --- pure aggregation helpers ---------------------------------------------


def test_weighted_point_mean_duration_weighted():
    points = [
        {"value": 80.0, "duration": 1000},
        {"value": 90.0, "duration": 3000},
    ]
    mean, n, dur = _weighted_point_mean(points)
    # (80*1000 + 90*3000) / 4000 = 87.5
    assert mean == 87.5
    assert n == 2
    assert dur == 4000


def test_weighted_point_mean_falls_back_to_simple_mean():
    points = [{"value": 10.0}, {"value": 20.0}]
    mean, n, dur = _weighted_point_mean(points)
    assert mean == 15.0
    assert n == 2
    assert dur == 0.0


def test_weighted_point_mean_rejects_booleans_and_junk():
    points = [{"value": True}, {"value": "x"}, "not-a-dict", {"value": 5.0}]
    mean, n, _ = _weighted_point_mean(points)
    assert mean == 5.0
    assert n == 1


def test_weighted_point_mean_empty():
    assert _weighted_point_mean([]) == (None, 0, 0.0)
    assert _weighted_point_mean("not-a-list") == (None, 0, 0.0)


def test_aggregate_across_samples():
    agg = _aggregate_across_samples([85.0, 70.0, None, True])
    assert agg["n_samples"] == 2
    assert agg["mean"] == 77.5
    assert agg["min"] == 70.0
    assert agg["max"] == 85.0
    assert agg["stddev"] > 0


def test_aggregate_across_samples_single_value_zero_stddev():
    agg = _aggregate_across_samples([42.0])
    assert agg == {
        "n_samples": 1,
        "mean": 42.0,
        "weighting": "unweighted",
        "min": 42.0,
        "max": 42.0,
        "stddev": 0.0,
    }


def test_aggregate_across_samples_empty():
    assert _aggregate_across_samples([None, True]) == {"n_samples": 0}


def test_weighted_point_mean_ignores_nonpositive_and_nonfinite_duration():
    points = [
        {"value": 100.0, "duration": 0},
        {"value": 100.0, "duration": -5},
        {"value": 100.0, "duration": float("nan")},
        {"value": 100.0, "duration": float("inf")},
        {"value": 100.0, "duration": 1000},
    ]
    mean, n, dur = _weighted_point_mean(points)
    # Only the last point carries a usable duration, so the weighted mean is
    # just that value and the negative/zero/NaN/inf durations are discarded.
    assert mean == 100.0
    assert n == 5
    assert dur == 1000.0


def test_weighted_point_mean_rejects_nan_and_inf_values():
    points = [
        {"value": float("nan")},
        {"value": float("inf")},
        {"value": 5.0},
    ]
    mean, n, _ = _weighted_point_mean(points)
    assert mean == 5.0
    assert n == 1


def test_aggregate_across_samples_duration_weighted():
    agg = _aggregate_across_samples([(85.0, 2000.0), (70.0, 1000.0)])
    # (85*2000 + 70*1000) / 3000 = 80.0
    assert agg["n_samples"] == 2
    assert agg["mean"] == 80.0
    assert agg["weighting"] == "duration"
    assert agg["min"] == 70.0
    assert agg["max"] == 85.0


def test_aggregate_across_samples_weights_short_period_lightly():
    # 10s at 80 and 100s at 70: weighting by duration gives ~70.9 rather than
    # the unweighted 75.0, so a short active period cannot dominate the mean.
    agg = _aggregate_across_samples([(80.0, 10_000.0), (70.0, 100_000.0)])
    assert agg["n_samples"] == 2
    assert agg["mean"] == pytest.approx((80.0 * 10 + 70.0 * 100) / 110)
    assert agg["min"] == 70.0
    assert agg["max"] == 80.0


def test_aggregate_across_samples_falls_back_without_full_durations():
    # One period lacks a duration, so a duration-weighted mean is not
    # well-defined; fall back to the unweighted mean over both periods.
    agg = _aggregate_across_samples([(85.0, 2000.0), 70.0])
    assert agg["n_samples"] == 2
    assert agg["mean"] == 77.5
    assert agg["weighting"] == "unweighted"


def test_aggregate_across_samples_ignores_nonpositive_duration_pairs():
    agg = _aggregate_across_samples([(90.0, 0.0), (70.0, -1.0), (80.0, 1000.0)])
    # Zero/negative durations are not usable, forcing the unweighted path.
    assert agg["n_samples"] == 3
    assert agg["mean"] == 80.0
    assert agg["weighting"] == "unweighted"


def test_aggregate_across_samples_rejects_nan_and_inf_means():
    agg = _aggregate_across_samples(
        [float("nan"), float("inf"), (float("-inf"), 1000.0), 10.0]
    )
    assert agg["n_samples"] == 1
    assert agg["mean"] == 10.0


def test_discovered_period_ids_non_dict_input():
    assert _discovered_period_ids("not-a-mapping") == []
    assert _discovered_period_ids(None) == []
    assert _discovered_period_ids(["p1"]) == []


def test_discovered_period_ids_deduplicates_and_skips_junk():
    result = {
        "periods": [
            {"primary_period_id": "p1"},
            {"primary_period_id": ""},
            {"primary_period_id": "p2"},
            {"primary_period_id": "p1"},
            {"primary_period_id": 7},
            "not-a-dict",
        ]
    }
    assert _discovered_period_ids(result) == ["p1", "p2"]
    assert _discovered_period_ids({"periods": "nope"}) == []
    assert _discovered_period_ids({}) == []


# --- tool path -------------------------------------------------------------


def _metric_values(period: str) -> dict[str, Any]:
    values = {
        "p1": {
            "values": {
                "": [
                    {"value": 80.0, "duration": 1000},
                    {"value": 90.0, "duration": 1000},
                ]
            }
        },
        "p2": {"values": {"": [{"value": 70.0, "duration": 1000}]}},
    }
    return values[period]


@pytest.mark.asyncio
async def test_get_run_metric_by_period_happy_path(install_adapter):
    adapter = _FakeAdapter(
        metrics={"p1": _metric_values("p1"), "p2": _metric_values("p2")}
    )
    install_adapter(adapter)
    raw = await review_server.get_run_metric_by_period(
        controller="controller.example",
        run_id="run-1",
        source="mpstat",
        type="Busy-CPU",
    )
    parsed = json.loads(raw)
    assert parsed["status"] == "ok"
    assert parsed["period_bounded"] is True
    assert "idle" in parsed["note"]
    assert [p["period"] for p in parsed["period_results"]] == ["p1", "p2"]
    # p1 = mean(80, 90) = 85; p2 = 70.
    assert parsed["period_results"][0]["values"][""] == {
        "mean": 85.0,
        "points": 2,
        "duration_ms": 2000.0,
    }
    assert parsed["period_results"][1]["values"][""]["mean"] == 70.0
    # The cross-period aggregate is duration-weighted: p1 spans 2000ms and p2
    # spans 1000ms, so (85*2000 + 70*1000) / 3000 = 80.0, not the unweighted
    # 77.5. min/max/stddev stay unweighted statistics over the period means.
    assert parsed["aggregate"][""]["n_samples"] == 2
    assert parsed["aggregate"][""]["mean"] == 80.0
    assert parsed["aggregate"][""]["weighting"] == "duration"
    assert parsed["aggregate"][""]["min"] == 70.0
    assert parsed["aggregate"][""]["max"] == 85.0
    assert "failed_periods" not in parsed


@pytest.mark.asyncio
async def test_get_run_metric_by_period_queries_only_discovered_periods(
    install_adapter,
):
    adapter = _FakeAdapter(
        metrics={"p1": _metric_values("p1"), "p2": _metric_values("p2")}
    )
    install_adapter(adapter)
    await review_server.get_run_metric_by_period(
        controller="controller.example",
        run_id="run-1",
        source="mpstat",
        type="Busy-CPU",
    )
    discovery = [c for c in adapter.calls if c[0] == "list_indexed_periods"]
    assert discovery == [("list_indexed_periods", {"run": "run-1"})]
    metric_calls = [
        args for name, args in adapter.calls if name == "get_indexed_metric"
    ]
    # Every metric query is scoped to a specific period, never the whole run.
    assert [args["period"] for args in metric_calls] == ["p1", "p2"]
    for args in metric_calls:
        assert args["run"] == "run-1"
        assert args["source"] == "mpstat"
        assert args["type"] == "Busy-CPU"
        assert args["resolution"] == 1
        assert "breakout" not in args
        assert "filter" not in args


@pytest.mark.asyncio
async def test_get_run_metric_by_period_passes_breakout_filter_resolution(
    install_adapter,
):
    adapter = _FakeAdapter(
        metrics={"p1": _metric_values("p1"), "p2": _metric_values("p2")}
    )
    install_adapter(adapter)
    await review_server.get_run_metric_by_period(
        controller="controller.example",
        run_id="run-1",
        source="mpstat",
        type="Busy-CPU",
        breakout=["cpu"],
        filter="cpu=0",
        resolution=5,
    )
    for name, args in adapter.calls:
        if name != "get_indexed_metric":
            continue
        assert args["breakout"] == ["cpu"]
        assert args["filter"] == "cpu=0"
        assert args["resolution"] == 5


@pytest.mark.asyncio
async def test_get_run_metric_by_period_dedupes_repeated_periods(install_adapter):
    adapter = _FakeAdapter(
        periods=("p1", "p1"),
        metrics={"p1": _metric_values("p1")},
    )
    install_adapter(adapter)
    parsed = json.loads(
        await review_server.get_run_metric_by_period(
            controller="controller.example",
            run_id="run-1",
            source="mpstat",
            type="Busy-CPU",
        )
    )
    assert [p["period"] for p in parsed["period_results"]] == ["p1"]
    assert parsed["aggregate"][""]["n_samples"] == 1


@pytest.mark.asyncio
async def test_get_run_metric_by_period_discovery_failure(install_adapter):
    adapter = _FakeAdapter(discovery_error=RuntimeError("cdm down"))
    install_adapter(adapter)
    parsed = json.loads(
        await review_server.get_run_metric_by_period(
            controller="controller.example",
            run_id="run-1",
            source="mpstat",
            type="Busy-CPU",
        )
    )
    assert parsed["status"] == "error"
    assert parsed["reason"] == "period_discovery_failed"


@pytest.mark.asyncio
async def test_get_run_metric_by_period_invalid_discovery_response(install_adapter):
    class _BadAdapter(_FakeAdapter):
        async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
            self.calls.append((name, arguments))
            return "not-json"

    install_adapter(_BadAdapter())
    parsed = json.loads(
        await review_server.get_run_metric_by_period(
            controller="controller.example",
            run_id="run-1",
            source="mpstat",
            type="Busy-CPU",
        )
    )
    assert parsed["status"] == "error"
    assert parsed["reason"] == "period_discovery_failed"


@pytest.mark.asyncio
async def test_get_run_metric_by_period_no_periods(install_adapter):
    adapter = _FakeAdapter(periods=())
    install_adapter(adapter)
    parsed = json.loads(
        await review_server.get_run_metric_by_period(
            controller="controller.example",
            run_id="run-1",
            source="mpstat",
            type="Busy-CPU",
        )
    )
    assert parsed["status"] == "no_data"
    assert [name for name, _ in adapter.calls] == ["list_indexed_periods"]


@pytest.mark.asyncio
async def test_get_run_metric_by_period_all_periods_fail(install_adapter):
    adapter = _FakeAdapter(
        metrics={"p1": RuntimeError("boom"), "p2": RuntimeError("boom")}
    )
    install_adapter(adapter)
    parsed = json.loads(
        await review_server.get_run_metric_by_period(
            controller="controller.example",
            run_id="run-1",
            source="mpstat",
            type="Busy-CPU",
        )
    )
    assert parsed["status"] == "error"
    assert parsed["reason"] == "metric_query_failed"
    assert [p["period"] for p in parsed["period_results"]] == ["p1", "p2"]
    assert all("error" in p for p in parsed["period_results"])


@pytest.mark.asyncio
async def test_get_run_metric_by_period_partial_failure(install_adapter):
    adapter = _FakeAdapter(
        metrics={"p1": _metric_values("p1"), "p2": RuntimeError("boom")}
    )
    install_adapter(adapter)
    parsed = json.loads(
        await review_server.get_run_metric_by_period(
            controller="controller.example",
            run_id="run-1",
            source="mpstat",
            type="Busy-CPU",
        )
    )
    assert parsed["status"] == "ok"
    assert parsed["failed_periods"] == ["p2"]
    assert parsed["aggregate"][""]["n_samples"] == 1
    assert parsed["aggregate"][""]["mean"] == 85.0


@pytest.mark.asyncio
async def test_get_run_metric_by_period_skips_junk_values(install_adapter):
    adapter = _FakeAdapter(
        periods=("p1",),
        metrics={"p1": {"values": {"": [{"value": "junk"}, {"value": True}]}}},
    )
    install_adapter(adapter)
    parsed = json.loads(
        await review_server.get_run_metric_by_period(
            controller="controller.example",
            run_id="run-1",
            source="mpstat",
            type="Busy-CPU",
        )
    )
    assert parsed["period_results"][0]["values"][""] == {
        "mean": None,
        "points": 0,
        "duration_ms": 0.0,
    }
    assert parsed["aggregate"][""] == {"n_samples": 0}


@pytest.mark.asyncio
async def test_get_run_metric_by_period_controller_mismatch(install_adapter):
    adapter = _FakeAdapter()
    install_adapter(adapter)
    parsed = json.loads(
        await review_server.get_run_metric_by_period(
            controller="other.example",
            run_id="run-1",
            source="mpstat",
            type="Busy-CPU",
        )
    )
    assert parsed["status"] == "error"
    assert parsed["reason"] == "controller_mismatch"
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_get_run_metric_by_period_unsupported_controller(install_adapter):
    install_adapter(None)
    parsed = json.loads(
        await review_server.get_run_metric_by_period(
            controller="controller.example",
            run_id="run-1",
            source="mpstat",
            type="Busy-CPU",
        )
    )
    assert parsed["status"] == "error"
    assert parsed["reason"] == "unsupported_controller"


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"run_id": ""}, "run_id"),
        ({"source": ""}, "run_id, source, and type"),
        ({"type": ""}, "run_id, source, and type"),
        ({"resolution": 0}, "resolution"),
        ({"resolution": True}, "resolution"),
        ({"resolution": 1.5}, "resolution"),
        ({"resolution": -3}, "resolution"),
    ],
)
@pytest.mark.asyncio
async def test_get_run_metric_by_period_invalid_arguments(
    install_adapter, overrides, fragment
):
    # Argument validation must short-circuit before adapter init, so a missing
    # adapter does not surface ``unsupported_controller``.
    install_adapter(None)
    arguments: dict[str, Any] = {
        "controller": "controller.example",
        "run_id": "run-1",
        "source": "mpstat",
        "type": "Busy-CPU",
    }
    arguments.update(overrides)
    parsed = json.loads(await review_server.get_run_metric_by_period(**arguments))
    assert parsed["status"] == "error"
    assert parsed["reason"] == "invalid_arguments"
    assert fragment in parsed["message"]


@pytest.mark.asyncio
async def test_get_run_metric_by_period_non_dict_values_is_empty(install_adapter):
    adapter = _FakeAdapter(periods=("p1",), metrics={"p1": {"values": "junk"}})
    install_adapter(adapter)
    parsed = json.loads(
        await review_server.get_run_metric_by_period(
            controller="controller.example",
            run_id="run-1",
            source="mpstat",
            type="Busy-CPU",
        )
    )
    assert parsed["status"] == "ok"
    assert parsed["period_results"][0] == {"period": "p1", "values": {}}
    assert parsed["aggregate"] == {}
