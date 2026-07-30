"""Tests for cumulative agent iterations, global iteration caps, and EventBus budget state recovery."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agents.base import AgentBase
from providers.events import EventBus
from providers.llm.base import LLMProvider, LLMResponse, ToolDefinition


class _StubAgent(AgentBase):
    def _system_prompt(self, ticket: dict[str, Any]) -> str:
        return "test"

    def _build_messages(self, ticket: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"role": "user", "content": "test"}]

    async def _handle_completion(self, ticket_id: str, response: LLMResponse) -> None:
        pass


class _InfiniteToolLLM(LLMProvider):
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


@pytest.mark.asyncio
async def test_cumulative_iteration_limit_respected(tmp_path):
    """Verifies that past agent iterations are counted and subtracted from the agent's phase budget."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    ticket_id = "PERF-TEST"

    # Pre-write 3 llm_request events for this agent
    jsonl_path = log_dir / f"{ticket_id}.jsonl"
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for i in range(3):
            f.write(
                json.dumps(
                    {
                        "seq": i + 1,
                        "agent": "test-agent",
                        "event_type": "llm_request",
                        "data": {"iteration": i},
                    }
                )
                + "\n"
            )

    event_bus = EventBus(log_dir=log_dir)
    llm = _InfiniteToolLLM()
    agent = _StubAgent(
        agent_name="test-agent",
        llm_provider=llm,
        state_store_url="http://localhost:8090",
        event_bus=event_bus,
        max_iterations=5,  # 3 already consumed, only 2 remaining!
    )

    agent._client = AsyncMock()
    agent._client.get = AsyncMock(
        return_value=AsyncMock(
            status_code=200,
            json=lambda: {
                "id": ticket_id,
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

    await agent.run(ticket_id)

    # Respects budget: should run 2 iterations + 1 grace iteration = 3 calls
    assert llm.call_count == 3


@pytest.mark.asyncio
async def test_global_iteration_limit_respected(tmp_path):
    """Verifies that the global iteration limit halts execution across all agents."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    ticket_id = "PERF-TEST"

    # Pre-write 98 llm_request events across other agents
    jsonl_path = log_dir / f"{ticket_id}.jsonl"
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for i in range(98):
            f.write(
                json.dumps(
                    {
                        "seq": i + 1,
                        "agent": "some-other-agent",
                        "event_type": "llm_request",
                        "data": {"iteration": i},
                    }
                )
                + "\n"
            )

    event_bus = EventBus(log_dir=log_dir)
    llm = _InfiniteToolLLM()
    agent = _StubAgent(
        agent_name="test-agent",
        llm_provider=llm,
        state_store_url="http://localhost:8090",
        event_bus=event_bus,
        max_iterations=10,  # 98 consumed globally, global limit 100 -> only 2 remaining!
    )

    # Mock client and ticket fields
    agent._client = AsyncMock()
    ticket_data = {
        "id": ticket_id,
        "status": "triage_pending",
        "summary": "test",
        "custom_fields": {
            "global_max_iterations_override": 100,
        },
    }
    agent._client.get = AsyncMock(
        return_value=AsyncMock(
            status_code=200,
            json=lambda: ticket_data,
            raise_for_status=lambda: None,
        ),
    )

    posted_comments = []
    transitions = []

    async def mock_post(url, **kwargs):
        data = kwargs.get("json", {})
        if "comments" in url:
            posted_comments.append(data.get("body", ""))
        elif "transition" in url:
            transitions.append(data.get("status", ""))
        return AsyncMock(
            status_code=200, json=lambda: {}, raise_for_status=lambda: None
        )

    agent._client.post = mock_post
    agent._client.patch = AsyncMock(
        return_value=AsyncMock(
            status_code=200, json=lambda: {}, raise_for_status=lambda: None
        )
    )

    await agent.run(ticket_id)

    # 2 remaining global iterations -> 2 calls before halting
    assert llm.call_count == 2

    # Verify transitions & comments
    assert any("reached global maximum iteration limit" in c for c in posted_comments)
    assert "awaiting_customer_guidance" in transitions


def test_event_bus_loads_cumulative_from_file(tmp_path):
    """Verifies that EventBus reconstructs LLM usage statistics from previous log entries."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    ticket_id = "PERF-BUDGET-RECOVERY"

    # Pre-write llm_usage events from previous runs
    jsonl_path = log_dir / f"{ticket_id}.jsonl"
    with open(jsonl_path, "w", encoding="utf-8") as f:
        # First event: test-agent usage
        f.write(
            json.dumps(
                {
                    "seq": 1,
                    "agent": "test-agent",
                    "event_type": "llm_usage",
                    "data": {
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "duration_ms": 1000,
                        "model": "model-a",
                    },
                }
            )
            + "\n"
        )
        # Second event: other-agent usage
        f.write(
            json.dumps(
                {
                    "seq": 2,
                    "agent": "other-agent",
                    "event_type": "llm_usage",
                    "data": {
                        "input_tokens": 200,
                        "output_tokens": 80,
                        "duration_ms": 2000,
                        "model": "model-b",
                    },
                }
            )
            + "\n"
        )

    event_bus = EventBus(log_dir=log_dir)

    # Verify cumulative overall usage
    overall = event_bus.get_cumulative_usage(ticket_id)
    assert overall["input_tokens"] == 300
    assert overall["output_tokens"] == 130
    assert overall["llm_calls"] == 2

    # Verify per-agent usage breakdown
    agent_usage = event_bus.get_agent_usage(ticket_id)
    assert "test-agent" in agent_usage
    assert agent_usage["test-agent"]["input_tokens"] == 100
    assert agent_usage["test-agent"]["output_tokens"] == 50

    assert "other-agent" in agent_usage
    assert agent_usage["other-agent"]["input_tokens"] == 200
    assert agent_usage["other-agent"]["output_tokens"] == 80
