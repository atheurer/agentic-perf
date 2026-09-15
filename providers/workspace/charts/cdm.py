from __future__ import annotations

import math
import re
from typing import Any

from providers.workspace.charts.base import BaseChartAdapter
from providers.workspace.charts.models import ChartDataset, ChartPanel, ChartSpec


class CdmChartAdapter(BaseChartAdapter):
    """Extracts charts from Crucible CommonDataModel (CDM) telemetry and summaries.

    Supports single-metric time-series/breakouts as well as multi-metric batched queries
    with synchronized stacked chart panels (e.g. uperf throughput panel + mpstat CPU panel).
    """

    def can_handle(self, data: Any, harness: str | None = None) -> bool:
        if isinstance(data, list):
            # If list contains transformed/generic record dicts (e.g. from jq_filter with group_by/x_field),
            # let GenericJsonChartAdapter handle it.
            if (
                data
                and isinstance(data[0], dict)
                and any(
                    k in data[0]
                    for k in ("series", "group", "category", "x_field", "y_field")
                )
            ):
                return False
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
            if harness and harness.lower() in ("crucible", "cdm"):
                return True
        return False

    def build_chart(
        self,
        data: Any,
        title: str | None = None,
        chart_type: str | None = None,
        metric: str | None = None,
        metrics: list[str] | None = None,
        breakout: str | None = None,
        unit: str | None = None,
        max_points: int = 60,
        **kwargs: Any,
    ) -> ChartSpec:
        # Resolve target metric list (from metrics param, comma-separated metric, or singular metric)
        target_metrics: list[str] = []
        if metrics:
            target_metrics = [m.strip() for m in metrics if m.strip()]
        elif metric:
            target_metrics = [m.strip() for m in metric.split(",") if m.strip()]

        # Check if data is a batched query dictionary
        if isinstance(data, dict) and "values" not in data:
            subqueries: dict[str, dict[str, Any]] = {}
            for k, v in data.items():
                if isinstance(v, dict) and "values" in v:
                    subqueries[k] = v

            if subqueries:
                # Filter subqueries if target metrics were specified
                matched_subqueries: dict[str, dict[str, Any]] = {}
                if target_metrics:
                    for tm in target_metrics:
                        tm_lower = tm.lower()
                        for k, v in subqueries.items():
                            if tm_lower in k.lower() or k.lower() in tm_lower:
                                matched_subqueries[k] = v
                if not matched_subqueries:
                    # If target metrics not specified or none matched, use all subqueries
                    matched_subqueries = subqueries

                # If multiple subqueries matched, build a synchronized multi-panel chart
                if len(matched_subqueries) > 1:
                    return self._build_multi_panel_chart(
                        matched_subqueries,
                        title=title,
                        chart_type=chart_type or "line",
                        max_points=max_points,
                        source_file=kwargs.get("source_file"),
                    )
                elif len(matched_subqueries) == 1:
                    single_key, single_data = next(iter(matched_subqueries.items()))
                    return self._build_single_metric_chart(
                        single_data,
                        metric_name=single_key,
                        title=title,
                        chart_type=chart_type,
                        unit=unit,
                        max_points=max_points,
                        source_file=kwargs.get("source_file"),
                    )

        if isinstance(data, dict) and "values" in data:
            return self._build_single_metric_chart(
                data,
                metric_name=target_metrics[0] if target_metrics else (metric or ""),
                title=title,
                chart_type=chart_type,
                unit=unit,
                max_points=max_points,
                source_file=kwargs.get("source_file"),
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

    def _build_multi_panel_chart(
        self,
        subqueries: dict[str, dict[str, Any]],
        title: str | None = None,
        chart_type: str = "line",
        max_points: int = 60,
        source_file: str | None = None,
    ) -> ChartSpec:
        """Build a synchronized multi-panel chart from multiple CDM subqueries."""
        panels: list[ChartPanel] = []
        all_datasets: list[ChartDataset] = []
        labels: list[str] = []

        # First pass: determine global reference timeline (longest duration / most points)
        max_duration = 0
        max_sample_count = 0
        ref_series: list[dict[str, Any]] = []

        for qname, qdata in subqueries.items():
            raw_vals = qdata.get("values", {})
            if isinstance(raw_vals, dict):
                for pts in raw_vals.values():
                    if isinstance(pts, list) and len(pts) > max_sample_count:
                        max_sample_count = len(pts)
                        if pts and isinstance(pts[0], dict) and "begin" in pts[0]:
                            dur = pts[-1].get("end", pts[-1]["begin"]) - pts[0]["begin"]
                            if dur > max_duration:
                                max_duration = dur
                                ref_series = pts
            elif isinstance(raw_vals, list) and len(raw_vals) > max_sample_count:
                max_sample_count = len(raw_vals)
                if (
                    raw_vals
                    and isinstance(raw_vals[0], dict)
                    and "begin" in raw_vals[0]
                ):
                    dur = (
                        raw_vals[-1].get("end", raw_vals[-1]["begin"])
                        - raw_vals[0]["begin"]
                    )
                    if dur > max_duration:
                        max_duration = dur
                        ref_series = raw_vals

        step = (
            math.ceil(max_sample_count / max_points)
            if max_sample_count > max_points
            else 1
        )

        if ref_series and isinstance(ref_series[0], dict) and "begin" in ref_series[0]:
            t0 = ref_series[0]["begin"]
            for pt in ref_series[::step]:
                rel_s = round((pt["begin"] - t0) / 1000)
                labels.append(f"{rel_s}s")
        else:
            labels = [f"{i}s" for i in range(len(range(0, max_sample_count, step)))]

        # Second pass: build panel for each subquery
        for qname, qdata in subqueries.items():
            panel_title, panel_unit = _resolve_metric_title_and_unit(qname, qdata)
            panel_datasets: list[ChartDataset] = []
            raw_vals = qdata.get("values", {})

            if isinstance(raw_vals, dict):
                for raw_key, pts in raw_vals.items():
                    if not isinstance(pts, list):
                        continue
                    ds_label = _clean_cdm_label(str(raw_key), qname)
                    values: list[float] = []
                    for pt in pts[::step]:
                        val = (
                            pt.get("value", pt.get("val", 0.0))
                            if isinstance(pt, dict)
                            else pt
                        )
                        try:
                            values.append(round(float(val), 4))
                        except (ValueError, TypeError):
                            values.append(0.0)
                    panel_datasets.append(
                        ChartDataset(label=ds_label, values=values, unit=panel_unit)
                    )
            elif isinstance(raw_vals, list):
                values = []
                for pt in raw_vals[::step]:
                    val = (
                        pt.get("value", pt.get("val", 0.0))
                        if isinstance(pt, dict)
                        else pt
                    )
                    try:
                        values.append(round(float(val), 4))
                    except (ValueError, TypeError):
                        values.append(0.0)
                panel_datasets.append(
                    ChartDataset(label=panel_title, values=values, unit=panel_unit)
                )

            if panel_datasets:
                panel = ChartPanel(
                    title=panel_title,
                    unit=panel_unit,
                    y_label=panel_unit or panel_title,
                    type=chart_type,
                    datasets=panel_datasets,
                )
                panels.append(panel)
                all_datasets.extend(panel_datasets)

        resolved_title = title or "Crucible Multi-Metric Telemetry"
        return ChartSpec(
            title=resolved_title,
            type=chart_type,
            labels=labels,
            datasets=all_datasets,
            panels=panels,
            x_label="Elapsed Time",
            y_label="Multi-Metric",
            sync_id="cdm-timeline",
            source_file=source_file,
        )

    def _build_single_metric_chart(
        self,
        data: dict[str, Any],
        metric_name: str = "",
        title: str | None = None,
        chart_type: str | None = None,
        unit: str | None = None,
        max_points: int = 60,
        source_file: str | None = None,
    ) -> ChartSpec:
        """Build single-metric chart from CDM dictionary containing values."""
        raw_vals = data.get("values", {})
        used_bo = data.get("usedBreakouts", {})
        resolved_title_inferred, resolved_unit = _resolve_metric_title_and_unit(
            metric_name, data, explicit_unit=unit
        )

        # Case 1: Breakout dictionary mapping series keys -> time series list
        if isinstance(raw_vals, dict) and raw_vals:
            datasets: list[ChartDataset] = []
            labels: list[str] = []

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
                elif any(k in first_series[0] for k in ("time", "timestamp", "date")):
                    t_field = (
                        "time"
                        if "time" in first_series[0]
                        else ("timestamp" if "timestamp" in first_series[0] else "date")
                    )
                    labels = [str(pt.get(t_field, "")) for pt in first_series[::step]]
            if not labels:
                labels = [f"{i}s" for i in range(len(first_series[::step]))]

            for raw_key, pts in raw_vals.items():
                if not isinstance(pts, list):
                    continue
                ds_label = _clean_cdm_label(str(raw_key), metric_name)
                values: list[float] = []
                for pt in pts[::step]:
                    val = (
                        pt.get("value", pt.get("val", 0.0))
                        if isinstance(pt, dict)
                        else pt
                    )
                    try:
                        values.append(round(float(val), 4))
                    except (ValueError, TypeError):
                        values.append(0.0)

                datasets.append(
                    ChartDataset(label=ds_label, values=values, unit=resolved_unit)
                )

            resolved_title = (
                title or resolved_title_inferred or "Crucible Metric Telemetry"
            )
            resolved_type = chart_type or "line"
            panel = ChartPanel(
                title=resolved_title,
                unit=resolved_unit,
                y_label=resolved_unit or (metric_name or "Value"),
                type=resolved_type,
                datasets=datasets,
            )
            return ChartSpec(
                title=resolved_title,
                type=resolved_type,
                labels=labels,
                datasets=datasets,
                panels=[panel],
                x_label="Time",
                y_label=resolved_unit or (metric_name or "Value"),
                unit=resolved_unit,
                sync_id="cdm-timeline",
                source_file=source_file,
            )

        # Case 2: Time series metric data [{time, value}] or [{timestamp, value}]
        if (
            raw_vals
            and isinstance(raw_vals, list)
            and isinstance(raw_vals[0], dict)
            and any(k in raw_vals[0] for k in ("time", "timestamp", "date", "begin"))
        ):
            t_field = (
                "begin"
                if "begin" in raw_vals[0]
                else (
                    "time"
                    if "time" in raw_vals[0]
                    else ("timestamp" if "timestamp" in raw_vals[0] else "date")
                )
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

            if pts and t_field == "begin":
                t0 = pts[0][0]
                labels = [f"{round((p[0] - t0) / 1000)}s" for p in pts]
            else:
                labels = [f"{i}s" for i in range(len(pts))] if pts else []
            values = [round(p[1], 4) for p in pts]

            ds_label = metric_name or "Value"
            if used_bo:
                bo_str = ", ".join(f"{k}={v}" for k, v in used_bo.items())
                ds_label = f"{ds_label} ({bo_str})"

            resolved_title = (
                title or resolved_title_inferred or "Metric Telemetry Timeline"
            )
            resolved_type = chart_type or "line"
            ds = ChartDataset(label=ds_label, values=values, unit=resolved_unit)
            panel = ChartPanel(
                title=resolved_title,
                unit=resolved_unit,
                y_label=resolved_unit or "Value",
                type=resolved_type,
                datasets=[ds],
            )
            return ChartSpec(
                title=resolved_title,
                type=resolved_type,
                labels=labels,
                datasets=[ds],
                panels=[panel],
                x_label="Time",
                y_label=resolved_unit or (metric_name or "Value"),
                unit=resolved_unit,
                sync_id="cdm-timeline",
                source_file=source_file,
            )

        # Case 3: Breakout summary values (e.g. per-CPU busy %, per-NIC throughput)
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
                        item.get("value") or item.get("mean") or item.get("val") or 0.0
                    )
                else:
                    lbl = f"Item {i}"
                    val = item
                try:
                    values.append(round(float(val), 4))
                    labels.append(lbl)
                except (ValueError, TypeError):
                    pass

            resolved_title = title or resolved_title_inferred or "Metric Breakout"
            resolved_type = chart_type or "bar"
            ds = ChartDataset(
                label=metric_name or "Value", values=values, unit=resolved_unit
            )
            panel = ChartPanel(
                title=resolved_title,
                unit=resolved_unit,
                y_label=resolved_unit or "Value",
                type=resolved_type,
                datasets=[ds],
            )
            return ChartSpec(
                title=resolved_title,
                type=resolved_type,
                labels=labels,
                datasets=[ds],
                panels=[panel],
                unit=resolved_unit,
                source_file=source_file,
            )

        # Empty fallback
        resolved_title = title or "Crucible Metric Chart"
        return ChartSpec(
            title=resolved_title, type=chart_type or "line", source_file=source_file
        )


def _resolve_metric_title_and_unit(
    key: str, data: dict[str, Any], explicit_unit: str | None = None
) -> tuple[str, str | None]:
    """Resolve human-friendly title and unit strictly from source metadata or explicit caller arguments.

    Does NOT make assumptions or fabricate units (e.g., does NOT assume mpstat is '%').
    If a metric has a unit in its name (e.g. 'uperf::Gbps'), extracts 'Gbps'.
    Otherwise leaves unit as None unless explicitly provided.
    """
    if explicit_unit:
        return _format_metric_title(key), explicit_unit

    # Check if data contains unit metadata
    if isinstance(data, dict):
        if "unit" in data and isinstance(data["unit"], str):
            return _format_metric_title(key), data["unit"]
        if "units" in data and isinstance(data["units"], str):
            return _format_metric_title(key), data["units"]

    # Extract unit if embedded in metric identifier (e.g. 'uperf::Gbps' -> unit='Gbps')
    if "::" in key:
        parts = key.split("::", 1)
        # If the part after :: looks like a standard unit (Gbps, MB/s, ms, etc.)
        candidate = parts[1].strip()
        if candidate in (
            "Gbps",
            "Mbps",
            "Kbps",
            "bps",
            "MB/s",
            "KB/s",
            "GB/s",
            "ms",
            "us",
            "ns",
            "IOPS",
        ):
            return _format_metric_title(key), candidate

    return _format_metric_title(key), None


def _format_metric_title(key: str) -> str:
    """Format query key or metric name into clean display title."""
    if "::" in key:
        key = key.split("::", 1)[0]
    cleaned = key.replace("_", " ").strip()
    if cleaned.endswith(" ts"):
        cleaned = cleaned[:-3].strip()
    return cleaned.title() if cleaned.islower() else cleaned


def _clean_cdm_label(raw_key: str, metric_hint: str = "") -> str:
    """Format CDM breakout identifiers into human-readable series labels.

    Examples:
        - "<1>" -> "uperf ID 1" (if uperf) or "ID 1"
        - "<server.bos2.dc.redhat.com>-<16>" -> "server CPU 16"
        - "<client>-<eth0>" -> "client eth0"
    """
    segments = re.findall(r"<([^>]+)>", raw_key)
    if not segments:
        segments = [raw_key.strip("<>")]

    if len(segments) == 1:
        seg = segments[0]
        if seg.isdigit():
            prefix = "uperf ID" if "uperf" in metric_hint.lower() else "ID"
            return f"{prefix} {seg}"
        return seg

    if len(segments) >= 2:
        host, sub = segments[0], segments[1]
        host_short = host.split(".")[0]
        if sub.isdigit():
            if "mpstat" in metric_hint.lower() or "cpu" in metric_hint.lower():
                return f"{host_short} CPU {sub}"
            return f"{host_short} ID {sub}"
        return f"{host_short} {sub}"

    return raw_key
