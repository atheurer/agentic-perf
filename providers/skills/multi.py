from __future__ import annotations

from typing import Any

from .base import BenchmarkSuite, RunfileTemplate, SkillProvider
from .private import PrivateSkillProvider


class MultiHarnessSkillProvider(SkillProvider):
    """Aggregates multiple benchmark harness providers into a single interface.

    Each harness provider (Crucible, Zathras, etc.) is registered by name.
    Benchmark discovery spans all harnesses. Resolution prefers the default
    harness when multiple harnesses offer the same benchmark. Private config
    is delegated to a separate PrivateSkillProvider.
    """

    def __init__(
        self,
        harnesses: dict[str, SkillProvider],
        private: PrivateSkillProvider | None = None,
        default_harness: str = "crucible",
    ) -> None:
        self._harnesses = harnesses
        self._private = private or PrivateSkillProvider()
        self._default = default_harness

    def list_harnesses(self) -> list[str]:
        return list(self._harnesses.keys())

    def get_provider(self, harness_name: str) -> SkillProvider | None:
        return self._harnesses.get(harness_name)

    async def list_benchmarks(self) -> list[BenchmarkSuite]:
        results = []
        private_suites = set(self._private.list_suites_with_private_config())
        for benchmarks in [
            await p.list_benchmarks() for p in self._harnesses.values()
        ]:
            for b in benchmarks:
                if b.name in private_suites:
                    b.visibility = "public+private"
                results.append(b)
        return results

    async def get_benchmark(self, name: str) -> BenchmarkSuite | None:
        if self._default in self._harnesses:
            result = await self._harnesses[self._default].get_benchmark(name)
            if result is not None:
                return result

        for harness_name, provider in self._harnesses.items():
            if harness_name == self._default:
                continue
            result = await provider.get_benchmark(name)
            if result is not None:
                return result

        return None

    async def resolve_benchmark(self, requirements: dict[str, Any]) -> str | None:
        harness_pref = requirements.get("harness")

        if harness_pref and harness_pref in self._harnesses:
            return await self._harnesses[harness_pref].resolve_benchmark(requirements)

        if self._default in self._harnesses:
            result = await self._harnesses[self._default].resolve_benchmark(requirements)
            if result is not None:
                return result

        for harness_name, provider in self._harnesses.items():
            if harness_name == self._default:
                continue
            result = await provider.resolve_benchmark(requirements)
            if result is not None:
                return result

        return None

    async def generate_runfile(
        self, benchmark: str, params: dict[str, Any]
    ) -> RunfileTemplate:
        harness = params.get("harness")
        if harness and harness in self._harnesses:
            return await self._harnesses[harness].generate_runfile(benchmark, params)

        suite = await self.get_benchmark(benchmark)
        if suite and suite.harness and suite.harness in self._harnesses:
            return await self._harnesses[suite.harness].generate_runfile(
                benchmark, params
            )

        if self._default in self._harnesses:
            return await self._harnesses[self._default].generate_runfile(
                benchmark, params
            )

        first = next(iter(self._harnesses.values()))
        return await first.generate_runfile(benchmark, params)

    async def get_private_config(
        self, suite_name: str, key: str
    ) -> Any | None:
        return await self._private.get_private_config(suite_name, key)

    async def get_all_private_config(
        self, suite_name: str
    ) -> dict[str, Any]:
        return await self._private.get_all_private_config(suite_name)

    async def find_capable_harnesses(
        self, benchmark_name: str, requirements: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Return harnesses that offer the given benchmark, with capability summaries.

        This is the hook for the benchmark agent to evaluate which harnesses
        can satisfy a request during the planning/negotiation phase.
        """
        capable = []
        for harness_name, provider in self._harnesses.items():
            suite = await provider.get_benchmark(benchmark_name)
            if suite is None:
                continue
            capable.append({
                "harness": harness_name,
                "benchmark": suite.name,
                "description": suite.description,
                "roles": suite.roles,
                "min_hosts": suite.min_hosts,
                "supported_params": suite.supported_params,
                "is_default": harness_name == self._default,
            })
        return capable
