"""Tests for OpenTelemetry LLM instrumentation integration.

Tests the EventBus cumulative usage tracking, the span processor
bridge, and the ticket context correlation.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from providers.events import CumulativeUsage, EventBus

# Skip tests that need OpenTelemetry when it's not installed
# (telemetry deps are optional: pip install -e ".[telemetry]")
try:
    import opentelemetry.sdk  # noqa: F401

    has_otlp = True
except ImportError:
    has_otlp = False

requires_otlp = pytest.mark.skipif(
    not has_otlp,
    reason="opentelemetry-sdk not installed",
)

# --- CumulativeUsage ---


def test_cumulative_usage_initial():
    """New CumulativeUsage starts at zero."""
    u = CumulativeUsage()
    d = u.to_dict()
    assert d["input_tokens"] == 0
    assert d["output_tokens"] == 0
    assert d["total_tokens"] == 0
    assert d["llm_calls"] == 0
    assert d["total_duration_ms"] == 0
    assert d["models_used"] == []


def test_cumulative_usage_single_record():
    """Single record updates all fields."""
    u = CumulativeUsage()
    u.record(
        input_tokens=100,
        output_tokens=50,
        duration_ms=500,
        model="claude-sonnet-4-6",
    )
    d = u.to_dict()
    assert d["input_tokens"] == 100
    assert d["output_tokens"] == 50
    assert d["total_tokens"] == 150
    assert d["llm_calls"] == 1
    assert d["total_duration_ms"] == 500
    assert d["models_used"] == ["claude-sonnet-4-6"]


def test_cumulative_usage_accumulates():
    """Multiple records accumulate correctly."""
    u = CumulativeUsage()
    u.record(100, 50, 500, "claude-sonnet-4-6")
    u.record(200, 80, 700, "claude-sonnet-4-6")
    u.record(150, 60, 400, "gpt-4o")
    d = u.to_dict()
    assert d["input_tokens"] == 450
    assert d["output_tokens"] == 190
    assert d["total_tokens"] == 640
    assert d["llm_calls"] == 3
    assert d["total_duration_ms"] == 1600
    assert set(d["models_used"]) == {
        "claude-sonnet-4-6",
        "gpt-4o",
    }


# --- EventBus usage tracking ---


@pytest.fixture
def event_bus(tmp_path: Path) -> EventBus:
    return EventBus(log_dir=tmp_path / "logs")


def test_eventbus_no_usage(event_bus: EventBus):
    """No recorded usage returns zeros."""
    d = event_bus.get_cumulative_usage("PERF-TEST")
    assert d["total_tokens"] == 0
    assert d["llm_calls"] == 0


def test_eventbus_record_usage(event_bus: EventBus):
    """Recording usage accumulates per ticket."""
    event_bus.record_llm_usage("PERF-TEST", 100, 50, 500, "claude-sonnet-4-6")
    event_bus.record_llm_usage("PERF-TEST", 200, 80, 700, "claude-sonnet-4-6")

    d = event_bus.get_cumulative_usage("PERF-TEST")
    assert d["input_tokens"] == 300
    assert d["output_tokens"] == 130
    assert d["total_tokens"] == 430
    assert d["llm_calls"] == 2
    assert d["total_duration_ms"] == 1200


def test_eventbus_usage_per_ticket(event_bus: EventBus):
    """Usage is tracked independently per ticket."""
    event_bus.record_llm_usage("PERF-A", 100, 50, 500)
    event_bus.record_llm_usage("PERF-B", 200, 80, 700)

    a = event_bus.get_cumulative_usage("PERF-A")
    b = event_bus.get_cumulative_usage("PERF-B")
    assert a["input_tokens"] == 100
    assert b["input_tokens"] == 200


# --- Span processor ---


@requires_otlp
def test_span_processor_extracts_usage(
    event_bus: EventBus,
):
    """Span processor extracts token usage from spans."""
    from providers.telemetry import (
        EventBusSpanProcessor,
    )

    processor = EventBusSpanProcessor(event_bus)

    # Create a mock span with GenAI attributes
    # using the legacy prompt_tokens/completion_tokens names
    span = MagicMock()
    span.attributes = {
        "gen_ai.request.model": "claude-sonnet-4-6",
        "gen_ai.usage.prompt_tokens": 150,
        "gen_ai.usage.completion_tokens": 75,
        "agentic_perf.ticket_id": "PERF-SPAN01",
    }
    span.start_time = 1000000000  # 1s in ns
    span.end_time = 3000000000  # 3s in ns

    processor.on_end(span)

    d = event_bus.get_cumulative_usage("PERF-SPAN01")
    assert d["input_tokens"] == 150
    assert d["output_tokens"] == 75
    assert d["total_tokens"] == 225
    assert d["llm_calls"] == 1
    assert d["total_duration_ms"] == 2000
    assert "claude-sonnet-4-6" in d["models_used"]


@requires_otlp
def test_span_processor_anthropic_attribute_names(
    event_bus: EventBus,
):
    """Span processor handles the input_tokens/output_tokens
    names that the Anthropic instrumentor emits (different from
    the semconv prompt_tokens/completion_tokens).
    """
    from providers.telemetry import (
        EventBusSpanProcessor,
    )

    processor = EventBusSpanProcessor(event_bus)

    span = MagicMock()
    span.attributes = {
        "gen_ai.request.model": "claude-sonnet-4-6",
        "gen_ai.usage.input_tokens": 200,
        "gen_ai.usage.output_tokens": 100,
        "agentic_perf.ticket_id": "PERF-SPAN02",
        "agentic_perf.agent_name": "triage-agent",
    }
    span.start_time = 1000000000
    span.end_time = 2500000000

    processor.on_end(span)

    d = event_bus.get_cumulative_usage("PERF-SPAN02")
    assert d["input_tokens"] == 200
    assert d["output_tokens"] == 100
    assert d["total_tokens"] == 300
    assert d["llm_calls"] == 1
    assert d["total_duration_ms"] == 1500


@requires_otlp
def test_span_processor_ignores_non_llm_spans(
    event_bus: EventBus,
):
    """Non-LLM spans are ignored."""
    from providers.telemetry import (
        EventBusSpanProcessor,
    )

    processor = EventBusSpanProcessor(event_bus)

    span = MagicMock()
    span.attributes = {
        "http.method": "GET",
        "http.url": "https://example.com",
    }

    processor.on_end(span)

    d = event_bus.get_cumulative_usage("PERF-ANY")
    assert d["llm_calls"] == 0


@requires_otlp
def test_span_processor_ignores_missing_ticket(
    event_bus: EventBus,
):
    """LLM spans without ticket context are ignored."""
    from providers.telemetry import (
        EventBusSpanProcessor,
    )

    processor = EventBusSpanProcessor(event_bus)

    span = MagicMock()
    span.attributes = {
        "gen_ai.request.model": "claude-sonnet-4-6",
        "gen_ai.usage.prompt_tokens": 100,
        "gen_ai.usage.completion_tokens": 50,
        # No agentic_perf.ticket_id
    }

    processor.on_end(span)

    # Nothing recorded — no ticket to attribute to
    d = event_bus.get_cumulative_usage("PERF-ANY")
    assert d["llm_calls"] == 0


# --- Ticket context ---


@requires_otlp
def test_ticket_context_roundtrip():
    """Set and get ticket ID from OpenTelemetry context."""
    from providers.telemetry import (
        get_ticket_from_context,
        set_ticket_context,
    )

    ctx = set_ticket_context("PERF-CTX001")
    ticket = get_ticket_from_context(ctx)
    assert ticket == "PERF-CTX001"


# --- Per-agent tracking ---


def test_eventbus_per_agent_usage(event_bus: EventBus):
    """Usage is tracked per agent within a ticket."""
    event_bus.record_llm_usage(
        "PERF-A",
        100,
        50,
        500,
        model="claude-sonnet-4-6",
        agent_name="triage-agent",
    )
    event_bus.record_llm_usage(
        "PERF-A",
        200,
        80,
        700,
        model="claude-sonnet-4-6",
        agent_name="benchmark-agent",
    )
    event_bus.record_llm_usage(
        "PERF-A",
        150,
        60,
        400,
        model="claude-sonnet-4-6",
        agent_name="triage-agent",
    )

    # Ticket total
    total = event_bus.get_cumulative_usage("PERF-A")
    assert total["input_tokens"] == 450
    assert total["llm_calls"] == 3

    # Per-agent breakdown
    agents = event_bus.get_agent_usage("PERF-A")
    assert len(agents) == 2
    assert agents["triage-agent"]["input_tokens"] == 250
    assert agents["triage-agent"]["llm_calls"] == 2
    assert agents["benchmark-agent"]["input_tokens"] == 200
    assert agents["benchmark-agent"]["llm_calls"] == 1


def test_eventbus_agent_usage_empty(event_bus: EventBus):
    """No agent usage returns empty dict."""
    agents = event_bus.get_agent_usage("PERF-NONE")
    assert agents == {}


@requires_otlp
def test_span_processor_captures_agent(
    event_bus: EventBus,
):
    """Span processor passes agent name to EventBus."""
    from providers.telemetry import (
        EventBusSpanProcessor,
    )

    processor = EventBusSpanProcessor(event_bus)

    span = MagicMock()
    span.attributes = {
        "gen_ai.request.model": "claude-sonnet-4-6",
        "gen_ai.usage.prompt_tokens": 100,
        "gen_ai.usage.completion_tokens": 50,
        "agentic_perf.ticket_id": "PERF-AGENT1",
        "agentic_perf.agent_name": "review-agent",
    }
    span.start_time = 1000000000
    span.end_time = 2000000000

    processor.on_end(span)

    agents = event_bus.get_agent_usage("PERF-AGENT1")
    assert "review-agent" in agents
    assert agents["review-agent"]["input_tokens"] == 100


# --- Span processor event emission ---


@requires_otlp
def test_span_processor_emits_llm_usage_event(
    event_bus: EventBus,
):
    """Span processor emits a llm_usage event to the
    EventBus JSONL log so the state store process can
    compute usage from persisted events.
    """
    from providers.telemetry import (
        EventBusSpanProcessor,
    )

    processor = EventBusSpanProcessor(event_bus)

    span = MagicMock()
    span.attributes = {
        "gen_ai.request.model": "claude-sonnet-4-6",
        "gen_ai.usage.input_tokens": 300,
        "gen_ai.usage.output_tokens": 120,
        "agentic_perf.ticket_id": "PERF-EVT01",
        "agentic_perf.agent_name": "benchmark-agent",
    }
    span.start_time = 1000000000
    span.end_time = 3000000000

    processor.on_end(span)

    events = event_bus.get_events("PERF-EVT01")
    usage_events = [e for e in events if e["event_type"] == "llm_usage"]
    assert len(usage_events) == 1
    evt = usage_events[0]
    assert evt["agent"] == "benchmark-agent"
    assert evt["data"]["input_tokens"] == 300
    assert evt["data"]["output_tokens"] == 120
    assert evt["data"]["duration_ms"] == 2000
    assert evt["data"]["model"] == "claude-sonnet-4-6"


# --- Global usage ---


def test_eventbus_global_usage(event_bus: EventBus):
    """Global usage sums across all tickets."""
    event_bus.record_llm_usage(
        "PERF-A",
        100,
        50,
        500,
        model="claude-sonnet-4-6",
        agent_name="triage-agent",
    )
    event_bus.record_llm_usage(
        "PERF-B",
        200,
        80,
        700,
        model="gpt-4o",
    )

    g = event_bus.get_global_usage()
    assert g["input_tokens"] == 300
    assert g["output_tokens"] == 130
    assert g["llm_calls"] == 2
    assert set(g["models_used"]) == {
        "claude-sonnet-4-6",
        "gpt-4o",
    }
