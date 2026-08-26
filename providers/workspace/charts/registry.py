from __future__ import annotations

from typing import Any

from providers.workspace.charts.base import BaseChartAdapter
from providers.workspace.charts.cdm import CdmChartAdapter
from providers.workspace.charts.generic import CsvChartAdapter, GenericJsonChartAdapter
from providers.workspace.charts.kubeburner import KubeBurnerChartAdapter
from providers.workspace.charts.models import ChartSpec


class ChartAdapterRegistry:
    """Registry for benchmark chart adapters."""

    def __init__(self) -> None:
        self._adapters: list[BaseChartAdapter] = [
            CdmChartAdapter(),
            KubeBurnerChartAdapter(),
            CsvChartAdapter(),
            GenericJsonChartAdapter(),
        ]

    def register_adapter(self, adapter: BaseChartAdapter, prepend: bool = True) -> None:
        if prepend:
            self._adapters.insert(0, adapter)
        else:
            self._adapters.append(adapter)

    def get_adapter(self, data: Any, harness: str | None = None) -> BaseChartAdapter:
        for adapter in self._adapters:
            if adapter.can_handle(data, harness=harness):
                return adapter
        return GenericJsonChartAdapter()

    def generate_chart_spec(
        self,
        data: Any,
        harness: str | None = None,
        title: str = "Performance Chart",
        chart_type: str = "bar",
        unit: str | None = None,
        source_file: str | None = None,
        **kwargs: Any,
    ) -> ChartSpec:
        adapter = self.get_adapter(data, harness=harness)
        spec = adapter.build_chart(
            data, title=title, chart_type=chart_type, unit=unit, **kwargs
        )
        if source_file:
            spec.source_file = source_file
        return spec


_global_registry = ChartAdapterRegistry()


def get_chart_registry() -> ChartAdapterRegistry:
    return _global_registry
