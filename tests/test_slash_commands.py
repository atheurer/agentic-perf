"""Tests for the slash command system (cli.py + agents/base.py + agents/review/agent.py)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# AgentBase._handle_slash_command — generic command validation
# ---------------------------------------------------------------------------


class TestAgentBaseSlashCommands:
    """Tests for the base agent slash command handler."""

    @pytest.fixture()
    def agent(self):
        """Minimal AgentBase subclass for testing _handle_slash_command."""
        from agents.base import AgentBase
        from providers.llm.base import LLMProvider

        class _StubAgent(AgentBase):
            def _system_prompt(self, ticket):
                return "stub"

            def _build_messages(self, ticket):
                return []

            async def _handle_completion(self, ticket_id, response):
                pass

        llm = MagicMock(spec=LLMProvider)
        return _StubAgent(
            agent_name="stub-agent",
            llm_provider=llm,
            state_store_url="http://localhost:8090",
        )

    @pytest.mark.asyncio
    async def test_unknown_command_returns_error(self, agent):
        result = await agent._handle_slash_command("PERF-XXXX", "/bogus")
        assert result is not None
        assert "not a recognised" in result.lower() or "unknown" in result.lower()
        assert "/bogus" in result

    @pytest.mark.asyncio
    async def test_unknown_command_lists_valid_commands(self, agent):
        result = await agent._handle_slash_command("PERF-XXXX", "/bogus")
        assert "/abort" in result
        assert "/close" in result
        assert "/model" in result
        assert "/extend-iterations" in result

    @pytest.mark.asyncio
    async def test_submit_falls_through_to_subclass(self, agent):
        # Base class returns None for /submit (delegates to subclass)
        result = await agent._handle_slash_command("PERF-XXXX", "/submit")
        assert result is None

    @pytest.mark.asyncio
    async def test_cli_commands_not_intercepted_by_agent(self, agent):
        # /abort and /close are CLI-side — agent returns None and lets them pass
        # (they never reach the agent because cli.py handles them first, but
        # if they somehow do, the base class lets them through)
        for cmd in ("/abort", "/close"):
            result = await agent._handle_slash_command("PERF-XXXX", cmd)
            assert result is None, f"{cmd} should not be intercepted by base agent"


# ---------------------------------------------------------------------------
# ReviewAgent._handle_slash_command — /submit unlocks submission gate
# ---------------------------------------------------------------------------


class TestReviewAgentSlashCommands:
    """Tests for the review agent's /submit slash command."""

    @pytest.fixture()
    def review_agent(self):
        from agents.review.agent import ReviewAgent
        from providers.llm.base import LLMProvider

        llm = MagicMock(spec=LLMProvider)
        return ReviewAgent(
            llm_provider=llm,
            state_store_url="http://localhost:8090",
        )

    @pytest.mark.asyncio
    async def test_submit_unlocks_gate(self, review_agent):
        # Default is auto-submit (True). Set to False
        # to simulate interactive mode for this test.
        review_agent._user_approved_submit = False
        result = await review_agent._handle_slash_command("PERF-XXXX", "/submit")
        assert review_agent._user_approved_submit is True
        assert result is not None
        assert "submit_review_result" in result

    @pytest.mark.asyncio
    async def test_submit_directive_is_strong(self, review_agent):
        result = await review_agent._handle_slash_command("PERF-XXXX", "/submit")
        assert "MUST" in result or "immediately" in result.lower()
        assert "request_clarification" in result

    @pytest.mark.asyncio
    async def test_unknown_command_rejected_by_review_agent(self, review_agent):
        result = await review_agent._handle_slash_command("PERF-XXXX", "/unknown")
        assert result is not None
        assert "not a recognised" in result.lower() or "unknown" in result.lower()


# ---------------------------------------------------------------------------
# _resume_ticket — returns bool, warns on missing previous_status
# ---------------------------------------------------------------------------


class TestResumeTicket:
    """Tests for the _resume_ticket helper."""

    def _make_client(self, transition_status=200):
        client = MagicMock()
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        client.post.return_value = resp
        return client

    def test_returns_true_when_previous_status_exists(self):
        from cli import _resume_ticket

        client = self._make_client()
        ticket = {"previous_status": "executing_benchmark"}
        result = _resume_ticket(client, ticket, "PERF-XXXX")
        assert result is True
        client.post.assert_called_once()

    def test_returns_false_when_no_previous_status(self):
        from cli import _resume_ticket

        client = self._make_client()
        ticket = {}
        result = _resume_ticket(client, ticket, "PERF-XXXX")
        assert result is False
        client.post.assert_not_called()

    def test_returns_false_when_previous_status_is_none(self):
        from cli import _resume_ticket

        client = self._make_client()
        ticket = {"previous_status": None}
        result = _resume_ticket(client, ticket, "PERF-XXXX")
        assert result is False
        client.post.assert_not_called()


# ---------------------------------------------------------------------------
# HITL timeout two-strike policy tests
# ---------------------------------------------------------------------------


class TestAgentBaseHITLTimeout:
    """Tests for the HITL timeout two-strike policy."""

    @pytest.fixture()
    def agent(self):
        """AgentBase subclass for testing _request_human_input."""
        from agents.base import AgentBase
        from providers.llm.base import LLMProvider

        class _TimeoutTestAgent(AgentBase):
            def _system_prompt(self, ticket):
                return "stub"

            def _build_messages(self, ticket):
                return []

            async def _handle_completion(self, ticket_id, response):
                pass

        llm = MagicMock(spec=LLMProvider)
        agent = _TimeoutTestAgent(
            agent_name="timeout-agent",
            llm_provider=llm,
            state_store_url="http://localhost:8090",
        )
        # Use short intervals/timeout for fast tests
        agent._HITL_POLL_INTERVAL = 0.01
        agent._HITL_TIMEOUT = 0.02
        return agent

    @pytest.mark.asyncio
    async def test_successful_input_on_first_try(self, agent):
        from unittest.mock import AsyncMock

        # First check returns awaiting_customer_guidance, second check is modified
        # (leaves the status), returning comments
        agent._get_ticket = AsyncMock(
            side_effect=[
                {
                    "status": "some_other_status",
                    "comments": [],
                },  # initial call in _request_human_input
                {
                    "status": "awaiting_customer_guidance",
                    "comments": [],
                },  # first poll loop entry status
                {
                    "status": "resumed",
                    "comments": [{"author": "alice", "body": "hello user reply"}],
                },  # resumed
            ]
        )
        agent._add_comment = AsyncMock()
        agent._transition_ticket = AsyncMock()
        agent._emit = MagicMock()

        reply = await agent._request_human_input("PERF-123", "question?")
        assert reply == "hello user reply"
        agent._transition_ticket.assert_called_with(
            "PERF-123",
            "awaiting_customer_guidance",
            comment="Agent timeout-agent needs clarification",
        )

    @pytest.mark.asyncio
    async def test_first_timeout_retries_second_timeout_raises_hitl_timeout_error(
        self, agent
    ):
        from unittest.mock import AsyncMock

        from agents.base import HITLTimeoutError

        # Continuous status of awaiting_customer_guidance
        ticket = {"status": "awaiting_customer_guidance", "comments": []}
        agent._get_ticket = AsyncMock(return_value=ticket)
        agent._add_comment = AsyncMock()
        agent._transition_ticket = AsyncMock()
        agent._emit = MagicMock()

        with pytest.raises(HITLTimeoutError) as exc_info:
            await agent._request_human_input("PERF-123", "question?")

        assert "stopped after two consecutive" in str(exc_info.value)
        assert agent._hitl_timeout_count == 2
        # Verify both warning and final comments were added
        assert (
            agent._add_comment.call_count == 3
        )  # initial "Input needed" + 1st warning + final comment

    @pytest.mark.asyncio
    async def test_resumes_on_retry_wait_loop(self, agent):
        from unittest.mock import AsyncMock

        # Times out on first period, but gets reply during the second period (re-ask)
        agent._get_ticket = AsyncMock(
            side_effect=[
                {"status": "awaiting_customer_guidance", "comments": []},  # initial
                # 1st wait loop (times out, so stays awaiting_customer_guidance)
                {"status": "awaiting_customer_guidance", "comments": []},
                {"status": "awaiting_customer_guidance", "comments": []},
                {"status": "awaiting_customer_guidance", "comments": []},
                # 2nd wait loop after warning (resumes)
                {
                    "status": "resumed",
                    "comments": [{"author": "alice", "body": "hello after warning"}],
                },
            ]
        )
        agent._add_comment = AsyncMock()
        agent._transition_ticket = AsyncMock()
        agent._emit = MagicMock()

        reply = await agent._request_human_input("PERF-123", "question?")
        assert reply == "hello after warning"
        assert agent._hitl_timeout_count == 0
        assert agent._add_comment.call_count == 2  # initial + warning comment
