"""The deterministic kernel transition step of the provisioning agent.

A kernel step is driven by fixed tool calls, never by model reasoning:
inventory -> prepare -> approval pause -> the intent's actions -> submit.
These tests script the MCP tool results and assert the call order, the
strict reply interpretation, and that every failure pauses without
submitting.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import paths  # noqa: F401
from agents.provisioning.agent import ProvisioningAgent
from tests.test_provisioning_kernel_tools import KERNEL_B, kernel_step_ticket

HOSTS = ["10.0.0.1", "10.0.0.2"]


class ScriptedMCP:
    """call_tool(name, arguments) -> JSON string, from a per-tool script."""

    def __init__(self, script: dict[str, object]) -> None:
        self.script = script
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name: str, arguments: dict, **_kw) -> str:
        self.calls.append((name, dict(arguments)))
        result = self.script[name]
        if callable(result):
            result = result(arguments)
        return json.dumps(result)

    async def disconnect(self) -> None:
        return None


def ok_inventory(hosts, requested_state="available"):
    return {
        "results": {
            h: {
                "host": h,
                "state": "ok",
                "inventory": {"running": "5.14.0-1.el9.x86_64"},
                "requested": {"release": KERNEL_B, "state": requested_state},
            }
            for h in hosts
        }
    }


def _agent(ticket, reply="The user replied: approve\n\nInterpret this."):
    agent = ProvisioningAgent.__new__(ProvisioningAgent)
    agent.agent_name = "provisioning-agent"
    agent.store_url = "http://localhost:8090"
    agent._ticket_id = ticket["id"]
    agent._skill_provider = None
    agent._active_kernel_approval_id = None
    agent._add_comment = AsyncMock()
    agent._transition_ticket = AsyncMock()
    agent._get_ticket = AsyncMock(return_value=ticket)
    agent._handle_completion = AsyncMock()
    agent._resolve_kernel_approval = AsyncMock()

    async def _approval(*, intent_id, summary):
        agent._active_kernel_approval_id = "apr-" + "a" * 32
        return reply

    agent._request_kernel_approval = AsyncMock(side_effect=_approval)
    agent._effective_harness = MagicMock(return_value="crucible")
    return agent


def full_script(actions=("install", "select_default", "reboot")):
    return {
        "get_kernel_inventory": ok_inventory(HOSTS),
        "prepare_kernel_change": {
            "status": "ok",
            "intent_id": "kci-" + "1" * 32,
            "actions_required": list(actions),
        },
        "install_kernel": {
            "results": {h: {"host": h, "state": "installed"} for h in HOSTS}
        },
        "select_default_kernel": {
            "results": {h: {"host": h, "state": "ok"} for h in HOSTS}
        },
        "reboot_hosts_and_verify": {
            "status": "verified",
            "hosts": {h: {"state": "verified"} for h in HOSTS},
        },
    }


class TestDeterministicKernelStep:
    def test_mode_detection(self):
        cf = kernel_step_ticket()["custom_fields"]
        assert ProvisioningAgent._kernel_step_is_deterministic(cf) is True
        cf["directives"] = {"kernel_step_mode": "llm"}
        assert ProvisioningAgent._kernel_step_is_deterministic(cf) is False
        assert (
            ProvisioningAgent._kernel_step_is_deterministic(
                kernel_step_ticket(current_step=1)["custom_fields"]
            )
            is False
        )

    @pytest.mark.asyncio
    async def test_happy_path_calls_tools_in_order_and_submits(self):
        ticket = kernel_step_ticket()
        agent = _agent(ticket)
        mcp = ScriptedMCP(full_script())

        await agent._run_kernel_step(ticket["id"], ticket, mcp)

        names = [n for n, _ in mcp.calls]
        assert names == [
            "get_kernel_inventory",
            "prepare_kernel_change",
            "install_kernel",
            "select_default_kernel",
            "reboot_hosts_and_verify",
        ]
        for name, args in mcp.calls:
            assert args["hosts"] == HOSTS
        assert mcp.calls[1][1]["kernel"] == {"release": KERNEL_B, "package": "kernel"}
        approval_id = "apr-" + "a" * 32
        for name, args in mcp.calls[2:]:
            assert args["approval_request_id"] == approval_id
        assert mcp.calls[4][1]["expected_kernel"] == KERNEL_B
        assert mcp.calls[4][1]["reboot_policy"] == "always"
        agent._resolve_kernel_approval.assert_awaited_once()
        assert (
            agent._resolve_kernel_approval.await_args.kwargs["decision"] == "approved"
        )
        agent._handle_completion.assert_awaited_once()
        submitted = agent._handle_completion.await_args.args[1].tool_calls[0].input
        assert submitted["provisioning_complete"] is True
        assert submitted["hosts_provisioned"] == HOSTS
        assert submitted["configuration_applied"]["kernel_transition"]["intent_id"]
        agent._transition_ticket.assert_not_called()

    @pytest.mark.asyncio
    async def test_actions_follow_the_intent(self):
        ticket = kernel_step_ticket()
        agent = _agent(ticket)
        mcp = ScriptedMCP(full_script(actions=("reboot",)))
        await agent._run_kernel_step(ticket["id"], ticket, mcp)
        names = [n for n, _ in mcp.calls]
        assert names == [
            "get_kernel_inventory",
            "prepare_kernel_change",
            "reboot_hosts_and_verify",
        ]
        agent._handle_completion.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_structured_approval_already_granted(self):
        ticket = kernel_step_ticket()
        agent = _agent(
            ticket, reply="Approval already granted for request apr-x; pass it on."
        )
        mcp = ScriptedMCP(full_script(actions=("reboot",)))
        await agent._run_kernel_step(ticket["id"], ticket, mcp)
        agent._resolve_kernel_approval.assert_not_called()
        agent._handle_completion.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unclear_reply_is_a_rejection(self):
        ticket = kernel_step_ticket()
        agent = _agent(
            ticket, reply="The user replied: hmm, what does this do?\n\nInterpret."
        )
        mcp = ScriptedMCP(full_script())
        await agent._run_kernel_step(ticket["id"], ticket, mcp)
        names = [n for n, _ in mcp.calls]
        assert names == ["get_kernel_inventory", "prepare_kernel_change"]
        assert (
            agent._resolve_kernel_approval.await_args.kwargs["decision"] == "rejected"
        )
        agent._handle_completion.assert_not_called()
        agent._transition_ticket.assert_awaited_once()
        assert (
            agent._transition_ticket.await_args.args[1] == "awaiting_customer_guidance"
        )

    @pytest.mark.asyncio
    async def test_inventory_problem_pauses_before_prepare(self):
        ticket = kernel_step_ticket()
        agent = _agent(ticket)
        script = full_script()
        script["get_kernel_inventory"]["results"]["10.0.0.2"] = {
            "host": "10.0.0.2",
            "state": "unreachable",
            "error": "no route",
        }
        mcp = ScriptedMCP(script)
        await agent._run_kernel_step(ticket["id"], ticket, mcp)
        assert [n for n, _ in mcp.calls] == ["get_kernel_inventory"]
        agent._request_kernel_approval.assert_not_called()
        agent._transition_ticket.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_kernel_not_found_pauses_with_repo_hint(self):
        ticket = kernel_step_ticket()
        agent = _agent(ticket)
        script = full_script()
        script["get_kernel_inventory"] = ok_inventory(
            HOSTS, requested_state="not_found"
        )
        mcp = ScriptedMCP(script)
        await agent._run_kernel_step(ticket["id"], ticket, mcp)
        assert [n for n, _ in mcp.calls] == ["get_kernel_inventory"]
        comment = agent._add_comment.await_args.args[1]
        assert "repository" in comment

    @pytest.mark.asyncio
    async def test_prepare_refusal_pauses(self):
        ticket = kernel_step_ticket()
        agent = _agent(ticket)
        script = full_script()
        script["prepare_kernel_change"] = {
            "status": "rejected",
            "reason_code": "host_set_mismatch",
        }
        mcp = ScriptedMCP(script)
        await agent._run_kernel_step(ticket["id"], ticket, mcp)
        agent._request_kernel_approval.assert_not_called()
        agent._transition_ticket.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_install_failure_stops_before_select(self):
        ticket = kernel_step_ticket()
        agent = _agent(ticket)
        script = full_script()
        script["install_kernel"] = {
            "results": {
                "10.0.0.1": {"host": "10.0.0.1", "state": "installed"},
                "10.0.0.2": {"host": "10.0.0.2", "state": "install_failed"},
            }
        }
        mcp = ScriptedMCP(script)
        await agent._run_kernel_step(ticket["id"], ticket, mcp)
        names = [n for n, _ in mcp.calls]
        assert names[-1] == "install_kernel"
        agent._handle_completion.assert_not_called()
        agent._transition_ticket.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reboot_pause_is_not_doubled(self):
        """The reboot tool already paused the ticket; the runner only comments."""
        ticket = kernel_step_ticket()
        agent = _agent(ticket)
        paused = dict(ticket, status="awaiting_customer_guidance")
        script = full_script(actions=("reboot",))
        script["reboot_hosts_and_verify"] = {
            "status": "paused",
            "reason_code": "reconnect_timeout",
            "stopped_at": "10.0.0.1",
        }
        mcp = ScriptedMCP(script)
        agent._get_ticket = AsyncMock(return_value=paused)
        await agent._run_kernel_step(ticket["id"], ticket, mcp)
        agent._handle_completion.assert_not_called()
        agent._transition_ticket.assert_not_called()
        assert "reconnect_timeout" in agent._add_comment.await_args.args[1]

    @pytest.mark.asyncio
    async def test_reboot_policy_directive_is_forwarded(self):
        ticket = kernel_step_ticket()
        ticket["custom_fields"]["directives"] = {"kernel_reboot_policy": "if_needed"}
        agent = _agent(ticket)
        mcp = ScriptedMCP(full_script(actions=("reboot",)))
        await agent._run_kernel_step(ticket["id"], ticket, mcp)
        assert mcp.calls[-1][1]["reboot_policy"] == "if_needed"
