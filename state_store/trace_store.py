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

from providers.tracing import (
    ActionDescriptor,
    ActionType,
    LifecycleDescriptor,
    LifecycleState,
    OperationOutcome,
    PayloadDescriptor,
    TraceEventV1,
)

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


class OperationConflictError(TraceStoreWriteError):
    """An idempotency key was reused for different immutable input."""


class OperationLeaseError(TraceStoreWriteError):
    """An operation write was attempted by a stale or unauthorized owner."""


class OperationTransitionError(TraceStoreWriteError):
    """An operation state transition is not legal."""


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
    terminal_outcome: str | None = None


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

    def _rollback_if_open(self) -> None:
        """Leave no transaction open when a pre-commit policy check raises."""
        connection = getattr(self, "_connection", None)
        if connection is not None and connection.in_transaction:
            connection.rollback()

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
            connection.execute("BEGIN IMMEDIATE")
            stored, duplicate = self._insert_event_in_transaction(connection, event)
            if duplicate:
                connection.rollback()
                return stored, True
            connection.commit()
            return stored, False
        except TraceEventConflictError:
            raise
        except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
            connection = getattr(self, "_connection", None)
            if connection is not None and connection.in_transaction:
                connection.rollback()
            raise TraceStoreWriteError("could not write trace event") from exc

    def _insert_event_in_transaction(
        self, connection: sqlite3.Connection, event: TraceEventV1
    ) -> tuple[TraceEventV1, bool]:
        """Insert an event in the caller's immediate transaction."""
        _, content_hash = self._content(event)
        existing = connection.execute(
            "SELECT content_hash, event_json FROM trace_events WHERE event_id = ?",
            (str(event.event_id),),
        ).fetchone()
        if existing:
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
        return stored, False

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
                "INSERT INTO operations(operation_key,request_hash,state,owner,lease_expires_at,fencing_generation,result_descriptor,external_ids,terminal_outcome) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                if insert
                else "UPDATE operations SET request_hash=?, state=?, owner=?, "
                "lease_expires_at=?, fencing_generation=?, result_descriptor=?, "
                "external_ids=?, terminal_outcome=? WHERE operation_key=?"
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
                operation.terminal_outcome,
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

    def get_operation_result(self, operation_key: str) -> dict[str, Any] | None:
        """Return an authenticated operation's exact terminal result."""
        try:
            row = (
                self._open_connection()
                .execute(
                    "SELECT result_json FROM operation_results WHERE operation_key=?",
                    (operation_key,),
                )
                .fetchone()
            )
            return json.loads(row["result_json"]) if row is not None else None
        except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
            raise TraceStoreWriteError("could not read operation result") from exc

    @staticmethod
    def _lease_time(value: datetime | None = None) -> str:
        """Use one UTC policy for every lease comparison and persisted expiry."""
        value = value or datetime.now(timezone.utc)
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise OperationTransitionError("lease times must be timezone-aware UTC")
        return value.isoformat()

    @staticmethod
    def _bounded_descriptor(value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode()) > 4096:
            raise OperationTransitionError("terminal descriptor exceeds 4096 bytes")
        return json.loads(encoded)

    @staticmethod
    def _from_operation_row(row: sqlite3.Row) -> OperationRecord:
        values = dict(row)
        for key in ("result_descriptor", "external_ids"):
            if values[key] is not None:
                values[key] = json.loads(values[key])
        return OperationRecord(**values)

    def _history(
        self, connection: sqlite3.Connection, operation: OperationRecord, reason: str
    ) -> None:
        connection.execute(
            "INSERT INTO operation_history(operation_key,state,owner,fencing_generation,reason,occurred_at,terminal_outcome) "
            "VALUES (?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), ?)",
            (
                operation.operation_key,
                operation.state,
                operation.owner,
                operation.fencing_generation,
                reason,
                operation.terminal_outcome,
            ),
        )

    def _audit(
        self, connection: sqlite3.Connection, operation: OperationRecord, reason: str
    ) -> None:
        """Append operation history and its canonical trace envelope atomically."""
        self._history(connection, operation, reason)
        rejected = reason.startswith("rejected:")
        terminal = operation.state == "terminal"
        state = (
            LifecycleState.REJECTED
            if rejected
            else {
                "success": LifecycleState.COMPLETED,
                "failure": LifecycleState.FAILED,
                "cancelled": LifecycleState.CANCELLED,
                "rejected": LifecycleState.REJECTED,
                "indeterminate": LifecycleState.INDETERMINATE,
            }.get(operation.terminal_outcome or "success", LifecycleState.INDETERMINATE)
            if terminal
            else LifecycleState(operation.state)
        )
        event = TraceEventV1(
            ticket_id=operation.operation_key,
            action=ActionDescriptor(
                type=ActionType.OPERATION, target=operation.operation_key
            ),
            lifecycle=LifecycleDescriptor(state=state),
            idempotency={
                "key": operation.operation_key,
                "request_hash": operation.request_hash,
                "fencing_token": operation.fencing_generation,
                "outcome": "rejected" if rejected else "claimed",
            },
            outcome=(
                OperationOutcome.REJECTED
                if rejected
                else OperationOutcome(operation.terminal_outcome or "success")
                if terminal
                else None
            ),
            duration_ms=0 if rejected or terminal else None,
            attributes={"operation_state": operation.state, "reason": reason},
        )
        self._insert_event_in_transaction(connection, event)

    def operation_history(self, operation_key: str) -> list[dict[str, Any]]:
        try:
            return [
                dict(row)
                for row in self._open_connection().execute(
                    "SELECT state, owner, fencing_generation, reason, occurred_at, terminal_outcome "
                    "FROM operation_history WHERE operation_key=? ORDER BY history_id",
                    (operation_key,),
                )
            ]
        except sqlite3.Error as exc:
            raise TraceStoreWriteError("could not read operation history") from exc

    def register_or_get(
        self, operation: OperationRecord
    ) -> tuple[OperationRecord, bool]:
        """Atomically register immutable input, returning ``(record, existed)``."""
        if not operation.operation_key or not operation.request_hash:
            raise OperationTransitionError(
                "operation key and request hash are required"
            )
        try:
            connection = self._open_connection()
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM operations WHERE operation_key=?",
                (operation.operation_key,),
            ).fetchone()
            if row is not None:
                found = self._from_operation_row(row)
                if found.request_hash != operation.request_hash:
                    self._audit(connection, found, "rejected:request_hash_conflict")
                    connection.commit()
                    raise OperationConflictError(
                        "operation key is already bound to different input"
                    )
                connection.rollback()
                return found, True
            registered = OperationRecord(
                operation.operation_key, operation.request_hash, "registered"
            )
            connection.execute(
                "INSERT INTO operations(operation_key,request_hash,state,owner,lease_expires_at,fencing_generation,result_descriptor,external_ids,terminal_outcome) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    registered.operation_key,
                    registered.request_hash,
                    registered.state,
                    None,
                    None,
                    0,
                    None,
                    None,
                    None,
                ),
            )
            self._audit(connection, registered, "registered")
            connection.commit()
            return registered, False
        except (OperationConflictError, OperationTransitionError):
            raise
        except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
            if self._connection is not None and self._connection.in_transaction:
                self._connection.rollback()
            raise TraceStoreWriteError("could not register operation") from exc

    def acquire_operation(
        self, operation_key: str, request_hash: str, owner: str, ttl_seconds: float
    ) -> OperationRecord:
        """Claim and return a record; use ``acquire_operation_result`` for launch policy."""
        return self._acquire_operation(operation_key, request_hash, owner, ttl_seconds)[
            0
        ]

    def _acquire_operation(
        self, operation_key: str, request_hash: str, owner: str, ttl_seconds: float
    ) -> tuple[OperationRecord, str]:
        """Claim an unleased/expired pre-launch operation with a new fence."""
        if not owner or ttl_seconds <= 0:
            raise OperationTransitionError("owner and positive lease TTL are required")
        record, _ = self.register_or_get(
            OperationRecord(operation_key, request_hash, "registered")
        )
        try:
            connection = self._open_connection()
            connection.execute("BEGIN IMMEDIATE")
            current = self._from_operation_row(
                connection.execute(
                    "SELECT * FROM operations WHERE operation_key=?", (operation_key,)
                ).fetchone()
            )
            now = datetime.now(timezone.utc)
            expired = (
                current.lease_expires_at is None
                or datetime.fromisoformat(current.lease_expires_at) <= now
            )
            if current.state == "terminal":
                connection.rollback()
                return current, "terminal"
            if current.state == "side_effect_started":
                self._audit(connection, current, "rejected:post_launch_takeover")
                connection.commit()
                raise OperationTransitionError(
                    "post-launch operations require reconciliation"
                )
            if current.owner and not expired and current.owner == owner:
                connection.rollback()
                return current, "existing"
            if current.owner and not expired and current.owner != owner:
                self._audit(connection, current, "rejected:lease_held")
                connection.commit()
                raise OperationLeaseError("operation lease is held by another owner")
            if current.fencing_generation >= 2**63 - 1:
                raise OperationTransitionError("fencing generation exhausted")
            updated = OperationRecord(
                operation_key,
                current.request_hash,
                "lease_acquired",
                owner,
                self._lease_time(
                    datetime.fromtimestamp(now.timestamp() + ttl_seconds, timezone.utc)
                ),
                current.fencing_generation + 1,
                current.result_descriptor,
                current.external_ids,
            )
            self._update_operation_tx(connection, updated)
            self._audit(connection, updated, "lease_acquired")
            connection.commit()
            return updated, "acquired"
        except (OperationLeaseError, OperationTransitionError):
            self._rollback_if_open()
            raise
        except (sqlite3.Error, ValueError, OSError) as exc:
            if self._connection is not None and self._connection.in_transaction:
                self._connection.rollback()
            raise TraceStoreWriteError("could not acquire operation lease") from exc

    def acquire_operation_result(
        self, operation_key: str, request_hash: str, owner: str, ttl_seconds: float
    ) -> tuple[OperationRecord, str]:
        """Return explicit claim disposition; callers must launch only ``acquired``."""
        return self._acquire_operation(operation_key, request_hash, owner, ttl_seconds)

    def _update_operation_tx(
        self, connection: sqlite3.Connection, operation: OperationRecord
    ) -> None:
        connection.execute(
            "UPDATE operations SET state=?,owner=?,lease_expires_at=?,fencing_generation=?,result_descriptor=?,external_ids=?,terminal_outcome=? WHERE operation_key=?",
            (
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
                operation.terminal_outcome,
                operation.operation_key,
            ),
        )

    def transition_operation(
        self,
        operation_key: str,
        owner: str,
        fencing_token: int,
        state: str,
        *,
        descriptor: dict[str, Any] | None = None,
        external_ids: dict[str, Any] | None = None,
        terminal_outcome: str | None = None,
        result: dict[str, Any] | None = None,
        allow_expired_reconciliation: bool = False,
    ) -> OperationRecord:
        """Perform a fenced legal transition and append immutable history atomically."""
        legal = {
            "lease_acquired": {"prepared", "terminal"},
            "prepared": {"side_effect_started", "terminal"},
            "side_effect_started": {"terminal"},
        }
        try:
            connection = self._open_connection()
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM operations WHERE operation_key=?", (operation_key,)
            ).fetchone()
            if row is None:
                raise OperationTransitionError("operation was not found")
            current = self._from_operation_row(row)
            if current.owner != owner or current.fencing_generation != fencing_token:
                self._audit(connection, current, "rejected:stale_fence")
                connection.commit()
                raise OperationLeaseError("stale owner or fencing token")
            if (
                current.state != "terminal"
                and current.lease_expires_at is not None
                and not allow_expired_reconciliation
                and datetime.fromisoformat(current.lease_expires_at)
                <= datetime.now(timezone.utc)
            ):
                self._audit(connection, current, "rejected:expired_lease")
                connection.commit()
                raise OperationLeaseError("operation lease has expired")
            if state not in legal.get(current.state, set()) or (
                allow_expired_reconciliation and current.state != "side_effect_started"
            ):
                self._audit(connection, current, "rejected:illegal_transition")
                connection.commit()
                raise OperationTransitionError("illegal operation transition")
            terminal = state == "terminal"
            if terminal and terminal_outcome not in {
                "success",
                "failure",
                "cancelled",
                "rejected",
                "indeterminate",
            }:
                raise OperationTransitionError(
                    "a distinct terminal outcome is required"
                )
            updated = OperationRecord(
                operation_key,
                current.request_hash,
                state,
                owner,
                None if terminal else current.lease_expires_at,
                fencing_token,
                self._bounded_descriptor(descriptor)
                if terminal
                else current.result_descriptor,
                external_ids if external_ids is not None else current.external_ids,
                terminal_outcome if terminal else None,
            )
            self._update_operation_tx(connection, updated)
            if result is not None:
                if not terminal:
                    raise OperationTransitionError(
                        "operation result requires terminal state"
                    )
                connection.execute(
                    "INSERT INTO operation_results(operation_key,result_json) VALUES (?, ?) "
                    "ON CONFLICT(operation_key) DO UPDATE SET result_json=excluded.result_json",
                    (
                        operation_key,
                        json.dumps(result, sort_keys=True, separators=(",", ":")),
                    ),
                )
            self._audit(
                connection,
                updated,
                "reconciled" if allow_expired_reconciliation else state,
            )
            connection.commit()
            return updated
        except (OperationLeaseError, OperationTransitionError):
            self._rollback_if_open()
            raise
        except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
            if self._connection is not None and self._connection.in_transaction:
                self._connection.rollback()
            raise TraceStoreWriteError("could not transition operation") from exc

    def renew_operation(
        self, operation_key: str, owner: str, fencing_token: int, ttl_seconds: float
    ) -> OperationRecord:
        if ttl_seconds <= 0:
            raise OperationTransitionError("lease TTL must be positive")
        try:
            connection = self._open_connection()
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM operations WHERE operation_key=?", (operation_key,)
            ).fetchone()
            if row is None:
                raise OperationTransitionError("operation was not found")
            current = self._from_operation_row(row)
            expired = not current.lease_expires_at or datetime.fromisoformat(
                current.lease_expires_at
            ) <= datetime.now(timezone.utc)
            if (
                current.owner != owner
                or current.fencing_generation != fencing_token
                or expired
            ):
                self._audit(connection, current, "rejected:stale_renew")
                connection.commit()
                raise OperationLeaseError("cannot renew a stale operation lease")
            updated = OperationRecord(
                **{
                    **current.__dict__,
                    "lease_expires_at": self._lease_time(
                        datetime.fromtimestamp(
                            datetime.now(timezone.utc).timestamp() + ttl_seconds,
                            timezone.utc,
                        )
                    ),
                }
            )
            self._update_operation_tx(connection, updated)
            self._audit(connection, updated, "renewed")
            connection.commit()
            return updated
        except (OperationLeaseError, OperationTransitionError):
            self._rollback_if_open()
            raise
        except (sqlite3.Error, OSError, ValueError) as exc:
            if self._connection is not None and self._connection.in_transaction:
                self._connection.rollback()
            raise TraceStoreWriteError("could not renew operation lease") from exc

    def attach_external_id(
        self,
        operation_key: str,
        owner: str,
        fencing_token: int,
        external_ids: dict[str, Any],
    ) -> OperationRecord:
        return self._fenced_update(
            operation_key, owner, fencing_token, external_ids=external_ids
        )

    def _fenced_update(
        self,
        operation_key: str,
        owner: str,
        fencing_token: int,
        *,
        external_ids: dict[str, Any],
    ) -> OperationRecord:
        try:
            connection = self._open_connection()
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM operations WHERE operation_key=?", (operation_key,)
            ).fetchone()
            if row is None:
                raise OperationTransitionError("operation was not found")
            current = self._from_operation_row(row)
            if current.owner != owner or current.fencing_generation != fencing_token:
                self._audit(connection, current, "rejected:stale_external_id")
                connection.commit()
                raise OperationLeaseError("stale owner or fencing token")
            if current.state == "terminal" or (
                current.lease_expires_at is not None
                and datetime.fromisoformat(current.lease_expires_at)
                <= datetime.now(timezone.utc)
            ):
                self._audit(
                    connection, current, "rejected:immutable_or_expired_external_id"
                )
                connection.commit()
                raise OperationLeaseError("cannot modify terminal or expired operation")
            updated = OperationRecord(
                **{
                    **current.__dict__,
                    "external_ids": self._bounded_descriptor(external_ids),
                }
            )
            self._update_operation_tx(connection, updated)
            self._audit(connection, updated, "external_id_attached")
            connection.commit()
            return updated
        except (OperationLeaseError, OperationTransitionError):
            self._rollback_if_open()
            raise
        except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
            if self._connection is not None and self._connection.in_transaction:
                self._connection.rollback()
            raise TraceStoreWriteError("could not attach external ID") from exc

    def mark_prepared(
        self, operation_key: str, owner: str, fencing_token: int
    ) -> OperationRecord:
        return self.transition_operation(
            operation_key, owner, fencing_token, "prepared"
        )

    def mark_side_effect_started(
        self, operation_key: str, owner: str, fencing_token: int
    ) -> OperationRecord:
        return self.transition_operation(
            operation_key, owner, fencing_token, "side_effect_started"
        )

    def complete(
        self,
        operation_key: str,
        owner: str,
        fencing_token: int,
        descriptor: dict[str, Any],
        *,
        result: dict[str, Any] | None = None,
    ) -> OperationRecord:
        return self.transition_operation(
            operation_key,
            owner,
            fencing_token,
            "terminal",
            descriptor=descriptor,
            terminal_outcome="success",
            result=result,
        )

    def fail(
        self,
        operation_key: str,
        owner: str,
        fencing_token: int,
        descriptor: dict[str, Any],
        *,
        result: dict[str, Any] | None = None,
    ) -> OperationRecord:
        return self.transition_operation(
            operation_key,
            owner,
            fencing_token,
            "terminal",
            descriptor=descriptor,
            terminal_outcome="failure",
            result=result,
        )

    def reject(
        self,
        operation_key: str,
        owner: str,
        fencing_token: int,
        descriptor: dict[str, Any],
    ) -> OperationRecord:
        return self.transition_operation(
            operation_key,
            owner,
            fencing_token,
            "terminal",
            descriptor=descriptor,
            terminal_outcome="rejected",
        )

    def mark_indeterminate(
        self,
        operation_key: str,
        owner: str,
        fencing_token: int,
        descriptor: dict[str, Any],
    ) -> OperationRecord:
        return self.transition_operation(
            operation_key,
            owner,
            fencing_token,
            "terminal",
            descriptor=descriptor,
            terminal_outcome="indeterminate",
        )

    def reconcile(
        self,
        operation_key: str,
        owner: str,
        fencing_token: int,
        descriptor: dict[str, Any],
        outcome: str = "indeterminate",
    ) -> OperationRecord:
        """Only reconciliation may terminalize a launched expired operation."""
        return self.transition_operation(
            operation_key,
            owner,
            fencing_token,
            "terminal",
            descriptor=descriptor,
            terminal_outcome=outcome,
            allow_expired_reconciliation=True,
        )

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
