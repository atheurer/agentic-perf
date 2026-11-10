from __future__ import annotations

import json
from typing import Any

import pytest

import agents.review.server as review


class ReviewMCP:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        self.calls.append((name, arguments))
        if name == "get_run_summary":
            if arguments.get("mcp_job_id") == "job-1":
                return json.dumps({"summary": {"status": "completed"}})
            raise RuntimeError("unknown mcp job")
        if name == "list_local_runs":
            assert arguments == {"limit": 1000, "offset": 0}
            return json.dumps(
                {
                    "runs": [{"run_id": "historical-1", "path": "/runs/historical-1"}],
                    "complete": True,
                }
            )
        if name == "get_local_run_summary":
            return json.dumps({"summary": {"status": "historical"}})
        if name == "list_run_artifacts":
            if "mcp_job_id" in arguments and arguments["mcp_job_id"] != "job-1":
                raise RuntimeError("unknown mcp job")
            artifacts = [
                {"artifact_path": "results.json", "size": 12, "retrievable": True},
                {"artifact_path": "raw/result.json", "size": 20, "retrievable": True},
            ]
            return json.dumps({"artifacts": artifacts, "complete": True})
        if name == "get_run_artifact":
            return json.dumps(
                {
                    "text": "metric payload",
                    "artifact_path": arguments["artifact_path"],
                    "run_path": arguments.get("run_path"),
                    "size": 13,
                    "offset": arguments.get("offset", 0),
                    "next_offset": None,
                    "complete": True,
                }
            )
        if name == "list_indexed_periods":
            periods = {"run-1": ["p1"], "run-2": ["p1"], "run-3": ["p1", "p2"]}
            return json.dumps(
                {
                    "periods": [
                        {"primary_period_id": period}
                        for period in periods[arguments["run"]]
                    ]
                }
            )
        if name == "get_indexed_metric":
            return json.dumps({"values": {"all": [{"value": 4}, {"value": 6}]}})
        raise AssertionError(f"unexpected MCP tool {name}")


@pytest.fixture
def review_mcp(monkeypatch):
    adapter = ReviewMCP()
    monkeypatch.setattr(review, "_cdm_initialized", True)
    monkeypatch.setattr(review, "_initialized", False)
    monkeypatch.setattr(
        review,
        "_ticket",
        {
            "id": "PERF-REVIEW",
            "custom_fields": {
                "assigned_hardware_ips": {"controller": "controller.example"}
            },
        },
    )
    monkeypatch.setattr(review, "_crucible_mcp", adapter)
    return adapter


@pytest.mark.asyncio
async def test_summary_uses_direct_mcp_job_and_bounded_historical_lookup(review_mcp):
    direct = json.loads(await review.get_run_summary("controller.example", "job-1"))
    assert direct["identifier_type"] == "mcp_job"
    assert direct["summary"] == {"status": "completed"}
    assert ("get_run_summary", {"mcp_job_id": "job-1"}) in review_mcp.calls

    historical = json.loads(
        await review.get_run_summary("controller.example", "historical-1")
    )
    assert historical["identifier_type"] == "historical_run"
    assert historical["summary"] == {"status": "historical"}
    assert ("get_local_run_summary", {"run_path": "/runs/historical-1"}) in (
        review_mcp.calls
    )


@pytest.mark.asyncio
async def test_artifacts_use_full_pages_and_bounded_reads(review_mcp):
    listing = json.loads(await review.read_run_results("controller.example", "job-1"))
    assert listing["status"] == "ok"
    assert [item["path"] for item in listing["files"]] == [
        "results.json",
        "raw/result.json",
    ]
    assert ("list_run_artifacts", {"mcp_job_id": "job-1", "limit": 1000}) in (
        review_mcp.calls
    )

    payload = json.loads(
        await review.read_run_results(
            "controller.example",
            "historical-1",
            file_path="results.json",
            max_bytes=100,
        )
    )
    assert payload["status"] == "ok"
    assert payload["content"] == "metric payload"
    assert (
        "get_run_artifact",
        {
            "run_path": "/runs/historical-1",
            "artifact_path": "results.json",
            "offset": 0,
            "limit": 100,
        },
    ) in review_mcp.calls

    compressed = json.loads(
        await review.read_run_results(
            "controller.example", "job-1", file_path="results.json.xz"
        )
    )
    assert compressed["status"] == "unsupported"
    assert compressed["reason"] == "compressed_artifact_not_supported"


@pytest.mark.asyncio
async def test_structured_metric_tools_map_period_queries(review_mcp):
    periods = json.loads(await review.list_cdm_periods("controller.example", "run-1"))
    assert periods["periods"] == [{"primary_period_id": "p1"}]

    metric = json.loads(
        await review.get_cdm_metric(
            "controller.example",
            "run-1",
            source="cpu",
            metric_type="throughput",
            period="p1",
            aggregation="avg",
            breakout=["host"],
            filter="role=client",
        )
    )
    assert metric["values"]["all"] == [{"value": 4}, {"value": 6}]
    assert (
        "get_indexed_metric",
        {
            "run": "run-1",
            "source": "cpu",
            "type": "throughput",
            "resolution": 1,
            "period": "p1",
            "aggregation": "avg",
            "breakout": ["host"],
            "filter": "role=client",
        },
    ) in review_mcp.calls


@pytest.mark.asyncio
async def test_comparison_discovers_periods_before_metric_queries(review_mcp):
    comparison = json.loads(
        await review.compare_results(
            "controller.example",
            ["run-1", "run-2"],
            "cpu::throughput",
            aggregation="avg",
            breakout=["host"],
            filter="role=client",
        )
    )
    assert comparison["status"] == "ok"
    assert comparison["results"]["run-1"]["period"] == "p1"
    assert comparison["results"]["run-1"]["aggregates"]["all"]["value"] == 5
    period_calls = [
        call for call in review_mcp.calls if call[0] == "list_indexed_periods"
    ]
    metric_calls = [
        call for call in review_mcp.calls if call[0] == "get_indexed_metric"
    ]
    assert len(period_calls) == 2
    assert len(metric_calls) == 2


@pytest.mark.asyncio
async def test_comparison_reports_ambiguous_periods_without_query(review_mcp):
    result = json.loads(
        await review.compare_results(
            "controller.example", ["run-1", "run-3"], "cpu::throughput"
        )
    )
    assert result["status"] == "ambiguous_period"
    assert result["periods"]["run-3"] == ["p1", "p2"]
    assert not any(call[0] == "get_indexed_metric" for call in review_mcp.calls)


@pytest.mark.asyncio
async def test_review_tools_reject_controller_and_contract_mismatches(monkeypatch):
    monkeypatch.setattr(review, "_cdm_initialized", True)
    monkeypatch.setattr(review, "_ticket", {})
    monkeypatch.setattr(review, "_crucible_mcp", None)
    mismatch = json.loads(await review.get_run_summary("other.example", "job-1"))
    assert mismatch["reason"] == "controller_mismatch"

    monkeypatch.setattr(
        review,
        "_ticket",
        {
            "custom_fields": {
                "assigned_hardware_ips": {"controller": "controller.example"}
            }
        },
    )
    unsupported = json.loads(
        await review.get_run_summary("controller.example", "job-1")
    )
    assert unsupported["reason"] == "unsupported_controller"
