"""Tests for transition event emission.

Verifies that transition events are emitted by the agent's
EventBus (orchestrator-side), not by the state store.  This
ensures all events share a single sequence counter, preventing
seq collisions that drop events or cause the UI to miss
transitions.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.base import AgentBase
from providers.events import EventBus
from providers.llm.base import LLMProvider, LLMResponse, ToolDefinition


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


async def test_transition_emits_event(
    agent: _StubAgent,
    event_bus: EventBus,
) -> None:
    """_transition_ticket emits a transition event through the EventBus."""
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
    assert len(events) == 1
    evt = events[0]
    assert evt["event_type"] == "transition"
    assert evt["data"]["to"] == "awaiting_hardware"
    assert evt["data"]["comment"] == "triage complete"
    assert evt["data"]["ticket_id"] == "TICKET-1"


async def test_transition_events_share_seq_with_agent_events(
    agent: _StubAgent,
    event_bus: EventBus,
) -> None:
    """Transition events use the same seq counter as agent events."""
    # Emit an agent event first
    event_bus.emit("TICKET-1", "triage-agent", "agent_started", {})

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"status": "awaiting_hardware"}

    with patch.object(
        agent._client, "post", new_callable=AsyncMock, return_value=mock_response
    ):
        await agent._transition_ticket("TICKET-1", "awaiting_hardware")

    events = event_bus.get_events("TICKET-1")
    assert len(events) == 2
    # Seq numbers should be monotonically increasing from the same counter
    assert events[0]["seq"] == 1
    assert events[1]["seq"] == 2
    assert events[0]["event_type"] == "agent_started"
    assert events[1]["event_type"] == "transition"


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
