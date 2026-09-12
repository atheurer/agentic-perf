from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from paths import LOG_DIR as DEFAULT_LOG_DIR
from providers.event_projection import (
    event_order_key,
    legacy_record,
    legacy_to_trace,
    trace_to_legacy,
)
from state_store.trace_store import TraceStore

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
    "status_change",
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
    def __init__(
        self,
        log_dir: str | Path | None = None,
        redactor: Any | None = None,
        usage_ledger: Any | None = None,
        trace_store: TraceStore | None = None,
        comparison_mode: bool = False,
        comparison_writer: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if comparison_mode and "PYTEST_CURRENT_TEST" not in os.environ:
            raise RuntimeError("trace dual-write comparison mode is tests-only")
        if comparison_mode and comparison_writer is None:
            raise ValueError(
                "trace dual-write comparison mode requires a comparison sink"
            )
        self._log_dir = Path(log_dir) if log_dir else DEFAULT_LOG_DIR
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._redactor = redactor
        self._events: dict[str, list[Event]] = {}
        self._seq: dict[str, int] = {}
        self._lock = threading.Lock()
        # JSONL is strictly a read-only schema-0 compatibility source.  SQLite
        # serializes producers, avoiding the former independent append handles.
        self._trace_store = trace_store or TraceStore(self._log_dir.parent / "trace.db")
        self._owns_trace_store = trace_store is None
        self._comparison_mode = comparison_mode
        self._comparison_writer = comparison_writer
        self._cumulative: dict[str, CumulativeUsage] = {}
        self._last_event_time: dict[str, float] = {}
        self._usage_ledger = usage_ledger
        self._ticket_owners: dict[str, tuple[str, list[str]]] = {}

    def _ensure_loaded_locked(self, ticket_id: str) -> None:
        """Restore ticket sequence number and cumulative usage from jsonl.

        MUST be called with self._lock held.
        """
        if ticket_id in self._seq:
            return

        self._seq[ticket_id] = 0
        path = self._log_dir / f"{ticket_id}.jsonl"
        line_count = 0
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        line_count += 1
                        try:
                            evt = json.loads(line)
                            if evt.get("event_type") == "llm_usage":
                                data = evt.get("data", {})
                                agent_name = evt.get("agent", "")
                                input_tokens = data.get("input_tokens", 0)
                                output_tokens = data.get("output_tokens", 0)
                                duration_ms = (
                                    data.get("duration_ms", 0)
                                    or data.get("total_duration_ms", 0)
                                    or 0
                                )
                                model = data.get("model", "")
                                cache_read = data.get("cache_read_input_tokens", 0)
                                cache_creation = data.get(
                                    "cache_creation_input_tokens", 0
                                )
                                self._record_cumulative_in_memory(
                                    ticket_id,
                                    input_tokens,
                                    output_tokens,
                                    duration_ms,
                                    model,
                                    cache_read,
                                    cache_creation,
                                )
                                if agent_name:
                                    self._record_cumulative_in_memory(
                                        f"{ticket_id}:{agent_name}",
                                        input_tokens,
                                        output_tokens,
                                        duration_ms,
                                        model,
                                        cache_read,
                                        cache_creation,
                                    )
                        except Exception:
                            continue
            except Exception:
                logger.exception(f"Failed to load ticket events from {path}")

        # New records are durable trace envelopes.  Rebuild the same in-memory
        # budget snapshot on restart without treating a comparison projection as
        # a second usage record.
        for trace_event in self._trace_store.list_events(ticket_id):
            evt = trace_to_legacy(trace_event)
            if evt.get("event_type") != "llm_usage":
                continue
            data = evt.get("data", {})
            agent_name = evt.get("agent", "")
            input_tokens = data.get("input_tokens", 0)
            output_tokens = data.get("output_tokens", 0)
            duration_ms = data.get("duration_ms", 0) or data.get("total_duration_ms", 0)
            model = data.get("model", "")
            cache_read = data.get("cache_read_input_tokens", 0)
            cache_creation = data.get("cache_creation_input_tokens", 0)
            self._record_cumulative_in_memory(
                ticket_id,
                input_tokens,
                output_tokens,
                duration_ms,
                model,
                cache_read,
                cache_creation,
            )
            if agent_name:
                self._record_cumulative_in_memory(
                    f"{ticket_id}:{agent_name}",
                    input_tokens,
                    output_tokens,
                    duration_ms,
                    model,
                    cache_read,
                    cache_creation,
                )

        self._seq[ticket_id] = line_count + len(
            self._trace_store.list_events(ticket_id)
        )

    def _record_cumulative_in_memory(
        self,
        key: str,
        input_tokens: int,
        output_tokens: int,
        duration_ms: int,
        model: str,
        cache_read_input_tokens: int,
        cache_creation_input_tokens: int,
    ) -> None:
        if key not in self._cumulative:
            self._cumulative[key] = CumulativeUsage()
        self._cumulative[key].record(
            input_tokens,
            output_tokens,
            duration_ms,
            model,
            cache_read_input_tokens=cache_read_input_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
        )

    def _next_seq(self, ticket_id: str) -> int:
        """Return the next sequence number for a ticket."""
        self._ensure_loaded_locked(ticket_id)
        self._seq[ticket_id] += 1
        return self._seq[ticket_id]

    def emit(
        self,
        ticket_id: str,
        agent: str,
        event_type: str,
        data: dict[str, Any] | None = None,
    ) -> Event:
        if data and self._redactor:
            data = self._redactor.redact(ticket_id, data)
        payload = data or {}
        with self._lock:
            # The trace commit is the publication barrier. Never expose an event
            # locally if a durable canonical record was not accepted.
            stored = self._trace_store.insert_event(
                legacy_to_trace(ticket_id, agent, event_type, payload)
            )
            if self._comparison_mode and self._comparison_writer is not None:
                # The test-only sink is deliberately outside all readers: it
                # compares the legacy projection without producing a second
                # visible usage/iteration/event record.
                self._comparison_writer(trace_to_legacy(stored))
            event = Event(stored.ticket_seq or 0, ticket_id, agent, event_type, payload)
            self._events.setdefault(ticket_id, []).append(event)
            self._last_event_time[ticket_id] = time.time()
        return event

    def last_event_time(self, ticket_id: str) -> float | None:
        """Return the wall-clock time of the last event for a ticket.

        Returns None if no events have been emitted for this ticket
        in the current process.
        """
        return self._last_event_time.get(ticket_id)

    def register_ticket_owner(
        self,
        ticket_id: str,
        charged_to: str,
        groups: list[str],
    ) -> None:
        """Register who to charge for a ticket's LLM usage.

        Called by the orchestrator at dispatch time so that
        ``record_llm_usage`` can write ledger entries with
        correct attribution.
        """
        self._ticket_owners[ticket_id] = (charged_to, list(groups))

    def unregister_ticket_owner(self, ticket_id: str) -> None:
        """Remove ownership attribution for a terminal ticket."""
        self._ticket_owners.pop(ticket_id, None)

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
            self._ensure_loaded_locked(ticket_id)
            # Ticket-level accumulation
            self._record_cumulative_in_memory(
                ticket_id,
                input_tokens,
                output_tokens,
                duration_ms,
                model,
                cache_read_input_tokens,
                cache_creation_input_tokens,
            )

            # Per-agent accumulation
            if agent_name:
                self._record_cumulative_in_memory(
                    f"{ticket_id}:{agent_name}",
                    input_tokens,
                    output_tokens,
                    duration_ms,
                    model,
                    cache_read_input_tokens,
                    cache_creation_input_tokens,
                )

        self._write_ledger_entry(
            ticket_id,
            input_tokens,
            output_tokens,
            model,
            cache_read_input_tokens,
            cache_creation_input_tokens,
        )

    def get_cumulative_usage(self, ticket_id: str) -> dict[str, Any]:
        """Get accumulated LLM usage for a ticket."""
        with self._lock:
            self._ensure_loaded_locked(ticket_id)
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
            self._ensure_loaded_locked(ticket_id)
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
        # Project both sources once, then assign compatibility cursors after the
        # stable mixed-history ordering. Cursors therefore advance over filtered
        # SSE records without skips or trace/legacy sequence collisions.
        legacy = self._read_from_file(ticket_id, since=0, limit=100_000)
        with self._lock:
            traces = [
                trace_to_legacy(event)
                for event in self._trace_store.list_events(ticket_id)
            ]
        merged = sorted(legacy + traces, key=event_order_key)
        for cursor, item in enumerate(merged, start=1):
            item["seq"] = cursor
        return [item for item in merged if item["seq"] > since][:limit]

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
                    evt = legacy_record(evt, line_num)
                    if line_num > since:
                        results.append(evt)
                        if len(results) >= limit:
                            break
        except Exception:
            logger.exception(f"Failed to read events from file for {ticket_id}")
        return results

    def _write_ledger_entry(
        self,
        ticket_id: str,
        input_tokens: int,
        output_tokens: int,
        model: str,
        cache_read_input_tokens: int,
        cache_creation_input_tokens: int,
    ) -> None:
        """Append a usage record to the quota ledger."""
        if self._usage_ledger is None:
            return
        charged_to, groups = self._ticket_owners.get(ticket_id, ("", []))
        if not charged_to:
            return
        try:
            from providers.cost import estimate_cost
            from providers.quota import LedgerEntry

            cost = estimate_cost(
                model,
                input_tokens,
                output_tokens,
                cache_read_input_tokens=cache_read_input_tokens,
                cache_creation_input_tokens=cache_creation_input_tokens,
            )
            entry = LedgerEntry(
                ts=datetime.now(timezone.utc).isoformat(),
                ticket_id=ticket_id,
                charged_to=charged_to,
                groups=groups,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_input_tokens=cache_read_input_tokens,
                cache_creation_input_tokens=cache_creation_input_tokens,
                cost_usd=cost,
            )
            self._usage_ledger.append(entry)
        except Exception:
            logger.exception("Failed to write ledger entry for %s", ticket_id)

    def close(self) -> None:
        if self._usage_ledger is not None:
            self._usage_ledger.close()
        if self._owns_trace_store:
            self._trace_store.close()
