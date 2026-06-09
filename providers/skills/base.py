from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class BenchmarkSuite:
    name: str
    description: str
    supported_params: dict[str, Any] = field(default_factory=dict)
    endpoint_types: list[str] = field(default_factory=list)
    visibility: str = "public"
    roles: list[str] = field(default_factory=list)
    min_hosts: int = 1
    harness: str = ""


@dataclass
class RunfileTemplate:
    benchmark: str
    template: dict[str, Any] = field(default_factory=dict)


class SkillProvider(ABC):
    @abstractmethod
    async def list_benchmarks(self) -> list[BenchmarkSuite]:
        ...

    @abstractmethod
    async def get_benchmark(self, name: str) -> BenchmarkSuite | None:
        ...

    @abstractmethod
    async def resolve_benchmark(self, requirements: dict[str, Any]) -> str | None:
        ...

    @abstractmethod
    async def generate_runfile(
        self, benchmark: str, params: dict[str, Any]
    ) -> RunfileTemplate:
        ...

    async def get_private_config(
        self, suite_name: str, key: str
    ) -> Any | None:
        return None
