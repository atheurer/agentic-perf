from __future__ import annotations

import csv
import io
import math
from typing import Any

from providers.workspace.charts.base import BaseChartAdapter
from providers.workspace.charts.models import ChartDataset, ChartSpec


class GenericJsonChartAdapter(BaseChartAdapter):
    """Extracts charts from arbitrary JSON arrays or objects using field names or paths."""

    def can_handle(self, data: Any, harness: str | None = None) -> bool:
        return isinstance(data, (list, dict))

    def build_chart(
        self,
        data: Any,
        title: str = "Metric Chart",
        chart_type: str = "bar",
        x_field: str | None = None,
        y_field: str | None = None,
        group_by: str | None = None,
        unit: str | None = None,
        max_points: int = 60,
        **kwargs: Any,
    ) -> ChartSpec:
        records: list[dict[str, Any]] = []
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    records.append(item)
                elif isinstance(item, (int, float)):
                    records.append({"value": item, "index": len(records)})
        elif isinstance(data, dict):
            # Check if dict maps labels to numeric values directly
            if all(isinstance(v, (int, float)) for v in data.values()):
                labels = list(data.keys())
                values = [float(data[k]) for k in labels]
                ds = ChartDataset(label=unit or "Value", values=values, unit=unit)
                return ChartSpec(
                    title=title,
                    type=chart_type,
                    labels=labels,
                    datasets=[ds],
                    unit=unit,
                )

            # Check for inner arrays like 'data', 'metrics', 'results', 'values'
            for key in ("data", "metrics", "results", "values", "datapoints"):
                if isinstance(data.get(key), list):
                    records = [r for r in data[key] if isinstance(r, dict)]
                    break

            if not records:
                # Treat key/value dict entries as records
                for k, v in data.items():
                    if isinstance(v, dict):
                        rec = dict(v)
                        rec.setdefault("name", k)
                        records.append(rec)

        if not records:
            return ChartSpec(
                title=title, type=chart_type, labels=[], datasets=[], unit=unit
            )

        # Auto-detect x_field and y_field if not specified
        sample = records[0]
        if not x_field:
            for candidate in (
                "label",
                "name",
                "cpu",
                "cpu_id",
                "id",
                "time",
                "timestamp",
                "step",
                "iteration",
            ):
                if candidate in sample:
                    x_field = candidate
                    break
            if not x_field:
                x_field = next(
                    (k for k, v in sample.items() if isinstance(v, str)), "index"
                )

        if not y_field:
            for candidate in (
                "value",
                "val",
                "busy",
                "busy_pct",
                "gbps",
                "throughput",
                "iops",
                "latency",
                "mean",
            ):
                if candidate in sample:
                    y_field = candidate
                    break
            if not y_field:
                y_field = next(
                    (k for k, v in sample.items() if isinstance(v, (int, float))), None
                )

        if not y_field:
            # Cannot plot without numeric field
            return ChartSpec(
                title=title, type=chart_type, labels=[], datasets=[], unit=unit
            )

        if group_by and any(group_by in r for r in records):
            groups: dict[str, dict[str, float]] = {}
            all_x: set[str] = set()
            for r in records:
                grp = str(r.get(group_by, "default"))
                x_val = str(r.get(x_field, len(all_x)))
                y_raw = r.get(y_field)
                if y_raw is not None:
                    try:
                        y_num = float(y_raw)
                        groups.setdefault(grp, {})[x_val] = y_num
                        all_x.add(x_val)
                    except (ValueError, TypeError):
                        pass

            labels = sorted(all_x, key=lambda x: (len(x), x))
            datasets = []
            for grp_name, val_map in sorted(groups.items()):
                vals = [val_map.get(lbl, 0.0) for lbl in labels]
                datasets.append(ChartDataset(label=grp_name, values=vals, unit=unit))
            return ChartSpec(
                title=title,
                type=chart_type,
                labels=labels,
                datasets=datasets,
                unit=unit,
            )

        # Flat single dataset
        labels_list = []
        values_list = []
        for i, r in enumerate(records):
            x_val = str(r.get(x_field, i))
            y_raw = r.get(y_field)
            if y_raw is not None:
                try:
                    values_list.append(float(y_raw))
                    labels_list.append(x_val)
                except (ValueError, TypeError):
                    pass

        # Downsample if exceeds max_points and chart is line
        if len(labels_list) > max_points and chart_type == "line":
            step = math.ceil(len(labels_list) / max_points)
            labels_list = labels_list[::step]
            values_list = values_list[::step]

        ds = ChartDataset(
            label=y_field.replace("_", " ").title(), values=values_list, unit=unit
        )
        return ChartSpec(
            title=title,
            type=chart_type,
            labels=labels_list,
            datasets=[ds],
            x_label=x_field.replace("_", " ").title(),
            y_label=y_field.replace("_", " ").title(),
            unit=unit,
        )


class CsvChartAdapter(BaseChartAdapter):
    """Extracts charts from CSV text files."""

    def can_handle(self, data: Any, harness: str | None = None) -> bool:
        if isinstance(data, str) and ("\n" in data or "," in data):
            try:
                reader = csv.DictReader(io.StringIO(data.strip()))
                return bool(reader.fieldnames and len(reader.fieldnames) > 1)
            except Exception:
                return False
        return False

    def build_chart(
        self,
        data: str,
        title: str = "CSV Metric Chart",
        chart_type: str = "bar",
        x_field: str | None = None,
        y_field: str | None = None,
        group_by: str | None = None,
        unit: str | None = None,
        max_points: int = 60,
        **kwargs: Any,
    ) -> ChartSpec:
        reader = csv.DictReader(io.StringIO(data.strip()))
        records = [row for row in reader]
        generic = GenericJsonChartAdapter()
        return generic.build_chart(
            records,
            title=title,
            chart_type=chart_type,
            x_field=x_field,
            y_field=y_field,
            group_by=group_by,
            unit=unit,
            max_points=max_points,
            **kwargs,
        )
