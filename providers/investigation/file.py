"""File-based Investigation Record storage.

Stores each record as a JSON file in a configurable directory.
This is the default backend for development and testing — no
external services required.

Records are stored as:
    {persist_dir}/{investigation_id}.json
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .base import InvestigationRecordProvider
from .models import (
    BuildHistoryEntry,
    InvestigationRecord,
    InvestigationState,
)

logger = logging.getLogger(__name__)

_DEFAULT_DIR = Path.home() / ".agentic-perf" / "investigation-records"


class FileRecordProvider(InvestigationRecordProvider):
    """Stores investigation records as JSON files on disk."""

    provider_name = "file"

    def __init__(self, persist_dir: Path | str | None = None) -> None:
        self._dir = Path(persist_dir or _DEFAULT_DIR)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, investigation_id: str) -> Path:
        """Path to the JSON file for a given record."""
        safe = investigation_id.replace("/", "_")
        return self._dir / f"{safe}.json"

    def _write(self, record: InvestigationRecord) -> None:
        """Persist a record to disk."""
        path = self._path(record.investigation_id)
        path.write_text(
            record.model_dump_json(indent=2),
            encoding="utf-8",
        )

    def _read(self, path: Path) -> InvestigationRecord | None:
        """Read a record from disk. Returns None on error."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return InvestigationRecord.model_validate(data)
        except (json.JSONDecodeError, OSError, ValueError):
            logger.warning(f"[investigation] Failed to read {path}")
            return None

    async def create(self, record: InvestigationRecord) -> str:
        """Store a new record. Returns the investigation_id."""
        record.created_at = datetime.now(timezone.utc)
        record.updated_at = record.created_at
        self._write(record)
        logger.info(f"[investigation] Created record {record.investigation_id}")
        return record.investigation_id

    async def get(self, investigation_id: str) -> InvestigationRecord | None:
        """Fetch a single record by ID."""
        path = self._path(investigation_id)
        if not path.exists():
            return None
        return self._read(path)

    async def query(
        self,
        state: str | None = None,
        subsystem: str | None = None,
        platform: str | None = None,
        metric: str | None = None,
        limit: int = 100,
    ) -> list[InvestigationRecord]:
        """Query records by field filters.

        Scans all JSON files in the directory and filters in
        memory. Suitable for small-to-medium record counts
        (hundreds). For larger corpora, use a database-backed
        provider.
        """
        results: list[InvestigationRecord] = []
        for path in sorted(self._dir.glob("*.json"), reverse=True):
            record = self._read(path)
            if record is None:
                continue

            if state and record.state.value != state:
                continue
            if subsystem and record.anomaly_context.subsystem != subsystem:
                continue
            if platform and record.anomaly_context.platform != platform:
                continue
            if metric and record.anomaly_context.metric != metric:
                continue

            results.append(record)
            if len(results) >= limit:
                break

        # Sort by updated_at descending
        results.sort(key=lambda r: r.updated_at, reverse=True)
        return results

    async def update(
        self,
        investigation_id: str,
        updates: dict[str, Any],
    ) -> InvestigationRecord:
        """Update fields on an existing record."""
        record = await self.get(investigation_id)
        if record is None:
            raise KeyError(f"Record not found: {investigation_id}")

        # Apply updates to the model
        data = record.model_dump()
        for key, value in updates.items():
            if key in data:
                data[key] = value
        data["updated_at"] = datetime.now(timezone.utc).isoformat()

        record = InvestigationRecord.model_validate(data)
        self._write(record)
        logger.info(f"[investigation] Updated record {investigation_id}")
        return record

    async def append_build_history(
        self,
        investigation_id: str,
        entry: BuildHistoryEntry,
    ) -> None:
        """Append a build history entry to an existing record."""
        record = await self.get(investigation_id)
        if record is None:
            raise KeyError(f"Record not found: {investigation_id}")

        record.build_history.append(entry)
        record.updated_at = datetime.now(timezone.utc)
        self._write(record)
        logger.info(
            f"[investigation] Appended build history to "
            f"{investigation_id}: {entry.build_id}"
        )

    async def close_record(self, investigation_id: str) -> None:
        """Mark a record as resolved."""
        record = await self.get(investigation_id)
        if record is None:
            raise KeyError(f"Record not found: {investigation_id}")

        record.state = InvestigationState.RESOLVED
        record.updated_at = datetime.now(timezone.utc)
        self._write(record)
        logger.info(f"[investigation] Closed record {investigation_id}")
