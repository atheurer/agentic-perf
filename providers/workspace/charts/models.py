from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
class ChartSpec:
    title: str
    type: str = "bar"  # "bar", "line", "doughnut", "scatter"
    labels: list[str] = field(default_factory=list)
    datasets: list[ChartDataset] = field(default_factory=list)
    x_label: str | None = None
    y_label: str | None = None
    unit: str | None = None
    description: str | None = None
    source_file: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "title": self.title,
            "type": self.type,
            "labels": self.labels,
            "datasets": [ds.to_dict() for ds in self.datasets],
        }
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
        return d
