"""Tests for provisioning agent assigned_hardware_ips protection.

Regression tests for #532: provisioning agent must never write
assigned_hardware_ips.  That field is owned by the resource agent
(or platform agent for Jumpstarter).  Provisioning may read it but
never overwrites it with a derived controller-only fallback.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.provisioning.agent import ProvisioningAgent


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


# ------------------------------------------------------------------
# Regression test: _handle_completion must not write
# assigned_hardware_ips (#532)
# ------------------------------------------------------------------


class TestHandleCompletionIPGuard:
    @pytest.mark.asyncio
    async def test_no_overwrite_without_explicit_field(self):
        """Provisioning result without assigned_hardware_ips must
        not touch the existing allocation."""
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
            patch.object(
                agent,
                "_update_fields",
                new_callable=AsyncMock,
            ) as mock_fields,
            patch.object(
                agent,
                "_add_comment",
                new_callable=AsyncMock,
            ),
            patch.object(
                agent,
                "_transition_ticket",
                new_callable=AsyncMock,
            ),
            patch.object(
                agent,
                "_plan_controls_next_transition",
                return_value=False,
            ),
        ):
            await agent._handle_completion("PERF-TEST", response)

            fields = mock_fields.call_args[0][1]
            assert "assigned_hardware_ips" not in fields
            assert fields["ssh_hardware_ips"] == {
                "controller": "10.0.0.1",
                "targets": ["10.0.0.1"],
            }
            assert fields["hosts_provisioned"] == ["10.0.0.1"]

    @pytest.mark.asyncio
    async def test_explicit_assigned_hardware_ips_ignored(self):
        """Even when the result explicitly carries
        assigned_hardware_ips, provisioning must not write it."""
        agent = _make_agent()

        response = _make_response(
            {
                "provisioning_complete": True,
                "hosts_provisioned": ["10.0.0.1"],
                "harness_name": "crucible",
                "harness_version": "1.0",
                "assigned_hardware_ips": {
                    "controller": "10.0.0.1",
                    "targets": ["10.0.0.1"],
                },
            }
        )

        with (
            patch.object(
                agent,
                "_update_fields",
                new_callable=AsyncMock,
            ) as mock_fields,
            patch.object(
                agent,
                "_add_comment",
                new_callable=AsyncMock,
            ),
            patch.object(
                agent,
                "_transition_ticket",
                new_callable=AsyncMock,
            ),
            patch.object(
                agent,
                "_plan_controls_next_transition",
                return_value=False,
            ),
        ):
            await agent._handle_completion("PERF-TEST", response)

            fields = mock_fields.call_args[0][1]
            assert "assigned_hardware_ips" not in fields

    @pytest.mark.asyncio
    async def test_ssh_hardware_ips_derived_from_hosts_provisioned(self):
        """ssh_hardware_ips is provisioning's own output and should
        reflect which hosts were actually SSH-provisioned."""
        agent = _make_agent()

        response = _make_response(
            {
                "provisioning_complete": True,
                "hosts_provisioned": ["10.0.0.5"],
                "harness_name": "crucible",
                "harness_version": "1.0",
            }
        )

        with (
            patch.object(
                agent,
                "_update_fields",
                new_callable=AsyncMock,
            ) as mock_fields,
            patch.object(
                agent,
                "_add_comment",
                new_callable=AsyncMock,
            ),
            patch.object(
                agent,
                "_transition_ticket",
                new_callable=AsyncMock,
            ),
            patch.object(
                agent,
                "_plan_controls_next_transition",
                return_value=False,
            ),
        ):
            await agent._handle_completion("PERF-TEST", response)

            fields = mock_fields.call_args[0][1]
            assert fields["ssh_hardware_ips"] == {
                "controller": "10.0.0.5",
                "targets": ["10.0.0.5"],
            }

    @pytest.mark.asyncio
    async def test_get_ticket_not_called_for_normal_submission(self):
        """Normal submissions (no assigned_hardware_ips in result)
        should not make extra HTTP calls to read the ticket."""
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
            patch.object(
                agent,
                "_update_fields",
                new_callable=AsyncMock,
            ),
            patch.object(
                agent,
                "_add_comment",
                new_callable=AsyncMock,
            ),
            patch.object(
                agent,
                "_transition_ticket",
                new_callable=AsyncMock,
            ),
            patch.object(
                agent,
                "_plan_controls_next_transition",
                return_value=False,
            ),
            patch.object(
                agent,
                "_get_ticket",
                new_callable=AsyncMock,
            ) as mock_get,
        ):
            await agent._handle_completion("PERF-TEST", response)
            mock_get.assert_not_called()


# ------------------------------------------------------------------
# Handoff validation: proves the user-visible symptom is fixed
# ------------------------------------------------------------------


class TestHandoffAfterFix:
    def test_handoff_passes_with_preserved_allocation(self):
        """After the fix, _check_awaiting_provision passes for a
        3-role ticket because assigned_hardware_ips is preserved
        (never overwritten by provisioning)."""
        from orchestrator.handoff import check_handoff

        ticket = {
            "status": "awaiting_provision",
            "custom_fields": {
                "resource_provider": "user_provided",
                "assigned_hardware_ips": {
                    "controller": "10.0.0.1",
                    "targets": ["10.0.0.2", "10.0.0.3"],
                },
                "required_hosts": [
                    {"roles": ["controller"]},
                    {"roles": ["client"]},
                    {"roles": ["server"]},
                ],
            },
        }
        ok, reason = check_handoff("awaiting_provision", ticket)
        assert ok, f"Handoff should pass: {reason}"

    def test_handoff_fails_with_controller_only(self):
        """Shows the original bug symptom: if assigned_hardware_ips
        was shrunk to controller-only, the handoff rejects."""
        from orchestrator.handoff import check_handoff

        ticket = {
            "status": "awaiting_provision",
            "custom_fields": {
                "resource_provider": "user_provided",
                "assigned_hardware_ips": {
                    "controller": "10.0.0.1",
                    "targets": ["10.0.0.1"],
                },
                "required_hosts": [
                    {"roles": ["controller"]},
                    {"roles": ["client"]},
                    {"roles": ["server"]},
                ],
            },
        }
        ok, reason = check_handoff("awaiting_provision", ticket)
        assert not ok
        assert "Insufficient hosts" in reason

    def test_single_host_ticket_unaffected(self):
        """A ticket without required_hosts (min_hosts=1) should
        still pass with controller-only IPs."""
        from orchestrator.handoff import check_handoff

        ticket = {
            "status": "awaiting_provision",
            "custom_fields": {
                "resource_provider": "user_provided",
                "assigned_hardware_ips": {
                    "controller": "10.0.0.1",
                    "targets": ["10.0.0.1"],
                },
            },
        }
        ok, reason = check_handoff("awaiting_provision", ticket)
        assert ok, f"Single-host should pass: {reason}"
