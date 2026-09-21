"""Read-only benchmark catalog helpers shared by chat and triage."""

from __future__ import annotations

import logging
from typing import Any

from .base import BenchmarkSuite

logger = logging.getLogger(__name__)

# This benchmark is supplied by a standalone agent tool rather than a harness
# skill provider.  Keep it in the common catalog so chat and triage describe
# the same capabilities.
STANDALONE_BENCHMARKS = (
    BenchmarkSuite(
        name="boot-time",
        description=(
            "Boot time analysis — reboots a remote system multiple times and "
            "collects kernel, initrd, and userspace timing metrics per cycle. "
            "Uses boot-time-analysis-tools."
        ),
        roles=["client"],
        min_hosts=1,
        harness="boot-time",
    ),
)


def benchmark_entry(suite: BenchmarkSuite) -> dict[str, Any]:
    """Return the public, serializable capability view of one suite."""
    entry: dict[str, Any] = {
        "name": suite.name,
        "description": suite.description,
        "supported_params": suite.supported_params,
        "roles": suite.roles,
        "min_hosts": suite.min_hosts,
        "harness": suite.harness,
        "source": suite.source,
    }
    if suite.endpoint_types:
        entry["endpoint_types"] = suite.endpoint_types
    return entry


async def list_benchmark_catalog(
    provider: Any,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Collect available suites without making one unavailable harness fatal.

    ``build_skill_provider(catalog_only=True)`` already contains one Crucible
    catalog provider.  Iterating that aggregate exactly once avoids the stale
    PR's second, duplicate Crucible lookup.
    """
    entries = [benchmark_entry(suite) for suite in STANDALONE_BENCHMARKS]
    unavailable: list[str] = []
    seen = {(entry["harness"], entry["name"]) for entry in entries}

    for harness in provider.list_harnesses():
        harness_provider = provider.get_provider(harness)
        if harness_provider is None:
            unavailable.append(harness)
            continue
        try:
            suites = await harness_provider.list_benchmarks()
        except Exception:
            logger.debug(
                "Benchmark catalog unavailable for harness %s", harness, exc_info=True
            )
            unavailable.append(harness)
            continue
        for suite in suites:
            key = (suite.harness, suite.name)
            if key not in seen:
                entries.append(benchmark_entry(suite))
                seen.add(key)

    entries.sort(key=lambda entry: (entry["harness"], entry["name"]))
    return entries, unavailable


async def get_catalog_benchmark(provider: Any, name: str) -> dict[str, Any] | None:
    """Return one benchmark detail record using the same catalog sources."""
    for suite in STANDALONE_BENCHMARKS:
        if suite.name == name:
            return benchmark_entry(suite)
    try:
        suite = await provider.get_benchmark(name)
    except Exception:
        logger.debug("Benchmark details unavailable for %s", name, exc_info=True)
        return None
    return benchmark_entry(suite) if suite is not None else None
