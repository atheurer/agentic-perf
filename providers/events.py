from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from paths import LOG_DIR as DEFAULT_LOG_DIR

logger = logging.getLogger(__name__)

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
    "tool_progress",
    "llm_usage",
    "agent_stopped",
    "user_interjection",
    "escalation",
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


class CumulativeUsage:
    """Accumulated LLM token and cost metrics per ticket.

    Fed by the OTLP span processor — each completed LLM span
    contributes its token counts, duration, and model info.
    """

    __slots__ = (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "llm_calls",
        "total_duration_ms",
        "models_used",
    )

    def __init__(self) -> None:
        self.input_tokens: int = 0
        self.output_tokens: int = 0
        self.cache_read_input_tokens: int = 0
        self.cache_creation_input_tokens: int = 0
        self.llm_calls: int = 0
        self.total_duration_ms: int = 0
        self.models_used: set[str] = set()

    def record(
        self,
        input_tokens: int,
        output_tokens: int,
        duration_ms: int,
        model: str = "",
        cache_read_input_tokens: int = 0,
        cache_creation_input_tokens: int = 0,
    ) -> None:
        """Add one LLM call's usage to the totals."""
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cache_read_input_tokens += cache_read_input_tokens
        self.cache_creation_input_tokens += cache_creation_input_tokens
        self.total_duration_ms += duration_ms
        self.llm_calls += 1
        if model:
            self.models_used.add(model)

    def to_dict(self) -> dict[str, Any]:
        """Snapshot of accumulated usage."""
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "total_tokens": (self.input_tokens + self.output_tokens),
            "llm_calls": self.llm_calls,
            "total_duration_ms": self.total_duration_ms,
            "models_used": sorted(self.models_used),
        }


class EventBus:
    def __init__(self, log_dir: str | Path | None = None) -> None:
        self._log_dir = Path(log_dir) if log_dir else DEFAULT_LOG_DIR
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._events: dict[str, list[Event]] = {}
        self._seq: dict[str, int] = {}
        self._lock = threading.Lock()
        self._file_handles: dict[str, Any] = {}
        self._cumulative: dict[str, CumulativeUsage] = {}
        self._last_event_time: dict[str, float] = {}

    def _next_seq(self, ticket_id: str) -> int:
        """Return the next sequence number for a ticket.

        On first call for a given ticket, counts lines in the existing
        JSONL file so that sequence numbers survive restarts. Uses line
        count (not embedded seq values) to stay consistent with
        _read_from_file which renumbers events by line position.
        """
        if ticket_id not in self._seq:
            line_count = 0
            path = self._log_dir / f"{ticket_id}.jsonl"
            if path.exists():
                try:
                    with open(path, encoding="utf-8") as f:
                        for line in f:
                            if line.strip():
                                line_count += 1
                except Exception:
                    logger.exception(f"Failed to count lines in {path}")
            self._seq[ticket_id] = line_count
        self._seq[ticket_id] += 1
        return self._seq[ticket_id]

    def emit(
        self,
        ticket_id: str,
        agent: str,
        event_type: str,
        data: dict[str, Any] | None = None,
    ) -> Event:
        with self._lock:
            seq = self._next_seq(ticket_id)
            event = Event(seq, ticket_id, agent, event_type, data)
            self._events.setdefault(ticket_id, []).append(event)
            self._last_event_time[ticket_id] = time.time()

        self._write_to_file(ticket_id, event)
        return event

    def last_event_time(self, ticket_id: str) -> float | None:
        """Return the wall-clock time of the last event for a ticket.

        Returns None if no events have been emitted for this ticket
        in the current process.
        """
        return self._last_event_time.get(ticket_id)

    def record_llm_usage(
        self,
        ticket_id: str,
        input_tokens: int,
        output_tokens: int,
        duration_ms: int,
        model: str = "",
        agent_name: str = "",
        cache_read_input_tokens: int = 0,
        cache_creation_input_tokens: int = 0,
    ) -> None:
        """Accumulate LLM token usage for a ticket.

        Called by the OTLP span processor when a GenAI span
        completes. Can also be called directly for providers
        that don't use OTLP instrumentation.

        Usage is tracked both at the ticket level and per
        agent within the ticket.
        """
        with self._lock:
            # Ticket-level accumulation
            if ticket_id not in self._cumulative:
                self._cumulative[ticket_id] = CumulativeUsage()
            self._cumulative[ticket_id].record(
                input_tokens,
                output_tokens,
                duration_ms,
                model,
                cache_read_input_tokens=cache_read_input_tokens,
                cache_creation_input_tokens=cache_creation_input_tokens,
            )

            # Per-agent accumulation
            if agent_name:
                agent_key = f"{ticket_id}:{agent_name}"
                if agent_key not in self._cumulative:
                    self._cumulative[agent_key] = CumulativeUsage()
                self._cumulative[agent_key].record(
                    input_tokens,
                    output_tokens,
                    duration_ms,
                    model,
                    cache_read_input_tokens=cache_read_input_tokens,
                    cache_creation_input_tokens=cache_creation_input_tokens,
                )

    def get_cumulative_usage(self, ticket_id: str) -> dict[str, Any]:
        """Get accumulated LLM usage for a ticket."""
        with self._lock:
            usage = self._cumulative.get(ticket_id)
            if usage is None:
                return CumulativeUsage().to_dict()
            return usage.to_dict()

    def get_agent_usage(self, ticket_id: str) -> dict[str, dict[str, Any]]:
        """Get per-agent LLM usage breakdown for a ticket.

        Returns a dict of agent_name -> usage dict. Only
        includes agents that have recorded usage.
        """
        with self._lock:
            result = {}
            prefix = f"{ticket_id}:"
            for key, usage in self._cumulative.items():
                if key.startswith(prefix):
                    agent_name = key[len(prefix) :]
                    result[agent_name] = usage.to_dict()
            return result

    def get_global_usage(self) -> dict[str, Any]:
        """Get accumulated LLM usage across all tickets.

        Useful for system-wide budget enforcement (see #127).
        Only includes ticket-level entries, not per-agent
        sub-entries.
        """
        with self._lock:
            total = CumulativeUsage()
            for key, usage in self._cumulative.items():
                if ":" in key:
                    continue
                total.input_tokens += usage.input_tokens
                total.output_tokens += usage.output_tokens
                total.cache_read_input_tokens += usage.cache_read_input_tokens
                total.cache_creation_input_tokens += usage.cache_creation_input_tokens
                total.llm_calls += usage.llm_calls
                total.total_duration_ms += usage.total_duration_ms
                total.models_used.update(usage.models_used)
            return total.to_dict()

    def get_events(
        self,
        ticket_id: str,
        since: int = 0,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        in_memory = [
            e.to_dict() for e in self._events.get(ticket_id, []) if e.seq > since
        ]
        from_file = self._read_from_file(ticket_id, since=since, limit=limit)
        seen_seqs = {e["seq"] for e in in_memory}
        merged = in_memory + [e for e in from_file if e["seq"] not in seen_seqs]
        merged.sort(key=lambda e: e["seq"])
        return merged[:limit]

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
            line_num = 0
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    line_num += 1
                    evt["seq"] = line_num
                    if line_num > since:
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
