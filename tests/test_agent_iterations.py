"""Tests for configurable agent iteration budget.

Verifies that max_iterations can be set per agent, that 0 means
unlimited, and that the default matches DEFAULT_MAX_ITERATIONS.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from agents.base import AgentBase
from providers.events import EventBus
from providers.llm.base import LLMProvider, LLMResponse, ToolDefinition

# --- Minimal concrete agent for testing ---


class _StubAgent(AgentBase):
    """Minimal agent that counts iterations."""

    def _system_prompt(self, ticket: dict[str, Any]) -> str:
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


class _CountingLLM(LLMProvider):
    """LLM that always returns tool_use to keep the loop going."""

    def __init__(self) -> None:
        self.call_count = 0

    async def complete(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        self.call_count += 1
        return LLMResponse(
            text=f"call {self.call_count}",
            tool_calls=[],
            stop_reason="end_turn",
            raw_content=[],
        )


class _InfiniteToolLLM(LLMProvider):
    """LLM that always calls a tool, never finishes."""

    def __init__(self) -> None:
        self.call_count = 0

    async def complete(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        from providers.llm.base import ToolCall

        self.call_count += 1
        return LLMResponse(
            text=None,
            tool_calls=[
                ToolCall(
                    id=f"tc_{self.call_count}",
                    name="some_tool",
                    input={},
                ),
            ],
            stop_reason="tool_use",
            raw_content=[
                {
                    "type": "tool_use",
                    "id": f"tc_{self.call_count}",
                    "name": "some_tool",
                    "input": {},
                },
            ],
        )


# --- Tests ---


def test_default_max_iterations():
    """Default matches the class constant."""
    llm = _CountingLLM()
    agent = _StubAgent(
        agent_name="test",
        llm_provider=llm,
        state_store_url="http://localhost:8090",
    )
    assert agent.max_iterations == AgentBase.DEFAULT_MAX_ITERATIONS
    assert agent.max_iterations == 20


def test_custom_max_iterations():
    """Agent accepts a custom iteration budget."""
    llm = _CountingLLM()
    agent = _StubAgent(
        agent_name="test",
        llm_provider=llm,
        state_store_url="http://localhost:8090",
        max_iterations=5,
    )
    assert agent.max_iterations == 5


def test_zero_means_unlimited():
    """max_iterations=0 means no iteration limit."""
    llm = _CountingLLM()
    agent = _StubAgent(
        agent_name="test",
        llm_provider=llm,
        state_store_url="http://localhost:8090",
        max_iterations=0,
    )
    assert agent.max_iterations == 0


@pytest.mark.asyncio
async def test_iteration_limit_respected(tmp_path):
    """Agent stops after max_iterations and emits error."""
    llm = _InfiniteToolLLM()
    event_bus = EventBus(log_dir=tmp_path / "logs")
    agent = _StubAgent(
        agent_name="test-agent",
        llm_provider=llm,
        state_store_url="http://localhost:8090",
        event_bus=event_bus,
        max_iterations=3,
    )

    # Mock the HTTP calls and tool execution
    agent._client = AsyncMock()
    agent._client.get = AsyncMock(
        return_value=AsyncMock(
            status_code=200,
            json=lambda: {
                "id": "PERF-TEST",
                "status": "triage_pending",
                "summary": "test",
                "custom_fields": {},
            },
            raise_for_status=lambda: None,
        ),
    )
    agent._client.post = AsyncMock(
        return_value=AsyncMock(
            status_code=200,
            json=lambda: {},
            raise_for_status=lambda: None,
        ),
    )

    await agent.run("PERF-TEST")

    # Should have made exactly 3 LLM calls
    assert llm.call_count == 3

    # Should have emitted max_iterations error
    events = event_bus.get_events("PERF-TEST")
    error_events = [
        e
        for e in events
        if e["event_type"] == "agent_error"
        and e["data"].get("reason") == "max_iterations"
    ]
    assert len(error_events) == 1


@pytest.mark.asyncio
async def test_unlimited_iterations_no_error(tmp_path):
    """Agent with max_iterations=0 runs until LLM finishes,
    no max_iterations error emitted.
    """
    # LLM that runs 50 tool calls then finishes
    call_count = 0

    class _FinishAfterN(LLMProvider):
        async def complete(
            self,
            system_prompt: str,
            messages: list[dict[str, Any]],
            tools: list[ToolDefinition] | None = None,
            max_tokens: int = 4096,
        ) -> LLMResponse:
            nonlocal call_count
            call_count += 1
            if call_count >= 50:
                return LLMResponse(
                    text="done",
                    tool_calls=[],
                    stop_reason="end_turn",
                    raw_content=[],
                )
            from providers.llm.base import ToolCall

            return LLMResponse(
                text=None,
                tool_calls=[
                    ToolCall(
                        id=f"tc_{call_count}",
                        name="some_tool",
                        input={},
                    ),
                ],
                stop_reason="tool_use",
                raw_content=[
                    {
                        "type": "tool_use",
                        "id": f"tc_{call_count}",
                        "name": "some_tool",
                        "input": {},
                    },
                ],
            )

    llm = _FinishAfterN()
    event_bus = EventBus(log_dir=tmp_path / "logs")
    agent = _StubAgent(
        agent_name="test-agent",
        llm_provider=llm,
        state_store_url="http://localhost:8090",
        event_bus=event_bus,
        max_iterations=0,
    )

    agent._client = AsyncMock()
    agent._client.get = AsyncMock(
        return_value=AsyncMock(
            status_code=200,
            json=lambda: {
                "id": "PERF-TEST",
                "status": "triage_pending",
                "summary": "test",
                "custom_fields": {},
            },
            raise_for_status=lambda: None,
        ),
    )

    await agent.run("PERF-TEST")

    # Should have run all 50 iterations without hitting a limit
    assert call_count == 50

    # No max_iterations error
    events = event_bus.get_events("PERF-TEST")
    error_events = [
        e
        for e in events
        if e["event_type"] == "agent_error"
        and e["data"].get("reason") == "max_iterations"
    ]
    assert len(error_events) == 0

    # Should have finished cleanly
    finished = [e for e in events if e["event_type"] == "agent_finished"]
    assert len(finished) == 1


@pytest.mark.asyncio
async def test_early_exit_no_error(tmp_path):
    """Agent that finishes before max_iterations doesn't
    emit an error.
    """
    llm = _CountingLLM()  # Finishes on first call (end_turn)
    event_bus = EventBus(log_dir=tmp_path / "logs")
    agent = _StubAgent(
        agent_name="test-agent",
        llm_provider=llm,
        state_store_url="http://localhost:8090",
        event_bus=event_bus,
        max_iterations=20,
    )

    agent._client = AsyncMock()
    agent._client.get = AsyncMock(
        return_value=AsyncMock(
            status_code=200,
            json=lambda: {
                "id": "PERF-TEST",
                "status": "triage_pending",
                "summary": "test",
                "custom_fields": {},
            },
            raise_for_status=lambda: None,
        ),
    )

    await agent.run("PERF-TEST")

    assert llm.call_count == 1

    events = event_bus.get_events("PERF-TEST")
    error_events = [e for e in events if e["event_type"] == "agent_error"]
    assert len(error_events) == 0


# --- Tool rate limiting ---


@pytest.mark.asyncio
async def test_tool_rate_limiting():
    """Tool calls are throttled by min_interval_sec."""
    import time

    from providers.llm.base import ToolCall

    llm = _CountingLLM()
    agent = _StubAgent(
        agent_name="test",
        llm_provider=llm,
        state_store_url="http://localhost:8090",
    )
    agent._tool_min_interval = 0.1  # 100ms for fast test

    # Make 3 rapid tool calls and measure elapsed time
    start = time.monotonic()
    for i in range(3):
        tc = ToolCall(id=f"tc_{i}", name="unknown_tool", input={})
        await agent._execute_tool(tc)
    elapsed = time.monotonic() - start

    # Should take at least 0.2s (2 intervals between 3 calls)
    assert elapsed >= 0.18, f"Expected >= 0.18s, got {elapsed:.3f}s"


def test_tool_rate_limit_default():
    """Default rate limit is loaded."""
    llm = _CountingLLM()
    agent = _StubAgent(
        agent_name="test",
        llm_provider=llm,
        state_store_url="http://localhost:8090",
    )
    assert agent._tool_min_interval > 0


@pytest.mark.asyncio
async def test_tool_rate_limit_zero_disables():
    """Setting min_interval_sec to 0 disables throttling."""
    import time

    llm = _CountingLLM()
    agent = _StubAgent(
        agent_name="test",
        llm_provider=llm,
        state_store_url="http://localhost:8090",
    )
    agent._tool_min_interval = 0.0
    start = time.monotonic()
    await agent._throttle_tool_call()
    elapsed = time.monotonic() - start
    assert elapsed < 0.05
