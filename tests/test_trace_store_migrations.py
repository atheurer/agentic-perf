"""Migration and startup failure tests for TraceStore."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from state_store import trace_store
from state_store.trace_migrations import LATEST_SCHEMA_VERSION
from state_store.trace_store import TraceStore, TraceStoreMigrationError


def test_empty_database_is_migrated(tmp_path: Path) -> None:
    path = tmp_path / "trace.db"
    with TraceStore(path):
        connection = sqlite3.connect(path)
        try:
            assert (
                connection.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
                == LATEST_SCHEMA_VERSION
            )
        finally:
            connection.close()


def test_preceding_version_zero_is_migrated(tmp_path: Path) -> None:
    path = tmp_path / "trace.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        connection.commit()
    finally:
        connection.close()
    with TraceStore(path):
        connection = sqlite3.connect(path)
        try:
            assert (
                connection.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0]
                == 1
            )
        finally:
            connection.close()


def test_failed_migration_is_wrapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_migration(_: sqlite3.Connection) -> None:
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr(trace_store, "migrate", fail_migration)
    with pytest.raises(TraceStoreMigrationError):
        TraceStore(tmp_path / "trace.db")


def test_corrupt_database_is_explicit(tmp_path: Path) -> None:
    path = tmp_path / "trace.db"
    path.write_text("this is not sqlite")
    with pytest.raises(TraceStoreMigrationError):
        TraceStore(path)
