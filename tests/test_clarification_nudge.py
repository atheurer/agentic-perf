"""Tests for post-HITL clarification nudge.

Verifies that when an agent answers a clarification in prose
(end_turn without submit), a one-shot [SYSTEM] nudge is injected
before escalating. The second consecutive prose end_turn still
triggers the normal escalation.

Closes #732.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.base import AgentBase
from providers.events import EventBus
from providers.llm.base import LLMProvider, LLMResponse, ToolCall, ToolDefinition


class _StubAgent(AgentBase):
    def _system_prompt(self, ticket: dict[str, Any] | None = None) -> str:
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


class _ClarificationLLM(LLMProvider):
    """LLM that simulates: tool_call → end_turn (escalation) →
    HITL resume → prose end_turn (should get nudge) → behavior varies.
    """

    def __init__(self, post_nudge_response: str = "end_turn") -> None:
        self._call_count = 0
        self._post_nudge = post_nudge_response

    async def complete(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        self._call_count += 1

        if self._call_count == 1:
            return LLMResponse(
                text=None,
                tool_calls=[
                    ToolCall(id="call_1", name="some_tool", input={"x": 1}),
                ],
                stop_reason="tool_use",
                raw_content=[],
            )

        if self._call_count == 2:
            return LLMResponse(
                text="I need help with something",
                tool_calls=[],
                stop_reason="end_turn",
                raw_content=["I need help with something"],
            )

        if self._call_count == 3:
            return LLMResponse(
                text="Here is my prose clarification answer",
                tool_calls=[],
                stop_reason="end_turn",
                raw_content=["Here is my prose clarification answer"],
            )

        if self._call_count == 4:
            if self._post_nudge == "submit":
                return LLMResponse(
                    text=None,
                    tool_calls=[
                        ToolCall(
                            id="call_2",
                            name="submit_benchmark_result",
                            input={"status": "completed"},
                        ),
                    ],
                    stop_reason="tool_use",
                    raw_content=[],
                )
            return LLMResponse(
                text="Still just prose after nudge",
                tool_calls=[],
                stop_reason="end_turn",
                raw_content=["Still just prose after nudge"],
            )

        # After the second escalation cycle resolves, submit cleanly
        return LLMResponse(
            text=None,
            tool_calls=[
                ToolCall(
                    id=f"call_{self._call_count}",
                    name="submit_benchmark_result",
                    input={"status": "completed"},
                ),
            ],
            stop_reason="tool_use",
            raw_content=[],
        )


def _make_submit_tool() -> ToolDefinition:
    return ToolDefinition(
        name="submit_benchmark_result",
        description="Submit benchmark result",
        input_schema={"type": "object", "properties": {}},
    )


def _make_regular_tool() -> ToolDefinition:
    return ToolDefinition(
        name="some_tool",
        description="A regular tool",
        input_schema={"type": "object", "properties": {}},
    )


@pytest.fixture
def event_bus(tmp_path: Any) -> EventBus:
    return EventBus(log_dir=tmp_path / "logs")


def _mock_ticket(status: str = "executing_benchmark") -> MagicMock:
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {
        "status": status,
        "comments": [],
        "custom_fields": {},
    }
    return response


def _mock_hitl_reply(reply_text: str) -> MagicMock:
    """Mock GET that returns awaiting_customer_guidance then resumes."""
    call_count = 0

    def make_response(status: str, comments: list | None = None) -> MagicMock:
        r = MagicMock()
        r.raise_for_status = MagicMock()
        r.json.return_value = {
            "status": status,
            "comments": comments or [],
            "custom_fields": {},
        }
        return r

    async def mock_get(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            return make_response("executing_benchmark")
        if call_count == 3:
            return make_response(
                "executing_benchmark",
                [{"body": reply_text, "author": "user1"}],
            )
        return make_response("executing_benchmark")

    return mock_get


class TestPostHITLNudge:
    """Nudge is injected once after HITL resume + prose end_turn."""

    async def test_nudge_injected_after_hitl_prose(
        self,
        event_bus: EventBus,
    ) -> None:
        """After HITL resume, prose end_turn gets a nudge, not escalation."""
        llm = _ClarificationLLM(post_nudge_response="submit")
        agent = _StubAgent(
            agent_name="bench-test",
            llm_provider=llm,
            state_store_url="http://localhost:9999",
            event_bus=event_bus,
            tools=[_make_regular_tool(), _make_submit_tool()],
            max_iterations=10,
        )

        post_response = MagicMock()
        post_response.raise_for_status = MagicMock()
        post_response.json.return_value = {
            "status": "awaiting_customer_guidance",
        }

        async def mock_submit_handler(**kwargs):
            return '{"status": "completed"}'

        agent._tool_handlers["submit_benchmark_result"] = mock_submit_handler
        agent._tool_handlers["some_tool"] = AsyncMock(return_value="tool result")

        with (
            patch.object(
                agent._client,
                "get",
                side_effect=_mock_hitl_reply("Please continue"),
            ),
            patch.object(
                agent._client,
                "post",
                new_callable=AsyncMock,
                return_value=post_response,
            ),
            patch.object(agent, "_HITL_POLL_INTERVAL", 0.01),
        ):
            await agent.run("TICKET-NUDGE")

        events = event_bus.get_events("TICKET-NUDGE")
        escalations = [e for e in events if e["event_type"] == "escalation"]
        assert len(escalations) == 1
        assert escalations[0]["data"]["reason"] == "end_turn_without_submit"

        assert llm._call_count == 4

    async def test_escalation_after_nudge_ignored(
        self,
        event_bus: EventBus,
    ) -> None:
        """Second prose end_turn after nudge triggers full escalation."""
        llm = _ClarificationLLM(post_nudge_response="end_turn")
        agent = _StubAgent(
            agent_name="bench-test",
            llm_provider=llm,
            state_store_url="http://localhost:9999",
            event_bus=event_bus,
            tools=[_make_regular_tool(), _make_submit_tool()],
            max_iterations=10,
        )

        post_response = MagicMock()
        post_response.raise_for_status = MagicMock()
        post_response.json.return_value = {
            "status": "awaiting_customer_guidance",
        }

        async def mock_submit_handler(**kwargs):
            return '{"status": "completed"}'

        agent._tool_handlers["some_tool"] = AsyncMock(return_value="tool result")
        agent._tool_handlers["submit_benchmark_result"] = mock_submit_handler

        call_count = 0

        async def mock_get(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            r = MagicMock()
            r.raise_for_status = MagicMock()
            if call_count <= 2:
                r.json.return_value = {
                    "status": "executing_benchmark",
                    "comments": [],
                    "custom_fields": {},
                }
            elif call_count == 3:
                r.json.return_value = {
                    "status": "executing_benchmark",
                    "comments": [
                        {"body": "Please continue", "author": "user1"},
                    ],
                    "custom_fields": {},
                }
            else:
                r.json.return_value = {
                    "status": "executing_benchmark",
                    "comments": [
                        {"body": "Please continue", "author": "user1"},
                        {"body": "Just do it", "author": "user1"},
                    ],
                    "custom_fields": {},
                }
            return r

        with (
            patch.object(agent._client, "get", side_effect=mock_get),
            patch.object(
                agent._client,
                "post",
                new_callable=AsyncMock,
                return_value=post_response,
            ),
            patch.object(agent, "_HITL_POLL_INTERVAL", 0.01),
        ):
            await agent.run("TICKET-ESC")

        events = event_bus.get_events("TICKET-ESC")
        escalations = [e for e in events if e["event_type"] == "escalation"]
        assert len(escalations) == 2

    async def test_no_nudge_without_hitl(
        self,
        event_bus: EventBus,
    ) -> None:
        """Without prior HITL, prose end_turn escalates immediately."""

        class _DirectEndTurnLLM(LLMProvider):
            async def complete(
                self,
                system_prompt: str,
                messages: list[dict[str, Any]],
                tools: list[ToolDefinition] | None = None,
                max_tokens: int = 4096,
            ) -> LLMResponse:
                return LLMResponse(
                    text="I give up",
                    tool_calls=[],
                    stop_reason="end_turn",
                    raw_content=["I give up"],
                )

        agent = _StubAgent(
            agent_name="bench-test",
            llm_provider=_DirectEndTurnLLM(),
            state_store_url="http://localhost:9999",
            event_bus=event_bus,
            tools=[_make_submit_tool()],
            max_iterations=5,
        )

        get_response = MagicMock()
        get_response.raise_for_status = MagicMock()
        get_response.json.return_value = {
            "status": "executing_benchmark",
            "comments": [],
            "custom_fields": {},
        }

        post_response = MagicMock()
        post_response.raise_for_status = MagicMock()
        post_response.json.return_value = {
            "status": "awaiting_customer_guidance",
        }

        call_count = 0

        async def mock_get(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                r = MagicMock()
                r.raise_for_status = MagicMock()
                r.json.return_value = {
                    "status": "executing_benchmark",
                    "comments": [
                        {"body": "Just proceed", "author": "user1"},
                    ],
                    "custom_fields": {},
                }
                return r
            return get_response

        with (
            patch.object(agent._client, "get", side_effect=mock_get),
            patch.object(
                agent._client,
                "post",
                new_callable=AsyncMock,
                return_value=post_response,
            ),
            patch.object(agent, "_HITL_POLL_INTERVAL", 0.01),
        ):
            await agent.run("TICKET-DIRECT")

        events = event_bus.get_events("TICKET-DIRECT")
        escalations = [e for e in events if e["event_type"] == "escalation"]
        assert len(escalations) >= 1
        assert escalations[0]["data"]["reason"] == "end_turn_without_submit"

    async def test_tool_calls_clear_hitl_flag(
        self,
        event_bus: EventBus,
    ) -> None:
        """Tool calls after HITL consume the nudge — later prose escalates."""

        class _ToolThenProseLLM(LLMProvider):
            def __init__(self) -> None:
                self._call_count = 0

            async def complete(
                self,
                system_prompt: str,
                messages: list[dict[str, Any]],
                tools: list[ToolDefinition] | None = None,
                max_tokens: int = 4096,
            ) -> LLMResponse:
                self._call_count += 1

                if self._call_count == 1:
                    # Prose end_turn → escalation → HITL
                    return LLMResponse(
                        text="I need help",
                        tool_calls=[],
                        stop_reason="end_turn",
                        raw_content=["I need help"],
                    )

                if self._call_count == 2:
                    # After HITL: make a tool call (clears flag)
                    return LLMResponse(
                        text=None,
                        tool_calls=[
                            ToolCall(
                                id="call_2",
                                name="some_tool",
                                input={"x": 1},
                            ),
                        ],
                        stop_reason="tool_use",
                        raw_content=[],
                    )

                if self._call_count == 3:
                    # Prose end_turn — should escalate, not nudge
                    return LLMResponse(
                        text="Still prose",
                        tool_calls=[],
                        stop_reason="end_turn",
                        raw_content=["Still prose"],
                    )

                return LLMResponse(
                    text=None,
                    tool_calls=[
                        ToolCall(
                            id=f"call_{self._call_count}",
                            name="submit_benchmark_result",
                            input={"status": "completed"},
                        ),
                    ],
                    stop_reason="tool_use",
                    raw_content=[],
                )

        llm = _ToolThenProseLLM()
        agent = _StubAgent(
            agent_name="bench-test",
            llm_provider=llm,
            state_store_url="http://localhost:9999",
            event_bus=event_bus,
            tools=[_make_regular_tool(), _make_submit_tool()],
            max_iterations=10,
        )

        post_response = MagicMock()
        post_response.raise_for_status = MagicMock()
        post_response.json.return_value = {
            "status": "awaiting_customer_guidance",
        }

        async def mock_submit_handler(**kwargs):
            return '{"status": "completed"}'

        agent._tool_handlers["some_tool"] = AsyncMock(return_value="tool result")
        agent._tool_handlers["submit_benchmark_result"] = mock_submit_handler

        call_count = 0

        async def mock_get(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            r = MagicMock()
            r.raise_for_status = MagicMock()
            if call_count <= 1:
                r.json.return_value = {
                    "status": "executing_benchmark",
                    "comments": [],
                    "custom_fields": {},
                }
            elif call_count <= 3:
                r.json.return_value = {
                    "status": "executing_benchmark",
                    "comments": [
                        {"body": "Please continue", "author": "user1"},
                    ],
                    "custom_fields": {},
                }
            else:
                r.json.return_value = {
                    "status": "executing_benchmark",
                    "comments": [
                        {"body": "Please continue", "author": "user1"},
                        {"body": "OK", "author": "user1"},
                    ],
                    "custom_fields": {},
                }
            return r

        with (
            patch.object(agent._client, "get", side_effect=mock_get),
            patch.object(
                agent._client,
                "post",
                new_callable=AsyncMock,
                return_value=post_response,
            ),
            patch.object(agent, "_HITL_POLL_INTERVAL", 0.01),
        ):
            await agent.run("TICKET-TOOL-CLEAR")

        events = event_bus.get_events("TICKET-TOOL-CLEAR")
        escalations = [e for e in events if e["event_type"] == "escalation"]
        # Two escalations: call 1 (no prior HITL) and call 3 (flag
        # cleared by tool call at call 2). No nudge fired.
        assert len(escalations) == 2
        assert llm._call_count == 4
