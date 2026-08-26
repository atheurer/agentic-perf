from __future__ import annotations

from typing import Any

from .base import BenchmarkSuite, RunfileTemplate, SkillProvider
from .private import PrivateSkillProvider


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge override into base, recursing into nested dicts."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


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
        for benchmarks in [await p.list_benchmarks() for p in self._harnesses.values()]:
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
            result = await self._harnesses[self._default].resolve_benchmark(
                requirements
            )
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

    async def get_private_config(self, suite_name: str, key: str) -> Any | None:
        return await self._private.get_private_config(suite_name, key)

    async def get_all_private_config(self, suite_name: str) -> dict[str, Any]:
        provider = self._harnesses.get(suite_name)
        defaults = await provider.get_default_config() if provider else {}
        private = await self._private.get_all_private_config(suite_name)
        if not defaults:
            return private
        if not private:
            return defaults
        return _deep_merge(defaults, private)

    async def get_runfile_schema(
        self, harness: str | None = None
    ) -> dict[str, Any] | None:
        harness_name = harness or self._default
        provider = self._harnesses.get(harness_name)
        if provider:
            return await provider.get_runfile_schema()
        return None

    async def get_benchmark_params(
        self, benchmark: str, harness: str | None = None
    ) -> dict[str, Any] | None:
        if harness and harness in self._harnesses:
            return await self._harnesses[harness].get_benchmark_params(benchmark)

        suite = await self.get_benchmark(benchmark)
        if suite and suite.harness and suite.harness in self._harnesses:
            return await self._harnesses[suite.harness].get_benchmark_params(benchmark)

        if self._default in self._harnesses:
            return await self._harnesses[self._default].get_benchmark_params(benchmark)

        return None

    async def list_tools(self, harness: str | None = None) -> list[str]:
        harness_name = harness or self._default
        provider = self._harnesses.get(harness_name)
        if provider:
            return await provider.list_tools()
        return []

    async def get_tool_params(
        self, tool: str, harness: str | None = None
    ) -> dict[str, Any] | None:
        if harness:
            provider = self._harnesses.get(harness)
            return await provider.get_tool_params(tool) if provider else None

        if self._default in self._harnesses:
            res = await self._harnesses[self._default].get_tool_params(tool)
            if res is not None:
                return res

        for harness_name, provider in self._harnesses.items():
            if harness_name == self._default:
                continue
            res = await provider.get_tool_params(tool)
            if res is not None:
                return res

        return None

    async def get_tool_metadata(
        self, tool: str, harness: str | None = None
    ) -> dict[str, Any] | None:
        if harness:
            provider = self._harnesses.get(harness)
            return await provider.get_tool_metadata(tool) if provider else None

        if self._default in self._harnesses:
            res = await self._harnesses[self._default].get_tool_metadata(tool)
            if res is not None:
                return res

        for harness_name, provider in self._harnesses.items():
            if harness_name == self._default:
                continue
            res = await provider.get_tool_metadata(tool)
            if res is not None:
                return res

        return None

    async def get_example_runfile(
        self, benchmark: str, harness: str | None = None
    ) -> dict[str, Any] | None:
        if harness and harness in self._harnesses:
            return await self._harnesses[harness].get_example_runfile(benchmark)

        suite = await self.get_benchmark(benchmark)
        if suite and suite.harness and suite.harness in self._harnesses:
            return await self._harnesses[suite.harness].get_example_runfile(benchmark)

        if self._default in self._harnesses:
            return await self._harnesses[self._default].get_example_runfile(benchmark)

        return None

    async def validate_runfile(
        self, run_file: dict[str, Any], harness: str | None = None
    ) -> dict[str, Any]:
        harness_name = harness or self._default
        provider = self._harnesses.get(harness_name)
        if provider:
            return await provider.validate_runfile(run_file)
        return {
            "valid": True,
            "errors": [],
            "warning": f"No provider for harness '{harness_name}'",
        }

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
            capable.append(
                {
                    "harness": harness_name,
                    "benchmark": suite.name,
                    "description": suite.description,
                    "roles": suite.roles,
                    "min_hosts": suite.min_hosts,
                    "supported_params": suite.supported_params,
                    "is_default": harness_name == self._default,
                }
            )
        return capable
