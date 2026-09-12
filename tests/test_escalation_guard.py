"""Tests for escalation transition guard.

Verifies that _request_human_input and the max-iterations pause
check VALID_TRANSITIONS before attempting to transition to
awaiting_customer_guidance.  When the transition is invalid
(e.g., from retrospective_pending), the agent aborts cleanly
without posting a misleading "Input needed" comment.

Closes #749.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.base import AgentAbortedError, AgentBase
from providers.events import EventBus
from providers.llm.base import LLMProvider, LLMResponse, ToolCall, ToolDefinition


class _StubAgent(AgentBase):
    """Minimal agent for testing escalation guard."""

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


class TestCanPauseForGuidance:
    """Unit tests for the _can_pause_for_guidance static method."""

    def test_valid_from_executing_benchmark(self) -> None:
        assert AgentBase._can_pause_for_guidance("executing_benchmark") is True

    def test_valid_from_awaiting_teardown(self) -> None:
        assert AgentBase._can_pause_for_guidance("awaiting_teardown") is True

    def test_valid_from_triage_pending(self) -> None:
        assert AgentBase._can_pause_for_guidance("triage_pending") is True

    def test_invalid_from_retrospective_pending(self) -> None:
        assert AgentBase._can_pause_for_guidance("retrospective_pending") is False

    def test_invalid_from_closed(self) -> None:
        assert AgentBase._can_pause_for_guidance("closed") is False

    def test_invalid_from_unknown_status(self) -> None:
        assert AgentBase._can_pause_for_guidance("nonexistent_status") is False


class TestRequestHumanInputGuard:
    """_request_human_input aborts when pause is invalid."""

    async def test_closes_ticket_from_retrospective_pending(
        self,
        agent: _StubAgent,
        event_bus: EventBus,
    ) -> None:
        """From retrospective_pending, closes the ticket instead of pausing."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "status": "retrospective_pending",
            "comments": [],
            "custom_fields": {},
        }

        post_response = MagicMock()
        post_response.raise_for_status = MagicMock()
        post_response.json.return_value = {"status": "closed"}

        with (
            patch.object(
                agent._client,
                "get",
                new_callable=AsyncMock,
                return_value=mock_response,
            ),
            patch.object(
                agent._client,
                "post",
                new_callable=AsyncMock,
                return_value=post_response,
            ) as mock_post,
        ):
            with pytest.raises(AgentAbortedError, match="Cannot pause"):
                await agent._request_human_input(
                    "TICKET-1",
                    "How to proceed?",
                )

            # Should have posted a closing comment and transition
            urls = [
                str(call.args[0]) if call.args else ""
                for call in mock_post.call_args_list
            ]
            assert any("comments" in u for u in urls), (
                "Closing comment should be posted"
            )
            assert any("transition" in u for u in urls), (
                "Transition to closed should be attempted"
            )

    async def test_escalation_event_emitted_on_block(
        self,
        agent: _StubAgent,
        event_bus: EventBus,
    ) -> None:
        """Blocked pause emits an escalation event with pause_blocked reason."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "status": "retrospective_pending",
            "comments": [],
            "custom_fields": {},
        }

        post_response = MagicMock()
        post_response.raise_for_status = MagicMock()
        post_response.json.return_value = {"status": "closed"}

        with (
            patch.object(
                agent._client,
                "get",
                new_callable=AsyncMock,
                return_value=mock_response,
            ),
            patch.object(
                agent._client,
                "post",
                new_callable=AsyncMock,
                return_value=post_response,
            ),
        ):
            with pytest.raises(AgentAbortedError):
                await agent._request_human_input("TICKET-1", "Question?")

        events = event_bus.get_events("TICKET-1")
        escalations = [e for e in events if e["event_type"] == "escalation"]
        assert len(escalations) == 1
        assert escalations[0]["data"]["reason"] == "pause_blocked"
        assert escalations[0]["data"]["from_status"] == "retrospective_pending"

    async def test_proceeds_from_valid_status(
        self,
        agent: _StubAgent,
        event_bus: EventBus,
    ) -> None:
        """From executing_benchmark, the comment and transition proceed normally."""
        get_response = MagicMock()
        get_response.raise_for_status = MagicMock()
        get_response.json.return_value = {
            "status": "executing_benchmark",
            "comments": [],
            "custom_fields": {},
        }

        post_response = MagicMock()
        post_response.raise_for_status = MagicMock()
        post_response.json.return_value = {"status": "awaiting_customer_guidance"}

        # After transition, the poll finds user replied (status changed)
        poll_response = MagicMock()
        poll_response.raise_for_status = MagicMock()
        poll_response.json.return_value = {
            "status": "executing_benchmark",
            "comments": [{"body": "User's answer", "author": "user1"}],
            "custom_fields": {},
        }

        call_count = 0

        async def mock_get(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return get_response
            return poll_response

        with (
            patch.object(agent._client, "get", side_effect=mock_get),
            patch.object(
                agent._client,
                "post",
                new_callable=AsyncMock,
                return_value=post_response,
            ) as mock_post,
            patch.object(agent, "_HITL_POLL_INTERVAL", 0.01),
        ):
            reply = await agent._request_human_input("TICKET-1", "Question?")

        assert reply == "User's answer"
        post_calls = [str(c) for c in mock_post.call_args_list]
        assert any("comments" in c for c in post_calls)


class TestMaxIterationsGuard:
    """Max iterations pause respects the transition guard."""

    async def test_max_iterations_closes_from_retrospective(
        self,
        event_bus: EventBus,
    ) -> None:
        """Hit max iterations from retrospective_pending -> close, not pause."""

        class _OneShotLLM(LLMProvider):
            async def complete(
                self,
                system_prompt: str,
                messages: list[dict[str, Any]],
                tools: list[ToolDefinition] | None = None,
                max_tokens: int = 4096,
            ) -> LLMResponse:
                return LLMResponse(
                    text=None,
                    tool_calls=[
                        ToolCall(
                            id="call_1",
                            name="some_tool",
                            input={"arg": "val"},
                        ),
                    ],
                    stop_reason="tool_use",
                    raw_content=[],
                )

        agent = _StubAgent(
            agent_name="test-retro",
            llm_provider=_OneShotLLM(),
            state_store_url="http://localhost:9999",
            event_bus=event_bus,
            max_iterations=1,
        )

        get_response = MagicMock()
        get_response.raise_for_status = MagicMock()
        get_response.json.return_value = {
            "status": "retrospective_pending",
            "comments": [],
            "custom_fields": {},
        }

        post_response = MagicMock()
        post_response.raise_for_status = MagicMock()
        post_response.json.return_value = {"status": "closed"}

        with (
            patch.object(
                agent._client,
                "get",
                new_callable=AsyncMock,
                return_value=get_response,
            ),
            patch.object(
                agent._client,
                "post",
                new_callable=AsyncMock,
                return_value=post_response,
            ),
        ):
            with pytest.raises(AgentAbortedError, match="Cannot pause"):
                await agent.run("TICKET-RETRO")

        events = event_bus.get_events("TICKET-RETRO")
        escalations = [e for e in events if e["event_type"] == "escalation"]
        assert len(escalations) == 1
        assert escalations[0]["data"]["trigger"] == "max_iterations"
