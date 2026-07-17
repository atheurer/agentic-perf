"""Tests for the /usage/summary API endpoint.

Covers: empty state, single ticket, multi-ticket aggregation,
and tickets with no LLM usage.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from providers.events import EventBus
from state_store.api.events import _compute_ticket_usage, get_usage, get_usage_summary
from state_store.models import CreateTicketRequest
from state_store.store import TicketStore


@pytest.fixture
def event_bus(tmp_path: Path) -> EventBus:
    return EventBus(log_dir=tmp_path / "logs")


@pytest.fixture
def store(tmp_path: Path) -> TicketStore:
    return TicketStore(persist_dir=tmp_path / "tickets")


def _emit_usage(
    event_bus: EventBus,
    ticket_id: str,
    input_tokens: int,
    output_tokens: int,
    duration_ms: int,
    model: str = "claude-haiku-4-5",
    agent: str = "system",
    cache_read_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
) -> None:
    """Emit an llm_usage event, matching what the OTLP span processor does."""
    event_bus.emit(
        ticket_id,
        agent,
        "llm_usage",
        {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read_input_tokens,
            "cache_creation_input_tokens": cache_creation_input_tokens,
            "duration_ms": duration_ms,
            "model": model,
        },
    )


def _make_request(store: TicketStore, event_bus: EventBus) -> MagicMock:
    request = MagicMock()
    request.app.state.store = store
    request.app.state.event_bus = event_bus
    return request


class TestComputeTicketUsage:
    def test_no_events(self, event_bus: EventBus):
        result = _compute_ticket_usage(event_bus, "PERF-EMPTY")
        assert result["total_tokens"] == 0
        assert result["llm_calls"] == 0
        assert result["estimated_cost_usd"] == 0.0

    def test_single_usage_event(self, event_bus: EventBus):
        _emit_usage(event_bus, "PERF-A", 100, 50, 500)
        result = _compute_ticket_usage(event_bus, "PERF-A")
        assert result["total_tokens"] == 150
        assert result["input_tokens"] == 100
        assert result["output_tokens"] == 50
        assert result["llm_calls"] == 1
        assert result["estimated_cost_usd"] > 0

    def test_multiple_usage_events(self, event_bus: EventBus):
        _emit_usage(event_bus, "PERF-A", 100, 50, 500)
        _emit_usage(event_bus, "PERF-A", 200, 80, 700)
        result = _compute_ticket_usage(event_bus, "PERF-A")
        assert result["total_tokens"] == 430
        assert result["llm_calls"] == 2


class TestGetUsageSummary:
    def test_no_tickets(self, store: TicketStore, event_bus: EventBus):
        request = _make_request(store, event_bus)
        result = get_usage_summary(request)
        assert result["global"]["total_tokens"] == 0
        assert result["global"]["llm_calls"] == 0
        assert result["global"]["estimated_cost_usd"] == 0.0
        assert result["by_ticket"] == {}

    def test_no_event_bus(self, store: TicketStore):
        request = MagicMock()
        request.app.state.store = store
        request.app.state.event_bus = None
        result = get_usage_summary(request)
        assert result["global"]["total_tokens"] == 0
        assert result["by_ticket"] == {}

    def test_tickets_with_no_usage(self, store: TicketStore, event_bus: EventBus):
        store.create_ticket(CreateTicketRequest(summary="test", description="desc"))
        request = _make_request(store, event_bus)
        result = get_usage_summary(request)
        assert result["global"]["total_tokens"] == 0
        assert result["by_ticket"] == {}

    def test_single_ticket_with_usage(self, store: TicketStore, event_bus: EventBus):
        ticket = store.create_ticket(
            CreateTicketRequest(summary="test", description="desc")
        )
        _emit_usage(event_bus, ticket.id, 100, 50, 500)
        request = _make_request(store, event_bus)
        result = get_usage_summary(request)
        assert result["global"]["total_tokens"] == 150
        assert result["global"]["llm_calls"] == 1
        assert result["global"]["estimated_cost_usd"] > 0
        assert ticket.id in result["by_ticket"]
        assert result["by_ticket"][ticket.id]["total_tokens"] == 150

    def test_multi_ticket_aggregation(self, store: TicketStore, event_bus: EventBus):
        t1 = store.create_ticket(CreateTicketRequest(summary="first", description="d"))
        t2 = store.create_ticket(CreateTicketRequest(summary="second", description="d"))
        _emit_usage(event_bus, t1.id, 100, 50, 500)
        _emit_usage(event_bus, t2.id, 200, 80, 700)
        request = _make_request(store, event_bus)
        result = get_usage_summary(request)
        assert result["global"]["total_tokens"] == 430
        assert result["global"]["llm_calls"] == 2
        assert len(result["by_ticket"]) == 2
        assert result["by_ticket"][t1.id]["total_tokens"] == 150
        assert result["by_ticket"][t2.id]["total_tokens"] == 280

    def test_cache_tokens_in_summary(self, store: TicketStore, event_bus: EventBus):
        ticket = store.create_ticket(
            CreateTicketRequest(summary="test", description="desc")
        )
        _emit_usage(
            event_bus,
            ticket.id,
            1000,
            500,
            500,
            cache_read_input_tokens=800,
            cache_creation_input_tokens=50,
        )
        request = _make_request(store, event_bus)
        result = get_usage_summary(request)
        ticket_usage = result["by_ticket"][ticket.id]
        assert ticket_usage["cache_read_input_tokens"] == 800
        assert ticket_usage["cache_creation_input_tokens"] == 50
        # Cost should be lower than if all 1000 tokens were uncached
        no_cache_cost = 1000 * 0.000003 + 500 * 0.000015
        assert ticket_usage["estimated_cost_usd"] < no_cache_cost

    def test_mixed_tickets_with_and_without_usage(
        self, store: TicketStore, event_bus: EventBus
    ):
        t1 = store.create_ticket(
            CreateTicketRequest(summary="has usage", description="d")
        )
        store.create_ticket(CreateTicketRequest(summary="no usage", description="d"))
        _emit_usage(event_bus, t1.id, 100, 50, 500)
        request = _make_request(store, event_bus)
        result = get_usage_summary(request)
        assert len(result["by_ticket"]) == 1
        assert t1.id in result["by_ticket"]


class TestMultiModelCost:
    """Regression tests for #327: total cost must equal sum of per-agent costs.

    When agents use different models (e.g., haiku for triage, sonnet for
    provisioning), the total cost was computed using only the first model
    alphabetically, making it lower than the sum of per-agent costs.
    """

    def test_total_equals_agent_sum(self, event_bus: EventBus):
        """Total cost should be the sum of per-agent costs, not a
        re-estimate using a single model."""
        tid = "PERF-MULTI"
        # Triage uses the cheaper haiku model
        _emit_usage(
            event_bus,
            tid,
            1000,
            200,
            500,
            model="claude-haiku-4-5",
            agent="triage-agent",
        )
        # Provisioning uses the more expensive sonnet model
        _emit_usage(
            event_bus,
            tid,
            5000,
            800,
            2000,
            model="claude-sonnet-4-6",
            agent="provisioning-agent",
        )

        request = MagicMock()
        request.app.state.event_bus = event_bus
        result = get_usage(tid, request)

        agent_cost_sum = sum(
            a["estimated_cost_usd"] for a in result["by_agent"].values()
        )
        # Total must match the sum of per-agent costs
        assert result["estimated_cost_usd"] == pytest.approx(
            agent_cost_sum,
            abs=1e-6,
        ), (
            f"Total ${result['estimated_cost_usd']:.6f} != "
            f"agent sum ${agent_cost_sum:.6f}"
        )
        # Sanity: sonnet agent should cost more than haiku agent
        agents = result["by_agent"]
        assert (
            agents["provisioning-agent"]["estimated_cost_usd"]
            > agents["triage-agent"]["estimated_cost_usd"]
        )

    def test_total_ge_max_agent(
        self,
        event_bus: EventBus,
    ):
        """Total cost must be >= the most expensive single agent."""
        tid = "PERF-GE"
        _emit_usage(
            event_bus,
            tid,
            500,
            100,
            300,
            model="claude-haiku-4-5",
            agent="triage-agent",
        )
        _emit_usage(
            event_bus,
            tid,
            8000,
            2000,
            5000,
            model="claude-sonnet-4-6",
            agent="provisioning-agent",
        )

        request = MagicMock()
        request.app.state.event_bus = event_bus
        result = get_usage(tid, request)

        max_agent_cost = max(
            a["estimated_cost_usd"] for a in result["by_agent"].values()
        )
        assert result["estimated_cost_usd"] >= max_agent_cost

    def test_single_model_unchanged(self, event_bus: EventBus):
        """When all agents use the same model, per-event and aggregate
        cost estimation should produce the same result."""
        tid = "PERF-SINGLE"
        _emit_usage(
            event_bus,
            tid,
            1000,
            200,
            500,
            model="claude-sonnet-4-6",
            agent="triage-agent",
        )
        _emit_usage(
            event_bus,
            tid,
            2000,
            400,
            800,
            model="claude-sonnet-4-6",
            agent="benchmark-agent",
        )

        request = MagicMock()
        request.app.state.event_bus = event_bus
        result = get_usage(tid, request)

        agent_cost_sum = sum(
            a["estimated_cost_usd"] for a in result["by_agent"].values()
        )
        assert result["estimated_cost_usd"] == pytest.approx(
            agent_cost_sum,
            abs=1e-6,
        )

    def test_compute_ticket_usage_multi_model(
        self,
        event_bus: EventBus,
    ):
        """_compute_ticket_usage (used by /usage/summary) should also
        compute per-event costs correctly with mixed models."""
        tid = "PERF-SUMMARY"
        _emit_usage(
            event_bus,
            tid,
            1000,
            200,
            500,
            model="claude-haiku-4-5",
            agent="triage-agent",
        )
        _emit_usage(
            event_bus,
            tid,
            5000,
            800,
            2000,
            model="claude-sonnet-4-6",
            agent="provisioning-agent",
        )

        result = _compute_ticket_usage(event_bus, tid)

        # Should match the per-ticket detail endpoint
        request = MagicMock()
        request.app.state.event_bus = event_bus
        detail = get_usage(tid, request)

        assert result["estimated_cost_usd"] == pytest.approx(
            detail["estimated_cost_usd"],
            abs=1e-6,
        )
