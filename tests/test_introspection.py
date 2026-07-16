"""Tests for the introspection agent's observation engine.

Covers: event reading, event truncation, anomaly detection
(consecutive failures, content-based failure detection, error
classification, wasted iterations, retry loops, max iterations),
skill loading, continuous agent observation loop, observation
building, and orchestrator integration (config, dispatcher,
startup ordering).
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from agents.introspection.agent import IntrospectionAgent
from agents.introspection.server import (
    _classify_error,
    _detect_anomalies_from_events,
    _error_similarity,
    _extract_error_message,
    _is_tool_failure,
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


# Default skill-loaded thresholds and empty patterns for tests
# that don't need custom values.
_EMPTY_PATTERNS: dict = {"infrastructure": [], "transient": []}
_DEFAULT_THRESHOLDS: dict = {
    "consecutive_failure_min": 2,
    "consecutive_failure_high": 4,
    "error_similarity_threshold": 0.3,
    "repeated_error_min": 3,
    "repeated_error_high": 5,
    "retry_loop_min": 3,
    "retry_loop_high": 5,
    "wasted_iterations_min_calls": 4,
    "wasted_iterations_min_wasted": 2,
    "wasted_iterations_pct": 25,
    "wasted_iterations_high_pct": 50,
}


class TestToolFailureDetection:
    """Tests for _is_tool_failure content-based detection."""

    def test_is_error_true(self) -> None:
        evt = _make_event(1, "tool_result", data={"is_error": True})
        assert _is_tool_failure(evt) is True

    def test_exit_code_nonzero(self) -> None:
        evt = _make_event(
            1,
            "tool_result",
            data={
                "tool": "jmp_run",
                "is_error": False,
                "content": json.dumps({"exit_code": 1, "stderr": "fail"}),
            },
        )
        assert _is_tool_failure(evt) is True

    def test_success_false(self) -> None:
        evt = _make_event(
            1,
            "tool_result",
            data={
                "tool": "scp_file",
                "is_error": False,
                "content": json.dumps({"success": False, "error": "denied"}),
            },
        )
        assert _is_tool_failure(evt) is True

    def test_status_failed(self) -> None:
        evt = _make_event(
            1,
            "tool_result",
            data={
                "tool": "check_os",
                "is_error": False,
                "content": json.dumps({"status": "failed"}),
            },
        )
        assert _is_tool_failure(evt) is True

    def test_error_field_present(self) -> None:
        evt = _make_event(
            1,
            "tool_result",
            data={
                "tool": "run_cmd",
                "is_error": False,
                "content": json.dumps({"error": "something broke"}),
            },
        )
        assert _is_tool_failure(evt) is True

    def test_successful_tool_result(self) -> None:
        evt = _make_event(
            1,
            "tool_result",
            data={
                "tool": "run_cmd",
                "is_error": False,
                "content": json.dumps({"exit_code": 0, "stdout": "ok"}),
            },
        )
        assert _is_tool_failure(evt) is False

    def test_non_tool_result_event(self) -> None:
        evt = _make_event(1, "tool_called", data={"tool": "x"})
        assert _is_tool_failure(evt) is False

    def test_error_field_none_string_not_failure(self) -> None:
        """error: 'none' should not be treated as a failure."""
        evt = _make_event(
            1,
            "tool_result",
            data={
                "tool": "run_cmd",
                "is_error": False,
                "content": json.dumps({"error": "none"}),
            },
        )
        assert _is_tool_failure(evt) is False

    def test_error_field_na_not_failure(self) -> None:
        """error: 'N/A' should not be treated as a failure."""
        evt = _make_event(
            1,
            "tool_result",
            data={
                "tool": "run_cmd",
                "is_error": False,
                "content": json.dumps({"error": "N/A"}),
            },
        )
        assert _is_tool_failure(evt) is False

    def test_error_field_real_error_still_detected(self) -> None:
        """A real error string should still be detected."""
        evt = _make_event(
            1,
            "tool_result",
            data={
                "tool": "run_cmd",
                "is_error": False,
                "content": json.dumps({"error": "connection refused"}),
            },
        )
        assert _is_tool_failure(evt) is True


class TestErrorClassification:
    """Tests for _classify_error and _error_similarity."""

    def test_infrastructure_pattern(self) -> None:
        from agents.introspection.skills import load_error_patterns

        patterns = load_error_patterns()
        assert _classify_error("address already in use", patterns) == "infrastructure"

    def test_transient_pattern(self) -> None:
        from agents.introspection.skills import load_error_patterns

        patterns = load_error_patterns()
        assert _classify_error("connection timed out", patterns) == "transient"

    def test_logic_fallback(self) -> None:
        from agents.introspection.skills import load_error_patterns

        patterns = load_error_patterns()
        assert _classify_error("invalid argument --foo", patterns) == "logic"

    def test_similarity_identical(self) -> None:
        assert (
            _error_similarity("address already in use", "address already in use") == 1.0
        )

    def test_similarity_different(self) -> None:
        assert _error_similarity("address already in use", "file not found") < 0.3

    def test_similarity_similar(self) -> None:
        # Same root cause, different context.
        a = "[Errno 98] address already in use on port 8080"
        b = "[Errno 98] address already in use on port 9090"
        assert _error_similarity(a, b) > 0.5

    def test_similarity_strips_hex_addresses(self) -> None:
        a = "segfault at 0xDEADBEEF in libfoo.so"
        b = "segfault at 0x12345678 in libfoo.so"
        assert _error_similarity(a, b) > 0.8

    def test_similarity_strips_timestamps(self) -> None:
        a = "2026-07-15T10:30:00 connection refused"
        b = "2026-07-15T11:45:22 connection refused"
        assert _error_similarity(a, b) > 0.8

    def test_similarity_strips_uuids(self) -> None:
        a = "task abc12345-1234-5678-9abc-def012345678 failed: OOM"
        b = "task 99999999-aaaa-bbbb-cccc-dddddddddddd failed: OOM"
        assert _error_similarity(a, b) > 0.8

    def test_similarity_traceback_noise(self) -> None:
        """Long traceback with same root cause should still match."""
        a = (
            "File /app/foo.py line 42 in bar\n"
            "  raise ConnectionError('refused')\n"
            "ConnectionError: connection refused"
        )
        b = (
            "File /app/foo.py line 99 in baz\n"
            "  raise ConnectionError('refused')\n"
            "ConnectionError: connection refused"
        )
        assert _error_similarity(a, b) > 0.7

    def test_extract_error_from_json(self) -> None:
        evt = _make_event(
            1,
            "tool_result",
            data={
                "content": json.dumps({"exit_code": 1, "error": "port 8080 in use"}),
            },
        )
        assert "port 8080 in use" in _extract_error_message(evt)


class TestDetectAnomalies:
    def test_detects_consecutive_failures(self) -> None:
        """Consecutive failures of the same tool with similar errors."""
        events = [
            _make_event(
                i,
                "tool_result",
                data={
                    "tool": "jmp_run",
                    "is_error": False,
                    "content": json.dumps(
                        {"exit_code": 1, "error": "address already in use"}
                    ),
                },
            )
            for i in range(1, 5)
        ]
        anomalies = _detect_anomalies_from_events(
            events,
            error_patterns=_EMPTY_PATTERNS,
            thresholds=_DEFAULT_THRESHOLDS,
        )
        consec = [a for a in anomalies if a["type"] == "consecutive_failure"]
        assert len(consec) == 1
        assert consec[0]["severity"] == "high"  # 4 >= consec_high
        assert "jmp_run" in consec[0]["description"]

    def test_consecutive_with_different_flags(self) -> None:
        """Same tool, different inputs, same error — should still detect."""
        events = [
            _make_event(
                1,
                "tool_result",
                data={
                    "tool": "jmp_run",
                    "is_error": False,
                    "content": json.dumps(
                        {"exit_code": 1, "error": "address already in use port 8080"}
                    ),
                },
            ),
            _make_event(
                2,
                "tool_result",
                data={
                    "tool": "jmp_run",
                    "is_error": False,
                    "content": json.dumps(
                        {
                            "exit_code": 1,
                            "error": "address already in use port 8080 --insecure",
                        }
                    ),
                },
            ),
        ]
        anomalies = _detect_anomalies_from_events(
            events,
            error_patterns=_EMPTY_PATTERNS,
            thresholds=_DEFAULT_THRESHOLDS,
        )
        consec = [a for a in anomalies if a["type"] == "consecutive_failure"]
        assert len(consec) == 1

    def test_content_based_repeated_errors(self) -> None:
        """Detects failures from content JSON, not just is_error."""
        events = [
            _make_event(
                i,
                "tool_result",
                data={
                    "tool": "execute_command",
                    "is_error": False,
                    "content": json.dumps(
                        {"exit_code": 1, "stderr": "Connection refused"}
                    ),
                },
            )
            for i in range(1, 5)
        ]
        anomalies = _detect_anomalies_from_events(
            events,
            error_patterns=_EMPTY_PATTERNS,
            thresholds=_DEFAULT_THRESHOLDS,
        )
        # Should detect as consecutive (4 in a row)
        consec = [a for a in anomalies if a["type"] == "consecutive_failure"]
        assert len(consec) == 1

    def test_is_error_true_still_works(self) -> None:
        """Backward compat: is_error=True still detected."""
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
        anomalies = _detect_anomalies_from_events(
            events,
            error_patterns=_EMPTY_PATTERNS,
            thresholds=_DEFAULT_THRESHOLDS,
        )
        # 6 consecutive → consecutive_failure (high)
        consec = [a for a in anomalies if a["type"] == "consecutive_failure"]
        assert len(consec) == 1
        assert consec[0]["severity"] == "high"

    def test_no_anomaly_for_single_error(self) -> None:
        events = [
            _make_event(
                1,
                "tool_result",
                data={
                    "tool": "execute_command",
                    "is_error": True,
                    "content": "Error",
                },
            ),
        ]
        anomalies = _detect_anomalies_from_events(
            events,
            error_patterns=_EMPTY_PATTERNS,
            thresholds=_DEFAULT_THRESHOLDS,
        )
        assert len(anomalies) == 0

    def test_error_classification_in_description(self) -> None:
        """Infrastructure errors get classified in the anomaly description."""
        from agents.introspection.skills import load_error_patterns

        patterns = load_error_patterns()
        events = [
            _make_event(
                i,
                "tool_result",
                data={
                    "tool": "jmp_run",
                    "is_error": False,
                    "content": json.dumps(
                        {"exit_code": 1, "error": "address already in use"}
                    ),
                },
            )
            for i in range(1, 4)
        ]
        anomalies = _detect_anomalies_from_events(
            events,
            error_patterns=patterns,
            thresholds=_DEFAULT_THRESHOLDS,
        )
        consec = [a for a in anomalies if a["type"] == "consecutive_failure"]
        assert len(consec) == 1
        assert consec[0]["error_class"] == "infrastructure"
        assert "retrying won't help" in consec[0]["description"]

    def test_wasted_iterations(self) -> None:
        """Detects agents where most LLM calls produce only failures."""
        events = []
        for i in range(8):
            events.append(
                _make_event(
                    i * 2 + 1,
                    "llm_request",
                    agent="prov-agent",
                    data={"iteration": i},
                )
            )
            # First 5 iterations: all failures.
            if i < 5:
                events.append(
                    _make_event(
                        i * 2 + 2,
                        "tool_result",
                        agent="prov-agent",
                        data={
                            "tool": "jmp_run",
                            "is_error": True,
                            "content": "failed",
                        },
                    )
                )
            else:
                events.append(
                    _make_event(
                        i * 2 + 2,
                        "tool_result",
                        agent="prov-agent",
                        data={
                            "tool": "jmp_run",
                            "is_error": False,
                            "content": json.dumps({"exit_code": 0}),
                        },
                    )
                )
        anomalies = _detect_anomalies_from_events(
            events,
            error_patterns=_EMPTY_PATTERNS,
            thresholds=_DEFAULT_THRESHOLDS,
        )
        wasted = [a for a in anomalies if a["type"] == "wasted_iterations"]
        assert len(wasted) == 1
        assert "prov-agent" in wasted[0]["description"]
        # 5 wasted out of 8 = 62%
        assert "62%" in wasted[0]["description"]

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
        anomalies = _detect_anomalies_from_events(
            events,
            error_patterns=_EMPTY_PATTERNS,
            thresholds=_DEFAULT_THRESHOLDS,
        )
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
        anomalies = _detect_anomalies_from_events(
            events,
            error_patterns=_EMPTY_PATTERNS,
            thresholds=_DEFAULT_THRESHOLDS,
        )
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
        anomalies = _detect_anomalies_from_events(
            events,
            error_patterns=_EMPTY_PATTERNS,
            thresholds=_DEFAULT_THRESHOLDS,
        )
        max_iter = [a for a in anomalies if a["type"] == "excessive_iterations"]
        assert len(max_iter) == 1
        assert max_iter[0]["severity"] == "high"

    def test_empty_events_no_anomalies(self) -> None:
        anomalies = _detect_anomalies_from_events(
            [],
            error_patterns=_EMPTY_PATTERNS,
            thresholds=_DEFAULT_THRESHOLDS,
        )
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
        anomalies = _detect_anomalies_from_events(
            events,
            error_patterns=_EMPTY_PATTERNS,
            thresholds=_DEFAULT_THRESHOLDS,
        )
        assert anomalies == []

    def test_custom_thresholds(self) -> None:
        """Thresholds from skills control detection sensitivity."""
        events = [
            _make_event(
                i,
                "tool_result",
                data={
                    "tool": "cmd",
                    "is_error": True,
                    "content": "same error",
                },
            )
            for i in range(1, 4)
        ]
        # With default min=2, should detect.
        anomalies = _detect_anomalies_from_events(
            events,
            error_patterns=_EMPTY_PATTERNS,
            thresholds=_DEFAULT_THRESHOLDS,
        )
        assert any(a["type"] == "consecutive_failure" for a in anomalies)

        # With raised min=5, should NOT detect.
        strict = dict(_DEFAULT_THRESHOLDS)
        strict["consecutive_failure_min"] = 5
        anomalies = _detect_anomalies_from_events(
            events,
            error_patterns=_EMPTY_PATTERNS,
            thresholds=strict,
        )
        assert not any(a["type"] == "consecutive_failure" for a in anomalies)


class TestSkillLoading:
    """Tests for introspection skill file loading."""

    def test_loads_error_patterns_from_skills(self) -> None:
        from agents.introspection.skills import load_error_patterns

        patterns = load_error_patterns()
        assert "infrastructure" in patterns
        assert "transient" in patterns
        assert len(patterns["infrastructure"]) > 0
        assert len(patterns["transient"]) > 0

    def test_loads_thresholds_from_skills(self) -> None:
        from agents.introspection.skills import load_thresholds

        thresholds = load_thresholds()
        assert "consecutive_failure_min" in thresholds
        assert "wasted_iterations_pct" in thresholds
        assert isinstance(thresholds["consecutive_failure_min"], int)

    def test_private_overrides_extend_patterns(self) -> None:
        from agents.introspection.skills import load_error_patterns

        private = {
            "error_patterns": {
                "infrastructure": ["custom org error pattern"],
            }
        }
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch(
                "agents.introspection.skills.PRIVATE_SKILLS_DIR",
                Path(tmp),
                create=True,
            ),
        ):
            path = Path(tmp) / "introspection.json"
            path.write_text(json.dumps(private))
            # Re-import to pick up patched path.
            with patch(
                "agents.introspection.skills._load_private_overrides",
                return_value=private,
            ):
                patterns = load_error_patterns()

        # Should include both shipped and private patterns.
        all_patterns_str = [p.pattern for p in patterns["infrastructure"]]
        assert "custom org error pattern" in all_patterns_str
        # Should still have shipped patterns.
        assert any("address already in use" in p for p in all_patterns_str)


# --- Continuous agent ---


class TestIntrospectionAgent:
    def test_builds_observation_with_anomalies(self) -> None:
        agent = IntrospectionAgent(
            state_store_url="http://localhost:8090",
        )
        agent._all_events = [
            _make_event(1, "llm_request", data={"iteration": 0}),
            _make_event(
                2,
                "tool_result",
                data={
                    "tool": "ssh",
                    "is_error": True,
                    "content": "fail",
                },
            ),
            _make_event(
                3,
                "tool_result",
                data={
                    "tool": "ssh",
                    "is_error": True,
                    "content": "fail",
                },
            ),
            _make_event(
                4,
                "tool_result",
                data={
                    "tool": "ssh",
                    "is_error": True,
                    "content": "fail",
                },
            ),
        ]
        ticket = {"status": "executing_benchmark"}
        new_events = [_make_event(5, "agent_finished")]
        anomalies = [
            {
                "type": "repeated_error",
                "severity": "medium",
                "description": "Tool 'ssh' failed 3 times",
                "seq_range": [2, 4],
            }
        ]
        obs = agent._build_observation(ticket, new_events, anomalies)
        assert obs["total_events"] == 4
        assert len(obs["anomalies"]) == 1
        assert "1 anomaly" in obs["status_summary"]
        assert isinstance(obs["narrative"], list)
        assert any("benchmark-agent finished" in e for e in obs["narrative"])

    def test_builds_observation_clean(self) -> None:
        agent = IntrospectionAgent(
            state_store_url="http://localhost:8090",
        )
        agent._all_events = [
            _make_event(1, "agent_started"),
            _make_event(2, "agent_finished"),
        ]
        ticket = {"status": "awaiting_review"}
        obs = agent._build_observation(ticket, [], [])
        assert obs["anomalies"] == []
        assert "0 tool errors" in obs["status_summary"]
        assert obs["narrative"] == []

    def test_narrative_includes_transitions(self) -> None:
        agent = IntrospectionAgent(
            state_store_url="http://localhost:8090",
        )
        agent._all_events = []
        ticket = {"status": "triage_pending"}
        new_events = [
            _make_event(
                1,
                "transition",
                agent="system",
                data={"to": "awaiting_hardware"},
            ),
        ]
        obs = agent._build_observation(ticket, new_events, [])
        assert any("Transitioned to awaiting_hardware" in e for e in obs["narrative"])

    def test_narrative_accumulates_across_calls(self) -> None:
        agent = IntrospectionAgent(
            state_store_url="http://localhost:8090",
        )
        agent._all_events = []
        ticket = {"status": "executing_benchmark"}

        # First batch.
        events1 = [_make_event(1, "agent_started")]
        agent._build_observation(ticket, events1, [])

        # Second batch.
        events2 = [_make_event(2, "agent_finished")]
        obs = agent._build_observation(ticket, events2, [])

        # Both entries should be in the narrative.
        assert len(obs["narrative"]) == 2
        assert "benchmark-agent started" in obs["narrative"][0]
        assert "benchmark-agent finished" in obs["narrative"][1]

    def test_narrative_caps_at_max_entries(self) -> None:
        agent = IntrospectionAgent(
            state_store_url="http://localhost:8090",
        )
        agent._all_events = []
        ticket = {"status": "executing_benchmark"}

        # Feed 250 events (cap is 200).
        events = [_make_event(i, "agent_started") for i in range(1, 251)]
        obs = agent._build_observation(ticket, events, [])
        assert len(obs["narrative"]) == 200
        # Should keep the most recent entries.
        assert "benchmark-agent started" in obs["narrative"][-1]

    async def test_stops_on_terminal_status(self) -> None:
        agent = IntrospectionAgent(
            state_store_url="http://localhost:8090",
        )
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "status": "closed",
            "custom_fields": {},
        }
        mock_response.raise_for_status = MagicMock()
        agent._client = AsyncMock()
        agent._client.get = AsyncMock(return_value=mock_response)
        agent._client.patch = AsyncMock(return_value=mock_response)
        agent._client.aclose = AsyncMock()

        # Should exit after detecting closed + final flush.
        with patch(
            "agents.introspection.agent._POLL_INTERVAL",
            0.1,
        ):
            await asyncio.wait_for(
                agent.run("PERF-CLOSED"),
                timeout=3.0,
            )

    async def test_request_stop(self) -> None:
        agent = IntrospectionAgent(
            state_store_url="http://localhost:8090",
        )
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "status": "executing_benchmark",
            "custom_fields": {},
        }
        mock_response.raise_for_status = MagicMock()
        agent._client = AsyncMock()
        agent._client.get = AsyncMock(return_value=mock_response)
        agent._client.aclose = AsyncMock()

        # Request stop immediately so the loop exits.
        agent.request_stop()
        await asyncio.wait_for(
            agent.run("PERF-STOP"),
            timeout=3.0,
        )

    async def test_seeds_narrative_from_ticket(self) -> None:
        """On restart, narrative history is seeded from the ticket."""
        agent = IntrospectionAgent(
            state_store_url="http://localhost:8090",
        )
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "status": "executing_benchmark",
            "custom_fields": {
                "introspection": {
                    "narrative": [
                        "triage-agent started",
                        "Transitioned to awaiting_hardware",
                        "[observation] Pipeline progressing.",
                    ],
                    "anomalies": [
                        {"type": "repeated_error", "severity": "medium"},
                    ],
                },
            },
        }
        mock_response.raise_for_status = MagicMock()
        agent._client = AsyncMock()
        agent._client.get = AsyncMock(return_value=mock_response)
        agent._client.aclose = AsyncMock()

        await agent._seed_from_ticket("PERF-SEED")

        assert len(agent._narrative_log) == 3
        assert agent._narrative_log[0] == "triage-agent started"
        assert agent._prev_status == "executing_benchmark"
        assert agent._prev_anomaly_count == 1


# --- Orchestrator integration ---


class TestLLMIntegration:
    """Tests for LLM narrative and final summary."""

    async def test_maybe_narrate_skips_without_llm(self) -> None:
        agent = IntrospectionAgent(
            state_store_url="http://localhost:8090",
        )

        result = await agent._maybe_narrate(
            "PERF-1",
            {"status": "executing_benchmark"},
            [],
            [],
        )
        assert result is None

    async def test_maybe_narrate_triggers_on_anomaly(self) -> None:
        from providers.llm.base import LLMResponse

        mock_llm = AsyncMock()
        mock_llm.complete = AsyncMock(
            return_value=LLMResponse(
                text="The agent is retrying a failing operation.",
                tool_calls=[],
                stop_reason="end_turn",
                raw_content="",
                usage=None,
            )
        )
        agent = IntrospectionAgent(
            state_store_url="http://localhost:8090",
            llm_provider=mock_llm,
        )
        agent._prev_anomaly_count = 0
        agent._prev_status = "executing_benchmark"
        agent._all_events = [_make_event(1, "agent_started")]

        result = await agent._maybe_narrate(
            "PERF-1",
            {"status": "executing_benchmark"},
            [],
            [{"type": "consecutive_failure", "severity": "high"}],
        )
        assert result is not None
        assert "retrying" in result
        mock_llm.complete.assert_called_once()

    async def test_maybe_narrate_triggers_on_transition(self) -> None:
        from providers.llm.base import LLMResponse

        mock_llm = AsyncMock()
        mock_llm.complete = AsyncMock(
            return_value=LLMResponse(
                text="Ticket transitioned to review.",
                tool_calls=[],
                stop_reason="end_turn",
                raw_content="",
                usage=None,
            )
        )
        agent = IntrospectionAgent(
            state_store_url="http://localhost:8090",
            llm_provider=mock_llm,
        )
        agent._prev_anomaly_count = 0
        agent._prev_status = "executing_benchmark"
        agent._all_events = [_make_event(1, "agent_started")]

        result = await agent._maybe_narrate(
            "PERF-1",
            {"status": "awaiting_review"},
            [],
            [],
        )
        assert result is not None
        mock_llm.complete.assert_called_once()

    async def test_maybe_narrate_skips_when_no_trigger(self) -> None:
        mock_llm = AsyncMock()
        agent = IntrospectionAgent(
            state_store_url="http://localhost:8090",
            llm_provider=mock_llm,
        )
        agent._prev_anomaly_count = 0
        agent._prev_status = "executing_benchmark"

        result = await agent._maybe_narrate(
            "PERF-1",
            {"status": "executing_benchmark"},
            [],
            [],
        )
        assert result is None
        mock_llm.complete.assert_not_called()

    async def test_observation_includes_llm_narrative(self) -> None:
        agent = IntrospectionAgent(
            state_store_url="http://localhost:8090",
        )
        agent._all_events = [_make_event(1, "agent_started")]
        ticket = {"status": "executing_benchmark"}

        obs = agent._build_observation(
            ticket,
            [],
            [],
            llm_narrative="The pipeline is progressing normally.",
        )
        assert any("[observation]" in line for line in obs["narrative"])
        assert any("progressing normally" in line for line in obs["narrative"])

    async def test_deterministic_final_summary_without_llm(self) -> None:
        agent = IntrospectionAgent(
            state_store_url="http://localhost:8090",
        )
        agent._all_events = [
            _make_event(1, "agent_started"),
            _make_event(2, "agent_finished"),
        ]
        summary = agent._deterministic_final_summary([], agent._compute_stats())
        assert summary["verdict"] == "clean"
        assert summary["stats"]["total_events"] == 2

    async def test_usage_recording(self) -> None:
        from providers.events import EventBus

        bus = EventBus()
        agent = IntrospectionAgent(
            state_store_url="http://localhost:8090",
            event_bus=bus,
        )
        mock_response = MagicMock()
        mock_response.usage = {
            "input_tokens": 100,
            "output_tokens": 50,
            "model": "claude-haiku-4-5",
        }
        agent._record_usage("PERF-1", mock_response)

        usage = bus.get_cumulative_usage("PERF-1")
        assert usage["input_tokens"] == 100
        assert usage["output_tokens"] == 50

        agent_usage = bus.get_agent_usage("PERF-1")
        assert "introspection-agent" in agent_usage

    async def test_usage_recording_object_usage(self) -> None:
        """_record_usage handles usage as an object with attributes."""
        from providers.events import EventBus

        bus = EventBus()
        agent = IntrospectionAgent(
            state_store_url="http://localhost:8090",
            event_bus=bus,
        )

        # Simulate a provider that returns an object, not a dict.
        class UsageObj:
            input_tokens = 200
            output_tokens = 75
            model = "some-model"
            cache_read_input_tokens = 0
            cache_creation_input_tokens = 0

        mock_response = MagicMock()
        mock_response.usage = UsageObj()
        agent._record_usage("PERF-2", mock_response)

        usage = bus.get_cumulative_usage("PERF-2")
        assert usage["input_tokens"] == 200
        assert usage["output_tokens"] == 75

    async def test_async_context_manager(self) -> None:
        """IntrospectionAgent works as an async context manager."""
        agent = IntrospectionAgent(
            state_store_url="http://localhost:8090",
        )
        async with agent:
            assert agent._client is not None
        # After exit, client should be closed.
        assert agent._client.is_closed


class TestIntrospectionConfig:
    def test_default_disabled(self) -> None:
        from orchestrator.config import OrchestratorConfig

        with patch.dict("os.environ", {}, clear=True):
            config = OrchestratorConfig()

        assert config.introspection_enabled is False

    def test_enabled_via_config_file(self) -> None:
        from orchestrator.config import OrchestratorConfig

        cfg = {"introspection": {"enabled": True}}
        with (
            patch.dict("os.environ", {}, clear=True),
            patch(
                "orchestrator.config._load_config_file",
                return_value=cfg,
            ),
        ):
            config = OrchestratorConfig()

        assert config.introspection_enabled is True

    def test_enabled_via_env_var(self) -> None:
        from orchestrator.config import OrchestratorConfig

        with patch.dict(
            "os.environ",
            {"INTROSPECTION_ENABLED": "true"},
            clear=True,
        ):
            config = OrchestratorConfig()

        assert config.introspection_enabled is True


class TestMaybeStartIntrospection:
    def test_starts_when_globally_enabled(self) -> None:
        from orchestrator.config import OrchestratorConfig
        from orchestrator.main import _maybe_start_introspection

        config = MagicMock(spec=OrchestratorConfig)
        config.introspection_enabled = True
        dispatcher = MagicMock()
        dispatcher.is_introspection_active.return_value = False
        dispatcher.start_introspection.return_value = True
        ticket = {"custom_fields": {}}

        _maybe_start_introspection(dispatcher, config, ticket, "PERF-1")

        dispatcher.start_introspection.assert_called_once_with("PERF-1")

    def test_skips_when_globally_disabled(self) -> None:
        from orchestrator.config import OrchestratorConfig
        from orchestrator.main import _maybe_start_introspection

        config = MagicMock(spec=OrchestratorConfig)
        config.introspection_enabled = False
        dispatcher = MagicMock()
        dispatcher.is_introspection_active.return_value = False
        ticket = {"custom_fields": {}}

        _maybe_start_introspection(dispatcher, config, ticket, "PERF-1")

        dispatcher.start_introspection.assert_not_called()

    def test_per_ticket_override_enables(self) -> None:
        from orchestrator.config import OrchestratorConfig
        from orchestrator.main import _maybe_start_introspection

        config = MagicMock(spec=OrchestratorConfig)
        config.introspection_enabled = False
        dispatcher = MagicMock()
        dispatcher.is_introspection_active.return_value = False
        dispatcher.start_introspection.return_value = True
        ticket = {"custom_fields": {"introspection_enabled": True}}

        _maybe_start_introspection(dispatcher, config, ticket, "PERF-1")

        dispatcher.start_introspection.assert_called_once_with("PERF-1")

    def test_per_ticket_override_disables(self) -> None:
        from orchestrator.config import OrchestratorConfig
        from orchestrator.main import _maybe_start_introspection

        config = MagicMock(spec=OrchestratorConfig)
        config.introspection_enabled = True
        dispatcher = MagicMock()
        dispatcher.is_introspection_active.return_value = False
        ticket = {"custom_fields": {"introspection_enabled": False}}

        _maybe_start_introspection(dispatcher, config, ticket, "PERF-1")

        dispatcher.start_introspection.assert_not_called()

    def test_skips_when_already_active(self) -> None:
        from orchestrator.config import OrchestratorConfig
        from orchestrator.main import _maybe_start_introspection

        config = MagicMock(spec=OrchestratorConfig)
        config.introspection_enabled = True
        dispatcher = MagicMock()
        dispatcher.is_introspection_active.return_value = True
        ticket = {"custom_fields": {}}

        _maybe_start_introspection(dispatcher, config, ticket, "PERF-1")

        dispatcher.start_introspection.assert_not_called()


# --- Interleaved tool detection ---


class TestInterleavedDetection:
    """Tests for detection that survives interleaved diagnostic tools."""

    def test_consecutive_failure_survives_interleaved_success(self) -> None:
        """A successful diagnostic tool between retries of a failing
        tool should NOT reset the failing tool's streak."""
        events = [
            _make_event(
                1,
                "tool_result",
                data={
                    "tool": "jmp_run",
                    "is_error": False,
                    "content": json.dumps({"exit_code": 1, "error": "port in use"}),
                },
            ),
            # Diagnostic tool succeeds — should NOT reset jmp_run streak.
            _make_event(
                2,
                "tool_result",
                data={
                    "tool": "get_status",
                    "is_error": False,
                    "content": json.dumps({"exit_code": 0}),
                },
            ),
            _make_event(
                3,
                "tool_result",
                data={
                    "tool": "jmp_run",
                    "is_error": False,
                    "content": json.dumps({"exit_code": 1, "error": "port in use"}),
                },
            ),
            _make_event(
                4,
                "tool_result",
                data={
                    "tool": "check_os",
                    "is_error": False,
                    "content": json.dumps({"exit_code": 0}),
                },
            ),
            _make_event(
                5,
                "tool_result",
                data={
                    "tool": "jmp_run",
                    "is_error": False,
                    "content": json.dumps({"exit_code": 1, "error": "port in use"}),
                },
            ),
        ]
        anomalies = _detect_anomalies_from_events(
            events,
            error_patterns=_EMPTY_PATTERNS,
            thresholds=_DEFAULT_THRESHOLDS,
        )
        consec = [a for a in anomalies if a["type"] == "consecutive_failure"]
        assert len(consec) == 1
        assert "jmp_run" in consec[0]["description"]
        assert "3 times" in consec[0]["description"]

    def test_same_tool_success_resets_streak(self) -> None:
        """A success of the SAME tool should still reset its streak."""
        events = [
            _make_event(
                1,
                "tool_result",
                data={
                    "tool": "jmp_run",
                    "is_error": True,
                    "content": "fail",
                },
            ),
            _make_event(
                2,
                "tool_result",
                data={
                    "tool": "jmp_run",
                    "is_error": True,
                    "content": "fail",
                },
            ),
            # Same tool succeeds — resets the streak.
            _make_event(
                3,
                "tool_result",
                data={
                    "tool": "jmp_run",
                    "is_error": False,
                    "content": json.dumps({"exit_code": 0}),
                },
            ),
            _make_event(
                4,
                "tool_result",
                data={
                    "tool": "jmp_run",
                    "is_error": True,
                    "content": "fail",
                },
            ),
        ]
        anomalies = _detect_anomalies_from_events(
            events,
            error_patterns=_EMPTY_PATTERNS,
            thresholds=_DEFAULT_THRESHOLDS,
        )
        consec = [a for a in anomalies if a["type"] == "consecutive_failure"]
        # 2 before success + 1 after = two separate streaks,
        # but each is below the default min of 2 for the second.
        # First streak is exactly 2 → detected.
        assert len(consec) == 1
        assert "2 times" in consec[0]["description"]

    def test_retry_loop_survives_interleaved_tool(self) -> None:
        """Identical retries of a tool should be detected even when
        a different tool is called in between."""
        events = [
            _make_event(
                1,
                "tool_called",
                data={
                    "tool": "execute_command",
                    "input": {"command": "ls /tmp"},
                },
            ),
            # Different tool in between.
            _make_event(
                2,
                "tool_called",
                data={
                    "tool": "get_status",
                    "input": {"id": "PERF-1"},
                },
            ),
            _make_event(
                3,
                "tool_called",
                data={
                    "tool": "execute_command",
                    "input": {"command": "ls /tmp"},
                },
            ),
            _make_event(
                4,
                "tool_called",
                data={
                    "tool": "get_status",
                    "input": {"id": "PERF-1"},
                },
            ),
            _make_event(
                5,
                "tool_called",
                data={
                    "tool": "execute_command",
                    "input": {"command": "ls /tmp"},
                },
            ),
        ]
        anomalies = _detect_anomalies_from_events(
            events,
            error_patterns=_EMPTY_PATTERNS,
            thresholds=_DEFAULT_THRESHOLDS,
        )
        loops = [a for a in anomalies if a["type"] == "retry_loop"]
        assert len(loops) >= 1
        exec_loops = [a for a in loops if "execute_command" in a["description"]]
        assert len(exec_loops) == 1
        assert "3 times" in exec_loops[0]["description"]

    def test_retry_loop_different_input_no_detection(self) -> None:
        """Same tool with different input should not be a retry loop."""
        events = [
            _make_event(
                1,
                "tool_called",
                data={
                    "tool": "execute_command",
                    "input": {"command": "ls /tmp"},
                },
            ),
            _make_event(
                2,
                "tool_called",
                data={
                    "tool": "execute_command",
                    "input": {"command": "ls /var"},
                },
            ),
            _make_event(
                3,
                "tool_called",
                data={
                    "tool": "execute_command",
                    "input": {"command": "ls /home"},
                },
            ),
        ]
        anomalies = _detect_anomalies_from_events(
            events,
            error_patterns=_EMPTY_PATTERNS,
            thresholds=_DEFAULT_THRESHOLDS,
        )
        loops = [a for a in anomalies if a["type"] == "retry_loop"]
        assert len(loops) == 0


# --- Tool bypass detection ---


class TestToolBypassDetection:
    """Tests for tool bypass pattern detection."""

    def _bypass_patterns(self) -> dict:
        """Load real bypass patterns from skills."""
        from agents.introspection.skills import load_tool_bypass_patterns

        return load_tool_bypass_patterns()

    def test_generic_tool_bypass_detected(self) -> None:
        """Flags when benchmark-agent uses execute_command many
        times without calling execute_benchmark."""
        events = [
            _make_event(
                i,
                "tool_called",
                agent="benchmark-agent",
                data={
                    "tool": "execute_command",
                    "input": {"command": f"cmd-{i}"},
                },
            )
            for i in range(1, 6)
        ]
        anomalies = _detect_anomalies_from_events(
            events,
            error_patterns=_EMPTY_PATTERNS,
            thresholds=_DEFAULT_THRESHOLDS,
            bypass_patterns=self._bypass_patterns(),
        )
        bypass = [a for a in anomalies if a["type"] == "tool_bypass"]
        assert len(bypass) >= 1
        generic_bypass = [a for a in bypass if "execute_benchmark" in a["description"]]
        assert len(generic_bypass) == 1
        assert generic_bypass[0]["severity"] == "high"

    def test_no_bypass_when_specialized_tool_used(self) -> None:
        """No bypass flag when execute_benchmark is also called."""
        events = [
            _make_event(
                i,
                "tool_called",
                agent="benchmark-agent",
                data={
                    "tool": "execute_command",
                    "input": {"command": f"cmd-{i}"},
                },
            )
            for i in range(1, 6)
        ] + [
            _make_event(
                6,
                "tool_called",
                agent="benchmark-agent",
                data={
                    "tool": "execute_benchmark",
                    "input": {"run_file": "test.yaml"},
                },
            ),
        ]
        anomalies = _detect_anomalies_from_events(
            events,
            error_patterns=_EMPTY_PATTERNS,
            thresholds=_DEFAULT_THRESHOLDS,
            bypass_patterns=self._bypass_patterns(),
        )
        generic_bypass = [
            a
            for a in anomalies
            if a["type"] == "tool_bypass" and "execute_benchmark" in a["description"]
        ]
        assert len(generic_bypass) == 0

    def test_no_bypass_below_threshold(self) -> None:
        """No flag when generic tool count is below threshold."""
        events = [
            _make_event(
                i,
                "tool_called",
                agent="benchmark-agent",
                data={
                    "tool": "execute_command",
                    "input": {"command": f"cmd-{i}"},
                },
            )
            for i in range(1, 3)  # Only 2, below default 3
        ]
        anomalies = _detect_anomalies_from_events(
            events,
            error_patterns=_EMPTY_PATTERNS,
            thresholds=_DEFAULT_THRESHOLDS,
            bypass_patterns=self._bypass_patterns(),
        )
        generic_bypass = [
            a
            for a in anomalies
            if a["type"] == "tool_bypass" and "execute_benchmark" in a["description"]
        ]
        assert len(generic_bypass) == 0

    def test_schema_exploration_detected(self) -> None:
        """Flags podman run --schema via execute_command."""
        events = [
            _make_event(
                1,
                "tool_called",
                agent="benchmark-agent",
                data={
                    "tool": "execute_command",
                    "input": {
                        "command": (
                            "podman run --rm quay.io/plugin:latest --schema 2>/dev/null"
                        ),
                    },
                },
            ),
        ]
        anomalies = _detect_anomalies_from_events(
            events,
            error_patterns=_EMPTY_PATTERNS,
            thresholds=_DEFAULT_THRESHOLDS,
            bypass_patterns=self._bypass_patterns(),
        )
        schema_bypass = [
            a
            for a in anomalies
            if a["type"] == "tool_bypass" and "schema" in a["description"].lower()
        ]
        assert len(schema_bypass) == 1
        assert schema_bypass[0]["severity"] == "medium"

    def test_container_orchestration_detected(self) -> None:
        """Flags podman run (non-schema) via execute_command."""
        events = [
            _make_event(
                1,
                "tool_called",
                agent="benchmark-agent",
                data={
                    "tool": "execute_command",
                    "input": {
                        "command": (
                            "podman run -d --network=host quay.io/plugin:latest"
                        ),
                    },
                },
            ),
        ]
        anomalies = _detect_anomalies_from_events(
            events,
            error_patterns=_EMPTY_PATTERNS,
            thresholds=_DEFAULT_THRESHOLDS,
            bypass_patterns=self._bypass_patterns(),
        )
        container_bypass = [
            a
            for a in anomalies
            if a["type"] == "tool_bypass"
            and "container orchestration" in a["description"].lower()
        ]
        assert len(container_bypass) == 1
        assert container_bypass[0]["severity"] == "high"

    def test_non_benchmark_agent_not_flagged(self) -> None:
        """Bypass patterns are agent-scoped."""
        events = [
            _make_event(
                i,
                "tool_called",
                agent="provisioning-agent",
                data={
                    "tool": "execute_command",
                    "input": {"command": f"cmd-{i}"},
                },
            )
            for i in range(1, 6)
        ]
        anomalies = _detect_anomalies_from_events(
            events,
            error_patterns=_EMPTY_PATTERNS,
            thresholds=_DEFAULT_THRESHOLDS,
            bypass_patterns=self._bypass_patterns(),
        )
        bypass = [
            a
            for a in anomalies
            if a["type"] == "tool_bypass" and "execute_benchmark" in a["description"]
        ]
        assert len(bypass) == 0


# --- LLM summary parsing ---


class TestParseSummaryResponse:
    """Tests for _parse_summary_response edge cases."""

    def test_direct_json(self) -> None:
        text = json.dumps(
            {
                "verdict": "clean",
                "observations": ["all good"],
                "recommendations": [],
            }
        )
        result = IntrospectionAgent._parse_summary_response(text)
        assert result["verdict"] == "clean"
        assert result["observations"] == ["all good"]

    def test_code_fenced_json(self) -> None:
        text = (
            "Here is the summary:\n\n"
            "```json\n"
            '{"verdict": "minor_issues", '
            '"observations": ["one issue"], '
            '"recommendations": []}\n'
            "```\n"
        )
        result = IntrospectionAgent._parse_summary_response(text)
        assert result["verdict"] == "minor_issues"

    def test_code_fence_without_json_tag(self) -> None:
        text = (
            "Summary:\n\n"
            "```\n"
            '{"verdict": "needs_attention", '
            '"observations": [], '
            '"recommendations": []}\n'
            "```\n"
        )
        result = IntrospectionAgent._parse_summary_response(text)
        assert result["verdict"] == "needs_attention"

    def test_malformed_falls_back_to_narrative(self) -> None:
        text = "The pipeline ran cleanly with no issues."
        result = IntrospectionAgent._parse_summary_response(text)
        assert result["verdict"] == "unknown"
        assert len(result["observations"]) == 1
        assert "cleanly" in result["observations"][0]
        assert result["recommendations"] == []

    def test_none_input(self) -> None:
        result = IntrospectionAgent._parse_summary_response(None)
        assert result == {}

    def test_empty_string(self) -> None:
        result = IntrospectionAgent._parse_summary_response("")
        assert result == {}


class TestLLMFinalSummary:
    """Tests for the LLM final summary path."""

    async def test_llm_final_summary_happy_path(self) -> None:
        from providers.llm.base import LLMResponse

        mock_llm = AsyncMock()
        mock_llm.complete = AsyncMock(
            return_value=LLMResponse(
                text=json.dumps(
                    {
                        "verdict": "minor_issues",
                        "observations": ["Agent retried 3 times"],
                        "recommendations": [
                            {
                                "area": "infrastructure",
                                "suggestion": "Check port conflicts",
                            }
                        ],
                    }
                ),
                tool_calls=[],
                stop_reason="end_turn",
                raw_content="",
                usage=None,
            )
        )
        agent = IntrospectionAgent(
            state_store_url="http://localhost:8090",
            llm_provider=mock_llm,
        )
        agent._all_events = [
            _make_event(1, "agent_started"),
            _make_event(2, "agent_finished"),
        ]

        summary = await agent._llm_final_summary(
            "PERF-1",
            {"id": "PERF-1", "summary": "test"},
            [],
            agent._compute_stats(),
        )
        assert summary["verdict"] == "minor_issues"
        assert "stats" in summary
        assert summary["stats"]["total_events"] == 2
        mock_llm.complete.assert_called_once()

    async def test_llm_final_summary_falls_back_on_error(self) -> None:
        mock_llm = AsyncMock()
        mock_llm.complete = AsyncMock(
            side_effect=Exception("API timeout"),
        )
        agent = IntrospectionAgent(
            state_store_url="http://localhost:8090",
            llm_provider=mock_llm,
        )
        agent._all_events = [
            _make_event(1, "agent_started"),
            _make_event(2, "agent_finished"),
        ]

        summary = await agent._llm_final_summary(
            "PERF-1",
            {"id": "PERF-1", "summary": "test"},
            [],
            agent._compute_stats(),
        )
        # Should fall back to deterministic summary.
        assert summary["verdict"] == "clean"
        assert "stats" in summary
        assert summary["anomalies"] == []

    async def test_llm_final_summary_with_code_fence(self) -> None:
        from providers.llm.base import LLMResponse

        mock_llm = AsyncMock()
        mock_llm.complete = AsyncMock(
            return_value=LLMResponse(
                text=(
                    "Here is my analysis:\n\n"
                    "```json\n"
                    '{"verdict": "needs_attention", '
                    '"observations": ["High error rate"], '
                    '"recommendations": []}\n'
                    "```\n"
                ),
                tool_calls=[],
                stop_reason="end_turn",
                raw_content="",
                usage=None,
            )
        )
        agent = IntrospectionAgent(
            state_store_url="http://localhost:8090",
            llm_provider=mock_llm,
        )
        agent._all_events = [
            _make_event(1, "agent_started"),
        ]

        summary = await agent._llm_final_summary(
            "PERF-1",
            {"id": "PERF-1", "summary": "test"},
            [{"type": "consecutive_failure", "severity": "high"}],
            agent._compute_stats(),
        )
        assert summary["verdict"] == "needs_attention"
        assert summary["stats"]["total_events"] == 1
