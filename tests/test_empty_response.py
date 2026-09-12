"""Tests for empty LLM response handling."""

from __future__ import annotations

from agents.introspection.server import _detect_anomalies_from_events


class TestEmptyResponseAnomaly:
    """Test introspection detection of empty LLM responses."""

    def test_detects_empty_response(self):
        events = [
            {
                "event_type": "agent_error",
                "agent": "triage-agent",
                "data": {
                    "reason": "empty_response",
                    "iteration": 0,
                    "action": "retry",
                },
            },
        ]
        anomalies = _detect_anomalies_from_events(events)
        empty = [a for a in anomalies if a["type"] == "empty_llm_response"]
        assert len(empty) == 1
        assert "triage-agent" in empty[0]["description"]
        assert empty[0]["severity"] == "medium"

    def test_no_false_positive(self):
        events = [
            {
                "event_type": "agent_error",
                "agent": "review-agent",
                "data": {"reason": "rate_limited"},
            },
        ]
        anomalies = _detect_anomalies_from_events(events)
        empty = [a for a in anomalies if a["type"] == "empty_llm_response"]
        assert len(empty) == 0

    def test_multiple_empty_responses(self):
        events = [
            {
                "event_type": "agent_error",
                "agent": "triage-agent",
                "data": {"reason": "empty_response"},
            },
            {
                "event_type": "agent_error",
                "agent": "review-agent",
                "data": {"reason": "empty_response"},
            },
        ]
        anomalies = _detect_anomalies_from_events(events)
        empty = [a for a in anomalies if a["type"] == "empty_llm_response"]
        assert len(empty) == 2
