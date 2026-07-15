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
        agent._client.aclose = AsyncMock()

        # Should exit quickly since ticket is closed.
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


# --- Orchestrator integration ---


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
