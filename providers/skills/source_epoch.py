"""Durable, ticket-attempt scoped pins for mutable context sources."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Callable

from providers.execution import (
    AuditedFilesystem,
    RootedPath,
    durable_filesystem_emitter,
)

from .gateway import SkillGatewayError

_SOURCE_ID = re.compile(r"^[a-z0-9][a-z0-9:/._-]{0,127}$")
_MAX_RECORD_BYTES = 64 * 1024 * 1024


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class SourceEpoch:
    """Store immutable source captures for one trusted ticket and attempt.

    The index is the tombstone for a source capture: once indexed, a missing
    or corrupt snapshot fails closed instead of recapturing today's content.
    Source IDs and identities stay service-side; callers expose only revisions
    and document provenance that they explicitly project.
    """

    def __init__(
        self,
        snapshot_root: str | Path,
        *,
        ticket_id: str,
        attempt_id: str,
        audit_emit: Any | None = None,
    ) -> None:
        if not ticket_id or not attempt_id:
            raise SkillGatewayError("missing_attempt", "Trusted attempt is required")
        self.snapshot_root = Path(snapshot_root).resolve()
        self.ticket_id = ticket_id
        self.attempt_id = attempt_id
        self.key = hashlib.sha256(
            _canonical(["source-epoch", ticket_id, attempt_id])
        ).hexdigest()
        self.audit_emit = audit_emit or durable_filesystem_emitter()

    @staticmethod
    def _source_key(source_id: str) -> str:
        if not isinstance(source_id, str) or not _SOURCE_ID.fullmatch(source_id):
            raise SkillGatewayError("invalid_source", "Invalid context source id")
        return hashlib.sha256(source_id.encode("utf-8")).hexdigest()

    def _filesystem(self) -> AuditedFilesystem:
        return AuditedFilesystem(
            RootedPath(self.snapshot_root, "skill-service"),
            ticket_id=self.ticket_id,
            emit=self.audit_emit,
            critical=True,
        )

    def _directory(self) -> Path:
        return self.snapshot_root / self.key

    def _read_index(self) -> set[str]:
        directory = self._directory()
        index_path = directory / "epoch.json"
        pin_path = directory / "epoch.pin.json"
        if not index_path.exists() and not pin_path.exists():
            return set()
        if not index_path.is_file() or not pin_path.is_file():
            raise SkillGatewayError(
                "invalid_snapshot", "Context epoch index is missing"
            )
        try:
            data = index_path.read_bytes()
            if len(data) > 1024 * 1024:
                raise ValueError("index exceeds limit")
            pin = json.loads(pin_path.read_text(encoding="utf-8"))
            if pin.get("digest") != _digest(data):
                raise ValueError("index digest mismatch")
            value = json.loads(data)
            sources = value["sources"]
            if (
                value.get("schema_version") != 1
                or not isinstance(sources, list)
                or any(not isinstance(item, str) for item in sources)
                or sources != sorted(set(sources))
            ):
                raise ValueError("invalid index")
            return set(sources)
        except (OSError, ValueError, KeyError, TypeError, RecursionError):
            raise SkillGatewayError(
                "invalid_snapshot", "Context epoch index is corrupt"
            ) from None

    def _read_record(self, source_id: str, identity: str) -> dict[str, Any] | None:
        source_key = self._source_key(source_id)
        directory = self._directory()
        indexed = source_id in self._read_index()
        record_path = directory / f"source-{source_key}.json"
        pin_path = directory / f"source-{source_key}.pin.json"
        if not indexed:
            if record_path.exists() or pin_path.exists():
                raise SkillGatewayError(
                    "invalid_snapshot", "Context source pin is not indexed"
                )
            return None
        try:
            with record_path.open("rb") as handle:
                record_bytes = handle.read(_MAX_RECORD_BYTES + 1)
            if len(record_bytes) > _MAX_RECORD_BYTES:
                raise ValueError("source snapshot exceeds limit")
            pin = json.loads(pin_path.read_text(encoding="utf-8"))
            if pin.get("digest") != _digest(record_bytes):
                raise ValueError("source digest mismatch")
            record = json.loads(record_bytes)
            if (
                record.get("schema_version") != 1
                or record.get("source_id") != source_id
                or record.get("identity") != identity
                or not isinstance(record.get("snapshot"), dict)
            ):
                raise SkillGatewayError(
                    "source_binding_changed", "Context source binding changed"
                )
            return record["snapshot"]
        except SkillGatewayError:
            raise
        except (OSError, ValueError, KeyError, TypeError, RecursionError):
            raise SkillGatewayError(
                "invalid_snapshot", "Pinned context source is unavailable"
            ) from None

    def read(self, source_id: str, identity: str) -> dict[str, Any] | None:
        """Return a verified pin without reading the live source."""
        self._source_key(source_id)
        filesystem = self._filesystem()
        try:
            filesystem.mkdir(".", mode=0o700)
            filesystem.mkdir(self.key, mode=0o700)
            lock_fd = filesystem.open_descriptor(
                f"{self.key}/epoch.lock", os.O_CREAT | os.O_RDWR
            )
        except Exception:
            raise SkillGatewayError(
                "snapshot_unavailable", "Cannot access context epoch storage"
            ) from None
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            return self._read_record(source_id, identity)
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)

    def get_or_capture(
        self,
        source_id: str,
        identity: str,
        capture: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        """Return an existing verified pin or durably capture it exactly once."""
        source_key = self._source_key(source_id)
        filesystem = self._filesystem()
        directory = self._directory()
        try:
            filesystem.mkdir(".", mode=0o700)
            filesystem.mkdir(self.key, mode=0o700)
            lock_fd = filesystem.open_descriptor(
                f"{self.key}/epoch.lock", os.O_CREAT | os.O_RDWR
            )
        except Exception:
            raise SkillGatewayError(
                "snapshot_unavailable", "Cannot access context epoch storage"
            ) from None
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            existing = self._read_record(source_id, identity)
            if existing is not None:
                return existing
            snapshot = capture()
            if not isinstance(snapshot, dict):
                raise SkillGatewayError(
                    "source_snapshot_unavailable", "Context source capture is invalid"
                )
            record = {
                "schema_version": 1,
                "source_id": source_id,
                "identity": identity,
                "snapshot": snapshot,
            }
            record_bytes = _canonical(record)
            if len(record_bytes) > _MAX_RECORD_BYTES:
                raise SkillGatewayError(
                    "source_too_large", "Context source snapshot exceeds its limit"
                )
            pin_bytes = _canonical({"digest": _digest(record_bytes)})
            record_name = f"source-{source_key}.json"
            pin_name = f"source-{source_key}.pin.json"
            if (directory / record_name).exists() or (directory / pin_name).exists():
                raise SkillGatewayError(
                    "invalid_snapshot", "Pinned context source is incomplete"
                )
            filesystem.write(f"{self.key}/{record_name}", record_bytes)
            filesystem.write(f"{self.key}/{pin_name}", pin_bytes)
            sources = self._read_index()
            sources.add(source_id)
            index_bytes = _canonical({"schema_version": 1, "sources": sorted(sources)})
            filesystem.write(f"{self.key}/epoch.json", index_bytes)
            filesystem.write(
                f"{self.key}/epoch.pin.json",
                _canonical({"digest": _digest(index_bytes)}),
            )
            return snapshot
        except SkillGatewayError:
            raise
        except Exception:
            raise SkillGatewayError(
                "snapshot_unavailable", "Cannot persist context source snapshot"
            ) from None
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
