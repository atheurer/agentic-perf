"""Tests for provisioning agent assigned_hardware_ips protection.

Regression tests for #532: provisioning agent must never write
assigned_hardware_ips.  That field is owned by the resource agent
(or platform agent for Jumpstarter).  Provisioning may read it but
never overwrites it with a derived controller-only fallback.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import paths
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
            patch.object(
                agent,
                "_get_ticket",
                new_callable=AsyncMock,
                return_value={
                    "custom_fields": {
                        "assigned_hardware_ips": {
                            "controller": "10.0.0.1",
                            "targets": ["10.0.0.1"],
                        },
                    },
                },
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
    async def test_ssh_hardware_ips_projects_onto_assigned_roles(self):
        """ssh_hardware_ips is derived by projecting hosts_provisioned
        onto assigned_hardware_ips role map from the resource agent."""
        agent = _make_agent()

        response = _make_response(
            {
                "provisioning_complete": True,
                "hosts_provisioned": ["10.0.0.5", "10.0.0.6"],
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
            patch.object(
                agent,
                "_get_ticket",
                new_callable=AsyncMock,
                return_value={
                    "custom_fields": {
                        "assigned_hardware_ips": {
                            "controller": "10.0.0.5",
                            "targets": ["10.0.0.5", "10.0.0.6"],
                        },
                    },
                },
            ),
        ):
            await agent._handle_completion("PERF-TEST", response)

            fields = mock_fields.call_args[0][1]
            assert fields["ssh_hardware_ips"] == {
                "controller": "10.0.0.5",
                "targets": ["10.0.0.5", "10.0.0.6"],
            }

    @pytest.mark.asyncio
    async def test_get_ticket_called_for_role_projection(self):
        """Provisioning reads the ticket to project hosts onto
        assigned_hardware_ips role map."""
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
                return_value={
                    "custom_fields": {
                        "assigned_hardware_ips": {
                            "controller": "10.0.0.1",
                            "targets": [],
                        },
                    },
                },
            ) as mock_get,
        ):
            await agent._handle_completion("PERF-TEST", response)
            mock_get.assert_called_once()


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


# ------------------------------------------------------------------
# ssh_hardware_ips role projection: controller-only provisioning
# on a multi-host ticket (#577)
# ------------------------------------------------------------------


class TestRoleProjection:
    @pytest.mark.asyncio
    async def test_controller_only_provisioned_multi_host(self):
        """When only the controller was provisioned on a 3-host
        ticket, ssh_hardware_ips reflects controller only."""
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
            patch.object(
                agent,
                "_get_ticket",
                new_callable=AsyncMock,
                return_value={
                    "custom_fields": {
                        "assigned_hardware_ips": {
                            "controller": "10.0.0.1",
                            "targets": ["10.0.0.2", "10.0.0.3"],
                        },
                    },
                },
            ),
        ):
            await agent._handle_completion("PERF-TEST", response)

            fields = mock_fields.call_args[0][1]
            assert fields["ssh_hardware_ips"] == {
                "controller": "10.0.0.1",
                "targets": [],
            }

    @pytest.mark.asyncio
    async def test_no_assigned_ips_falls_back_to_first_host(self):
        """When ticket has no assigned_hardware_ips, provisioning
        falls back to first provisioned host as controller."""
        agent = _make_agent()

        response = _make_response(
            {
                "provisioning_complete": True,
                "hosts_provisioned": ["10.0.0.99"],
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
            patch.object(
                agent,
                "_get_ticket",
                new_callable=AsyncMock,
                return_value={"custom_fields": {}},
            ),
        ):
            await agent._handle_completion("PERF-TEST", response)

            fields = mock_fields.call_args[0][1]
            assert fields["ssh_hardware_ips"] == {
                "controller": "10.0.0.99",
                "targets": ["10.0.0.99"],
            }


# ------------------------------------------------------------------
# Platform agent: assigned_hardware_ips scoping (#577)
# ------------------------------------------------------------------


class TestPlatformIPScoping:
    @pytest.mark.asyncio
    async def test_jumpstarter_writes_assigned_ips(self):
        """Platform agent writes assigned_hardware_ips only for
        jumpstarter provider."""
        from agents.platform.agent import PlatformAgent

        agent = PlatformAgent.__new__(PlatformAgent)
        agent.agent_name = "platform-agent"
        agent.llm = MagicMock()
        agent.store_url = "http://localhost:8090"
        agent.tools = []
        agent._tool_handlers = {}
        agent._events = None
        agent._mcp = None
        agent._stop_requested = False
        agent._client = AsyncMock()

        tc = MagicMock()
        tc.name = "submit_platform_result"
        tc.input = {
            "platform_ready": True,
            "hosts_provisioned": ["10.0.0.1", "10.0.0.2"],
        }
        response = MagicMock()
        response.text = ""
        response.tool_calls = [tc]

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
                "_get_ticket",
                new_callable=AsyncMock,
                return_value={
                    "custom_fields": {
                        "resource_provider": "jumpstarter",
                    },
                },
            ),
        ):
            await agent._handle_completion("PERF-TEST", response)

            fields = mock_fields.call_args[0][1]
            assert fields["assigned_hardware_ips"] == {
                "controller": "10.0.0.1",
                "targets": ["10.0.0.2"],
            }

    @pytest.mark.asyncio
    async def test_non_jumpstarter_skips_assigned_ips(self):
        """Platform agent does NOT write assigned_hardware_ips for
        non-jumpstarter providers (field owned by resource agent)."""
        from agents.platform.agent import PlatformAgent

        agent = PlatformAgent.__new__(PlatformAgent)
        agent.agent_name = "platform-agent"
        agent.llm = MagicMock()
        agent.store_url = "http://localhost:8090"
        agent.tools = []
        agent._tool_handlers = {}
        agent._events = None
        agent._mcp = None
        agent._stop_requested = False
        agent._client = AsyncMock()

        tc = MagicMock()
        tc.name = "submit_platform_result"
        tc.input = {
            "platform_ready": True,
            "hosts_provisioned": ["10.0.0.1"],
        }
        response = MagicMock()
        response.text = ""
        response.tool_calls = [tc]

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
                "_get_ticket",
                new_callable=AsyncMock,
                return_value={
                    "custom_fields": {
                        "resource_provider": "user_provided",
                    },
                },
            ),
        ):
            await agent._handle_completion("PERF-TEST", response)

            fields = mock_fields.call_args[0][1]
            assert "assigned_hardware_ips" not in fields

    @pytest.mark.asyncio
    async def test_provisioning_summary_saved_to_workspace(self, tmp_path, monkeypatch):
        """Verify that _handle_completion saves provisioning_summary.json to workspace."""
        monkeypatch.setattr(paths, "TICKET_DIR", tmp_path / "tickets")
        agent = _make_agent()

        response = _make_response(
            {
                "provisioning_complete": True,
                "hosts_provisioned": ["10.0.0.1", "10.0.0.2"],
                "harness_name": "crucible",
                "harness_version": "1.0",
                "configuration_applied": {
                    "10.0.0.1": ["tuned nic", "pinned irqs"],
                },
            }
        )

        with (
            patch.object(agent, "_update_fields", new_callable=AsyncMock),
            patch.object(agent, "_add_comment", new_callable=AsyncMock),
            patch.object(agent, "_transition_ticket", new_callable=AsyncMock),
            patch.object(
                agent,
                "_get_ticket",
                new_callable=AsyncMock,
                return_value={
                    "custom_fields": {
                        "assigned_hardware_ips": {
                            "controller": "10.0.0.1",
                            "targets": ["10.0.0.2"],
                        }
                    }
                },
            ),
        ):
            await agent._handle_completion("PERF-TEST-PROV", response)

        from providers.workspace.manager import WorkspaceManager

        mgr = WorkspaceManager(ticket_id="PERF-TEST-PROV")
        assert mgr.resolve_path("workspace://provisioning_summary.json").exists()
        files = mgr.list_files()
        assert len(files) == 1
        assert files[0]["filename"] == "provisioning_summary.json"

