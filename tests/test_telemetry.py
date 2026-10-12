"""Tests for OpenTelemetry LLM instrumentation integration.

Tests the EventBus cumulative usage tracking, the span processor
bridge, and the ticket context correlation.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from providers.cost import estimate_cost, estimate_cumulative_cost
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
    assert d["cache_read_input_tokens"] == 0
    assert d["cache_creation_input_tokens"] == 0
    assert d["total_tokens"] == 0
    assert d["llm_calls"] == 0
    assert d["total_duration_ms"] == 0
    assert d["models_used"] == []
    assert d["estimated_cost_usd"] == 0


def test_cumulative_usage_single_record():
    """Single record updates all fields."""
    u = CumulativeUsage()
    u.record(
        input_tokens=100,
        output_tokens=50,
        duration_ms=500,
        model="claude-sonnet-4-6",
        cache_read_input_tokens=80,
        cache_creation_input_tokens=10,
    )
    d = u.to_dict()
    assert d["input_tokens"] == 100
    assert d["output_tokens"] == 50
    assert d["cache_read_input_tokens"] == 80
    assert d["cache_creation_input_tokens"] == 10
    assert d["total_tokens"] == 150
    assert d["llm_calls"] == 1
    assert d["total_duration_ms"] == 500
    assert d["models_used"] == ["claude-sonnet-4-6"]
    assert d["estimated_cost_usd"] == estimate_cost(
        "claude-sonnet-4-6",
        100,
        50,
        cache_read_input_tokens=80,
        cache_creation_input_tokens=10,
    )


def test_cumulative_usage_without_model_uses_fallback_pricing():
    """Unlabeled usage preserves estimate_cost's fallback model behavior."""
    u = CumulativeUsage()
    u.record(100, 50, 500)

    d = u.to_dict()
    assert d["models_used"] == []
    assert d["estimated_cost_usd"] == estimate_cost("", 100, 50)


def test_cumulative_usage_accumulates():
    """Multiple records accumulate correctly."""
    u = CumulativeUsage()
    u.record(100, 50, 500, "claude-sonnet-4-6", cache_read_input_tokens=60)
    u.record(200, 80, 700, "claude-sonnet-4-6", cache_read_input_tokens=150)
    u.record(150, 60, 400, "gpt-4o")
    d = u.to_dict()
    assert d["input_tokens"] == 450
    assert d["output_tokens"] == 190
    assert d["cache_read_input_tokens"] == 210
    assert d["cache_creation_input_tokens"] == 0
    assert d["total_tokens"] == 640
    assert d["llm_calls"] == 3
    assert d["total_duration_ms"] == 1600
    assert set(d["models_used"]) == {
        "claude-sonnet-4-6",
        "gpt-4o",
    }


def test_cumulative_cost_preserves_per_call_context_tiers():
    """Thresholds apply to each call, not the ticket's combined token total."""
    u = CumulativeUsage()
    u.record(150_000, 0, 0, "gemini-2.5-pro")
    u.record(150_000, 0, 0, "gemini-2.5-pro")

    d = u.to_dict()
    expected = 2 * estimate_cost("gemini-2.5-pro", 150_000, 0)
    aggregate_estimate = estimate_cost("gemini-2.5-pro", 300_000, 0)
    assert expected != aggregate_estimate
    assert d["estimated_cost_usd"] == expected
    assert estimate_cumulative_cost(d) == expected


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
def test_span_processor_on_end_is_noop(
    event_bus: EventBus,
):
    """Span processor on_end no longer emits to EventBus.

    Token accounting is now handled directly by AgentBase
    via LLMResponse.usage.
    """
    from providers.telemetry import (
        EventBusSpanProcessor,
    )

    processor = EventBusSpanProcessor(event_bus)

    span = MagicMock()
    span.attributes = {
        "gen_ai.request.model": "claude-sonnet-4-6",
        "gen_ai.usage.prompt_tokens": 150,
        "gen_ai.usage.completion_tokens": 75,
        "agentic_perf.ticket_id": "PERF-SPAN01",
    }
    span.start_time = 1000000000
    span.end_time = 3000000000

    processor.on_end(span)

    d = event_bus.get_cumulative_usage("PERF-SPAN01")
    assert d["llm_calls"] == 0


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
def test_span_processor_does_not_record_agent_usage(
    event_bus: EventBus,
):
    """Span processor no longer records per-agent usage."""
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
    assert agents == {}


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
    assert g["estimated_cost_usd"] == sum(
        (
            estimate_cost("claude-sonnet-4-6", 100, 50),
            estimate_cost("gpt-4o", 200, 80),
        )
    )
