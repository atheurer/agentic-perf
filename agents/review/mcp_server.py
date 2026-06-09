from __future__ import annotations

from typing import Any

from providers.llm.base import ToolDefinition


def get_review_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="get_run_summary",
            description=(
                "Get a high-level summary of a benchmark run including "
                "status, duration, and primary metrics."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "run_id": {"type": "string", "description": "Benchmark run ID"},
                },
                "required": ["run_id"],
            },
        ),
        ToolDefinition(
            name="query_metrics",
            description=(
                "Query detailed metric data from a benchmark run. "
                "Returns aggregate or time-series data for the specified metric."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "run_id": {"type": "string", "description": "Benchmark run ID"},
                    "metric_type": {
                        "type": "string",
                        "enum": ["throughput", "latency", "cpu_utilization", "memory_utilization", "network_utilization", "iops"],
                        "description": "Metric type to query",
                    },
                    "aggregation": {
                        "type": "string",
                        "enum": ["mean", "p50", "p90", "p95", "p99", "max", "min"],
                        "description": "Aggregation function (default: mean)",
                    },
                },
                "required": ["run_id", "metric_type"],
            },
        ),
        ToolDefinition(
            name="compare_results",
            description="Compare metrics between two benchmark runs. Returns deltas and percentage changes.",
            input_schema={
                "type": "object",
                "properties": {
                    "run_id": {"type": "string", "description": "Current run ID"},
                    "baseline_id": {"type": "string", "description": "Baseline run ID"},
                    "metrics": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Metrics to compare",
                    },
                },
                "required": ["run_id", "baseline_id"],
            },
        ),
        ToolDefinition(
            name="request_clarification",
            description="Ask the user for clarification. Pauses the ticket for human input.",
            input_schema={
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "Question to ask"},
                },
                "required": ["question"],
            },
        ),
        ToolDefinition(
            name="submit_review_result",
            description="Submit the performance review analysis when complete.",
            input_schema={
                "type": "object",
                "properties": {
                    "review_summary": {"type": "string", "description": "1-2 sentence summary"},
                    "verdict": {
                        "type": "string",
                        "enum": ["hypothesis_confirmed", "hypothesis_refuted", "inconclusive"],
                    },
                    "detailed_analysis": {"type": "string", "description": "Multi-paragraph markdown analysis"},
                    "key_metrics": {"type": "object", "description": "Key metric values and assessments"},
                    "recommendations": {"type": "array", "items": {"type": "string"}},
                    "follow_up_needed": {"type": "boolean"},
                },
                "required": ["review_summary", "verdict", "detailed_analysis"],
            },
        ),
    ]


def create_review_tool_handlers(
    request_clarification_fn,
) -> dict[str, Any]:

    async def get_run_summary(run_id: str) -> dict:
        return {
            "run_id": run_id,
            "benchmark": "uperf",
            "status": "completed",
            "duration_seconds": 324,
            "iterations": 3,
            "primary_metric": {"name": "throughput", "value": 9.42, "unit": "Gbps"},
            "secondary_metric": {"name": "latency_p99", "value": 312, "unit": "usec"},
            "host_count": 2,
            "start_time": "2026-06-08T10:00:00Z",
            "end_time": "2026-06-08T10:05:24Z",
            "message": "Run summary retrieved (simulated)",
        }

    async def query_metrics(
        run_id: str, metric_type: str, aggregation: str = "mean"
    ) -> dict:
        metrics = {
            "throughput": {
                "metric": "throughput",
                "value": 9.42,
                "unit": "Gbps",
                "samples": [9.1, 9.5, 9.7],
                "stddev": 0.25,
                "aggregation": aggregation,
            },
            "latency": {
                "metric": "latency",
                "p50_usec": 42,
                "p90_usec": 78,
                "p95_usec": 124,
                "p99_usec": 312,
                "unit": "microseconds",
                "aggregation": aggregation,
            },
            "cpu_utilization": {
                "metric": "cpu_utilization",
                "mean_pct": 34.2,
                "max_pct": 87.1,
                "per_cpu_mean": [42.1, 38.5, 31.2, 25.0],
                "unit": "percent",
                "aggregation": aggregation,
            },
            "memory_utilization": {
                "metric": "memory_utilization",
                "mean_pct": 12.3,
                "max_pct": 15.1,
                "unit": "percent",
                "aggregation": aggregation,
            },
            "network_utilization": {
                "metric": "network_utilization",
                "rx_gbps": 9.42,
                "tx_gbps": 9.38,
                "unit": "Gbps",
                "aggregation": aggregation,
            },
            "iops": {
                "metric": "iops",
                "value": 0,
                "unit": "ops/sec",
                "message": "Not applicable for this benchmark type",
                "aggregation": aggregation,
            },
        }
        return metrics.get(metric_type, {
            "metric": metric_type,
            "error": f"Unknown metric type: {metric_type}",
        })

    async def compare_results(
        run_id: str, baseline_id: str, metrics: list[str] | None = None
    ) -> dict:
        return {
            "current_run": run_id,
            "baseline_run": baseline_id,
            "comparison": {
                "throughput": {
                    "current": 9.42,
                    "baseline": 8.87,
                    "delta": 0.55,
                    "delta_pct": 6.2,
                    "unit": "Gbps",
                },
                "latency_p99": {
                    "current": 312,
                    "baseline": 340,
                    "delta": -28,
                    "delta_pct": -8.2,
                    "unit": "usec",
                },
            },
            "overall_assessment": "improved",
            "message": "Comparison complete (simulated)",
        }

    async def request_clarification(question: str) -> str:
        await request_clarification_fn(question)
        return "Clarification requested. Ticket paused for human input."

    return {
        "get_run_summary": get_run_summary,
        "query_metrics": query_metrics,
        "compare_results": compare_results,
        "request_clarification": request_clarification,
    }
