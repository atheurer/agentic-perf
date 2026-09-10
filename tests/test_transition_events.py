"""Tests for transition event emission.

Verifies that transition events are emitted exactly once per
status change.  Since 81fce78, the state store emits transition
events directly in ``transition_ticket`` — callers (agents,
orchestrator) no longer emit their own copies.
"""

from __future__ import annotations

from pathlib import Path
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
    tmp_path: Path,
) -> None:
    """One store transition → exactly one transition event in the log."""
    store = TicketStore(event_bus=event_bus, persist_dir=tmp_path)
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


async def test_terminal_events_via_accessor(
    event_bus: EventBus,
    tmp_path: Path,
) -> None:
    """get_terminal_events returns terminal events regardless of window."""
    tid = "PERF-TERM"
    for i in range(250):
        event_bus.emit(tid, "test-agent", "tool_called", {"tool": f"t{i}"})
    event_bus.emit(tid, "test-agent", "agent_finished", {})

    terminal = event_bus.get_terminal_events(tid)
    assert len(terminal) == 1
    assert terminal[0]["event_type"] == "agent_finished"

    events = event_bus.get_events(tid, since=0, limit=200)
    assert len(events) == 200
    assert all(e["event_type"] == "tool_called" for e in events)


async def test_contiguous_window_no_cursor_skip(
    event_bus: EventBus,
) -> None:
    """get_events returns a contiguous window — no seq gaps from terminals."""
    tid = "PERF-CURSOR"
    for i in range(250):
        event_bus.emit(tid, "test-agent", "tool_called", {"tool": f"t{i}"})
    event_bus.emit(tid, "test-agent", "agent_finished", {})

    all_seqs = []
    cursor = 0
    while True:
        page = event_bus.get_events(tid, since=cursor, limit=100)
        if not page:
            break
        for e in page:
            all_seqs.append(e["seq"])
        cursor = page[-1]["seq"]

    assert len(all_seqs) == 251
    assert all_seqs == list(range(1, 252))


async def test_terminal_events_file_backed_restart(
    tmp_path: Path,
) -> None:
    """After restart, get_terminal_events finds events from the JSONL file."""
    log_dir = tmp_path / "logs"
    tid = "PERF-RESTART"

    bus1 = EventBus(log_dir=log_dir)
    for i in range(250):
        bus1.emit(tid, "test-agent", "tool_called", {"tool": f"t{i}"})
    bus1.emit(tid, "test-agent", "agent_finished", {"result": "ok"})

    bus2 = EventBus(log_dir=log_dir)
    terminal = bus2.get_terminal_events(tid)
    assert len(terminal) == 1
    assert terminal[0]["event_type"] == "agent_finished"
    assert terminal[0]["data"]["result"] == "ok"


async def test_terminal_events_not_duplicated_within_limit(
    event_bus: EventBus,
) -> None:
    """When events fit within limit, terminal events are not duplicated."""
    tid = "PERF-NODUP"
    for i in range(10):
        event_bus.emit(tid, "test-agent", "tool_called", {"tool": f"t{i}"})
    event_bus.emit(tid, "test-agent", "agent_finished", {})

    events = event_bus.get_events(tid, since=0, limit=200)
    finished = [e for e in events if e.get("event_type") == "agent_finished"]
    assert len(finished) == 1

    terminal = event_bus.get_terminal_events(tid)
    assert len(terminal) == 1


async def test_terminal_events_mixed_disk_and_memory(
    tmp_path: Path,
) -> None:
    """Terminal events from both file (pre-restart) and memory merge correctly."""
    log_dir = tmp_path / "logs"
    tid = "PERF-MIXED"

    bus1 = EventBus(log_dir=log_dir)
    for i in range(100):
        bus1.emit(tid, "test-agent", "tool_called", {"tool": f"t{i}"})
    bus1.emit(tid, "test-agent", "agent_error", {"error": "retryable"})

    bus2 = EventBus(log_dir=log_dir)
    for i in range(50):
        bus2.emit(tid, "test-agent", "tool_called", {"tool": f"r{i}"})
    bus2.emit(tid, "test-agent", "agent_finished", {"result": "done"})

    terminal = bus2.get_terminal_events(tid)
    assert len(terminal) == 2
    types = [e["event_type"] for e in terminal]
    assert types == ["agent_error", "agent_finished"]
    assert terminal[0]["seq"] < terminal[1]["seq"]


async def test_terminal_cache_invalidated_on_new_events(
    tmp_path: Path,
) -> None:
    """Cache refreshes when new terminal events are appended after first read."""
    log_dir = tmp_path / "logs"
    tid = "PERF-CACHE"

    bus = EventBus(log_dir=log_dir)
    bus.emit(tid, "test-agent", "tool_called", {"tool": "t0"})
    bus.emit(tid, "test-agent", "agent_error", {"error": "fail1"})

    terminal = bus.get_terminal_events(tid)
    assert len(terminal) == 1
    assert terminal[0]["event_type"] == "agent_error"

    bus.emit(tid, "test-agent", "tool_called", {"tool": "t1"})
    bus.emit(tid, "test-agent", "agent_finished", {"result": "ok"})

    terminal = bus.get_terminal_events(tid)
    assert len(terminal) == 2
    assert terminal[1]["event_type"] == "agent_finished"


async def test_terminal_cache_cross_process_invalidation(
    tmp_path: Path,
) -> None:
    """Separate EventBus (simulating state store) sees new terminals."""
    log_dir = tmp_path / "logs"
    tid = "PERF-XPROC"

    bus1 = EventBus(log_dir=log_dir)
    bus1.emit(tid, "test-agent", "agent_error", {"error": "fail1"})

    bus2 = EventBus(log_dir=log_dir)
    terminal = bus2.get_terminal_events(tid)
    assert len(terminal) == 1

    bus1.emit(tid, "test-agent", "agent_finished", {"result": "done"})

    terminal = bus2.get_terminal_events(tid)
    assert len(terminal) == 2
    types = [e["event_type"] for e in terminal]
    assert types == ["agent_error", "agent_finished"]


async def test_store_no_double_emit_on_consecutive_transitions(
    event_bus: EventBus,
    tmp_path: Path,
) -> None:
    """Three consecutive transitions produce exactly three transition events."""
    store = TicketStore(event_bus=event_bus, persist_dir=tmp_path)
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
