from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_LOG_DIR = Path.home() / ".agentic-perf" / "logs"

EVENT_TYPES = {
    "agent_started",
    "agent_finished",
    "agent_error",
    "llm_request",
    "llm_response",
    "tool_called",
    "tool_result",
    "tool_skipped",
    "transition",
    "comment",
}


class Event:
    __slots__ = ("seq", "timestamp", "ticket_id", "agent", "event_type", "data")

    def __init__(
        self,
        seq: int,
        ticket_id: str,
        agent: str,
        event_type: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        self.seq = seq
        self.timestamp = datetime.now(timezone.utc).isoformat()
        self.ticket_id = ticket_id
        self.agent = agent
        self.event_type = event_type
        self.data = data or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "timestamp": self.timestamp,
            "ticket_id": self.ticket_id,
            "agent": self.agent,
            "event_type": self.event_type,
            "data": self.data,
        }


class EventBus:
    def __init__(self, log_dir: str | Path | None = None) -> None:
        self._log_dir = Path(log_dir) if log_dir else DEFAULT_LOG_DIR
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._events: dict[str, list[Event]] = {}
        self._seq = 0
        self._lock = threading.Lock()
        self._file_handles: dict[str, Any] = {}

    def emit(
        self,
        ticket_id: str,
        agent: str,
        event_type: str,
        data: dict[str, Any] | None = None,
    ) -> Event:
        with self._lock:
            self._seq += 1
            event = Event(self._seq, ticket_id, agent, event_type, data)
            self._events.setdefault(ticket_id, []).append(event)

        self._write_to_file(ticket_id, event)
        return event

    def get_events(
        self,
        ticket_id: str,
        since: int = 0,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        events = self._events.get(ticket_id, [])
        if events:
            filtered = [e for e in events if e.seq > since]
            return [e.to_dict() for e in filtered[:limit]]
        return self._read_from_file(ticket_id, since=since, limit=limit)

    def _read_from_file(
        self,
        ticket_id: str,
        since: int = 0,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        path = self._log_dir / f"{ticket_id}.jsonl"
        if not path.exists():
            return []
        results = []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if evt.get("seq", 0) > since:
                        results.append(evt)
                        if len(results) >= limit:
                            break
        except Exception:
            logger.exception(f"Failed to read events from file for {ticket_id}")
        return results

    def _write_to_file(self, ticket_id: str, event: Event) -> None:
        try:
            if ticket_id not in self._file_handles:
                path = self._log_dir / f"{ticket_id}.jsonl"
                self._file_handles[ticket_id] = open(path, "a", encoding="utf-8")
            fh = self._file_handles[ticket_id]
            fh.write(json.dumps(event.to_dict(), default=str) + "\n")
            fh.flush()
        except Exception:
            logger.exception(f"Failed to write event to file for {ticket_id}")

    def close(self) -> None:
        for fh in self._file_handles.values():
            try:
                fh.close()
            except Exception:
                pass
        self._file_handles.clear()
