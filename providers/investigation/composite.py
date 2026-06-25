"""Composite Investigation Record provider.

Routes writes to a single authoritative backend and fans out
reads across multiple backends. This supports scenarios like:

- Migration: old records in files, new records in OpenSearch
- Federated dedup: check multiple teams' record stores
- Local cache: write to primary backend, read from local mirror too

Query results are deduplicated by investigation_id — if the
same record exists in multiple backends, the writer's copy
takes precedence.
"""

from __future__ import annotations

import asyncio
import logging

from .base import InvestigationRecordProvider
from .models import BuildHistoryEntry, InvestigationRecord

logger = logging.getLogger(__name__)


class CompositeRecordProvider(InvestigationRecordProvider):
    """Fans out reads across multiple backends.

    All write operations (create, append_build_history,
    link_jira, close_record) go to the writer only. Read
    operations (get, query) check all readers and deduplicate
    results, with the writer's copy taking precedence.

    Args:
        writer: The authoritative backend for writes.
        readers: All backends to query for reads. Should
            include the writer if you want its records to
            appear in queries. Order matters for get() —
            the first match wins.
    """

    provider_name = "composite"

    def __init__(
        self,
        writer: InvestigationRecordProvider,
        readers: list[InvestigationRecordProvider],
    ) -> None:
        self._writer = writer
        self._readers = readers

    async def create(self, record: InvestigationRecord) -> str:
        """Write to the primary backend only."""
        return await self._writer.create(record)

    async def get(self, investigation_id: str) -> InvestigationRecord | None:
        """Check all readers in order, return first match.

        The reader list should have the writer first so its
        authoritative copy takes precedence.
        """
        for reader in self._readers:
            record = await reader.get(investigation_id)
            if record is not None:
                return record
        return None

    async def query(
        self,
        state: str | None = None,
        subsystem: str | None = None,
        platform: str | None = None,
        metric: str | None = None,
        limit: int = 100,
    ) -> list[InvestigationRecord]:
        """Fan out to all readers concurrently, deduplicate.

        Results are deduplicated by investigation_id. When the
        same ID appears in multiple backends, the first reader's
        copy wins (put the writer first in the readers list to
        give it precedence).
        """
        coros = [
            reader.query(
                state=state,
                subsystem=subsystem,
                platform=platform,
                metric=metric,
                limit=limit,
            )
            for reader in self._readers
        ]
        all_results = await asyncio.gather(*coros, return_exceptions=True)

        seen: dict[str, InvestigationRecord] = {}
        for result in all_results:
            if isinstance(result, Exception):
                logger.warning(f"[investigation] Reader query failed: {result}")
                continue
            for record in result:
                # First reader wins on duplicate IDs
                if record.investigation_id not in seen:
                    seen[record.investigation_id] = record

        records = sorted(
            seen.values(),
            key=lambda r: r.created_at,
            reverse=True,
        )
        return records[:limit]

    async def append_build_history(
        self,
        investigation_id: str,
        entry: BuildHistoryEntry,
    ) -> None:
        """Append to the writer only."""
        await self._writer.append_build_history(investigation_id, entry)

    async def link_jira(
        self,
        investigation_id: str,
        jira_ticket: str,
    ) -> None:
        """Link Jira on the writer only."""
        await self._writer.link_jira(investigation_id, jira_ticket)

    async def close_record(self, investigation_id: str) -> None:
        """Close on the writer only."""
        await self._writer.close_record(investigation_id)

    async def close(self) -> None:
        """Close all backends."""
        for reader in self._readers:
            try:
                await reader.close()
            except Exception:
                logger.warning("[investigation] Failed to close reader")
        # Writer may be in readers list; close it explicitly
        # only if it wasn't already closed
        if self._writer not in self._readers:
            try:
                await self._writer.close()
            except Exception:
                logger.warning("[investigation] Failed to close writer")
