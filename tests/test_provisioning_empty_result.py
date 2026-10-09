"""Tests for the provisioning submission contract in issue #1012."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import paths  # noqa: F401
from agents.provisioning.agent import ProvisioningAgent
from orchestrator.handoff import check_handoff

VERIFICATION = {
    "status": "verified",
    "details": "verify_harness_install passed on all provisioned hosts",
}


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


def _valid_complete_result(**overrides) -> dict:
    result = {
        "provisioning_complete": True,
        "hosts_provisioned": ["10.0.0.1"],
        "harness_name": "crucible",
        "harness_version": "1.0",
        "verification": VERIFICATION,
    }
    result.update(overrides)
    return result


class TestProvisioningSubmitValidation:
    @pytest.mark.asyncio
    async def test_empty_hosts_are_returned_as_retryable_tool_error(self):
        agent = _make_agent()
        response = _make_response(_valid_complete_result(hosts_provisioned=[]))
        agent._update_fields = AsyncMock()

        error = await agent._validate_submit_call("TICKET-1", response.tool_calls[0])

        assert error is not None
        assert "at least one provisioned host" in error
        agent._update_fields.assert_awaited_once()
        fields = agent._update_fields.await_args.args[1]
        assert fields["provisioning_complete"] is False
        assert fields["provisioning_verification"]["status"] == "not_verified"
        assert "hosts_provisioned" not in fields
        assert "harness_name" not in fields
        assert "assigned_hardware_ips" not in fields
        assert "ssh_hardware_ips" not in fields
        assert "ssh_user" not in fields
        assert "ssh_key_path" not in fields

    @pytest.mark.asyncio
    async def test_blank_host_and_missing_verification_are_rejected(self):
        agent = _make_agent()
        agent._update_fields = AsyncMock()

        for result in (
            _valid_complete_result(hosts_provisioned=[""]),
            {
                "provisioning_complete": True,
                "hosts_provisioned": ["10.0.0.1"],
            },
        ):
            response = _make_response(result)
            error = await agent._validate_submit_call(
                "TICKET-2", response.tool_calls[0]
            )
            assert error is not None

        assert agent._update_fields.await_count == 2

    @pytest.mark.asyncio
    async def test_wrong_field_types_are_rejected(self):
        agent = _make_agent()
        agent._update_fields = AsyncMock()
        response = _make_response(
            {
                "provisioning_complete": "true",
                "hosts_provisioned": ["10.0.0.1"],
                "verification": VERIFICATION,
                "configuration_applied": [],
            }
        )

        error = await agent._validate_submit_call("TICKET-3", response.tool_calls[0])

        assert error is not None
        assert "provisioning_complete must be a boolean" in error
        assert "configuration_applied must be an object" in error

    @pytest.mark.asyncio
    async def test_non_object_input_is_rejected_for_retry(self):
        agent = _make_agent()
        agent._update_fields = AsyncMock()
        response = _make_response(None)

        error = await agent._validate_submit_call("TICKET-3A", response.tool_calls[0])

        assert error is not None
        assert "must be a JSON object" in error

    @pytest.mark.asyncio
    async def test_ssh_identity_fields_are_not_model_writable(self):
        agent = _make_agent()
        agent._update_fields = AsyncMock()
        response = _make_response(
            _valid_complete_result(
                ssh_hardware_ips={"controller": "192.0.2.25", "targets": []},
                ssh_user="attacker",
                ssh_key_path="/tmp/other-key",
            )
        )

        error = await agent._validate_submit_call("TICKET-3B", response.tool_calls[0])

        assert error is not None
        assert "unsupported fields" in error

    @pytest.mark.asyncio
    async def test_incomplete_result_requires_actionable_notes(self):
        agent = _make_agent()
        agent._update_fields = AsyncMock()
        response = _make_response(
            {"provisioning_complete": False, "hosts_provisioned": []}
        )

        error = await agent._validate_submit_call("TICKET-4", response.tool_calls[0])

        assert error is not None
        assert "actionable, non-empty notes" in error

    @pytest.mark.asyncio
    async def test_incomplete_result_with_notes_is_valid(self):
        agent = _make_agent()
        response = _make_response(
            {
                "provisioning_complete": False,
                "hosts_provisioned": [],
                "notes": "SSH access failed; provide a reachable host.",
            }
        )

        assert (
            await agent._validate_submit_call("TICKET-5", response.tool_calls[0])
            is None
        )

    @pytest.mark.asyncio
    async def test_completed_hosts_must_match_ticket_allocation(self):
        agent = _make_agent()
        agent._update_fields = AsyncMock()
        agent._get_ticket = AsyncMock(
            return_value={
                "custom_fields": {
                    "assigned_hardware_ips": {
                        "controller": "10.0.0.1",
                        "targets": ["10.0.0.2"],
                    }
                }
            }
        )

        allowed_response = _make_response(_valid_complete_result())
        assert (
            await agent._validate_submit_call(
                "TICKET-ALLOCATED", allowed_response.tool_calls[0]
            )
            is None
        )

        unallocated_response = _make_response(
            _valid_complete_result(hosts_provisioned=["192.0.2.25"])
        )
        error = await agent._validate_submit_call(
            "TICKET-ALLOCATED", unallocated_response.tool_calls[0]
        )
        assert error is not None
        assert "not allocated to this ticket" in error
        incomplete_unallocated = _make_response(
            _valid_complete_result(
                provisioning_complete=False,
                hosts_provisioned=["192.0.2.25"],
                notes="The reported host did not match ticket allocation.",
            )
        )
        error = await agent._validate_submit_call(
            "TICKET-ALLOCATED", incomplete_unallocated.tool_calls[0]
        )
        assert error is not None
        assert "not allocated to this ticket" in error
        assert agent._update_fields.await_count == 2


class TestProvisioningCompletion:
    @pytest.mark.asyncio
    async def test_jumpstarter_auto_completion_records_verification(self):
        agent = _make_agent()
        agent._update_fields = AsyncMock()
        agent._add_comment = AsyncMock()
        agent._plan_controls_next_transition = AsyncMock(return_value=False)
        agent._transition_ticket = AsyncMock()

        await agent._auto_complete_jumpstarter(
            "TICKET-JS",
            {
                "hosts_provisioned": ["10.0.0.8"],
                "directives": {"harness": "arcaflow-plugins"},
            },
        )

        fields = agent._update_fields.await_args.args[1]
        assert fields["provisioning_complete"] is True
        assert fields["provisioning_verification"]["status"] == "verified"
        agent._transition_ticket.assert_awaited_once_with(
            "TICKET-JS",
            "executing_benchmark",
            comment="Provisioning complete (auto)",
        )

    @pytest.mark.asyncio
    async def test_valid_result_persists_verification_and_advances(self):
        agent = _make_agent()
        response = _make_response(_valid_complete_result())

        with (
            patch.object(
                agent, "_update_fields", new_callable=AsyncMock
            ) as mock_fields,
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
            patch("providers.workspace.manager.WorkspaceManager"),
        ):
            await agent._handle_completion("TICKET-6", response)

        fields = mock_fields.await_args.args[1]
        assert fields["provisioning_complete"] is True
        assert fields["provisioning_verification"] == VERIFICATION
        mock_transition.assert_awaited_once()
        assert mock_transition.await_args.args[1] == "executing_benchmark"

    @pytest.mark.asyncio
    async def test_incomplete_result_waits_for_guidance(self):
        agent = _make_agent()
        response = _make_response(
            {
                "provisioning_complete": False,
                "hosts_provisioned": [],
                "notes": "Host is unreachable; provide working SSH access.",
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
            patch("providers.workspace.manager.WorkspaceManager"),
        ):
            await agent._handle_completion("TICKET-7", response)

        fields = mock_fields.await_args.args[1]
        assert fields["provisioning_complete"] is False
        assert "hosts_provisioned" not in fields
        assert fields["provisioning_verification"]["status"] == "incomplete"
        assert "Incomplete" in mock_comment.await_args.args[1]
        assert mock_transition.await_args.args[1] == "awaiting_customer_guidance"


class TestProvisioningHandoff:
    def test_complete_result_requires_valid_hosts_and_verification(self):
        valid_fields = {
            "provisioning_complete": True,
            "hosts_provisioned": ["10.0.0.1"],
            "assigned_hardware_ips": {
                "controller": "10.0.0.1",
                "targets": [],
            },
            "provisioning_verification": VERIFICATION,
        }
        assert check_handoff(
            "executing_benchmark", {"custom_fields": valid_fields}
        ) == (
            True,
            "",
        )

        for invalid_fields in (
            {**valid_fields, "hosts_provisioned": [""]},
            {**valid_fields, "hosts_provisioned": ["192.0.2.25"]},
            {
                key: value
                for key, value in valid_fields.items()
                if key != "assigned_hardware_ips"
            },
            {**valid_fields, "provisioning_verification": {"status": "failed"}},
            {
                key: value
                for key, value in valid_fields.items()
                if key != "provisioning_verification"
            },
        ):
            ok, _reason = check_handoff(
                "executing_benchmark", {"custom_fields": invalid_fields}
            )
            assert not ok
