from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ChartValidationError(ValueError):
    """Raised when a chart specification cannot produce a usable chart."""


@dataclass
class ChartDataset:
    label: str
    values: list[float | int]
    min_values: list[float | int] | None = None
    max_values: list[float | int] | None = None
    stddev_values: list[float | int] | None = None
    unit: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "label": self.label,
            "values": self.values,
        }
        if self.min_values is not None:
            d["min_values"] = self.min_values
        if self.max_values is not None:
            d["max_values"] = self.max_values
        if self.stddev_values is not None:
            d["stddev_values"] = self.stddev_values
        if self.unit is not None:
            d["unit"] = self.unit
        return d


@dataclass
class ChartPanel:
    title: str
    unit: str | None = None
    y_label: str | None = None
    type: str = "line"
    datasets: list[ChartDataset] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "title": self.title,
            "type": self.type,
            "datasets": [ds.to_dict() for ds in self.datasets],
        }
        if self.unit is not None:
            d["unit"] = self.unit
        if self.y_label is not None:
            d["y_label"] = self.y_label
        return d


@dataclass
class ChartSpec:
    title: str
    type: str = "bar"  # "bar", "line", "doughnut", "scatter"
    labels: list[str] = field(default_factory=list)
    datasets: list[ChartDataset] = field(default_factory=list)
    panels: list[ChartPanel] = field(default_factory=list)
    x_label: str | None = None
    y_label: str | None = None
    unit: str | None = None
    description: str | None = None
    source_file: str | None = None
    sync_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "title": self.title,
            "type": self.type,
            "labels": self.labels,
            "datasets": [ds.to_dict() for ds in self.datasets],
        }
        if self.panels:
            d["panels"] = [p.to_dict() for p in self.panels]
        if self.x_label is not None:
            d["x_label"] = self.x_label
        if self.y_label is not None:
            d["y_label"] = self.y_label
        if self.unit is not None:
            d["unit"] = self.unit
        if self.description is not None:
            d["description"] = self.description
        if self.source_file is not None:
            d["source_file"] = self.source_file
        if self.sync_id is not None:
            d["sync_id"] = self.sync_id
        return d


def validate_chart_spec(spec: ChartSpec | dict[str, Any]) -> None:
    """Require a complete chart shape before saving or submitting it."""
    data = spec.to_dict() if isinstance(spec, ChartSpec) else spec
    labels = data.get("labels")
    datasets = data.get("datasets")

    if not isinstance(labels, list) or not labels:
        raise ChartValidationError("chart must contain at least one label")
    if not isinstance(datasets, list) or not datasets:
        raise ChartValidationError("chart must contain at least one dataset")

    expected_values = len(labels)
    for index, dataset in enumerate(datasets):
        if not isinstance(dataset, dict):
            raise ChartValidationError(f"chart dataset {index} must be an object")
        values = dataset.get("values")
        if not isinstance(values, list) or not values:
            raise ChartValidationError(
                f"chart dataset {index} must contain non-empty values"
            )
        if len(values) != expected_values:
            raise ChartValidationError(
                f"chart dataset {index} has {len(values)} values for "
                f"{expected_values} labels"
            )

    panels = data.get("panels", [])
    if panels is None:
        panels = []
    if not isinstance(panels, list):
        raise ChartValidationError("chart panels must be an array")
    for panel_index, panel in enumerate(panels):
        if not isinstance(panel, dict):
            raise ChartValidationError(f"chart panel {panel_index} must be an object")
        panel_datasets = panel.get("datasets")
        if not isinstance(panel_datasets, list) or not panel_datasets:
            raise ChartValidationError(
                f"chart panel {panel_index} must contain at least one dataset"
            )
        for dataset_index, dataset in enumerate(panel_datasets):
            if not isinstance(dataset, dict):
                raise ChartValidationError(
                    f"chart panel {panel_index} dataset {dataset_index} "
                    "must be an object"
                )
            values = dataset.get("values")
            if not isinstance(values, list) or not values:
                raise ChartValidationError(
                    f"chart panel {panel_index} dataset {dataset_index} "
                    "must contain non-empty values"
                )
            if len(values) != expected_values:
                raise ChartValidationError(
                    f"chart panel {panel_index} dataset {dataset_index} has "
                    f"{len(values)} values for {expected_values} labels"
                )
