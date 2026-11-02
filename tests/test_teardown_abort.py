"""Tests for aborted ticket teardown-close-on-failure behaviour (#1278).

When a ticket has been aborted and its teardown fails, the resource agent
should close the ticket (transition to retrospective_pending) instead of
pausing in awaiting_customer_guidance, which would create an infinite
HITL loop.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.resource.agent import ResourceAgent


def _make_agent() -> ResourceAgent:
    """Create a ResourceAgent with mocked dependencies."""
    llm = MagicMock()
    agent = ResourceAgent(
        llm_provider=llm,
        state_store_url="http://localhost:8090",
        mode="teardown",
    )
    # Mock the HTTP-backed helpers used by _run_teardown
    agent._get_ticket = AsyncMock()
    agent._add_comment = AsyncMock()
    agent._transition_ticket = AsyncMock()
    agent._update_fields = AsyncMock()
    agent._terminate_all = AsyncMock()
    agent._run_host_cleanup = AsyncMock()
    agent._plan_controls_next_transition = AsyncMock(return_value=False)
    return agent


def _ticket(
    *, abort_requested: bool = False, status: str = "awaiting_teardown"
) -> dict:
    """Build a minimal ticket dict for teardown tests."""
    fields: dict = {
        "resource_provider": "jumpstarter",
        "resource_reservation_id": "res-123",
    }
    if abort_requested:
        fields["abort_requested"] = True
    return {
        "id": "TICKET-42",
        "status": status,
        "custom_fields": fields,
    }


class TestAbortedTeardownCloseOnFailure:
    """Aborted tickets should close even when teardown fails."""

    @pytest.mark.asyncio
    async def test_aborted_ticket_closes_on_teardown_failure(self) -> None:
        agent = _make_agent()
        agent._get_ticket.return_value = _ticket(abort_requested=True)
        agent._terminate_all.return_value = False

        with patch("orchestrator.config._load_config_file", return_value={}):
            await agent._run_teardown("TICKET-42")

        # Should NOT transition to awaiting_customer_guidance
        for call in agent._transition_ticket.call_args_list:
            assert call.args[1] != "awaiting_customer_guidance"

        # Should proceed to retrospective_pending (close path)
        agent._transition_ticket.assert_called_once_with(
            "TICKET-42",
            "retrospective_pending",
            comment="Resource teardown complete, starting retrospective",
        )

    @pytest.mark.asyncio
    async def test_non_aborted_ticket_pauses_on_teardown_failure(self) -> None:
        agent = _make_agent()
        agent._get_ticket.return_value = _ticket(abort_requested=False)
        agent._terminate_all.return_value = False

        with patch("orchestrator.config._load_config_file", return_value={}):
            await agent._run_teardown("TICKET-42")

        # Should transition to awaiting_customer_guidance
        agent._transition_ticket.assert_called_once_with(
            "TICKET-42",
            "awaiting_customer_guidance",
            comment=(
                "Resource teardown could not confirm release; allocation "
                "details were retained for manual reconciliation."
            ),
        )

    @pytest.mark.asyncio
    async def test_aborted_ticket_adds_closure_comment(self) -> None:
        agent = _make_agent()
        agent._get_ticket.return_value = _ticket(abort_requested=True)
        agent._terminate_all.return_value = False

        with patch("orchestrator.config._load_config_file", return_value={}):
            await agent._run_teardown("TICKET-42")

        # Should add a comment explaining the forced closure
        comment_calls = agent._add_comment.call_args_list
        assert len(comment_calls) >= 1
        comment_text = comment_calls[0].args[1]
        assert "closing despite incomplete teardown" in comment_text
        assert "could not confirm release" in comment_text

    @pytest.mark.asyncio
    async def test_selective_teardown_abort_closes_on_failure(self) -> None:
        """Selective teardown (preserve_roles) also closes for aborted tickets."""
        agent = _make_agent()
        ticket = _ticket(abort_requested=True)
        ticket["custom_fields"]["teardown_preserve_roles"] = ["controller"]
        agent._get_ticket.return_value = ticket
        agent._run_selective_teardown = AsyncMock(return_value=False)

        with patch("orchestrator.config._load_config_file", return_value={}):
            await agent._run_teardown("TICKET-42")

        # Should NOT pause at HITL
        for call in agent._transition_ticket.call_args_list:
            assert call.args[1] != "awaiting_customer_guidance"

        # Should proceed to close
        agent._transition_ticket.assert_called_once_with(
            "TICKET-42",
            "retrospective_pending",
            comment="Resource teardown complete, starting retrospective",
        )

    @pytest.mark.asyncio
    async def test_aborted_ticket_succeeds_normally(self) -> None:
        """Abort flag doesn't interfere when teardown succeeds."""
        agent = _make_agent()
        agent._get_ticket.return_value = _ticket(abort_requested=True)
        agent._terminate_all.return_value = True

        with patch("orchestrator.config._load_config_file", return_value={}):
            await agent._run_teardown("TICKET-42")

        # Should close normally — no forced closure comment
        agent._transition_ticket.assert_called_once_with(
            "TICKET-42",
            "retrospective_pending",
            comment="Resource teardown complete, starting retrospective",
        )
        # No abort-specific comment added
        for call in agent._add_comment.call_args_list:
            body = call.args[1] if len(call.args) > 1 else ""
            assert "closing despite" not in body
