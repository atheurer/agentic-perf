"""Abstract interface for Investigation Record storage.

Any storage backend (file, Horreum, OpenSearch, Elasticsearch, S3,
PostgreSQL, etc.) implements this interface. Agents interact with
records through these methods, never with the backend directly.

Records are write-once: all investigation data (root cause,
confidence, operational metrics, change attribution) is set at
creation time and never modified. The only mutations allowed are:
- Appending build history entries (tracking regression across builds)
- Linking a Jira ticket (one-time, only if not already set)
- Closing the record (OPEN -> RESOLVED lifecycle transition)

This pattern follows the existing ResourceProvider abstraction in
providers/resource/base.py.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .models import BuildHistoryEntry, InvestigationRecord


class InvestigationRecordProvider(ABC):
    """Abstract base for investigation record storage backends.

    Records are write-once artifacts of completed investigations.
    Investigation data is immutable after creation — only build
    history (append-only), Jira linkage (one-time), and lifecycle
    state (OPEN -> RESOLVED) can change.
    """

    provider_name: str = "abstract"

    @abstractmethod
    async def create(self, record: InvestigationRecord) -> str:
        """Store a new record. Returns the investigation_id.

        All investigation data must be set before calling create.
        The record becomes immutable after this call.
        """
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
        Results are ordered by created_at descending (newest first).
        """
        ...

    @abstractmethod
    async def append_build_history(
        self,
        investigation_id: str,
        entry: BuildHistoryEntry,
    ) -> None:
        """Append a build history entry to an existing record.

        This is append-only — existing entries cannot be modified
        or removed.

        Raises:
            KeyError: If the record does not exist.
        """
        ...

    @abstractmethod
    async def link_jira(
        self,
        investigation_id: str,
        jira_ticket: str,
    ) -> None:
        """Link a Jira ticket to a record.

        Can only be called once — raises ValueError if the record
        already has a Jira ticket linked.

        Raises:
            KeyError: If the record does not exist.
            ValueError: If a Jira ticket is already linked.
        """
        ...

    @abstractmethod
    async def close_record(self, investigation_id: str) -> None:
        """Mark a record as resolved.

        One-way transition: OPEN -> RESOLVED. Cannot be reopened.

        Raises:
            KeyError: If the record does not exist.
        """
        ...

    async def close(self) -> None:
        """Release any held connections or resources."""
        pass
