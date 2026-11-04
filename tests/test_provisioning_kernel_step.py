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


def _agent(ticket, reply="The user replied: approve\n\nInterpret this response."):
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
            "hosts": {
                h: {"state": "verified", "observed_kernel": KERNEL_B} for h in HOSTS
            },
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
        assert mcp.calls[1][1]["kernel"] == KERNEL_B
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
    async def test_mapped_hosts_use_canonical_tool_keys_and_original_labels(
        self, monkeypatch
    ):
        import socket

        ticket = kernel_step_ticket(
            kernel={
                "release": KERNEL_B,
                "package": "kernel",
                "repo_url": "https://repo.example.test/kernel",
            },
            targets=["sut1.lab", "sut2.lab"],
        )
        ticket["custom_fields"]["assigned_hardware_ips"]["controller"] = "orch.lab"
        ticket["custom_fields"]["resource_provider_metadata"] = {
            "ip_mapping": {
                "orch.lab": "10.0.0.100",
                "10.0.0.100": "10.0.0.100",
                "sut1.lab": HOSTS[0],
                "sut2.lab": HOSTS[1],
            }
        }

        def resolve(host, port, *_args, **_kwargs):
            addresses = {
                "orch.lab": "10.0.0.100",
                "10.0.0.100": "10.0.0.100",
                "sut1.lab": HOSTS[0],
                "sut2.lab": HOSTS[1],
                HOSTS[0]: HOSTS[0],
                HOSTS[1]: HOSTS[1],
            }
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addresses[host], 0))]

        monkeypatch.setattr(socket, "getaddrinfo", resolve)
        monkeypatch.setattr(socket, "gethostname", lambda: "orch.lab")
        monkeypatch.setattr(socket, "getfqdn", lambda: "orch.lab")
        agent = _agent(ticket)
        script = full_script()
        script["run_repo_cleanup"] = {
            "status": "ok",
            "results": {host: {"state": "clean"} for host in HOSTS},
        }
        mcp = ScriptedMCP(script)

        await agent._run_kernel_step(ticket["id"], ticket, mcp)

        for name, args in mcp.calls:
            if name in {
                "get_kernel_inventory",
                "prepare_kernel_change",
                "install_kernel",
                "select_default_kernel",
                "reboot_hosts_and_verify",
                "run_repo_cleanup",
            }:
                assert args["hosts"] == HOSTS
        comments = "\n".join(
            call.args[1] for call in agent._add_comment.await_args_list
        )
        assert "sut1.lab:** kernel" in comments
        assert "sut2.lab:** kernel" in comments
        assert "sut1.lab:** temporary repository entry removed" in comments
        agent._handle_completion.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_ticket_comments_report_each_host_outcome(self):
        ticket = kernel_step_ticket()
        agent = _agent(ticket)
        script = full_script()
        script["install_kernel"]["results"] = {
            HOSTS[0]: {"host": HOSTS[0], "state": "installed"},
            HOSTS[1]: {"host": HOSTS[1], "state": "already_installed"},
        }
        script["select_default_kernel"]["results"] = {
            HOSTS[0]: {
                "host": HOSTS[0],
                "state": "ok",
                "after": f"/boot/vmlinuz-{KERNEL_B}",
            },
            HOSTS[1]: {"host": HOSTS[1], "state": "already_default"},
        }
        script["reboot_hosts_and_verify"]["hosts"] = {
            HOSTS[0]: {"state": "verified", "observed_kernel": KERNEL_B},
            HOSTS[1]: {"state": "already_verified", "observed_kernel": KERNEL_B},
        }
        mcp = ScriptedMCP(script)

        await agent._run_kernel_step(ticket["id"], ticket, mcp)

        comments = "\n".join(
            call.args[1] for call in agent._add_comment.await_args_list
        )
        assert f"{HOSTS[0]}:** kernel {KERNEL_B} installed" in comments
        assert f"{HOSTS[1]}:** kernel {KERNEL_B} was already present" in comments
        assert (
            f"{HOSTS[0]}:** default boot entry set to /boot/vmlinuz-{KERNEL_B}"
            in comments
        )
        assert f"{HOSTS[1]}:** requested kernel was already the default" in comments
        assert f"{HOSTS[0]}:** running kernel {KERNEL_B} was verified" in comments

    @pytest.mark.asyncio
    async def test_requested_repo_is_redacted_and_cleanup_is_reported(self):
        ticket = kernel_step_ticket(
            kernel={
                "release": KERNEL_B,
                "package": "kernel",
                "repo_url": (
                    "https://user:secret@repo.example.test/kernel"
                    "?token=query-secret#fragment-secret"
                ),
            }
        )
        agent = _agent(ticket)
        script = full_script()
        script["run_repo_cleanup"] = {
            "status": "ok",
            "results": {host: {"state": "clean"} for host in HOSTS},
        }
        mcp = ScriptedMCP(script)

        await agent._run_kernel_step(ticket["id"], ticket, mcp)

        comments = "\n".join(
            call.args[1] for call in agent._add_comment.await_args_list
        )
        assert "https://repo.example.test/kernel" in comments
        assert "secret" not in comments
        assert "token" not in comments
        assert "fragment" not in comments
        assert "Actual package source and signing key:** not confirmed" in comments
        assert "Temporary repository cleanup confirmed" in comments

    @pytest.mark.asyncio
    async def test_partial_cleanup_reports_confirmed_and_uncertain_hosts(self):
        ticket = kernel_step_ticket(
            kernel={
                "release": KERNEL_B,
                "package": "kernel",
                "repo_url": "https://repo.example.test/kernel?token=secret",
            }
        )
        agent = _agent(ticket)
        script = full_script()
        script["run_repo_cleanup"] = {
            "status": "incomplete",
            "results": {
                HOSTS[0]: {"state": "clean"},
                HOSTS[1]: {
                    "state": "cleanup_failed",
                    "error": "https://user:secret@repo.example.test/?token=secret",
                },
            },
        }
        mcp = ScriptedMCP(script)

        await agent._run_kernel_step(ticket["id"], ticket, mcp)

        comment_list = [call.args[1] for call in agent._add_comment.await_args_list]
        comments = "\n".join(comment_list)
        assert "Temporary repository cleanup not confirmed" in comments
        assert f"{HOSTS[0]}:** temporary repository entry removed" in comments
        assert f"{HOSTS[1]}:** removal not confirmed (cleanup failed)" in comments
        assert "secret" not in comments
        assert "may remain" in comments
        pause_comment = next(
            comment
            for comment in comment_list
            if "Kernel change step stopped" in comment
        )
        assert HOSTS[0] not in pause_comment
        assert HOSTS[1] in pause_comment
        agent._handle_completion.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cleanup_is_reported_pending_when_later_kernel_steps_remain(self):
        ticket = kernel_step_ticket(
            kernel={
                "release": KERNEL_B,
                "package": "kernel",
                "repo_url": "https://repo.example.test/kernel",
            }
        )
        ticket["custom_fields"]["execution_plan"]["steps"].append(
            {
                "id": 3,
                "agent_type": "provision",
                "params": {"label": "another kernel", "kernel": KERNEL_B},
            }
        )
        agent = _agent(ticket)
        mcp = ScriptedMCP(full_script())

        await agent._run_kernel_step(ticket["id"], ticket, mcp)

        comments = "\n".join(
            call.args[1] for call in agent._add_comment.await_args_list
        )
        assert "Temporary repository cleanup pending" in comments
        assert "Cleanup was not attempted at this step" in comments
        assert not any(name == "run_repo_cleanup" for name, _ in mcp.calls)

    @pytest.mark.asyncio
    async def test_failed_first_ab_step_cleans_repo_before_pausing(self):
        ticket = kernel_step_ticket(
            kernel={
                "release": KERNEL_B,
                "package": "kernel",
                "repo_url": "https://repo.example.test/kernel",
            }
        )
        ticket["custom_fields"]["execution_plan"]["steps"].append(
            {
                "id": 3,
                "agent_type": "provision",
                "params": {"label": "kernel-C", "kernel": KERNEL_B},
            }
        )
        agent = _agent(ticket)
        script = full_script(actions=("install",))
        script["install_kernel"] = {
            "status": "incomplete",
            "results": {
                host: {
                    "host": host,
                    "state": "install_failed",
                    "remote_write_quiescent": True,
                }
                for host in HOSTS
            },
        }
        script["run_repo_cleanup"] = {
            "status": "ok",
            "results": {host: {"state": "clean"} for host in HOSTS},
        }
        mcp = ScriptedMCP(script)

        await agent._run_kernel_step(ticket["id"], ticket, mcp)

        names = [name for name, _ in mcp.calls]
        assert "run_repo_cleanup" in names
        assert names.index("run_repo_cleanup") > names.index("install_kernel")
        comments = "\n".join(
            call.args[1] for call in agent._add_comment.await_args_list
        )
        assert "Temporary repository cleanup confirmed" in comments
        assert "Kernel change step stopped" in comments
        agent._handle_completion.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_partial_install_timeout_skips_cleanup_even_with_later_step(self):
        ticket = kernel_step_ticket(
            kernel={
                "release": KERNEL_B,
                "package": "kernel",
                "repo_url": "https://repo.example.test/kernel",
            }
        )
        ticket["custom_fields"]["execution_plan"]["steps"].append(
            {
                "id": 3,
                "agent_type": "provision",
                "params": {"label": "kernel-C", "kernel": KERNEL_B},
            }
        )
        agent = _agent(ticket)
        script = full_script(actions=("install",))
        script["install_kernel"] = {
            "status": "incomplete",
            "results": {
                HOSTS[0]: {
                    "host": HOSTS[0],
                    "state": "install_failed",
                    "remote_write_quiescent": True,
                },
                HOSTS[1]: {
                    "host": HOSTS[1],
                    "state": "indeterminate",
                    "remote_write_quiescent": False,
                },
            },
        }
        mcp = ScriptedMCP(script)

        await agent._run_kernel_step(ticket["id"], ticket, mcp)

        assert not any(name == "run_repo_cleanup" for name, _ in mcp.calls)
        comments = "\n".join(
            call.args[1] for call in agent._add_comment.await_args_list
        )
        assert "SSH write or reboot may still be in flight" in comments
        assert (
            "another host may still have an SSH write or reboot in flight" in comments
        )
        agent._handle_completion.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_resume_with_old_indeterminate_intent_stops_before_new_intent(self):
        ticket = kernel_step_ticket(
            kernel={
                "release": KERNEL_B,
                "package": "kernel",
                "repo_url": "https://repo.example.test/kernel",
            }
        )
        ticket["custom_fields"]["kernel_change_intents"] = {
            "records": {
                "kci-old": {
                    "intent_id": "kci-old",
                    "step_id": 2,
                    "progress": {"install": "indeterminate"},
                }
            }
        }
        agent = _agent(ticket)
        mcp = ScriptedMCP({})

        await agent._run_kernel_step(ticket["id"], ticket, mcp)

        assert not mcp.calls
        comments = "\n".join(
            call.args[1] for call in agent._add_comment.await_args_list
        )
        assert "host owner must reconcile it" in comments
        assert (
            "cleanup not attempted because an SSH write or reboot may still be in flight"
            in comments
        )
        assert "No further kernel changes were started" in comments
        agent._transition_ticket.assert_awaited_once()
        transition_comment = agent._transition_ticket.await_args.kwargs["comment"]
        assert "host owner must reconcile" in transition_comment
        assert "Automated resumption is currently unavailable" in transition_comment
        assert "restart the kernel change request" not in transition_comment
        agent._handle_completion.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_plan_reindex_does_not_bypass_prior_unresolved_intent(self):
        from agents.provisioning.kernel import new_intent
        from providers.rhel_kernel import KernelSpec

        ticket = kernel_step_ticket(
            kernel={
                "release": KERNEL_B,
                "package": "kernel",
                "repo_url": "https://repo.example.test/kernel",
            }
        )
        aliases = ["sut-a", "sut-b"]
        old_intent = new_intent(
            ticket["id"],
            1,
            aliases,
            KernelSpec(KERNEL_B),
            ["install"],
            {},
            dict(zip(aliases, HOSTS, strict=True)),
        )
        old_intent["progress"]["install"] = "indeterminate"
        ticket["custom_fields"]["kernel_change_intents"] = {
            "records": {old_intent["intent_id"]: old_intent}
        }
        agent = _agent(ticket)
        mcp = ScriptedMCP({})

        await agent._run_kernel_step(ticket["id"], ticket, mcp)

        assert not mcp.calls
        comments = "\n".join(
            call.args[1] for call in agent._add_comment.await_args_list
        )
        assert (
            "previous kernel or repository cleanup command has an unresolved"
            in comments
        )
        assert "host owner must reconcile it" in comments

    @pytest.mark.asyncio
    async def test_resumed_pending_reboot_reconciles_before_completing(self):
        from agents.provisioning.kernel import new_intent
        from providers.rhel_kernel import KernelSpec

        ticket = kernel_step_ticket(
            kernel={
                "release": KERNEL_B,
                "package": "kernel",
                "repo_url": "https://repo.example.test/kernel",
            }
        )
        spec = KernelSpec.parse(
            ticket["custom_fields"]["execution_plan"]["steps"][2]["params"]["kernel"]
        )
        intent = new_intent(
            ticket["id"],
            2,
            HOSTS,
            spec,
            ["reboot"],
            {},
            {host: host for host in HOSTS},
        )
        intent["progress"]["reboot"] = "started"
        intent["approval_request_id"] = "apr-existing"
        ticket["custom_fields"]["kernel_change_intents"] = {
            "records": {intent["intent_id"]: intent}
        }
        ticket["custom_fields"]["kernel_transitions"] = {
            "2": {
                "intent_id": intent["intent_id"],
                "approval_request_id": "apr-existing",
                "kernel": KERNEL_B,
                "kernel_spec": spec.to_dict(),
                "requested_hosts": HOSTS,
                "addresses": {host: host for host in HOSTS},
                "reboot_policy": "always",
                "state": "indeterminate",
                "hosts": {
                    host: {
                        "state": "indeterminate",
                        "reboot_issued": True,
                        "reboot_pending": True,
                        "pre_boot_id": f"before-{host}",
                        "post_boot_id": None,
                    }
                    for host in HOSTS
                },
            }
        }
        agent = _agent(ticket)
        mcp = ScriptedMCP(
            {
                "verify_kernel_state": {
                    "status": "ok",
                    "transition_state": "verified",
                    "results": {
                        host: {"host": host, "state": "verified"} for host in HOSTS
                    },
                },
                "run_repo_cleanup": {
                    "status": "ok",
                    "results": {
                        host: {"host": host, "state": "clean"} for host in HOSTS
                    },
                },
            }
        )

        await agent._run_kernel_step(ticket["id"], ticket, mcp)

        assert [name for name, _ in mcp.calls] == [
            "verify_kernel_state",
            "run_repo_cleanup",
        ]
        agent._handle_completion.assert_awaited_once()
        submitted = agent._handle_completion.await_args.args[1].tool_calls[0].input
        assert submitted["provisioning_complete"] is True
        assert (
            submitted["configuration_applied"]["kernel_transition"]["intent_id"]
            == (intent["intent_id"])
        )

    @pytest.mark.asyncio
    async def test_reboot_reservation_without_checkpoint_pauses_for_human(self):
        from agents.provisioning.kernel import new_intent
        from providers.rhel_kernel import KernelSpec

        ticket = kernel_step_ticket(
            kernel={
                "release": KERNEL_B,
                "package": "kernel",
                "repo_url": "https://repo.example.test/kernel",
            }
        )
        spec = KernelSpec.parse(
            ticket["custom_fields"]["execution_plan"]["steps"][2]["params"]["kernel"]
        )
        intent = new_intent(
            ticket["id"],
            2,
            HOSTS,
            spec,
            ["reboot"],
            {},
            {host: host for host in HOSTS},
        )
        intent["progress"]["reboot"] = "started"
        ticket["custom_fields"]["kernel_change_intents"] = {
            "records": {intent["intent_id"]: intent}
        }
        agent = _agent(ticket)
        mcp = ScriptedMCP(
            {
                "verify_kernel_state": {
                    "status": "indeterminate",
                    "reason_code": "reboot_checkpoint_unavailable",
                    "error": "human host-owner intervention is required",
                }
            }
        )

        await agent._run_kernel_step(ticket["id"], ticket, mcp)

        assert [name for name, _ in mcp.calls] == ["verify_kernel_state"]
        agent._handle_completion.assert_not_awaited()
        comments = "\n".join(
            call.args[1] for call in agent._add_comment.await_args_list
        )
        assert "prior reboot remains unresolved" in comments
        assert "host owner must reconcile it" in comments
        assert "No further kernel changes were started" in comments
        transition_comment = agent._transition_ticket.await_args.kwargs["comment"]
        assert "host owner must reconcile" in transition_comment
        assert "Automated resumption is currently unavailable" in transition_comment
        assert "restart the kernel change request" not in transition_comment

    @pytest.mark.asyncio
    async def test_pending_reboot_checkpoint_comment_offers_supported_verification(
        self,
    ):
        from agents.provisioning.kernel import new_intent
        from providers.rhel_kernel import KernelSpec

        ticket = kernel_step_ticket(
            kernel={
                "release": KERNEL_B,
                "package": "kernel",
                "repo_url": "https://repo.example.test/kernel",
            }
        )
        spec = KernelSpec.parse(
            ticket["custom_fields"]["execution_plan"]["steps"][2]["params"]["kernel"]
        )
        intent_id = "kci-recoverable-reboot"
        intent = new_intent(
            ticket["id"],
            2,
            HOSTS,
            spec,
            ["reboot"],
            {},
            {host: host for host in HOSTS},
        )
        intent["intent_id"] = intent_id
        intent["progress"]["reboot"] = "started"
        intent["approval_request_id"] = "apr-existing"
        latest_ticket = kernel_step_ticket(
            kernel={
                "release": KERNEL_B,
                "package": "kernel",
                "repo_url": "https://repo.example.test/kernel",
            }
        )
        latest_ticket["custom_fields"]["kernel_change_intents"] = {
            "records": {intent_id: intent}
        }
        latest_ticket["custom_fields"]["kernel_transitions"] = {
            "2": {
                "intent_id": intent_id,
                "kernel": KERNEL_B,
                "kernel_spec": spec.to_dict(),
                "requested_hosts": HOSTS,
                "addresses": {host: host for host in HOSTS},
                "reboot_policy": "always",
                "state": "indeterminate",
                "hosts": {
                    HOSTS[0]: {
                        "state": "verified",
                        "reboot_issued": True,
                        "reboot_pending": False,
                        "pre_boot_id": "before-1",
                        "post_boot_id": "after-1",
                        "observed_kernel": KERNEL_B,
                    },
                    HOSTS[1]: {
                        "state": "reconnect_timeout",
                        "reboot_issued": True,
                        "reboot_pending": True,
                        "pre_boot_id": "before-2",
                        "post_boot_id": None,
                    },
                },
            }
        }
        agent = _agent(ticket)
        agent._get_ticket = AsyncMock(return_value=latest_ticket)
        mcp = ScriptedMCP(
            {
                "get_kernel_inventory": ok_inventory(HOSTS),
                "prepare_kernel_change": {
                    "status": "ok",
                    "intent_id": intent_id,
                    "actions_required": ["reboot"],
                },
                "reboot_hosts_and_verify": {
                    "status": "paused",
                    "hosts": {
                        HOSTS[0]: {"host": HOSTS[0], "state": "verified"},
                        HOSTS[1]: {
                            "host": HOSTS[1],
                            "state": "reconnect_timeout",
                            "reboot_pending": True,
                        },
                    },
                },
            }
        )

        await agent._run_kernel_step(ticket["id"], ticket, mcp)

        assert not any(name == "run_repo_cleanup" for name, _ in mcp.calls)
        transition_comment = agent._transition_ticket.await_args.kwargs["comment"]
        assert "verify_kernel_state" in transition_comment
        assert "will not issue another reboot" in transition_comment
        assert "Automated resumption is currently unavailable" not in transition_comment

    @pytest.mark.asyncio
    async def test_failure_path_cleanup_timeout_reports_unknown_and_pauses(self):
        ticket = kernel_step_ticket(
            kernel={
                "release": KERNEL_B,
                "package": "kernel",
                "repo_url": "https://repo.example.test/kernel",
            }
        )
        agent = _agent(ticket)
        script = full_script(actions=("install",))
        script["install_kernel"] = {
            "results": {
                host: {
                    "host": host,
                    "state": "install_failed",
                    "remote_write_quiescent": True,
                }
                for host in HOSTS
            }
        }
        script["run_repo_cleanup"] = {
            "status": "indeterminate",
            "results": {
                host: {
                    "state": "indeterminate",
                    "remote_write_quiescent": False,
                }
                for host in HOSTS
            },
        }
        mcp = ScriptedMCP(script)

        await agent._run_kernel_step(ticket["id"], ticket, mcp)

        names = [name for name, _ in mcp.calls]
        assert names[-1] == "run_repo_cleanup"
        assert "select_default_kernel" not in names
        comments = "\n".join(
            call.args[1] for call in agent._add_comment.await_args_list
        )
        assert "cleanup outcome unknown" in comments
        assert "Kernel change step stopped" in comments
        transition_comment = agent._transition_ticket.await_args.kwargs["comment"]
        assert "host owner must reconcile" in transition_comment
        assert "Automated resumption is currently unavailable" in transition_comment
        assert "restart the kernel change request" not in transition_comment
        agent._handle_completion.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_final_success_cleanup_timeout_pauses_without_completion(self):
        ticket = kernel_step_ticket(
            kernel={
                "release": KERNEL_B,
                "package": "kernel",
                "repo_url": "https://repo.example.test/kernel",
            }
        )
        agent = _agent(ticket)
        script = full_script()
        script["run_repo_cleanup"] = {
            "status": "indeterminate",
            "results": {
                host: {
                    "state": "indeterminate",
                    "remote_write_quiescent": False,
                }
                for host in HOSTS
            },
        }
        mcp = ScriptedMCP(script)

        await agent._run_kernel_step(ticket["id"], ticket, mcp)

        assert [name for name, _ in mcp.calls][-1] == "run_repo_cleanup"
        comments = "\n".join(
            call.args[1] for call in agent._add_comment.await_args_list
        )
        assert "cleanup outcome unknown" in comments
        assert "Kernel change step stopped" in comments
        agent._handle_completion.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("result", "expected_text"),
        [
            (
                {
                    "status": "paused",
                    "hosts": {
                        HOSTS[0]: {
                            "state": "reconnect_timeout",
                            "error": "host did not reconnect",
                        },
                        HOSTS[1]: {"state": "not_attempted"},
                    },
                },
                "reconnect timeout",
            ),
            (
                {
                    "status": "verified",
                    "hosts": {
                        HOSTS[0]: {
                            "state": "kernel_mismatch",
                            "observed_kernel": "5.14.0-1.el9.x86_64",
                        },
                        HOSTS[1]: {
                            "state": "verified",
                            "observed_kernel": KERNEL_B,
                        },
                    },
                },
                "was observed",
            ),
        ],
    )
    async def test_reboot_failure_or_mismatch_is_reported_without_success(
        self, result, expected_text
    ):
        ticket = kernel_step_ticket()
        agent = _agent(ticket)
        script = full_script(actions=("reboot",))
        script["reboot_hosts_and_verify"] = result
        mcp = ScriptedMCP(script)

        await agent._run_kernel_step(ticket["id"], ticket, mcp)

        comments = "\n".join(
            call.args[1] for call in agent._add_comment.await_args_list
        )
        assert expected_text in comments
        assert "not confirmed" in comments or "not verified" in comments
        agent._handle_completion.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_host_action_result_pauses_and_is_reported(self):
        ticket = kernel_step_ticket()
        agent = _agent(ticket)
        script = full_script(actions=("install",))
        script["install_kernel"]["results"].pop(HOSTS[1])
        mcp = ScriptedMCP(script)

        await agent._run_kernel_step(ticket["id"], ticket, mcp)

        comments = "\n".join(
            call.args[1] for call in agent._add_comment.await_args_list
        )
        assert f"{HOSTS[1]}:** outcome not confirmed" in comments
        assert "no host result was returned" in comments
        agent._handle_completion.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_later_reboot_failure_keeps_earlier_verified_host_clear(self):
        ticket = kernel_step_ticket()
        agent = _agent(ticket)
        script = full_script(actions=("reboot",))
        script["reboot_hosts_and_verify"] = {
            "status": "paused",
            "hosts": {
                HOSTS[0]: {"state": "verified", "observed_kernel": KERNEL_B},
                HOSTS[1]: {"state": "reconnect_timeout"},
            },
        }
        mcp = ScriptedMCP(script)

        await agent._run_kernel_step(ticket["id"], ticket, mcp)

        comments = [call.args[1] for call in agent._add_comment.await_args_list]
        action_comment = next(comment for comment in comments if "result**" in comment)
        pause_comment = next(
            comment for comment in comments if "Kernel change step stopped" in comment
        )
        assert f"{HOSTS[0]}:** running kernel {KERNEL_B} was verified" in action_comment
        assert "not confirmed (reconnect timeout)" in action_comment
        assert HOSTS[0] not in pause_comment
        assert (
            f"{HOSTS[1]}:** outcome not confirmed (reconnect timeout)" in pause_comment
        )
        agent._handle_completion.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_arguments_match_the_real_tool_schemas(self):
        """Lab regression (PERF-29157B06): prepare_kernel_change was called
        with a dict for its string parameter and the MCP layer rejected it.
        Validate every call against the registered tool's input schema."""
        from agents.provisioning.server import get_registered_tools

        schemas = {t.name: t.input_schema for t in await get_registered_tools()}
        ticket = kernel_step_ticket()
        agent = _agent(ticket)
        mcp = ScriptedMCP(full_script())
        await agent._run_kernel_step(ticket["id"], ticket, mcp)
        type_map = {"string": str, "array": list, "integer": int, "boolean": bool}
        for name, args in mcp.calls:
            props = schemas[name]["properties"]
            for key, value in args.items():
                assert key in props, f"{name}: unknown argument {key}"
                expected = type_map.get(props[key].get("type"))
                if expected is not None:
                    assert isinstance(value, expected), (
                        f"{name}.{key} should be {props[key].get('type')}, got "
                        f"{type(value).__name__}"
                    )
            for required in schemas[name].get("required", []):
                assert required in args, f"{name}: missing required {required}"

    @pytest.mark.asyncio
    async def test_tool_call_error_pauses_instead_of_crashing(self):
        from agents.mcp_client import MCPToolCallError

        ticket = kernel_step_ticket()
        agent = _agent(ticket)

        class ExplodingMCP(ScriptedMCP):
            async def call_tool(self, name, arguments, **_kw):
                self.calls.append((name, dict(arguments)))
                raise MCPToolCallError("1 validation error for call[...]", "validation")

        mcp = ExplodingMCP(full_script())
        await agent._run_kernel_step(ticket["id"], ticket, mcp)
        agent._handle_completion.assert_not_called()
        agent._transition_ticket.assert_awaited_once()
        assert "could not check the requested kernel" in "\n".join(
            call.args[1] for call in agent._add_comment.await_args_list
        )

    @pytest.mark.asyncio
    async def test_approval_runs_under_a_fence_context_with_tool_call_id(self):
        """Lab regression (PERF-29157B06): the approval endpoint returned 422
        because the runner created the approval outside any tool call, so
        the fence context carried no tool-call id."""
        from providers.tracing import current_trace_context, new_trace_context

        ticket = kernel_step_ticket()
        agent = _agent(ticket)
        agent.trace_context = new_trace_context(
            ticket_id=ticket["id"], agent_id="provisioning-agent"
        )
        seen: dict[str, object] = {}

        async def _approval(*, intent_id, summary):
            ctx = current_trace_context()
            seen["invocation_id"] = ctx.invocation_id if ctx else None
            seen["tool_call_id"] = ctx.tool_call_id if ctx else None
            agent._active_kernel_approval_id = "apr-" + "a" * 32
            return "The user replied: approve\n\nInterpret this response."

        agent._request_kernel_approval = AsyncMock(side_effect=_approval)
        mcp = ScriptedMCP(full_script(actions=("reboot",)))
        await agent._run_kernel_step(ticket["id"], ticket, mcp)
        assert seen["invocation_id"] == agent.trace_context.invocation_id
        assert seen["tool_call_id"] == "kernel-step-2"
        assert current_trace_context() is None, "context must be unbound afterwards"
        agent._handle_completion.assert_awaited_once()

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
    async def test_install_failure_reports_safe_cause_without_raw_error(self):
        ticket = kernel_step_ticket()
        agent = _agent(ticket)
        script = full_script()
        script["install_kernel"]["results"][HOSTS[1]] = {
            "host": HOSTS[1],
            "state": "install_failed",
            "reason_code": "repository_trust_material_unavailable",
            "error": "https://user:secret@repo.example.test/?token=secret",
        }
        mcp = ScriptedMCP(script)

        await agent._run_kernel_step(ticket["id"], ticket, mcp)

        comments = "\n".join(
            call.args[1] for call in agent._add_comment.await_args_list
        )
        assert (
            "repository signing or certificate settings could not be applied"
            in comments
        )
        assert "secret" not in comments and "token" not in comments
        assert "https://" not in comments
        agent._handle_completion.assert_not_awaited()

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
        assert "reboot or running-kernel check did not complete" in "\n".join(
            call.args[1] for call in agent._add_comment.await_args_list
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("requires_manual", "reboot_available", "expected_guidance"),
        [
            (
                True,
                True,
                "Automated resumption is currently unavailable",
            ),
            (
                False,
                True,
                "read-only and will not issue another reboot",
            ),
        ],
    )
    async def test_already_paused_ticket_keeps_reconciliation_guidance(
        self, requires_manual, reboot_available, expected_guidance
    ):
        ticket = kernel_step_ticket()
        paused = dict(ticket, status="awaiting_customer_guidance")
        agent = _agent(ticket)
        agent._get_ticket = AsyncMock(return_value=paused)

        await agent._kernel_step_stop(
            ticket["id"],
            "repository cleanup outcome is unknown after reboot pause",
            {
                "status": "indeterminate",
                "reason_code": "repository_cleanup_indeterminate",
                "results": {
                    host: {
                        "state": "indeterminate",
                        "remote_write_quiescent": False,
                    }
                    for host in HOSTS
                },
            },
            requires_manual_reconciliation=requires_manual,
            reboot_reconciliation_available=reboot_available,
        )

        agent._transition_ticket.assert_not_called()
        comments = [call.args[1] for call in agent._add_comment.await_args_list]
        assert len(comments) == 2
        assert (
            sum(
                comment.count("**Kernel change step stopped:**") for comment in comments
            )
            == 1
        )
        comment = "\n".join(comments)
        assert expected_guidance in comment
        assert "The kernel change needs attention." not in comment

    @pytest.mark.asyncio
    async def test_status_read_failure_does_not_suppress_manual_guidance(self):
        ticket = kernel_step_ticket()
        agent = _agent(ticket)
        state = {"status": "awaiting_provision"}
        agent._get_ticket = AsyncMock(
            side_effect=RuntimeError("temporary read failure")
        )

        async def transition(ticket_id, status, comment=None):
            state["status"] = status

        agent._transition_ticket = AsyncMock(side_effect=transition)

        await agent._kernel_step_stop(
            ticket["id"],
            "repository cleanup outcome is unknown",
            {
                "status": "indeterminate",
                "reason_code": "repository_cleanup_indeterminate",
            },
            requires_manual_reconciliation=True,
        )

        agent._transition_ticket.assert_awaited_once()
        transition_call = agent._transition_ticket.await_args
        assert transition_call.args[:2] == (
            ticket["id"],
            "awaiting_customer_guidance",
        )
        assert "preceding reconciliation guidance" in transition_call.kwargs["comment"]
        assert state["status"] == "awaiting_customer_guidance"
        comments = "\n".join(
            call.args[1] for call in agent._add_comment.await_args_list
        )
        assert "**Kernel change step stopped:**" in comments
        assert "Automated resumption is currently unavailable" in comments

    @pytest.mark.asyncio
    async def test_transition_failure_with_unknown_status_propagates_unconfirmed_pause(
        self,
    ):
        ticket = kernel_step_ticket()
        agent = _agent(ticket)
        agent._get_ticket = AsyncMock(
            side_effect=[
                RuntimeError("temporary read failure"),
                {**ticket, "status": "awaiting_provision"},
            ]
        )
        agent._transition_ticket = AsyncMock(
            side_effect=RuntimeError("pause transition failed")
        )

        with pytest.raises(RuntimeError, match="pause transition failed"):
            await agent._kernel_step_stop(
                ticket["id"],
                "repository cleanup outcome is unknown",
                {"status": "indeterminate"},
                requires_manual_reconciliation=True,
            )

        comments = "\n".join(
            call.args[1] for call in agent._add_comment.await_args_list
        )
        assert "Automated resumption is currently unavailable" in comments
        assert "**Kernel change paused:**" not in comments

    @pytest.mark.asyncio
    async def test_unknown_status_transition_conflict_is_confirmed_if_already_paused(
        self,
    ):
        ticket = kernel_step_ticket()
        paused = {**ticket, "status": "awaiting_customer_guidance"}
        agent = _agent(ticket)
        agent._get_ticket = AsyncMock(
            side_effect=[RuntimeError("temporary read failure"), paused]
        )
        agent._transition_ticket = AsyncMock(
            side_effect=RuntimeError("transition rejected because already paused")
        )

        await agent._kernel_step_stop(
            ticket["id"],
            "repository cleanup outcome is unknown",
            {"status": "indeterminate"},
            requires_manual_reconciliation=True,
        )

        agent._transition_ticket.assert_awaited_once()
        assert agent._get_ticket.await_count == 2
        comments = "\n".join(
            call.args[1] for call in agent._add_comment.await_args_list
        )
        assert "Automated resumption is currently unavailable" in comments

    @pytest.mark.asyncio
    async def test_reboot_policy_directive_is_forwarded(self):
        ticket = kernel_step_ticket()
        ticket["custom_fields"]["directives"] = {"kernel_reboot_policy": "if_needed"}
        agent = _agent(ticket)
        mcp = ScriptedMCP(full_script(actions=("reboot",)))
        await agent._run_kernel_step(ticket["id"], ticket, mcp)
        assert mcp.calls[-1][1]["reboot_policy"] == "if_needed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [
        "approve if you first confirm the host is idle",
        "yes, but do not reboot yet",
        "approve\nonly after tomorrow's maintenance window",
        "ok\nDo not install anything until I confirm",
        "approved pending another review",
    ],
)
async def test_conditional_or_multiline_approval_cannot_authorize_kernel_change(reply):
    ticket = kernel_step_ticket()
    agent = _agent(
        ticket, reply=f"The user replied: {reply}\n\nInterpret this response."
    )
    mcp = ScriptedMCP(full_script())
    await agent._run_kernel_step(ticket["id"], ticket, mcp)
    assert [name for name, _ in mcp.calls] == [
        "get_kernel_inventory",
        "prepare_kernel_change",
    ]
    assert agent._resolve_kernel_approval.await_args.kwargs["decision"] == "rejected"
    agent._handle_completion.assert_not_awaited()
    agent._transition_ticket.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply", ["approve", "approved", "yes", "ok", "go ahead", "proceed"]
)
async def test_exact_positive_approval_authorizes_full_intent(reply):
    ticket = kernel_step_ticket()
    agent = _agent(
        ticket, reply=f"The user replied: {reply}\n\nInterpret this response."
    )
    mcp = ScriptedMCP(full_script())
    await agent._run_kernel_step(ticket["id"], ticket, mcp)
    assert agent._resolve_kernel_approval.await_args.kwargs["decision"] == "approved"
    assert [name for name, _ in mcp.calls][-3:] == [
        "install_kernel",
        "select_default_kernel",
        "reboot_hosts_and_verify",
    ]
    agent._handle_completion.assert_awaited_once()
