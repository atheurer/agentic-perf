"""Shared benchmark catalog behavior for chat and triage."""

from __future__ import annotations

import json

import pytest

from providers.skills.base import BenchmarkSuite
from providers.skills.catalog import list_benchmark_catalog


class _Harness:
    def __init__(self, suites: list[BenchmarkSuite] | Exception):
        self._suites = suites

    async def list_benchmarks(self) -> list[BenchmarkSuite]:
        if isinstance(self._suites, Exception):
            raise self._suites
        return self._suites


class _Provider:
    def __init__(self, harnesses: dict[str, _Harness]):
        self._harnesses = harnesses

    def list_harnesses(self) -> list[str]:
        return list(self._harnesses)

    def get_provider(self, harness: str) -> _Harness | None:
        return self._harnesses.get(harness)

    async def get_benchmark(self, name: str):
        for harness in self._harnesses.values():
            if isinstance(harness._suites, Exception):
                continue
            for suite in harness._suites:
                if suite.name == name:
                    return suite
        return None


def _suite(name: str, harness: str = "crucible") -> BenchmarkSuite:
    return BenchmarkSuite(
        name=name,
        description=f"{name} benchmark",
        harness=harness,
        roles=["client"],
        min_hosts=1,
    )


@pytest.mark.asyncio
async def test_catalog_deduplicates_crucible_and_reports_unavailable_harnesses():
    provider = _Provider(
        {
            "crucible": _Harness([_suite("fio"), _suite("fio")]),
            "offline": _Harness(RuntimeError("catalog offline")),
        }
    )

    entries, unavailable = await list_benchmark_catalog(provider)

    assert [(entry["harness"], entry["name"]) for entry in entries].count(
        ("crucible", "fio")
    ) == 1
    assert unavailable == ["offline"]
    assert any(entry["name"] == "boot-time" for entry in entries)


@pytest.mark.asyncio
async def test_triage_list_matches_shared_catalog(monkeypatch):
    import agents.triage.server as triage_server

    provider = _Provider({"crucible": _Harness([_suite("uperf")])})
    expected, _ = await list_benchmark_catalog(provider)
    monkeypatch.setattr(triage_server, "_get_provider", lambda: provider)

    result = json.loads(await triage_server.list_benchmarks())

    assert result == expected
