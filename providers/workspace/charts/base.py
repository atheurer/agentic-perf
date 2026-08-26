from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from providers.workspace.charts.models import ChartSpec


class BaseChartAdapter(ABC):
    """Abstract base class for benchmark harness chart adapters."""

    @abstractmethod
    def can_handle(self, data: Any, harness: str | None = None) -> bool:
        """Return True if this adapter can construct a chart from the given data."""
        raise NotImplementedError

    @abstractmethod
    def build_chart(self, data: Any, **kwargs: Any) -> ChartSpec:
        """Construct a declarative ChartSpec from the data."""
        raise NotImplementedError
