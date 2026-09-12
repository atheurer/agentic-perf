"""Durable, append-only SQLite storage for trace event envelopes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from providers.tracing import PayloadDescriptor, TraceEventV1

from .trace_migrations import migrate


class TraceStoreError(RuntimeError):
    """Base class for trace persistence failures."""


class TraceStoreMigrationError(TraceStoreError):
    """The database could not be checked or migrated safely."""


class TraceStoreWriteError(TraceStoreError):
    """A trace write could not be committed."""


class TraceEventConflictError(TraceStoreError):
    """An event ID was reused with different immutable content."""


class TracePayloadConflictError(TraceStoreWriteError):
    """A payload digest was reused with incompatible safe metadata."""


@dataclass(frozen=True)
class OperationRecord:
    operation_key: str
    request_hash: str
    state: str
    owner: str | None = None
    lease_expires_at: str | None = None
    fencing_generation: int = 0
    result_descriptor: dict[str, Any] | None = None
    external_ids: dict[str, Any] | None = None


class TraceStore:
    """The authoritative SQLite store, with one connection per store instance."""

    def __init__(self, db_path: Path, *, busy_timeout_ms: int = 5_000) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._connection = sqlite3.connect(
                self.db_path,
                timeout=busy_timeout_ms / 1000,
                isolation_level=None,
                check_same_thread=False,
            )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
            deadline = time.monotonic() + busy_timeout_ms / 1000
            self._startup_execute("PRAGMA journal_mode = WAL", deadline)
            check = self._startup_execute(
                "PRAGMA integrity_check", deadline
            ).fetchone()[0]
            if check != "ok":
                raise TraceStoreMigrationError(
                    f"trace database integrity check failed: {check}"
                )
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                migrate(self._connection)
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        except TraceStoreMigrationError:
            self.close()
            raise
        except (sqlite3.DatabaseError, OSError, RuntimeError) as exc:
            self.close()
            raise TraceStoreMigrationError(
                "could not initialize trace database"
            ) from exc

    def _startup_execute(self, statement: str, deadline: float) -> sqlite3.Cursor:
        """Run startup pragmas while other processes initialize the same DB.

        SQLite serializes the WAL mode transition independently of the migration
        transaction.  Retrying only lock/busy failures keeps simultaneous first
        opens reliable while retaining a finite, explicit startup failure.
        """
        while True:
            try:
                return self._connection.execute(statement)
            except sqlite3.OperationalError as exc:
                message = str(exc).lower()
                if "locked" not in message and "busy" not in message:
                    raise
                if time.monotonic() >= deadline:
                    raise
                time.sleep(min(0.025, max(0, deadline - time.monotonic())))

    def close(self) -> None:
        """Close the connection; safe to call after a failed initialization."""
        connection = getattr(self, "_connection", None)
        if connection is not None:
            connection.close()
            self._connection = None

    def _open_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise TraceStoreWriteError("trace store is closed")
        return self._connection

    def __enter__(self) -> TraceStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def _content(event: TraceEventV1) -> tuple[str, str]:
        data = event.model_dump(mode="json")
        data.update({"recorded_at": None, "global_seq": None, "ticket_seq": None})
        encoded = json.dumps(data, sort_keys=True, separators=(",", ":"))
        return encoded, hashlib.sha256(encoded.encode()).hexdigest()

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> TraceEventV1:
        return TraceEventV1.model_validate_json(row["event_json"])

    def insert_event(self, event: TraceEventV1) -> TraceEventV1:
        """Append an event, or return its original record for an exact replay."""
        return self.insert_event_result(event)[0]

    def insert_event_result(self, event: TraceEventV1) -> tuple[TraceEventV1, bool]:
        """Atomically insert or return ``(event, duplicate)`` for a replay."""
        try:
            connection = self._open_connection()
            _, content_hash = self._content(event)
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT content_hash, event_json FROM trace_events WHERE event_id = ?",
                (str(event.event_id),),
            ).fetchone()
            if existing:
                connection.rollback()
                if existing["content_hash"] != content_hash:
                    raise TraceEventConflictError(
                        f"event {event.event_id} already exists with different content"
                    )
                return TraceEventV1.model_validate_json(existing["event_json"]), True
            global_seq = connection.execute(
                "SELECT COALESCE(MAX(global_seq), 0) + 1 FROM trace_events"
            ).fetchone()[0]
            ticket_seq = connection.execute(
                "SELECT COALESCE(MAX(ticket_seq), 0) + 1 FROM trace_events "
                "WHERE ticket_id = ?",
                (event.ticket_id,),
            ).fetchone()[0]
            stored = event.model_copy(
                update={
                    "global_seq": global_seq,
                    "ticket_seq": ticket_seq,
                    "recorded_at": datetime.now(timezone.utc),
                }
            )
            event_json = stored.model_dump_json()
            connection.execute(
                "INSERT INTO trace_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(stored.event_id),
                    content_hash,
                    global_seq,
                    stored.ticket_id,
                    ticket_seq,
                    stored.trace_id,
                    str(stored.invocation_id) if stored.invocation_id else None,
                    stored.action_id,
                    stored.parent_action_id,
                    stored.action.type.value,
                    stored.lifecycle.state.value,
                    stored.outcome.value if stored.outcome else None,
                    stored.producer.component,
                    stored.occurred_at.isoformat(),
                    event_json,
                ),
            )
            connection.commit()
            return stored, False
        except TraceEventConflictError:
            raise
        except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
            connection = getattr(self, "_connection", None)
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise TraceStoreWriteError("could not write trace event") from exc

    def list_events(self, ticket_id: str | None = None) -> list[TraceEventV1]:
        """Return immutable events in their authoritative insertion order."""
        try:
            query = "SELECT event_json FROM trace_events"
            values: tuple[str, ...] = ()
            if ticket_id is not None:
                query += " WHERE ticket_id = ?"
                values = (ticket_id,)
            query += " ORDER BY global_seq"
            return [
                TraceEventV1.model_validate_json(row["event_json"])
                for row in self._open_connection().execute(query, values)
            ]
        except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
            raise TraceStoreWriteError("could not read trace events") from exc

    def put_payload_descriptor(self, descriptor: PayloadDescriptor) -> None:
        """Persist safe payload metadata only; payload bytes are never stored in SQLite."""
        if not descriptor.digest:
            raise TraceStoreWriteError("payload descriptor requires a digest")
        try:
            connection = self._open_connection()
            connection.execute("BEGIN IMMEDIATE")
            encoded = descriptor.model_dump_json()
            existing = connection.execute(
                "SELECT descriptor_json FROM trace_payloads WHERE digest = ?",
                (descriptor.digest,),
            ).fetchone()
            if existing is not None:
                connection.rollback()
                if existing["descriptor_json"] != encoded:
                    raise TracePayloadConflictError(
                        f"payload {descriptor.digest} already has different metadata"
                    )
                return
            connection.execute(
                "INSERT INTO trace_payloads(digest, descriptor_json) VALUES (?, ?)",
                (descriptor.digest, encoded),
            )
            connection.commit()
        except TracePayloadConflictError:
            raise
        except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
            connection = getattr(self, "_connection", None)
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise TraceStoreWriteError("could not write payload metadata") from exc

    def get_payload_descriptor(self, digest: str) -> PayloadDescriptor | None:
        """Return a safe descriptor; this API intentionally cannot retrieve blobs."""
        try:
            row = (
                self._open_connection()
                .execute(
                    "SELECT descriptor_json FROM trace_payloads WHERE digest = ?",
                    (digest,),
                )
                .fetchone()
            )
            return (
                PayloadDescriptor.model_validate_json(row["descriptor_json"])
                if row is not None
                else None
            )
        except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
            raise TraceStoreWriteError("could not read payload metadata") from exc

    def create_operation(self, operation: OperationRecord) -> OperationRecord:
        """Persist an operation record; operation lifecycle policy lives elsewhere."""
        return self._write_operation(operation, insert=True)

    def update_operation(self, operation: OperationRecord) -> OperationRecord:
        """Replace stored operation fields without applying state-machine semantics."""
        return self._write_operation(operation, insert=False)

    def _write_operation(
        self, operation: OperationRecord, *, insert: bool
    ) -> OperationRecord:
        try:
            connection = self._open_connection()
            statement = (
                "INSERT INTO operations VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                if insert
                else "UPDATE operations SET request_hash=?, state=?, owner=?, "
                "lease_expires_at=?, fencing_generation=?, result_descriptor=?, "
                "external_ids=? WHERE operation_key=?"
            )
            values = (
                operation.operation_key,
                operation.request_hash,
                operation.state,
                operation.owner,
                operation.lease_expires_at,
                operation.fencing_generation,
                json.dumps(operation.result_descriptor)
                if operation.result_descriptor is not None
                else None,
                json.dumps(operation.external_ids)
                if operation.external_ids is not None
                else None,
            )
            if not insert:
                values = values[1:] + values[:1]
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(statement, values)
            if not insert and cursor.rowcount != 1:
                connection.rollback()
                raise TraceStoreWriteError(
                    f"operation {operation.operation_key} was not found"
                )
            connection.commit()
            return operation
        except (
            TraceStoreWriteError,
            sqlite3.Error,
            OSError,
            TypeError,
            ValueError,
        ) as exc:
            connection = getattr(self, "_connection", None)
            if connection is not None and connection.in_transaction:
                connection.rollback()
            if isinstance(exc, TraceStoreWriteError):
                raise
            raise TraceStoreWriteError("could not write operation") from exc

    def get_operation(self, operation_key: str) -> OperationRecord | None:
        try:
            row = (
                self._open_connection()
                .execute(
                    "SELECT * FROM operations WHERE operation_key = ?", (operation_key,)
                )
                .fetchone()
            )
            if row is None:
                return None
            values = dict(row)
            values["result_descriptor"] = (
                json.loads(values["result_descriptor"])
                if values["result_descriptor"] is not None
                else None
            )
            values["external_ids"] = (
                json.loads(values["external_ids"])
                if values["external_ids"] is not None
                else None
            )
            return OperationRecord(**values)
        except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
            raise TraceStoreWriteError("could not read operation") from exc

    def delete_operation(self, operation_key: str) -> None:
        """Delete an operation record; event rows intentionally have no equivalent API."""
        try:
            connection = self._open_connection()
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM operations WHERE operation_key = ?", (operation_key,)
            )
            connection.commit()
        except (TraceStoreWriteError, sqlite3.Error, OSError) as exc:
            connection = getattr(self, "_connection", None)
            if connection is not None and connection.in_transaction:
                connection.rollback()
            if isinstance(exc, TraceStoreWriteError):
                raise
            raise TraceStoreWriteError("could not delete operation") from exc
