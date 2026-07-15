"""Tests for the introspection agent's observation engine.

Covers: event reading, event truncation, anomaly detection
(repeated errors, retry loops, max iterations), and token usage
aggregation from the MCP server functions.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from agents.introspection.server import (
    _detect_anomalies_from_events,
    _read_events,
    _truncate_event,
)


def _make_event(
    seq: int,
    event_type: str,
    agent: str = "benchmark-agent",
    data: dict | None = None,
) -> dict:
    return {
        "seq": seq,
        "timestamp": "2026-07-15T00:00:00+00:00",
        "ticket_id": "PERF-INTRO",
        "agent": agent,
        "event_type": event_type,
        "data": data or {},
    }


# --- Event reading ---


class TestReadEvents:
    def test_reads_jsonl_file(self) -> None:
        events = [
            _make_event(1, "agent_started"),
            _make_event(2, "llm_request", data={"iteration": 0}),
            _make_event(3, "agent_finished"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "PERF-INTRO.jsonl"
            with open(path, "w") as f:
                for e in events:
                    f.write(json.dumps(e) + "\n")

            with patch(
                "agents.introspection.server.DEFAULT_LOG_DIR",
                Path(tmp),
            ):
                result = _read_events("PERF-INTRO")

        assert len(result) == 3
        assert result[0]["event_type"] == "agent_started"
        assert result[2]["event_type"] == "agent_finished"

    def test_since_filters_events(self) -> None:
        events = [
            _make_event(i, "llm_request", data={"iteration": i}) for i in range(1, 6)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "PERF-INTRO.jsonl"
            with open(path, "w") as f:
                for e in events:
                    f.write(json.dumps(e) + "\n")

            with patch(
                "agents.introspection.server.DEFAULT_LOG_DIR",
                Path(tmp),
            ):
                result = _read_events("PERF-INTRO", since=3)

        assert len(result) == 2
        assert result[0]["seq"] == 4
        assert result[1]["seq"] == 5

    def test_limit_caps_results(self) -> None:
        events = [_make_event(i, "llm_request") for i in range(1, 11)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "PERF-INTRO.jsonl"
            with open(path, "w") as f:
                for e in events:
                    f.write(json.dumps(e) + "\n")

            with patch(
                "agents.introspection.server.DEFAULT_LOG_DIR",
                Path(tmp),
            ):
                result = _read_events("PERF-INTRO", limit=3)

        assert len(result) == 3

    def test_missing_file_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch(
                "agents.introspection.server.DEFAULT_LOG_DIR",
                Path(tmp),
            ):
                result = _read_events("NONEXISTENT")

        assert result == []


# --- Event truncation ---


class TestTruncateEvent:
    def test_truncates_llm_response_text(self) -> None:
        evt = _make_event(
            1,
            "llm_response",
            data={
                "iteration": 0,
                "stop_reason": "end_turn",
                "tool_calls": ["foo"],
                "text_length": 5000,
                "text": "x" * 5000,
                "raw_content": "ignored",
            },
        )
        trimmed = _truncate_event(evt)
        assert len(trimmed["data"]["text"]) <= 500
        assert "raw_content" not in trimmed["data"]
        assert trimmed["data"]["tool_calls"] == ["foo"]

    def test_truncates_large_tool_input(self) -> None:
        evt = _make_event(
            1,
            "tool_called",
            data={
                "tool": "execute_command",
                "input": {"command": "a" * 1000},
            },
        )
        trimmed = _truncate_event(evt)
        assert "_truncated" in trimmed["data"]["input"]

    def test_preserves_small_tool_input(self) -> None:
        evt = _make_event(
            1,
            "tool_called",
            data={
                "tool": "get_status",
                "input": {"id": "PERF-1"},
            },
        )
        trimmed = _truncate_event(evt)
        assert trimmed["data"]["input"] == {"id": "PERF-1"}

    def test_truncates_tool_result_content(self) -> None:
        evt = _make_event(
            1,
            "tool_result",
            data={
                "tool": "execute_command",
                "is_error": False,
                "content_length": 2000,
                "content": "y" * 2000,
            },
        )
        trimmed = _truncate_event(evt)
        assert len(trimmed["data"]["content"]) <= 500


# --- Anomaly detection ---


class TestDetectAnomalies:
    def test_detects_repeated_errors(self) -> None:
        events = [
            _make_event(
                i,
                "tool_result",
                data={
                    "tool": "execute_command",
                    "is_error": True,
                    "content": "Connection refused",
                },
            )
            for i in range(1, 5)
        ]
        anomalies = _detect_anomalies_from_events(events)
        repeated = [a for a in anomalies if a["type"] == "repeated_error"]
        assert len(repeated) == 1
        assert repeated[0]["severity"] == "medium"
        assert "execute_command" in repeated[0]["description"]

    def test_high_severity_for_many_errors(self) -> None:
        events = [
            _make_event(
                i,
                "tool_result",
                data={
                    "tool": "ssh_connect",
                    "is_error": True,
                    "content": "Timeout",
                },
            )
            for i in range(1, 7)
        ]
        anomalies = _detect_anomalies_from_events(events)
        repeated = [a for a in anomalies if a["type"] == "repeated_error"]
        assert len(repeated) == 1
        assert repeated[0]["severity"] == "high"

    def test_no_anomaly_for_few_errors(self) -> None:
        events = [
            _make_event(
                i,
                "tool_result",
                data={
                    "tool": "execute_command",
                    "is_error": True,
                    "content": "Error",
                },
            )
            for i in range(1, 3)
        ]
        anomalies = _detect_anomalies_from_events(events)
        assert len(anomalies) == 0

    def test_detects_retry_loop(self) -> None:
        events = [
            _make_event(
                i,
                "tool_called",
                data={
                    "tool": "execute_command",
                    "input": {"command": "ls /tmp"},
                },
            )
            for i in range(1, 5)
        ]
        anomalies = _detect_anomalies_from_events(events)
        loops = [a for a in anomalies if a["type"] == "retry_loop"]
        assert len(loops) == 1
        assert "identical input" in loops[0]["description"]

    def test_no_loop_for_different_inputs(self) -> None:
        events = [
            _make_event(
                i,
                "tool_called",
                data={
                    "tool": "execute_command",
                    "input": {"command": f"cmd-{i}"},
                },
            )
            for i in range(1, 5)
        ]
        anomalies = _detect_anomalies_from_events(events)
        loops = [a for a in anomalies if a["type"] == "retry_loop"]
        assert len(loops) == 0

    def test_detects_max_iterations(self) -> None:
        events = [
            _make_event(
                1,
                "agent_error",
                data={"reason": "max_iterations"},
            ),
        ]
        anomalies = _detect_anomalies_from_events(events)
        max_iter = [a for a in anomalies if a["type"] == "excessive_iterations"]
        assert len(max_iter) == 1
        assert max_iter[0]["severity"] == "high"

    def test_empty_events_no_anomalies(self) -> None:
        anomalies = _detect_anomalies_from_events([])
        assert anomalies == []

    def test_clean_run_no_anomalies(self) -> None:
        events = [
            _make_event(1, "agent_started"),
            _make_event(2, "llm_request", data={"iteration": 0}),
            _make_event(
                3,
                "llm_response",
                data={"iteration": 0, "stop_reason": "end_turn"},
            ),
            _make_event(4, "agent_finished"),
        ]
        anomalies = _detect_anomalies_from_events(events)
        assert anomalies == []
