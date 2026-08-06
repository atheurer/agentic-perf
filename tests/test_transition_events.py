"""Tests for transition event emission.

Verifies that transition events are emitted exactly once per
status change.  Since 81fce78, the state store emits transition
events directly in ``transition_ticket`` — callers (agents,
orchestrator) no longer emit their own copies.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.base import AgentBase
from providers.events import EventBus
from providers.llm.base import LLMProvider, LLMResponse, ToolDefinition
from state_store.models import CreateTicketRequest, TicketStatus, TransitionRequest
from state_store.store import TicketStore


class _StubAgent(AgentBase):
    """Minimal agent for testing transition emission."""

    def _system_prompt(self) -> str:
        return "test"

    def _build_messages(
        self,
        ticket: dict[str, Any],
    ) -> list[dict[str, Any]]:
        return [{"role": "user", "content": "test"}]

    async def _handle_completion(
        self,
        ticket_id: str,
        response: LLMResponse,
    ) -> None:
        pass


class _MockLLM(LLMProvider):
    async def complete(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        return LLMResponse(text="done", stop_reason="end_turn")


@pytest.fixture
def event_bus(tmp_path: Any) -> EventBus:
    return EventBus(log_dir=tmp_path / "logs")


@pytest.fixture
def agent(event_bus: EventBus) -> _StubAgent:
    return _StubAgent(
        agent_name="test-agent",
        llm_provider=_MockLLM(),
        state_store_url="http://localhost:9999",
        event_bus=event_bus,
    )


async def test_agent_transition_does_not_emit(
    agent: _StubAgent,
    event_bus: EventBus,
) -> None:
    """Agent _transition_ticket no longer emits — the store handles it."""
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"status": "awaiting_hardware"}

    with patch.object(
        agent._client, "post", new_callable=AsyncMock, return_value=mock_response
    ):
        await agent._transition_ticket(
            "TICKET-1",
            "awaiting_hardware",
            comment="triage complete",
        )

    events = event_bus.get_events("TICKET-1")
    assert len(events) == 0


async def test_store_transition_emits_exactly_one_event(
    event_bus: EventBus,
) -> None:
    """One store transition → exactly one transition event in the log."""
    store = TicketStore(event_bus=event_bus)
    ticket = store.create_ticket(
        CreateTicketRequest(summary="Test ticket", description="test"),
    )
    tid = ticket.id

    store.transition_ticket(
        tid,
        TransitionRequest(status=TicketStatus.TRIAGE_PENDING),
    )
    store.transition_ticket(
        tid,
        TransitionRequest(
            status=TicketStatus.AWAITING_HARDWARE,
            comment="triage done",
        ),
    )

    events = event_bus.get_events(tid)
    transition_events = [e for e in events if e.get("event_type") == "status_change"]
    assert len(transition_events) == 2
    evt = transition_events[1]
    assert evt["data"]["to"] == "awaiting_hardware"
    assert evt["data"]["from"] == "triage_pending"
    assert evt["data"]["comment"] == "triage done"

    # status_trail on the ticket is the authoritative breadcrumb source
    ticket = store.get_ticket(tid)
    assert ticket.status_trail == ["new", "triage_pending", "awaiting_hardware"]


async def test_no_event_without_event_bus() -> None:
    """_transition_ticket works without an EventBus (no crash)."""
    agent = _StubAgent(
        agent_name="test-agent",
        llm_provider=_MockLLM(),
        state_store_url="http://localhost:9999",
        event_bus=None,
    )

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"status": "awaiting_hardware"}

    with patch.object(
        agent._client, "post", new_callable=AsyncMock, return_value=mock_response
    ):
        result = await agent._transition_ticket("TICKET-1", "awaiting_hardware")

    assert result["status"] == "awaiting_hardware"


async def test_store_no_double_emit_on_consecutive_transitions(
    event_bus: EventBus,
) -> None:
    """Three consecutive transitions produce exactly three transition events."""
    store = TicketStore(event_bus=event_bus)
    ticket = store.create_ticket(
        CreateTicketRequest(summary="Test ticket", description="test"),
    )
    tid = ticket.id

    store.transition_ticket(
        tid,
        TransitionRequest(status=TicketStatus.TRIAGE_PENDING),
    )
    store.transition_ticket(
        tid,
        TransitionRequest(status=TicketStatus.AWAITING_HARDWARE),
    )
    store.transition_ticket(
        tid,
        TransitionRequest(status=TicketStatus.AWAITING_PROVISION),
    )

    events = event_bus.get_events(tid)
    transition_events = [e for e in events if e.get("event_type") == "status_change"]
    assert len(transition_events) == 3
    assert transition_events[0]["data"]["to"] == "triage_pending"
    assert transition_events[1]["data"]["to"] == "awaiting_hardware"
    assert transition_events[2]["data"]["to"] == "awaiting_provision"
