from __future__ import annotations

import contextvars
import json
import logging
import threading
from pathlib import Path
from typing import Any

from paths import AUDIT_LOG
from providers.event_projection import audit_to_trace, trace_to_audit

from .trace_store import TraceStore, TraceStoreWriteError

logger = logging.getLogger(__name__)

_DEFAULT_ACTOR: dict[str, str] = {"kind": "unknown", "username": "", "ip": ""}
_actor_var: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "audit_actor",
    default=_DEFAULT_ACTOR,
)


def set_actor(kind: str, username: str, ip: str) -> contextvars.Token:
    return _actor_var.set({"kind": kind, "username": username, "ip": ip})


def get_actor() -> dict[str, str]:
    return _actor_var.get()


class AuditLog:
    def __init__(
        self,
        path: Path | None = None,
        redactor: Any | None = None,
        trace_store: TraceStore | None = None,
    ) -> None:
        self._path = path or AUDIT_LOG
        self._redactor = redactor
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._trace_lock = threading.Lock()
        self._trace_store = trace_store or TraceStore(self._path.parent / "trace.db")
        self._owns_trace_store = trace_store is None
        self._seq = self._recover_seq()

    def _recover_seq(self) -> int:
        if not self._path.exists():
            return 0
        last_seq = 0
        try:
            with open(self._path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        last_seq = entry.get("seq", last_seq)
                    except json.JSONDecodeError:
                        continue
        except OSError:
            logger.exception("Failed to recover audit seq from %s", self._path)
        return last_seq

    def log(
        self,
        mutation: str,
        ticket_id: str,
        data: dict[str, Any],
    ) -> None:
        with self._lock:
            if self._redactor:
                data = self._redactor.redact(ticket_id, data)
            try:
                with self._trace_lock:
                    stored = self._trace_store.insert_event(
                        audit_to_trace(ticket_id, mutation, get_actor(), data)
                    )
                self._seq = max(self._seq, stored.global_seq or 0)
            except TraceStoreWriteError:
                logger.exception(
                    "Failed to write audit entry for %s on %s",
                    mutation,
                    ticket_id,
                )

    def read(
        self,
        ticket_id: str | None = None,
        since: int = 0,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        if self._path.exists():
            try:
                with open(self._path, encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if (
                            ticket_id is not None
                            and entry.get("ticket_id") != ticket_id
                        ):
                            continue
                        entry["schema_version"] = "legacy_uncorrelated"
                        entries.append(entry)
            except OSError:
                logger.exception("Failed to read audit log %s", self._path)
        with self._trace_lock:
            trace_events = self._trace_store.list_events(ticket_id)
        for event in trace_events:
            projected = trace_to_audit(event)
            if projected is not None:
                entries.append(projected)
        entries.sort(
            key=lambda entry: (
                0 if entry.get("schema_version") == "legacy_uncorrelated" else 1,
                entry.get("seq", 0),
            )
        )
        for sequence, entry in enumerate(entries, start=1):
            entry["seq"] = sequence
        return [entry for entry in entries if entry["seq"] > since][:limit]

    @property
    def latest_seq(self) -> int:
        with self._lock:
            return max(self._seq, len(self.read(limit=1_000_000)))

    def close(self) -> None:
        with self._lock:
            if self._owns_trace_store:
                self._trace_store.close()
