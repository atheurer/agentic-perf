from __future__ import annotations

import math
from typing import Any

from providers.workspace.charts.base import BaseChartAdapter
from providers.workspace.charts.models import ChartDataset, ChartSpec


class CdmChartAdapter(BaseChartAdapter):
    """Extracts charts from Crucible CommonDataModel (CDM) telemetry and summaries."""

    def can_handle(self, data: Any, harness: str | None = None) -> bool:
        if harness and harness.lower() in ("crucible", "cdm"):
            return True
        if isinstance(data, dict):
            # Check for direct CDM response structures
            if "values" in data and (
                "usedBreakouts" in data or "remainingBreakouts" in data
            ):
                return True
            if "datapoints" in data and isinstance(data.get("label"), str):
                return True
            if "primaryMetrics" in data or "periodNames" in data:
                return True
            # Check for batched CDM query dictionary (e.g. {"query_name": {"values": ...}})
            if any(
                isinstance(v, dict)
                and "values" in v
                and ("usedBreakouts" in v or "remainingBreakouts" in v)
                for v in data.values()
            ):
                return True
        return False

    def build_chart(
        self,
        data: Any,
        title: str | None = None,
        chart_type: str | None = None,
        metric: str | None = None,
        breakout: str | None = None,
        unit: str | None = None,
        max_points: int = 60,
        **kwargs: Any,
    ) -> ChartSpec:
        # Handle batched query dictionary: extract target subquery
        if isinstance(data, dict) and "values" not in data:
            if metric and metric in data:
                data = data[metric]
            elif any(
                isinstance(v, dict) and "values" in v and "usedBreakouts" in v
                for v in data.values()
            ):
                # Pick metric matching keyword or first timeseries subquery
                matched = None
                for k, v in data.items():
                    if isinstance(v, dict) and "values" in v:
                        if metric and metric.lower() in k.lower():
                            matched = v
                            break
                        if matched is None and isinstance(v.get("values"), dict):
                            matched = v
                if matched:
                    data = matched

        if isinstance(data, dict) and "values" in data:
            raw_vals = data["values"]
            used_bo = data.get("usedBreakouts", {})

            # Case 1: Breakout dictionary mapping series keys -> time series list
            # e.g. {"<1>": [{"begin": 1787..., "value": 23.9}, ...], ...}
            if isinstance(raw_vals, dict) and raw_vals:
                datasets: list[ChartDataset] = []
                labels: list[str] = []

                # Determine reference timeline from the first non-empty series
                first_series = next(
                    (s for s in raw_vals.values() if isinstance(s, list) and s), []
                )
                n_pts = len(first_series)
                step = math.ceil(n_pts / max_points) if n_pts > max_points else 1

                if first_series and isinstance(first_series[0], dict):
                    if "begin" in first_series[0]:
                        t0 = first_series[0]["begin"]
                        for pt in first_series[::step]:
                            rel_s = round((pt["begin"] - t0) / 1000)
                            labels.append(f"{rel_s}s")
                    elif any(
                        k in first_series[0] for k in ("time", "timestamp", "date")
                    ):
                        t_field = (
                            "time"
                            if "time" in first_series[0]
                            else (
                                "timestamp"
                                if "timestamp" in first_series[0]
                                else "date"
                            )
                        )
                        labels = [str(pt.get(t_field, "")) for pt in first_series[::step]]
                if not labels:
                    labels = [f"{i}s" for i in range(len(first_series[::step]))]

                for raw_key, pts in raw_vals.items():
                    if not isinstance(pts, list):
                        continue
                    ds_label = _clean_cdm_label(str(raw_key), metric or "")
                    values: list[float] = []
                    for pt in pts[::step]:
                        if isinstance(pt, dict):
                            val = pt.get("value", pt.get("val", 0.0))
                        else:
                            val = pt
                        try:
                            values.append(round(float(val), 4))
                        except (ValueError, TypeError):
                            values.append(0.0)

                    datasets.append(ChartDataset(label=ds_label, values=values, unit=unit))

                resolved_title = title or f"{metric or 'Crucible Metric'} Telemetry"
                resolved_type = chart_type or "line"
                return ChartSpec(
                    title=resolved_title,
                    type=resolved_type,
                    labels=labels,
                    datasets=datasets,
                    x_label="Time",
                    y_label=unit or (metric or "Value"),
                    unit=unit,
                )

            # Case 2: Time series metric data [{time, value}] or [{timestamp, value}]
            if (
                raw_vals
                and isinstance(raw_vals, list)
                and isinstance(raw_vals[0], dict)
                and any(k in raw_vals[0] for k in ("time", "timestamp", "date"))
            ):
                t_field = (
                    "time"
                    if "time" in raw_vals[0]
                    else ("timestamp" if "timestamp" in raw_vals[0] else "date")
                )
                v_field = "value" if "value" in raw_vals[0] else "val"

                pts = []
                for pt in raw_vals:
                    try:
                        pts.append((pt[t_field], float(pt[v_field])))
                    except (ValueError, TypeError, KeyError):
                        pass

                if len(pts) > max_points:
                    step = math.ceil(len(pts) / max_points)
                    pts = pts[::step]

                labels = [f"{i}s" for i in range(len(pts))] if pts else []
                values = [round(p[1], 2) for p in pts]

                ds_label = metric or "Value"
                if used_bo:
                    bo_str = ", ".join(f"{k}={v}" for k, v in used_bo.items())
                    ds_label = f"{ds_label} ({bo_str})"

                resolved_title = title or f"{metric or 'Metric'} Telemetry Timeline"
                resolved_type = chart_type or "line"
                ds = ChartDataset(label=ds_label, values=values, unit=unit)
                return ChartSpec(
                    title=resolved_title,
                    type=resolved_type,
                    labels=labels,
                    datasets=[ds],
                    x_label="Time",
                    y_label=metric or "Value",
                    unit=unit,
                )

            # Case 3: Multi-core or breakout values (e.g. per-CPU busy %, per-NIC throughput)
            if raw_vals and isinstance(raw_vals, list):
                labels = []
                values = []
                for i, item in enumerate(raw_vals):
                    if isinstance(item, dict):
                        lbl = str(
                            item.get("label")
                            or item.get("breakout")
                            or item.get("cpu")
                            or item.get("name")
                            or i
                        )
                        val = (
                            item.get("value")
                            or item.get("mean")
                            or item.get("val")
                            or 0.0
                        )
                    else:
                        lbl = f"Item {i}"
                        val = item
                    try:
                        values.append(round(float(val), 2))
                        labels.append(lbl)
                    except (ValueError, TypeError):
                        pass

                resolved_title = title or f"{metric or 'Metric'} Breakout"
                resolved_type = chart_type or "bar"
                ds = ChartDataset(label=metric or "Value", values=values, unit=unit)
                return ChartSpec(
                    title=resolved_title,
                    type=resolved_type,
                    labels=labels,
                    datasets=[ds],
                    unit=unit,
                )

        # Fallback to Generic adapter
        from providers.workspace.charts.generic import GenericJsonChartAdapter

        return GenericJsonChartAdapter().build_chart(
            data,
            title=title or "Crucible Metric Chart",
            chart_type=chart_type or "bar",
            unit=unit,
            max_points=max_points,
            **kwargs,
        )


def _clean_cdm_label(raw_key: str, metric_hint: str = "") -> str:
    """Format CDM breakout identifiers into human-readable series labels."""
    parts = [p.strip("<>") for p in raw_key.split(">-<")]
    if len(parts) == 1:
        if parts[0].isdigit():
            prefix = "uperf ID" if "uperf" in metric_hint.lower() else "ID"
            return f"{prefix} {parts[0]}"
        return parts[0]
    elif len(parts) == 2:
        host, val = parts[0], parts[1]
        host_short = host.split(".")[0]
        if val.isdigit():
            return f"{host_short} CPU {val}"
        return f"{host_short} {val}"
    return raw_key
