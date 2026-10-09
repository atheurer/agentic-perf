"""Tests for rejecting empty provisioning results (#1012).

When the LLM submits an empty or near-empty provisioning result
(provisioning_complete=True but no hosts_provisioned), the agent
must reject it and the orchestrator handoff must block advancement.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import paths  # noqa: F401
from agents.provisioning.agent import ProvisioningAgent
from orchestrator.handoff import check_handoff

# --------------- helpers ---------------


def _make_agent() -> ProvisioningAgent:
    agent = ProvisioningAgent.__new__(ProvisioningAgent)
    agent.agent_name = "provisioning-agent"
    agent.llm = MagicMock()
    agent.store_url = "http://localhost:8090"
    agent.tools = []
    agent._tool_handlers = {}
    agent._events = None
    agent._mcp = None
    agent._stop_requested = False
    agent._client = AsyncMock()
    return agent


def _make_response(submit_input: dict) -> MagicMock:
    tc = MagicMock()
    tc.name = "submit_provisioning_result"
    tc.input = submit_input
    response = MagicMock()
    response.text = ""
    response.tool_calls = [tc]
    return response


# --------------- ProvisioningAgent._handle_completion ---------------


class TestRejectEmptyProvisioningResult:
    """_handle_completion must reject when hosts_provisioned is empty."""

    @pytest.mark.asyncio
    async def test_empty_hosts_rejected(self):
        """provisioning_complete=True with empty hosts_provisioned
        must NOT write fields or transition to executing_benchmark."""
        agent = _make_agent()

        response = _make_response(
            {
                "provisioning_complete": True,
                "hosts_provisioned": [],
            }
        )

        with (
            patch.object(
                agent, "_update_fields", new_callable=AsyncMock
            ) as mock_fields,
            patch.object(agent, "_add_comment", new_callable=AsyncMock) as mock_comment,
            patch.object(
                agent, "_transition_ticket", new_callable=AsyncMock
            ) as mock_transition,
        ):
            await agent._handle_completion("TICKET-1", response)

            # Must NOT write provisioning fields
            mock_fields.assert_not_called()

            # Must add a rejection comment
            mock_comment.assert_called_once()
            comment_text = mock_comment.call_args[0][1]
            assert "Rejected" in comment_text

            # Must transition to awaiting_customer_guidance, NOT executing_benchmark
            mock_transition.assert_called_once()
            target_status = mock_transition.call_args[0][1]
            assert target_status == "awaiting_customer_guidance"

    @pytest.mark.asyncio
    async def test_empty_object_not_marked_complete(self):
        """An entirely empty submit (no keys) has provisioning_complete=False
        so it proceeds normally but the handoff check will block it."""
        agent = _make_agent()

        # Empty object — _get_submit_result returns {}
        response = _make_response({})

        with (
            patch.object(
                agent, "_update_fields", new_callable=AsyncMock
            ) as mock_fields,
            patch.object(agent, "_add_comment", new_callable=AsyncMock),
            patch.object(agent, "_transition_ticket", new_callable=AsyncMock),
            patch.object(
                agent,
                "_plan_controls_next_transition",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            await agent._handle_completion("TICKET-2", response)

            # provisioning_complete defaults to False, so the agent
            # writes fields but the handoff check blocks advancement.
            mock_fields.assert_called_once()
            fields = mock_fields.call_args[0][1]
            assert fields["provisioning_complete"] is False

    @pytest.mark.asyncio
    async def test_missing_hosts_key_rejected(self):
        """provisioning_complete=True with hosts_provisioned key absent
        must be rejected."""
        agent = _make_agent()

        response = _make_response(
            {
                "provisioning_complete": True,
                # no hosts_provisioned key at all
            }
        )

        with (
            patch.object(
                agent, "_update_fields", new_callable=AsyncMock
            ) as mock_fields,
            patch.object(agent, "_add_comment", new_callable=AsyncMock),
            patch.object(
                agent, "_transition_ticket", new_callable=AsyncMock
            ) as mock_transition,
        ):
            await agent._handle_completion("TICKET-3", response)

            mock_fields.assert_not_called()
            mock_transition.assert_called_once()
            assert mock_transition.call_args[0][1] == "awaiting_customer_guidance"

    @pytest.mark.asyncio
    async def test_valid_result_accepted(self):
        """A proper result with hosts must be accepted normally."""
        agent = _make_agent()

        response = _make_response(
            {
                "provisioning_complete": True,
                "hosts_provisioned": ["10.0.0.1"],
                "harness_name": "crucible",
                "harness_version": "1.0",
            }
        )

        with (
            patch.object(agent, "_update_fields", new_callable=AsyncMock),
            patch.object(agent, "_add_comment", new_callable=AsyncMock),
            patch.object(
                agent, "_transition_ticket", new_callable=AsyncMock
            ) as mock_transition,
            patch.object(
                agent,
                "_get_ticket",
                new_callable=AsyncMock,
                return_value={
                    "custom_fields": {
                        "assigned_hardware_ips": {
                            "controller": "10.0.0.1",
                            "targets": ["10.0.0.1"],
                        }
                    }
                },
            ),
            patch.object(
                agent,
                "_plan_controls_next_transition",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            await agent._handle_completion("TICKET-4", response)

            # Must transition to executing_benchmark
            mock_transition.assert_called_once()
            assert mock_transition.call_args[0][1] == "executing_benchmark"


# --------------- Orchestrator handoff check ---------------


class TestHandoffRejectEmptyProvisioning:
    """check_handoff('executing_benchmark') must reject empty hosts."""

    def test_complete_but_no_hosts_rejected(self):
        """provisioning_complete=True but hosts_provisioned=[] must fail."""
        ticket = {
            "custom_fields": {
                "provisioning_complete": True,
                "hosts_provisioned": [],
            }
        }
        ok, reason = check_handoff("executing_benchmark", ticket)
        assert not ok
        assert "empty" in reason.lower() or "hosts_provisioned" in reason

    def test_complete_with_hosts_accepted(self):
        """provisioning_complete=True with hosts must pass."""
        ticket = {
            "custom_fields": {
                "provisioning_complete": True,
                "hosts_provisioned": ["10.0.0.1"],
            }
        }
        ok, reason = check_handoff("executing_benchmark", ticket)
        assert ok

    def test_not_complete_rejected(self):
        """provisioning_complete=False must still fail."""
        ticket = {
            "custom_fields": {
                "provisioning_complete": False,
                "hosts_provisioned": [],
            }
        }
        ok, reason = check_handoff("executing_benchmark", ticket)
        assert not ok

    def test_missing_provisioning_fields_rejected(self):
        """No provisioning fields at all must fail."""
        ticket = {"custom_fields": {}}
        ok, reason = check_handoff("executing_benchmark", ticket)
        assert not ok
