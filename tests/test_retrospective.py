"""Tests for the retrospective agent's transcript analysis engine.

Covers: signal extraction (tool errors, retry sequences, fail-then-succeed,
max_iterations, HITL escalations, self-correction), context windowing,
and per-agent statistics.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from agents.retrospective.server import (
    _compute_stats,
    _extract_signals,
    _read_transcript,
)


def _make_event(
    seq: int,
    event_type: str,
    agent: str = "benchmark-agent",
    data: dict | None = None,
) -> dict:
    return {
        "seq": seq,
        "timestamp": "2026-06-24T00:00:00+00:00",
        "ticket_id": "PERF-TEST",
        "agent": agent,
        "event_type": event_type,
        "data": data or {},
    }


SAMPLE_EVENTS = [
    _make_event(1, "transition", "system", {"to": "triage_pending"}),
    _make_event(2, "agent_started", "triage-agent"),
    _make_event(3, "llm_request", "triage-agent", {"iteration": 0}),
    _make_event(
        4,
        "llm_response",
        "triage-agent",
        {"iteration": 0, "stop_reason": "end_turn", "text": "Done."},
    ),
    _make_event(5, "agent_finished", "triage-agent"),
    # Tool error
    _make_event(
        10,
        "tool_called",
        "resource-agent",
        {"tool": "check_available_resources", "input": {"provider": "aws"}},
    ),
    _make_event(
        11,
        "tool_result",
        "resource-agent",
        {
            "tool": "check_available_resources",
            "is_error": True,
            "content": "TypeError: '>=' not supported between str and int",
        },
    ),
    # Fail-then-succeed
    _make_event(
        12,
        "tool_called",
        "resource-agent",
        {"tool": "check_available_resources", "input": {"provider": "aws"}},
    ),
    _make_event(
        13,
        "tool_result",
        "resource-agent",
        {
            "tool": "check_available_resources",
            "is_error": False,
            "content": '{"provider": "aws", "available_count": 3}',
        },
    ),
    # Retry sequence (4 consecutive calls to same tool)
    _make_event(
        20,
        "tool_called",
        "benchmark-agent",
        {"tool": "read_skill", "input": {"name": "run-file-pitfalls"}},
    ),
    _make_event(
        21,
        "tool_result",
        "benchmark-agent",
        {"tool": "read_skill", "is_error": False, "content": "doc content"},
    ),
    _make_event(
        22,
        "tool_called",
        "benchmark-agent",
        {"tool": "read_skill", "input": {"name": "uperf-run-file"}},
    ),
    _make_event(
        23,
        "tool_result",
        "benchmark-agent",
        {"tool": "read_skill", "is_error": False, "content": "doc content"},
    ),
    _make_event(
        24,
        "tool_called",
        "benchmark-agent",
        {"tool": "read_skill", "input": {"name": "userenv-guide"}},
    ),
    _make_event(
        25,
        "tool_result",
        "benchmark-agent",
        {"tool": "read_skill", "is_error": False, "content": "doc content"},
    ),
    _make_event(
        26,
        "tool_called",
        "benchmark-agent",
        {"tool": "read_skill", "input": {"name": "fio-guide"}},
    ),
    _make_event(
        27,
        "tool_result",
        "benchmark-agent",
        {"tool": "read_skill", "is_error": False, "content": "doc content"},
    ),
    # Self-correction language
    _make_event(
        30,
        "llm_response",
        "benchmark-agent",
        {
            "iteration": 3,
            "stop_reason": "tool_use",
            "text": "That didn't work. Let me try a different approach.",
        },
    ),
    # HITL escalation
    _make_event(
        40,
        "transition",
        "benchmark-agent",
        {"to": "awaiting_customer_guidance", "comment": "Need user input"},
    ),
    # Max iterations
    _make_event(
        50,
        "agent_error",
        "benchmark-agent",
        {"reason": "max_iterations"},
    ),
]


class TestSignalExtraction:
    def test_tool_errors(self):
        signals = _extract_signals(SAMPLE_EVENTS)
        tool_errors = [s for s in signals if s["type"] == "tool_error"]
        assert len(tool_errors) == 1
        assert tool_errors[0]["tool"] == "check_available_resources"
        assert "TypeError" in tool_errors[0]["error"]
        assert tool_errors[0]["agent"] == "resource-agent"

    def test_retry_sequences(self):
        signals = _extract_signals(SAMPLE_EVENTS)
        retries = [s for s in signals if s["type"] == "retry_sequence"]
        assert len(retries) == 1
        assert retries[0]["tool"] == "read_skill"
        assert retries[0]["count"] == 4
        assert retries[0]["agent"] == "benchmark-agent"

    def test_fail_then_succeed(self):
        signals = _extract_signals(SAMPLE_EVENTS)
        fts = [s for s in signals if s["type"] == "fail_then_succeed"]
        assert len(fts) == 1
        assert fts[0]["tool"] == "check_available_resources"
        assert "TypeError" in fts[0]["error"]

    def test_max_iterations(self):
        signals = _extract_signals(SAMPLE_EVENTS)
        maxiter = [s for s in signals if s["type"] == "max_iterations"]
        assert len(maxiter) == 1
        assert maxiter[0]["agent"] == "benchmark-agent"

    def test_hitl_escalation(self):
        signals = _extract_signals(SAMPLE_EVENTS)
        hitl = [s for s in signals if s["type"] == "hitl_escalation"]
        assert len(hitl) == 1
        assert hitl[0]["comment"] == "Need user input"

    def test_self_correction(self):
        signals = _extract_signals(SAMPLE_EVENTS)
        sc = [s for s in signals if s["type"] == "self_correction"]
        assert len(sc) == 1
        assert "didn't work" in sc[0]["text_snippet"].lower()

    def test_context_included(self):
        signals = _extract_signals(SAMPLE_EVENTS)
        tool_errors = [s for s in signals if s["type"] == "tool_error"]
        assert len(tool_errors[0]["context"]) > 0
        seqs = [e["seq"] for e in tool_errors[0]["context"]]
        assert 11 in seqs

    def test_empty_transcript(self):
        signals = _extract_signals([])
        assert signals == []

    def test_clean_transcript(self):
        clean = [
            _make_event(1, "agent_started", "triage-agent"),
            _make_event(
                2,
                "llm_response",
                "triage-agent",
                {
                    "iteration": 0,
                    "stop_reason": "end_turn",
                    "text": "Analysis complete.",
                },
            ),
            _make_event(3, "agent_finished", "triage-agent"),
        ]
        signals = _extract_signals(clean)
        assert signals == []


class TestStats:
    def test_compute_stats(self):
        stats = _compute_stats(SAMPLE_EVENTS)
        assert stats["total_events"] == len(SAMPLE_EVENTS)
        assert "benchmark-agent" in stats["by_agent"]
        assert "resource-agent" in stats["by_agent"]

        res = stats["by_agent"]["resource-agent"]
        assert res["tool_errors"] == 1

    def test_empty_stats(self):
        stats = _compute_stats([])
        assert stats["total_events"] == 0
        assert stats["by_agent"] == {}


class TestTranscriptReader:
    def test_read_transcript(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "PERF-TEST.jsonl"
            with open(path, "w") as f:
                for evt in SAMPLE_EVENTS[:3]:
                    f.write(json.dumps(evt) + "\n")

            with patch(
                "agents.retrospective.server.DEFAULT_LOG_DIR",
                Path(tmpdir),
            ):
                events = _read_transcript("PERF-TEST")
                assert len(events) == 3

    def test_read_missing_transcript(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch(
                "agents.retrospective.server.DEFAULT_LOG_DIR",
                Path(tmpdir),
            ):
                events = _read_transcript("PERF-MISSING")
                assert events == []


class TestMCPToolHandler:
    def test_get_transcript_analysis_missing(self):
        from agents.retrospective.server import get_transcript_analysis

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch(
                "agents.retrospective.server.DEFAULT_LOG_DIR",
                Path(tmpdir),
            ):
                result = get_transcript_analysis("PERF-MISSING")
                assert "error" in result
                assert result["signals"] == []

    def test_get_transcript_analysis_with_data(self):
        from agents.retrospective.server import get_transcript_analysis

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "PERF-TEST.jsonl"
            with open(path, "w") as f:
                for evt in SAMPLE_EVENTS:
                    f.write(json.dumps(evt) + "\n")

            with patch(
                "agents.retrospective.server.DEFAULT_LOG_DIR",
                Path(tmpdir),
            ):
                result = get_transcript_analysis("PERF-TEST")
                assert result["ticket_id"] == "PERF-TEST"
                assert len(result["signals"]) > 0
                assert result["stats"]["total_events"] == len(SAMPLE_EVENTS)


class TestStateTransitions:
    def test_retrospective_pending_status_exists(self):
        from state_store.models import TicketStatus

        assert hasattr(TicketStatus, "RETROSPECTIVE_PENDING")
        assert TicketStatus.RETROSPECTIVE_PENDING.value == "retrospective_pending"

    def test_teardown_to_retrospective_transition(self):
        from state_store.models import TicketStatus, VALID_TRANSITIONS

        allowed = VALID_TRANSITIONS[TicketStatus.AWAITING_TEARDOWN]
        assert TicketStatus.RETROSPECTIVE_PENDING in allowed

    def test_retrospective_to_closed_transition(self):
        from state_store.models import TicketStatus, VALID_TRANSITIONS

        allowed = VALID_TRANSITIONS[TicketStatus.RETROSPECTIVE_PENDING]
        assert TicketStatus.CLOSED in allowed

    def test_dispatcher_maps_retrospective(self):
        from orchestrator.dispatcher import STATUS_AGENT_MAP

        assert "retrospective_pending" in STATUS_AGENT_MAP
        assert STATUS_AGENT_MAP["retrospective_pending"] == "retrospective"
