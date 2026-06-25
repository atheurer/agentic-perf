"""Abstract interface for Investigation Record storage.

Any storage backend (file, Horreum, OpenSearch, Elasticsearch, S3,
PostgreSQL, etc.) implements this interface. Agents interact with
records through these methods, never with the backend directly.

This pattern follows the existing ResourceProvider abstraction in
providers/resource/base.py.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from .models import BuildHistoryEntry, InvestigationRecord


class InvestigationRecordProvider(ABC):
    """Abstract base for investigation record storage backends."""

    provider_name: str = "abstract"

    @abstractmethod
    async def create(self, record: InvestigationRecord) -> str:
        """Store a new record. Returns the investigation_id."""
        ...

    @abstractmethod
    async def get(self, investigation_id: str) -> InvestigationRecord | None:
        """Fetch a single record by ID. Returns None if not found."""
        ...

    @abstractmethod
    async def query(
        self,
        state: str | None = None,
        subsystem: str | None = None,
        platform: str | None = None,
        metric: str | None = None,
        limit: int = 100,
    ) -> list[InvestigationRecord]:
        """Query records by field filters.

        All filters are optional — omitted filters match everything.
        Results are ordered by updated_at descending (newest first).
        """
        ...

    @abstractmethod
    async def update(
        self,
        investigation_id: str,
        updates: dict[str, Any],
    ) -> InvestigationRecord:
        """Update fields on an existing record.

        Args:
            investigation_id: Record to update.
            updates: Dict of field names to new values. Nested
                fields use dot notation in the backend but are
                passed as nested dicts here.

        Returns:
            The updated record.

        Raises:
            KeyError: If the record does not exist.
        """
        ...

    @abstractmethod
    async def append_build_history(
        self,
        investigation_id: str,
        entry: BuildHistoryEntry,
    ) -> None:
        """Append a build history entry to an existing record.

        Raises:
            KeyError: If the record does not exist.
        """
        ...

    @abstractmethod
    async def close_record(self, investigation_id: str) -> None:
        """Mark a record as resolved.

        Raises:
            KeyError: If the record does not exist.
        """
        ...

    async def close(self) -> None:
        """Release any held connections or resources."""
        pass
