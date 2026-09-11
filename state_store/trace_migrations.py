"""Schema migrations for the canonical trace SQLite database."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

Migration = Callable[[sqlite3.Connection], None]
LATEST_SCHEMA_VERSION = 1


def _migration_1(connection: sqlite3.Connection) -> None:
    # ``executescript`` implicitly commits before running, which would create a
    # migration race between processes opening a new database.
    statements = """
        CREATE TABLE trace_events (
            event_id TEXT PRIMARY KEY,
            content_hash TEXT NOT NULL,
            global_seq INTEGER NOT NULL UNIQUE,
            ticket_id TEXT NOT NULL,
            ticket_seq INTEGER NOT NULL,
            trace_id TEXT NOT NULL,
            invocation_id TEXT,
            action_id TEXT NOT NULL,
            parent_action_id TEXT,
            action_type TEXT NOT NULL,
            lifecycle_state TEXT NOT NULL,
            outcome TEXT,
            producer_component TEXT,
            occurred_at TEXT NOT NULL,
            event_json TEXT NOT NULL,
            UNIQUE(ticket_id, ticket_seq)
        );
        CREATE TABLE operations (
            operation_key TEXT PRIMARY KEY,
            request_hash TEXT NOT NULL,
            state TEXT NOT NULL,
            owner TEXT,
            lease_expires_at TEXT,
            fencing_generation INTEGER NOT NULL DEFAULT 0,
            result_descriptor TEXT,
            external_ids TEXT
        );
        CREATE TABLE trace_payloads (
            digest TEXT PRIMARY KEY,
            descriptor_json TEXT NOT NULL
        );
        CREATE INDEX trace_events_ticket_seq_idx ON trace_events(ticket_id, ticket_seq);
        CREATE INDEX trace_events_trace_idx ON trace_events(trace_id);
        CREATE INDEX trace_events_invocation_idx ON trace_events(invocation_id);
        CREATE INDEX trace_events_action_idx ON trace_events(action_id);
        CREATE INDEX trace_events_parent_action_idx ON trace_events(parent_action_id);
        CREATE INDEX trace_events_type_idx ON trace_events(action_type);
        CREATE INDEX trace_events_outcome_idx ON trace_events(outcome);
        CREATE INDEX trace_events_producer_idx ON trace_events(producer_component);
        CREATE INDEX trace_events_occurred_at_idx ON trace_events(occurred_at);
    """.split(";")
    for statement in statements:
        if statement.strip():
            connection.execute(statement)


MIGRATIONS: dict[int, Migration] = {1: _migration_1}


def migrate(connection: sqlite3.Connection) -> None:
    """Bring a database to the current schema inside the caller transaction."""
    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    row = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    current = row[0] or 0
    if current > LATEST_SCHEMA_VERSION:
        raise RuntimeError(f"database schema {current} is newer than supported")
    for version in range(current + 1, LATEST_SCHEMA_VERSION + 1):
        MIGRATIONS[version](connection)
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) "
            "VALUES (?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))",
            (version,),
        )
