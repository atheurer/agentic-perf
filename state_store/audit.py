from __future__ import annotations

import contextvars
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any

from paths import AUDIT_LOG

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
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or AUDIT_LOG
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._fh: IO[str] | None = None
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

    def _ensure_fh(self) -> IO[str]:
        if self._fh is None or self._fh.closed:
            self._fh = open(self._path, "a", encoding="utf-8")
        return self._fh

    def log(
        self,
        mutation: str,
        ticket_id: str,
        data: dict[str, Any],
    ) -> None:
        with self._lock:
            self._seq += 1
            entry = {
                "seq": self._seq,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "mutation": mutation,
                "ticket_id": ticket_id,
                "actor": get_actor(),
                "data": data,
            }
            try:
                fh = self._ensure_fh()
                fh.write(json.dumps(entry, default=str) + "\n")
                fh.flush()
            except OSError:
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
        if not self._path.exists():
            return []
        entries: list[dict[str, Any]] = []
        try:
            with open(self._path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("seq", 0) <= since:
                        continue
                    if ticket_id and entry.get("ticket_id") != ticket_id:
                        continue
                    entries.append(entry)
                    if len(entries) >= limit:
                        break
        except OSError:
            logger.exception("Failed to read audit log %s", self._path)
        return entries

    @property
    def latest_seq(self) -> int:
        with self._lock:
            return self._seq

    def close(self) -> None:
        with self._lock:
            if self._fh and not self._fh.closed:
                self._fh.close()
                self._fh = None
