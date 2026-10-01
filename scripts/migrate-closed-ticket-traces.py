#!/usr/bin/env python3
"""Migrate trace events for closed tickets from active trace.db to trace-history.db.

Usage:
    # Dry-run / preview closed tickets and their trace event counts:
    python3 scripts/migrate-closed-ticket-traces.py

    # Apply migration:
    python3 scripts/migrate-closed-ticket-traces.py --apply

    # Optionally run PRAGMA incremental_vacuum or VACUUM after migration:
    python3 scripts/migrate-closed-ticket-traces.py --apply --vacuum
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from paths import TICKET_DIR, TRACE_DB_PATH, TRACE_HISTORY_DB_PATH
from state_store.trace_store import TraceStore

logger = logging.getLogger("migrate-closed-ticket-traces")


def find_closed_tickets(ticket_dir: Path) -> list[str]:
    """Find all ticket IDs that have status == 'closed'."""
    closed_tickets = []
    if not ticket_dir.exists():
        return closed_tickets
    for ticket_file in ticket_dir.glob("PERF-*.json"):
        try:
            data = json.loads(ticket_file.read_text(encoding="utf-8"))
            if data.get("status") == "closed":
                closed_tickets.append(data.get("id") or ticket_file.stem)
        except Exception as exc:
            logger.warning("Could not parse ticket file %s: %s", ticket_file, exc)
    return sorted(closed_tickets)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migrate closed ticket traces from active trace.db to trace-history.db."
    )
    parser.add_argument(
        "--ticket-dir",
        type=Path,
        default=TICKET_DIR,
        help=f"Directory containing ticket JSON files (default: {TICKET_DIR})",
    )
    parser.add_argument(
        "--trace-db",
        type=Path,
        default=TRACE_DB_PATH,
        help=f"Active trace SQLite DB (default: {TRACE_DB_PATH})",
    )
    parser.add_argument(
        "--history-db",
        type=Path,
        default=TRACE_HISTORY_DB_PATH,
        help=f"History trace SQLite DB (default: {TRACE_HISTORY_DB_PATH})",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Perform the migration (default is dry-run)",
    )
    parser.add_argument(
        "--vacuum",
        action="store_true",
        help="Run PRAGMA incremental_vacuum or VACUUM on active trace DB after migration",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if not args.trace_db.exists():
        logger.info("Active trace DB %s does not exist. Nothing to migrate.", args.trace_db)
        return 0

    closed_ticket_ids = find_closed_tickets(args.ticket_dir)
    logger.info("Found %d closed tickets in %s", len(closed_ticket_ids), args.ticket_dir)

    with TraceStore(args.trace_db, history_db_path=args.history_db) as store:
        total_migrated = 0
        candidates = []
        for tid in closed_ticket_ids:
            # Check count in active store directly
            with store._lock:
                try:
                    row = store._open_connection().execute(
                        "SELECT COUNT(*) FROM trace_events WHERE ticket_id = ?",
                        (tid,),
                    ).fetchone()
                    active_count = row[0] if row else 0
                except (sqlite3.Error, OSError):
                    active_count = 0

            if active_count > 0:
                candidates.append((tid, active_count))

        if not candidates:
            logger.info("No trace events for closed tickets found in active trace DB.")
            return 0

        logger.info(
            "Found %d closed tickets with %d total events in active DB:",
            len(candidates),
            sum(cnt for _, cnt in candidates),
        )
        for tid, cnt in candidates:
            logger.info("  %s: %d events", tid, cnt)

        if not args.apply:
            logger.info("\nDry run completed. Run with --apply to migrate.")
            return 0

        logger.info("\nApplying migration...")
        for tid, _ in candidates:
            migrated = store.migrate_ticket_traces(tid)
            total_migrated += migrated
            logger.info("  Migrated %d events for %s", migrated, tid)

        logger.info("Successfully migrated %d trace events to %s", total_migrated, args.history_db)

        if args.vacuum:
            logger.info("Running VACUUM on active trace DB %s...", args.trace_db)
            with store._lock:
                with store._write_lock:
                    store._open_connection().execute("VACUUM")
            logger.info("VACUUM complete.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
