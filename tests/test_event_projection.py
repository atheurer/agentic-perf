"""Trace-backed compatibility contract for legacy event and audit APIs."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

from providers.event_projection import legacy_to_trace
from providers.events import EventBus
from state_store.audit import AuditLog
from state_store.trace_store import TraceStoreWriteError


def _legacy(path: Path, ticket_id: str, timestamp: str) -> None:
    path.write_text(
        json.dumps(
            {
                "timestamp": timestamp,
                "ticket_id": ticket_id,
                "agent": "legacy",
                "event_type": "llm_request",
                "data": {"iteration": 1},
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_mixed_history_is_labeled_and_has_stable_cursors(tmp_path: Path) -> None:
    ticket_id = "PERF-MIXED"
    logs = tmp_path / "logs"
    logs.mkdir()
    _legacy(logs / f"{ticket_id}.jsonl", ticket_id, "2020-01-01T00:00:00+00:00")
    bus = EventBus(log_dir=logs)
    try:
        bus.emit(ticket_id, "new", "tool_called", {"tool": "safe"})
        first = bus.get_events(ticket_id, limit=100)
        assert first[0]["schema_version"] == "legacy_uncorrelated"
        assert [event["seq"] for event in first] == [1, 2]
        assert bus.get_events(ticket_id, since=1, limit=100) == [first[1]]
    finally:
        bus.close()


def test_backdated_trace_cannot_reorder_consumed_mixed_cursor(tmp_path: Path) -> None:
    ticket_id = "PERF-BACKDATED"
    logs = tmp_path / "logs"
    logs.mkdir()
    _legacy(logs / f"{ticket_id}.jsonl", ticket_id, "2020-01-01T00:00:00+00:00")
    bus = EventBus(log_dir=logs)
    try:
        bus.emit(ticket_id, "new", "tool_called", {})
        bus.get_events(ticket_id, limit=100)
        backdated = legacy_to_trace(ticket_id, "new", "tool_result", {}).model_copy(
            update={"occurred_at": datetime(1999, 1, 1, tzinfo=timezone.utc)}
        )
        bus._trace_store.insert_event(backdated)
        unseen = bus.get_events(ticket_id, since=2, limit=100)
        assert [event["event_type"] for event in unseen] == ["tool_result"]
        assert unseen[0]["seq"] == 3
    finally:
        bus.close()


def test_persistence_failure_does_not_publish_event_or_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus = EventBus(log_dir=tmp_path / "logs")

    def fail(_: object) -> None:
        raise TraceStoreWriteError("injected failure")

    monkeypatch.setattr(bus._trace_store, "insert_event", fail)
    with pytest.raises(TraceStoreWriteError):
        bus.emit("PERF-FAIL", "agent", "tool_called", {})
    assert bus.get_events("PERF-FAIL") == []
    assert bus.last_event_time("PERF-FAIL") is None
    bus.close()


def test_tests_only_comparison_sink_does_not_double_count(tmp_path: Path) -> None:
    compared: list[dict[str, object]] = []
    bus = EventBus(
        log_dir=tmp_path / "logs",
        comparison_mode=True,
        comparison_writer=compared.append,
    )
    try:
        bus.emit("PERF-COMPARE", "agent", "llm_usage", {"input_tokens": 2})
        assert len(compared) == 1
        assert len(bus.get_events("PERF-COMPARE")) == 1
        assert bus.get_cumulative_usage("PERF-COMPARE")["input_tokens"] == 2
    finally:
        bus.close()


def test_comparison_mode_refuses_production_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    with pytest.raises(RuntimeError, match="tests-only"):
        EventBus(log_dir=tmp_path / "logs", comparison_mode=True)


def test_comparison_mode_requires_a_sink(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="comparison sink"):
        EventBus(log_dir=tmp_path / "logs", comparison_mode=True)


def test_event_and_audit_adapters_use_independent_trace_connections(
    tmp_path: Path,
) -> None:
    logs = tmp_path / "logs"
    bus = EventBus(log_dir=logs)
    audit = AuditLog(path=tmp_path / "audit.jsonl")
    try:
        assert bus._trace_store is not audit._trace_store
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(
                bus.emit, "PERF-CONCURRENT", "agent", "tool_called", {}
            )
            second = executor.submit(
                audit.log, "update_fields", "PERF-CONCURRENT", {"field_names": ["x"]}
            )
            first.result()
            second.result()
        assert len(bus.get_events("PERF-CONCURRENT")) == 2
        assert len(audit.read(ticket_id="PERF-CONCURRENT")) == 1
    finally:
        bus.close()
        audit.close()


def test_restart_preserves_usage_and_iteration_records_once(tmp_path: Path) -> None:
    ticket_id = "PERF-RESTART"
    logs = tmp_path / "logs"
    first = EventBus(log_dir=logs)
    first.emit(ticket_id, "agent", "llm_request", {"iteration": 1})
    first.emit(ticket_id, "agent", "llm_usage", {"input_tokens": 3, "output_tokens": 5})
    first.close()
    second = EventBus(log_dir=logs)
    try:
        events = second.get_events(ticket_id, limit=100)
        assert [event["event_type"] for event in events] == ["llm_request", "llm_usage"]
        assert second.get_cumulative_usage(ticket_id)["total_tokens"] == 8
    finally:
        second.close()


def test_audit_log_projects_without_creating_a_jsonl_append_handle(
    tmp_path: Path,
) -> None:
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(path=path)
    try:
        audit.log("update_fields", "PERF-AUDIT", {"field_names": ["a"]})
        assert not path.exists()
        assert audit.read()[0]["mutation"] == "update_fields"
    finally:
        audit.close()


def test_audit_legacy_history_respects_ticket_filter(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    path.write_text(
        json.dumps({"seq": 1, "ticket_id": "PERF-A", "mutation": "one"})
        + "\n"
        + json.dumps({"seq": 2, "ticket_id": "PERF-B", "mutation": "two"})
        + "\n",
        encoding="utf-8",
    )
    audit = AuditLog(path=path)
    try:
        assert [entry["ticket_id"] for entry in audit.read(ticket_id="PERF-A")] == [
            "PERF-A"
        ]
    finally:
        audit.close()


def test_production_sources_do_not_append_per_ticket_jsonl() -> None:
    root = Path(__file__).parents[1]
    forbidden = ('open(path, "a"', 'open(log_path, "a"', 'open(jsonl_path, "a"')
    sources = [root / "providers/events.py", root / "agents/server_utils.py"]
    for source in sources:
        text = source.read_text(encoding="utf-8")
        assert not any(pattern in text for pattern in forbidden), source
