"""Provisioning agent behaviour for kernel transition steps.

Covers the native kernel approval tools and the two code gates that keep a
kernel step from completing on the LLM's word alone: the submit-time
validator and the completion handler both derive the verification from the
reboot tool's checkpoint.
"""

from __future__ import annotations

import copy
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import paths  # noqa: F401
from agents.provisioning.agent import ProvisioningAgent
from agents.provisioning.kernel import new_intent
from providers.rhel_kernel import KernelSpec
from tests.test_provisioning_kernel_tools import KERNEL_B, kernel_step_ticket

HOSTS = ["10.0.0.1", "10.0.0.2"]


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
    agent._ticket_id = "PERF-TEST"
    agent._active_kernel_approval_id = None
    agent._active_kernel_approval = None
    agent.trace_context = None
    agent._update_fields = AsyncMock()
    agent._add_comment = AsyncMock()
    return agent


def _response(payload, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


def _submit_call(payload: dict) -> MagicMock:
    tc = MagicMock()
    tc.name = "submit_provisioning_result"
    tc.input = payload
    return tc


def _llm_response(payload: dict) -> MagicMock:
    response = MagicMock()
    response.text = ""
    response.tool_calls = [_submit_call(payload)]
    return response


def _ticket_with_intent(transition_state: str | None = None) -> tuple[dict, dict]:
    ticket = kernel_step_ticket()
    intent = new_intent("PERF-TEST", 2, HOSTS, KernelSpec(KERNEL_B), ["reboot"], {})
    ticket["custom_fields"]["kernel_change_intents"] = {
        "records": {intent["intent_id"]: intent}
    }
    if transition_state is not None:
        verified = transition_state == "verified"
        ticket["custom_fields"]["kernel_transitions"] = {
            "2": {
                "kernel": KERNEL_B,
                "kernel_spec": {"release": KERNEL_B, "package": "kernel"},
                "requested_hosts": HOSTS,
                "reboot_policy": "always",
                "addresses": {host: host for host in HOSTS},
                "state": transition_state,
                "hosts": {
                    host: {
                        "state": "verified" if verified else "reconnect_timeout",
                        "post_boot_id": f"boot-{host}" if verified else None,
                        "pre_boot_id": "previous-boot",
                        "reboot_issued": verified,
                        "reboot_pending": not verified,
                        "observed_kernel": KERNEL_B if verified else None,
                    }
                    for host in HOSTS
                },
            }
        }
    return ticket, intent


COMPLETE = {
    "provisioning_complete": True,
    "hosts_provisioned": HOSTS,
    "harness_name": "crucible",
    "harness_version": "1.0",
    "verification": {"status": "verified", "details": "LLM says so"},
}


class TestPresentKernelChangeForApproval:
    @pytest.mark.asyncio
    async def test_creates_kernel_change_approval_bound_to_intent(self):
        agent = _make_agent()
        ticket, intent = _ticket_with_intent()
        agent._get_ticket = AsyncMock(return_value=ticket)
        approval = {
            "approval_request_id": "apr-" + "1" * 32,
            "kind": "kernel_change",
            "validation_id": intent["intent_id"],
            "status": "pending",
        }
        agent._client.post = AsyncMock(return_value=_response(approval, 201))
        agent._request_human_input = AsyncMock(return_value="looks good, go ahead")
        agent._client.get = AsyncMock(
            return_value=_response({"approvals": [dict(approval)]})
        )

        reply = await agent._request_kernel_approval(
            intent_id=intent["intent_id"], summary="Install kernel B and reboot"
        )

        body = agent._client.post.await_args.kwargs["json"]
        assert body["kind"] == "kernel_change"
        assert body["validation_id"] == intent["intent_id"]
        assert body["presented_run_file_digest"] == intent["intent_digest"]
        assert body["execution_intent_digest"] == intent["intent_digest"]
        # Approval authority is server-owned; the agent must not rewrite it.
        agent._update_fields.assert_not_awaited()
        assert agent._active_kernel_approval_id == approval["approval_request_id"]
        assert "resolve_kernel_approval" in reply
        assert "looks good, go ahead" in reply
        # The user saw the kernel, hosts and actions, not just an id.
        comment = agent._add_comment.await_args.args[1]
        assert KERNEL_B in comment and "10.0.0.2" in comment and "reboot" in comment
        assert "no additional repository URL was requested" in comment
        assert "may be added as an additional DNF repository" not in comment
        assert "which signing key was used; both remain unconfirmed" in comment
        assert "reboot each host, even if it is already running" in comment

    @pytest.mark.asyncio
    async def test_approval_redacts_repository_credentials_query_and_fragment(self):
        agent = _make_agent()
        ticket, intent = _ticket_with_intent()
        intent["kernel"]["repo_url"] = (
            "https://user:secret@repo.example.test/kernel"
            "?token=query-secret#fragment-secret"
        )
        agent._get_ticket = AsyncMock(return_value=ticket)
        approval = {
            "approval_request_id": "apr-" + "5" * 32,
            "kind": "kernel_change",
            "validation_id": intent["intent_id"],
            "status": "pending",
        }
        agent._client.post = AsyncMock(return_value=_response(approval, 201))
        agent._request_human_input = AsyncMock(return_value="approve")
        agent._client.get = AsyncMock(
            return_value=_response({"approvals": [dict(approval, status="approved")]})
        )

        await agent._request_kernel_approval(
            intent_id=intent["intent_id"], summary="Install and verify the kernel"
        )

        comment = agent._add_comment.await_args.args[1]
        assert "https://repo.example.test/kernel" in comment
        assert "secret" not in comment
        assert "token" not in comment
        assert "fragment" not in comment
        assert "additional DNF repository" in comment
        assert "approved_repositories entry" in comment
        assert "configured GPG key and CA" in comment
        assert "installation is refused" in comment
        assert (
            "other enabled repositories follow their existing host" in comment.lower()
        )

    @pytest.mark.asyncio
    async def test_approval_shows_mixed_per_host_before_state_and_plan(self):
        agent = _make_agent()
        ticket, intent = _ticket_with_intent()
        intent["actions_required"] = ["install", "select_default", "reboot"]
        intent["inventory_before"] = {
            HOSTS[0]: {
                "state": "ok",
                "inventory": {
                    "running": "5.14.0-503.14.1.el9_5.x86_64",
                    "default_entry": "/boot/vmlinuz-5.14.0-503.14.1.el9_5.x86_64",
                    "installed": ["5.14.0-503.14.1.el9_5.x86_64"],
                },
                "requested": {"release": KERNEL_B, "state": "not_checked"},
            },
            HOSTS[1]: {
                "state": "ok",
                "inventory": {
                    "running": KERNEL_B,
                    "default_entry": f"/boot/vmlinuz-{KERNEL_B}",
                    "installed": ["5.14.0-503.14.1.el9_5.x86_64", KERNEL_B],
                },
            },
        }
        agent._get_ticket = AsyncMock(return_value=ticket)
        approval = {
            "approval_request_id": "apr-" + "6" * 32,
            "kind": "kernel_change",
            "validation_id": intent["intent_id"],
            "status": "pending",
        }
        agent._client.post = AsyncMock(return_value=_response(approval, 201))
        agent._request_human_input = AsyncMock(return_value="approve")
        agent._client.get = AsyncMock(
            return_value=_response({"approvals": [dict(approval, status="approved")]})
        )

        await agent._request_kernel_approval(
            intent_id=intent["intent_id"], summary="Install and verify kernel B"
        )

        comment = agent._add_comment.await_args.args[1]
        assert approval["approval_request_id"] in comment
        assert intent["intent_id"] in comment
        assert "Connection targets" in comment and HOSTS[0] in comment
        assert (
            f"{HOSTS[0]}:** running 5.14.0-503.14.1.el9_5.x86_64; default "
            "/boot/vmlinuz-5.14.0-503.14.1.el9_5.x86_64; target kernel absent; "
            "availability not checked"
        ) in comment
        assert (
            "Planned: attempt installation; availability not checked; set target as "
            "default boot kernel" in comment
        )
        assert (
            f"{HOSTS[1]}:** running {KERNEL_B}; default /boot/vmlinuz-{KERNEL_B}; "
            "target kernel present"
        ) in comment
        assert (
            "Planned: no installation needed; no default boot change needed" in comment
        )
        assert "structured approval action" in comment

    @pytest.mark.asyncio
    async def test_unknown_intent_is_rejected_without_posting(self):
        agent = _make_agent()
        ticket, _ = _ticket_with_intent()
        agent._get_ticket = AsyncMock(return_value=ticket)
        reply = await agent._request_kernel_approval(
            intent_id="kci-" + "0" * 32, summary=None
        )
        assert reply.startswith("Approval rejected")
        agent._client.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_structured_approval_short_circuits_interpretation(self):
        agent = _make_agent()
        ticket, intent = _ticket_with_intent()
        agent._get_ticket = AsyncMock(return_value=ticket)
        approval = {
            "approval_request_id": "apr-" + "2" * 32,
            "kind": "kernel_change",
            "validation_id": intent["intent_id"],
            "status": "pending",
        }
        agent._client.post = AsyncMock(return_value=_response(approval, 201))
        agent._request_human_input = AsyncMock(return_value="")
        agent._client.get = AsyncMock(
            return_value=_response({"approvals": [dict(approval, status="approved")]})
        )
        reply = await agent._request_kernel_approval(
            intent_id=intent["intent_id"], summary=None
        )
        assert "already granted" in reply
        assert approval["approval_request_id"] in reply


class TestResolveKernelApproval:
    @pytest.mark.asyncio
    async def test_resolve_posts_decision(self):
        agent = _make_agent()
        agent._active_kernel_approval_id = "apr-" + "3" * 32
        agent._active_kernel_approval = {
            "validation_id": "kci-x",
            "presented_run_file_digest": "d" * 64,
            "execution_intent_digest": "d" * 64,
        }
        agent._client.post = AsyncMock(return_value=_response({}))
        reply = await agent._resolve_kernel_approval(decision="approved", reason=None)
        url = agent._client.post.await_args.args[0]
        assert url.endswith(f"/approvals/{agent._active_kernel_approval_id}/resolve")
        body = agent._client.post.await_args.kwargs["json"]
        assert body["decision"] == "approved"
        assert body["validation_id"] == "kci-x"
        assert "reboot_hosts_and_verify" in reply

    @pytest.mark.asyncio
    async def test_rejected_decision_says_do_not_reboot(self):
        agent = _make_agent()
        agent._active_kernel_approval_id = "apr-" + "4" * 32
        agent._active_kernel_approval = {}
        agent._client.post = AsyncMock(return_value=_response({}))
        reply = await agent._resolve_kernel_approval(decision="rejected", reason="no")
        assert "Do not change kernels or reboot" in reply

    @pytest.mark.asyncio
    async def test_no_active_request(self):
        agent = _make_agent()
        reply = await agent._resolve_kernel_approval(decision="approved")
        assert "No active kernel change approval" in reply
        agent._client.post.assert_not_called()


class TestSubmitGate:
    @pytest.mark.asyncio
    async def test_complete_blocked_when_transition_not_verified(self):
        agent = _make_agent()
        ticket, _ = _ticket_with_intent("partial_failure")
        agent._get_ticket = AsyncMock(return_value=ticket)
        error = await agent._validate_submit_call("PERF-TEST", _submit_call(COMPLETE))
        assert error is not None
        assert "kernel transition step" in error
        assert "reboot_hosts_and_verify" in error
        fields = agent._update_fields.await_args.args[1]
        assert fields["provisioning_complete"] is False
        assert fields["provisioning_verification"]["status"] == "not_verified"

    @pytest.mark.asyncio
    async def test_complete_blocked_when_no_checkpoint_at_all(self):
        agent = _make_agent()
        ticket, _ = _ticket_with_intent(None)
        agent._get_ticket = AsyncMock(return_value=ticket)
        error = await agent._validate_submit_call("PERF-TEST", _submit_call(COMPLETE))
        assert error is not None and "kernel transition step" in error

    @pytest.mark.asyncio
    async def test_complete_allowed_when_verified(self):
        agent = _make_agent()
        ticket, _ = _ticket_with_intent("verified")
        agent._get_ticket = AsyncMock(return_value=ticket)
        error = await agent._validate_submit_call("PERF-TEST", _submit_call(COMPLETE))
        assert error is None

    @pytest.mark.asyncio
    async def test_incomplete_submission_never_consults_checkpoint(self):
        agent = _make_agent()
        ticket, _ = _ticket_with_intent(None)
        agent._get_ticket = AsyncMock(return_value=ticket)
        agent._kernel_step_verification = AsyncMock(
            side_effect=AssertionError("gate must not run for incomplete results")
        )
        payload = dict(
            COMPLETE,
            provisioning_complete=False,
            notes="reboot of 10.0.0.2 timed out; waiting for guidance",
        )
        error = await agent._validate_submit_call("PERF-TEST", _submit_call(payload))
        assert error is None

    @pytest.mark.asyncio
    async def test_non_kernel_step_unaffected(self):
        agent = _make_agent()
        ticket = kernel_step_ticket(current_step=1)
        agent._get_ticket = AsyncMock(return_value=ticket)
        error = await agent._validate_submit_call("PERF-TEST", _submit_call(COMPLETE))
        assert error is None


class TestCompletionDerivesVerification:
    @pytest.mark.asyncio
    async def test_verified_checkpoint_writes_details_from_checkpoint(self):
        agent = _make_agent()
        ticket, _ = _ticket_with_intent("verified")
        agent._get_ticket = AsyncMock(return_value=copy.deepcopy(ticket))
        agent._plan_controls_next_transition = AsyncMock(return_value=True)
        agent._transition_ticket = AsyncMock()

        await agent._handle_completion("PERF-TEST", _llm_response(COMPLETE))

        fields = agent._update_fields.await_args_list[0].args[1]
        assert fields["provisioning_complete"] is True
        verification = fields["provisioning_verification"]
        assert verification["status"] == "verified"
        assert KERNEL_B in verification["details"]
        assert "LLM says so" not in verification["details"]
        agent._transition_ticket.assert_not_called()

    @pytest.mark.asyncio
    async def test_completion_redacts_repo_url_from_ticket_and_workspace_summary(self):
        ticket, _ = _ticket_with_intent("verified")
        repo_url = (
            "https://user:completion-secret@repo.example.test/kernel"
            "?token=query-secret#fragment-secret"
        )
        ticket["custom_fields"]["execution_plan"]["steps"][2]["params"]["kernel"][
            "repo_url"
        ] = repo_url
        spec = KernelSpec.parse(
            ticket["custom_fields"]["execution_plan"]["steps"][2]["params"]["kernel"]
        )
        ticket["custom_fields"]["kernel_transitions"]["2"]["kernel_spec"] = (
            spec.to_dict()
        )

        agent = _make_agent()
        agent._get_ticket = AsyncMock(return_value=copy.deepcopy(ticket))
        agent._plan_controls_next_transition = AsyncMock(return_value=True)
        agent._transition_ticket = AsyncMock()
        response = _llm_response(
            dict(
                COMPLETE,
                configuration_applied={
                    "kernel_transition": {
                        "step_id": 2,
                        "kernel": spec.to_dict(),
                        "actions": ["install", "select_default", "reboot"],
                    }
                },
            )
        )

        with patch("providers.workspace.manager.WorkspaceManager.save_file") as save:
            await agent._handle_completion("PERF-TEST", response)

        fields = agent._update_fields.await_args_list[0].args[1]
        projected = fields["configuration_applied"]["kernel_transition"]["kernel"]
        assert projected["repo_url"] == "https://repo.example.test/kernel"
        assert "completion-secret" not in str(fields)
        assert "query-secret" not in str(fields)
        assert "fragment-secret" not in str(fields)

        workspace_summary = json.loads(save.call_args.args[1])
        assert (
            workspace_summary["configuration_applied"]["kernel_transition"]["kernel"][
                "repo_url"
            ]
            == "https://repo.example.test/kernel"
        )
        comment = agent._add_comment.await_args.args[1]
        assert "https://repo.example.test/kernel" in comment
        assert "completion-secret" not in comment
        assert "query-secret" not in comment
        assert "fragment-secret" not in comment

    @pytest.mark.asyncio
    async def test_unverified_checkpoint_forces_incomplete_and_pauses(self):
        agent = _make_agent()
        ticket, _ = _ticket_with_intent("partial_failure")
        agent._get_ticket = AsyncMock(return_value=copy.deepcopy(ticket))
        agent._plan_controls_next_transition = AsyncMock(return_value=True)
        agent._transition_ticket = AsyncMock()

        await agent._handle_completion("PERF-TEST", _llm_response(COMPLETE))

        fields = agent._update_fields.await_args_list[0].args[1]
        assert fields["provisioning_complete"] is False
        assert fields["provisioning_verification"]["status"] == "not_verified"
        agent._transition_ticket.assert_awaited_once()
        assert (
            agent._transition_ticket.await_args.args[1] == "awaiting_customer_guidance"
        )

    @pytest.mark.asyncio
    async def test_single_ticket_read_serves_gate_and_projection(self):
        agent = _make_agent()
        ticket, _ = _ticket_with_intent("verified")
        agent._get_ticket = AsyncMock(return_value=copy.deepcopy(ticket))
        agent._plan_controls_next_transition = AsyncMock(return_value=True)
        agent._transition_ticket = AsyncMock()

        await agent._handle_completion("PERF-TEST", _llm_response(COMPLETE))

        agent._get_ticket.assert_awaited_once()
