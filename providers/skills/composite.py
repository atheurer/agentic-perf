from __future__ import annotations

from typing import Any

from .base import BenchmarkSuite, RunfileTemplate, SkillProvider
from .private import PrivateSkillProvider


class CompositeSkillProvider(SkillProvider):
    """Combines a public skill provider with a private one.

    Public skills come from the benchmark suite's git repo (e.g., Crucible).
    Private skills come from a local config directory with org-specific knowledge.
    The composite merges both: benchmark discovery from public, private config
    overlaid when available.
    """

    def __init__(
        self,
        public: SkillProvider,
        private: PrivateSkillProvider | None = None,
    ) -> None:
        self._public = public
        self._private = private or PrivateSkillProvider()

    async def list_benchmarks(self) -> list[BenchmarkSuite]:
        benchmarks = await self._public.list_benchmarks()
        private_suites = set(self._private.list_suites_with_private_config())
        for b in benchmarks:
            if b.name in private_suites:
                b.visibility = "public+private"
        return benchmarks

    async def get_benchmark(self, name: str) -> BenchmarkSuite | None:
        return await self._public.get_benchmark(name)

    async def resolve_benchmark(self, requirements: dict[str, Any]) -> str | None:
        return await self._public.resolve_benchmark(requirements)

    async def generate_runfile(
        self, benchmark: str, params: dict[str, Any]
    ) -> RunfileTemplate:
        return await self._public.generate_runfile(benchmark, params)

    async def get_runfile_schema(self) -> dict[str, Any] | None:
        return await self._public.get_runfile_schema()

    async def get_benchmark_params(self, benchmark: str) -> dict[str, Any] | None:
        return await self._public.get_benchmark_params(benchmark)

    async def get_example_runfile(
        self, benchmark: str, endpoint_type: str = "remotehosts"
    ) -> dict[str, Any] | None:
        return await self._public.get_example_runfile(benchmark, endpoint_type=endpoint_type)

    async def get_private_config(
        self, suite_name: str, key: str
    ) -> Any | None:
        return await self._private.get_private_config(suite_name, key)

    async def get_all_private_config(
        self, suite_name: str
    ) -> dict[str, Any]:
        return await self._private.get_all_private_config(suite_name)
