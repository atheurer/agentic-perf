"""Tests for #920: tool results are persisted before the next LLM call.

Verifies that _save_messages is called after each tool-result iteration,
so tool outputs survive agent crashes or LLM failures.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.base import AgentBase
from providers.events import EventBus
from providers.llm.base import LLMProvider, LLMResponse, ToolCall, ToolDefinition

# --- Minimal concrete agent ---


class _StubAgent(AgentBase):
    def _system_prompt(self, ticket: dict[str, Any]) -> str:
        return "test"

    def _build_messages(self, ticket: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"role": "user", "content": "test"}]

    async def _handle_completion(self, ticket_id: str, response: LLMResponse) -> None:
        pass


def _make_ticket(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": "PERF-920",
        "status": "investigating",
        "summary": "test",
        "custom_fields": {},
    }
    base.update(overrides)
    return base


def _mock_http_client(ticket: dict[str, Any] | None = None) -> AsyncMock:
    ticket = ticket or _make_ticket()
    client = AsyncMock()
    ok_response = AsyncMock(
        status_code=200,
        json=lambda: ticket,
        raise_for_status=MagicMock(),
    )
    client.get = AsyncMock(return_value=ok_response)
    client.post = AsyncMock(
        return_value=AsyncMock(
            status_code=200,
            json=lambda: {},
            raise_for_status=MagicMock(),
        ),
    )
    client.patch = AsyncMock(
        return_value=AsyncMock(
            status_code=200,
            json=lambda: {},
            raise_for_status=MagicMock(),
        ),
    )
    return client


class _ToolThenEndLLM(LLMProvider):
    """First call returns a tool call; second call returns end_turn."""

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
        if self.call_count == 1:
            return LLMResponse(
                text=None,
                tool_calls=[
                    ToolCall(id="tc_1", name="fetch_data", input={}),
                ],
                stop_reason="tool_use",
                raw_content=[
                    {
                        "type": "tool_use",
                        "id": "tc_1",
                        "name": "fetch_data",
                        "input": {},
                    },
                ],
            )
        return LLMResponse(
            text="Done.",
            tool_calls=[],
            stop_reason="end_turn",
            raw_content=[],
        )


@pytest.mark.asyncio
async def test_messages_persisted_after_tool_results(tmp_path):
    """_save_messages is called after tool results are appended to messages,
    before the next LLM call (#920)."""
    llm = _ToolThenEndLLM()
    event_bus = EventBus(log_dir=tmp_path / "logs")
    agent = _StubAgent(
        agent_name="test-agent",
        llm_provider=llm,
        state_store_url="http://localhost:8090",
        event_bus=event_bus,
        max_iterations=5,
    )
    agent._tool_handlers["fetch_data"] = AsyncMock(return_value='{"status": "ok"}')
    agent._client = _mock_http_client()
    agent._save_messages = AsyncMock()

    await agent.run("PERF-920")

    # _save_messages must have been called at least once after tool results
    assert agent._save_messages.call_count >= 1

    # The first persist call should include the tool result in messages
    first_call_args = agent._save_messages.call_args_list[0]
    ticket_id_arg = first_call_args[0][0]
    messages_arg = first_call_args[0][1]
    assert ticket_id_arg == "PERF-920"

    # Messages should contain the tool result
    tool_result_messages = [
        m
        for m in messages_arg
        if m.get("role") == "user"
        and isinstance(m.get("content"), list)
        and any(
            item.get("type") == "tool_result"
            for item in m["content"]
            if isinstance(item, dict)
        )
    ]
    assert len(tool_result_messages) >= 1


class _MultiToolLLM(LLMProvider):
    """Returns N tool calls, then end_turn."""

    def __init__(self, tool_iterations: int = 3) -> None:
        self.call_count = 0
        self.tool_iterations = tool_iterations

    async def complete(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        self.call_count += 1
        if self.call_count <= self.tool_iterations:
            return LLMResponse(
                text=None,
                tool_calls=[
                    ToolCall(
                        id=f"tc_{self.call_count}",
                        name="fetch_data",
                        input={},
                    ),
                ],
                stop_reason="tool_use",
                raw_content=[
                    {
                        "type": "tool_use",
                        "id": f"tc_{self.call_count}",
                        "name": "fetch_data",
                        "input": {},
                    },
                ],
            )
        return LLMResponse(
            text="Done.",
            tool_calls=[],
            stop_reason="end_turn",
            raw_content=[],
        )


@pytest.mark.asyncio
async def test_messages_persisted_after_every_tool_iteration(tmp_path):
    """_save_messages is called after each iteration with tool results,
    not just once at the end."""
    tool_iterations = 3
    llm = _MultiToolLLM(tool_iterations=tool_iterations)
    event_bus = EventBus(log_dir=tmp_path / "logs")
    agent = _StubAgent(
        agent_name="test-agent",
        llm_provider=llm,
        state_store_url="http://localhost:8090",
        event_bus=event_bus,
        max_iterations=10,
    )
    agent._tool_handlers["fetch_data"] = AsyncMock(return_value='{"value": 42}')
    agent._client = _mock_http_client()
    agent._save_messages = AsyncMock()

    await agent.run("PERF-920")

    # Should be called once per tool iteration
    assert agent._save_messages.call_count >= tool_iterations


class _CrashOnSecondCallLLM(LLMProvider):
    """First call returns tool_use, second call raises an exception."""

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
        if self.call_count == 1:
            return LLMResponse(
                text=None,
                tool_calls=[
                    ToolCall(id="tc_1", name="fetch_data", input={}),
                ],
                stop_reason="tool_use",
                raw_content=[
                    {
                        "type": "tool_use",
                        "id": "tc_1",
                        "name": "fetch_data",
                        "input": {},
                    },
                ],
            )
        raise RuntimeError("LLM unavailable")


@pytest.mark.asyncio
async def test_tool_results_survive_subsequent_llm_crash(tmp_path):
    """When the LLM crashes after a tool returns results, the results
    have already been persisted via _save_messages (#920)."""
    llm = _CrashOnSecondCallLLM()
    event_bus = EventBus(log_dir=tmp_path / "logs")
    agent = _StubAgent(
        agent_name="test-agent",
        llm_provider=llm,
        state_store_url="http://localhost:8090",
        event_bus=event_bus,
        max_iterations=5,
    )
    agent._tool_handlers["fetch_data"] = AsyncMock(return_value='{"result": "data"}')
    agent._client = _mock_http_client()

    saved_messages_calls: list[list[dict[str, Any]]] = []

    async def capture_save(tid: str, msgs: list[dict[str, Any]]) -> None:
        saved_messages_calls.append([dict(m) for m in msgs])

    agent._save_messages = AsyncMock(side_effect=capture_save)

    with pytest.raises(RuntimeError, match="LLM unavailable"):
        await agent.run("PERF-920")

    # _save_messages was called BEFORE the LLM crash
    assert len(saved_messages_calls) >= 1

    # The saved messages include the tool result
    saved = saved_messages_calls[0]
    has_tool_result = any(
        isinstance(m.get("content"), list)
        and any(
            item.get("type") == "tool_result"
            for item in m["content"]
            if isinstance(item, dict)
        )
        for m in saved
    )
    assert has_tool_result, "Tool results were not persisted before LLM crash"
