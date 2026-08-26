from __future__ import annotations

from typing import Any

from providers.workspace.charts.base import BaseChartAdapter
from providers.workspace.charts.models import ChartDataset, ChartSpec


class KubeBurnerChartAdapter(BaseChartAdapter):
    """Extracts latency quantiles and Prometheus summaries from Kube-Burner / k8s benchmarks."""

    def can_handle(self, data: Any, harness: str | None = None) -> bool:
        if harness and harness.lower() in (
            "kube-burner",
            "kubeburner",
            "k8s-netperf",
            "clusterbuster",
        ):
            return True
        if isinstance(data, dict) and any(
            k in data
            for k in ("quantiles", "latencies", "podLatencyQuantilesMeasurement")
        ):
            return True
        return False

    def build_chart(
        self,
        data: Any,
        title: str = "Latency Quantiles",
        chart_type: str = "bar",
        unit: str = "ms",
        **kwargs: Any,
    ) -> ChartSpec:
        quant_data = data.get("quantiles") or data.get("latencies") or data
        if isinstance(quant_data, list):
            labels = []
            p50_vals = []
            p95_vals = []
            p99_vals = []
            for item in quant_data:
                if isinstance(item, dict):
                    lbl = str(
                        item.get("quantileName")
                        or item.get("action")
                        or item.get("name")
                        or "latency"
                    )
                    labels.append(lbl)
                    p50_vals.append(float(item.get("P50", item.get("p50", 0.0))))
                    p95_vals.append(float(item.get("P95", item.get("p95", 0.0))))
                    p99_vals.append(float(item.get("P99", item.get("p99", 0.0))))

            datasets = [
                ChartDataset(label="P50 Latency", values=p50_vals, unit=unit),
                ChartDataset(label="P95 Latency", values=p95_vals, unit=unit),
                ChartDataset(label="P99 Latency", values=p99_vals, unit=unit),
            ]
            return ChartSpec(
                title=title,
                type=chart_type,
                labels=labels,
                datasets=datasets,
                unit=unit,
            )

        from providers.workspace.charts.generic import GenericJsonChartAdapter

        return GenericJsonChartAdapter().build_chart(
            data, title=title, chart_type=chart_type, unit=unit, **kwargs
        )
