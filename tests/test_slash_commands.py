"""Tests for the slash command system (cli.py + agents/base.py + agents/review/agent.py)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

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
        assert review_agent._user_approved_submit is False
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
