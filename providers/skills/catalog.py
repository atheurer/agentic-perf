"""Unified benchmark catalog for capability discovery.

Combines all benchmark sources into a single queryable
catalog.  Used by both triage (for harness selection) and
chat (for answering capability questions).

Sources:
- MultiHarnessSkillProvider (non-Crucible harnesses)
- CrucibleContextGateway (Crucible benchmarks)
- Standalone benchmarks (boot-time, etc.)

This abstraction is the migration boundary: as harness-
specific context gateways replace build_skill_provider(),
they register here and consumers (triage, chat) are
unaffected.
"""

from __future__ import annotations

import logging
from typing import Any

from providers.skills.base import BenchmarkSuite

logger = logging.getLogger(__name__)

# Benchmarks provided by standalone tools, not by any
# harness skill provider.
_STANDALONE_BENCHMARKS = [
    BenchmarkSuite(
        name="boot-time",
        description=(
            "Boot time analysis — reboots a remote system "
            "multiple times and collects kernel, initrd, and "
            "userspace timing metrics per cycle. Uses "
            "boot-time-analysis-tools. NO provisioning "
            "step — the benchmark tool installs "
            "dependencies on the SUT automatically via SSH. "
            "Do NOT tell provisioning to install any "
            "boot-time packages."
        ),
        roles=["client"],
        min_hosts=1,
        harness="boot-time",
    ),
]


class BenchmarkCatalog:
    """Unified benchmark catalog combining all sources.

    Lazily initializes providers on first access.
    Thread-safe for concurrent reads (providers are
    stateless after init).
    """

    def __init__(self) -> None:
        self._initialized = False
        self._providers: list[Any] = []

    def _ensure_init(self) -> None:
        if self._initialized:
            return
        self._initialized = True

        try:
            from agents.server_utils import (
                build_crucible_context_gateway,
                build_skill_provider,
            )

            self._providers.append(
                build_skill_provider(
                    resolve_source=False,
                    catalog_only=True,
                )
            )
        except Exception:
            logger.debug(
                "Failed to init skill provider for catalog",
                exc_info=True,
            )

        try:
            from agents.server_utils import (
                build_crucible_context_gateway,
            )

            self._providers.append(
                build_crucible_context_gateway(
                    resolve_source=False,
                    catalog_only=True,
                )
            )
        except Exception:
            logger.debug(
                "Failed to init Crucible gateway for catalog",
                exc_info=True,
            )

    async def list_benchmarks(
        self,
        harness: str = "",
        query: str = "",
    ) -> list[dict[str, Any]]:
        """List all available benchmarks with optional filtering.

        Args:
            harness: Filter by harness name (case-insensitive).
            query: Filter by name or description substring.

        Returns:
            List of benchmark dicts with name, description,
            harness, roles, min_hosts, endpoint_types.
        """
        self._ensure_init()

        all_suites: list[BenchmarkSuite] = list(_STANDALONE_BENCHMARKS)
        for provider in self._providers:
            try:
                suites = await provider.list_benchmarks()
                all_suites.extend(suites)
            except Exception:
                logger.debug(
                    "Failed to list benchmarks from provider",
                    exc_info=True,
                )

        harness_lower = harness.lower()
        query_lower = query.lower()

        results = []
        for b in all_suites:
            if harness_lower and b.harness.lower() != harness_lower:
                continue
            if query_lower and (
                query_lower not in b.name.lower()
                and query_lower not in b.description.lower()
            ):
                continue
            entry: dict[str, Any] = {
                "name": b.name,
                "description": b.description,
                "harness": b.harness,
                "roles": b.roles,
                "min_hosts": b.min_hosts,
            }
            if b.endpoint_types:
                entry["endpoint_types"] = b.endpoint_types
            if b.supported_params:
                entry["supported_params"] = b.supported_params
            if b.source:
                entry["source"] = b.source
            results.append(entry)

        return results

    async def get_benchmark(
        self,
        name: str,
    ) -> dict[str, Any] | None:
        """Get details for a specific benchmark by name."""
        self._ensure_init()

        # Check standalone first.
        for sb in _STANDALONE_BENCHMARKS:
            if sb.name == name:
                return {
                    "name": sb.name,
                    "description": sb.description,
                    "harness": sb.harness,
                    "roles": sb.roles,
                    "min_hosts": sb.min_hosts,
                }

        for provider in self._providers:
            try:
                b = await provider.get_benchmark(name)
                if b is not None:
                    return {
                        "name": b.name,
                        "description": b.description,
                        "harness": b.harness,
                        "roles": b.roles,
                        "min_hosts": b.min_hosts,
                        "endpoint_types": b.endpoint_types,
                        "supported_params": b.supported_params,
                        "source": b.source,
                    }
            except Exception:
                continue
        return None


# Module-level singleton.
_catalog: BenchmarkCatalog | None = None


def get_benchmark_catalog() -> BenchmarkCatalog:
    """Return the shared benchmark catalog singleton."""
    global _catalog
    if _catalog is None:
        _catalog = BenchmarkCatalog()
    return _catalog
