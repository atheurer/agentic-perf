from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from agents.base import AgentBase
from agents.mcp_client import AgentMCPClient
from agents.skill_context import skill_context_prompt
from providers.events import EventBus
from providers.llm.base import LLMProvider, LLMResponse, ToolCall, ToolDefinition

from .prompts import PROVISIONING_BASE_PROMPT

logger = logging.getLogger(__name__)

# The only two tools NOT served by the provisioning MCP server (server.py) —
# everything else the agent can call comes from that live connection.
_LOCAL_TOOLS = [
    ToolDefinition(
        name="request_clarification",
        description="Ask the user for clarification. Pauses the ticket for human input.",
        input_schema={
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "Question to ask"},
            },
            "required": ["question"],
        },
    ),
    ToolDefinition(
        name="submit_provisioning_result",
        description=(
            "Submit a provisioning result. Set provisioning_complete=true only "
            "when at least one host is provisioned and verification.status is "
            "'verified' with non-empty details. For incomplete work, set it to "
            "false and provide actionable notes. Invalid submissions are returned "
            "as tool errors so you can correct and retry."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "provisioning_complete": {
                    "type": "boolean",
                    "description": "True only after hosts are prepared and verified",
                },
                "hosts_provisioned": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "minLength": 1,
                        "pattern": ".*\\S.*",
                    },
                },
                "verification": {
                    "type": "object",
                    "properties": {
                        "status": {
                            "type": "string",
                            "enum": ["verified", "failed", "partial"],
                        },
                        "details": {"type": "string", "minLength": 1},
                    },
                    "required": ["status", "details"],
                    "additionalProperties": False,
                },
                "harness_version": {"type": "string"},
                "harness_name": {"type": "string"},
                "configuration_applied": {"type": "object"},
                "k3s_installed": {
                    "type": "boolean",
                    "description": "Whether K3s was installed",
                },
                "k3s_version": {
                    "type": "string",
                    "description": "K3s version string (if installed)",
                },
                "notes": {"type": "string"},
            },
            "required": ["provisioning_complete", "hosts_provisioned"],
            "additionalProperties": False,
        },
    ),
    ToolDefinition(
        name="present_kernel_change_for_approval",
        description=(
            "Present a prepared kernel change intent (from prepare_kernel_change) "
            "to the user for approval. Pauses for the human decision and returns "
            "the approval status. install_kernel, select_default_kernel and "
            "reboot_hosts_and_verify refuse to run without the resulting approval."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "intent_id": {
                    "type": "string",
                    "description": "intent_id returned by prepare_kernel_change",
                },
                "summary": {
                    "type": "string",
                    "description": "One paragraph for the user: what changes and why",
                },
            },
            "required": ["intent_id"],
        },
    ),
    ToolDefinition(
        name="resolve_kernel_approval",
        description=(
            "Resolve the active kernel change approval after interpreting the "
            "user's natural-language reply. Use approved only when the user "
            "clearly authorizes the kernel install/boot-entry change/reboot; use "
            "changes_requested or rejected otherwise. Never run a kernel action "
            "while approval is pending."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "decision": {
                    "type": "string",
                    "enum": ["approved", "changes_requested", "rejected"],
                },
                "reason": {
                    "type": "string",
                    "description": "Brief explanation for a non-approval decision",
                },
            },
            "required": ["decision"],
        },
    ),
]


class ProvisioningAgent(AgentBase):
    def __init__(
        self,
        llm_provider: LLMProvider,
        state_store_url: str,
        skill_provider=None,
        secrets_provider=None,
        event_bus: EventBus | None = None,
    ) -> None:
        self._skill_provider = skill_provider
        self._secrets_provider = secrets_provider
        self._ticket_id: str | None = None
        self._active_kernel_approval_id: str | None = None
        self._active_kernel_approval: dict[str, Any] | None = None

        local_tools = list(_LOCAL_TOOLS)

        async def _request_clarification(question: str) -> str:
            return await self._do_request_clarification(question)

        async def _present_kernel_change_for_approval(
            intent_id: str = "",
            summary: str | None = None,
        ) -> str:
            return await self._request_kernel_approval(
                intent_id=intent_id, summary=summary
            )

        async def _resolve_kernel_approval(
            decision: str,
            reason: str | None = None,
        ) -> str:
            return await self._resolve_kernel_approval(decision=decision, reason=reason)

        local_handlers = {
            "request_clarification": _request_clarification,
            "present_kernel_change_for_approval": _present_kernel_change_for_approval,
            "resolve_kernel_approval": _resolve_kernel_approval,
        }

        super().__init__(
            agent_name="provisioning-agent",
            llm_provider=llm_provider,
            state_store_url=state_store_url,
            tools=local_tools,
            tool_handlers=local_handlers,
            event_bus=event_bus,
        )

    async def _do_request_clarification(self, question: str) -> str:
        if self._ticket_id:
            return await self._request_human_input(self._ticket_id, question)
        return "No ticket context available."

    # ------------------------------------------------------------------
    # Kernel change approval (kind="kernel_change")
    # ------------------------------------------------------------------

    def _kernel_fence_headers(self, cf: dict[str, Any]) -> dict[str, str]:
        from providers.tracing import current_trace_context

        context = current_trace_context() or self.trace_context
        headers = {
            "X-Agentic-Perf-Orchestrator-Session": os.environ.get(
                "AGENTIC_PERF_ORCHESTRATOR_SESSION_ID", ""
            ),
            "X-Agentic-Perf-Orchestrator-Epoch": os.environ.get(
                "AGENTIC_PERF_ORCHESTRATOR_EPOCH", ""
            ),
            "X-Agentic-Perf-Claim-Id": os.environ.get("AGENTIC_PERF_CLAIM_ID", "")
            or (cf.get("claim") or {}).get("claim_id", ""),
            "X-Agentic-Perf-Invocation-Id": str(context.invocation_id)
            if context and context.invocation_id
            else "",
            "X-Agentic-Perf-Tool-Call-Id": context.tool_call_id
            if context and context.tool_call_id
            else "",
        }
        return {key: value for key, value in headers.items() if value}

    async def _request_kernel_approval(
        self,
        *,
        intent_id: str,
        summary: str | None,
    ) -> str:
        """Create one kernel_change approval bound to a persisted intent and wait.

        Mirrors the benchmark run-file approval: the human decision is bound
        to the intent digest the provisioning server persisted, never to text
        the LLM produced.
        """
        if not self._ticket_id:
            return "No ticket context available."
        from agents.provisioning.kernel import find_intent
        from providers.tracing import current_trace_context

        self._kernel_human_reply = None
        ticket = await self._get_ticket(self._ticket_id)
        cf = ticket.get("custom_fields", {})
        intent = find_intent(cf, intent_id) if intent_id else None
        if intent is None:
            return (
                "Approval rejected: no persisted kernel change intent with that "
                "intent_id. Call prepare_kernel_change first."
            )
        digest = intent.get("intent_digest", "")
        context = current_trace_context() or self.trace_context
        claim_id = os.environ.get("AGENTIC_PERF_CLAIM_ID") or (
            cf.get("claim") or {}
        ).get("claim_id")
        request = await self._client.post(
            f"{self.store_url}/api/v1/tickets/{self._ticket_id}/approvals",
            headers=self._kernel_fence_headers(cf),
            json={
                "kind": "kernel_change",
                "validation_id": intent_id,
                "presented_run_file_digest": digest,
                "execution_intent_digest": digest,
                "summary": summary or "Kernel change approval",
                "invocation_id": str(context.invocation_id)
                if context and context.invocation_id
                else None,
                "tool_call_id": context.tool_call_id if context else None,
                "session_id": os.environ.get("AGENTIC_PERF_ORCHESTRATOR_SESSION_ID"),
                "session_epoch": os.environ.get("AGENTIC_PERF_ORCHESTRATOR_EPOCH"),
                "waiter_owner": (
                    str(context.invocation_id)
                    if context and context.invocation_id
                    else None
                ),
                "ticket_attempt": claim_id,
                "claim_id": claim_id,
            },
        )
        request.raise_for_status()
        approval = request.json()
        approval_id = approval["approval_request_id"]
        self._active_kernel_approval_id = approval_id
        self._active_kernel_approval = approval

        kernel = intent.get("kernel", {})
        question = (
            f"Approval request {approval_id} for a kernel change.\n"
            f"Intent ID: {intent_id}\n"
            f"Kernel: {kernel.get('package', 'kernel')}-{kernel.get('release', '?')}\n"
            f"Hosts: {', '.join(intent.get('hosts', []))}\n"
            f"Approved addresses: {json.dumps(intent.get('addresses', {}), sort_keys=True)}\n"
            f"Actions: {', '.join(intent.get('actions_required', []))}\n"
            f"Reboot policy: {intent.get('reboot_policy', 'missing')}\n"
            + (
                f"Package source: {kernel.get('repo_url')}\n"
                "(from the organization's approved_repositories; installed "
                "with GPG and TLS verification)\n"
                if kernel.get("repo_url")
                else ""
            )
            + f"Intent digest: {digest}\n\n"
            f"{summary or ''}\n\n"
            "Reply approve / request changes / reject, or use the structured "
            "approval action."
        )
        await self._add_comment(self._ticket_id, f"**Approval requested:**\n{question}")
        return await self._wait_for_kernel_approval(approval_id)

    async def _wait_for_kernel_approval(self, approval_id: str) -> str:
        reply = await self._request_human_input(
            self._ticket_id,
            "Review the kernel change above and reply naturally with your "
            "decision. I will interpret your response before any package, "
            "boot-entry or reboot action.",
        )
        self._kernel_human_reply = reply
        approvals_response = await self._client.get(
            f"{self.store_url}/api/v1/tickets/{self._ticket_id}/approvals"
        )
        approvals_response.raise_for_status()
        record = next(
            (
                item
                for item in approvals_response.json().get("approvals", [])
                if item.get("approval_request_id") == approval_id
            ),
            None,
        )
        if record and record.get("status") == "approved":
            return (
                f"Approval already granted for request {approval_id}; pass "
                f"approval request ID {approval_id} to install_kernel, "
                "select_default_kernel and reboot_hosts_and_verify."
            )
        if record and record.get("status") != "pending":
            return f"Approval {record.get('status')} for request {approval_id}."
        return (
            f"The user replied: {reply}\n\n"
            "Interpret this response. If it clearly authorizes the kernel change, "
            f"call resolve_kernel_approval(decision='approved') for request "
            f"{approval_id}; otherwise resolve it as changes_requested or rejected. "
            "Do not run install_kernel, select_default_kernel or "
            "reboot_hosts_and_verify while approval remains pending."
        )

    async def _resolve_kernel_approval(
        self,
        *,
        decision: str,
        reason: str | None = None,
    ) -> str:
        if decision not in {"approved", "changes_requested", "rejected"}:
            return f"Unsupported approval decision: {decision}"
        approval_id = self._active_kernel_approval_id
        approval = self._active_kernel_approval or {}
        if not approval_id:
            return "No active kernel change approval request is awaiting a decision."
        response = await self._client.post(
            f"{self.store_url}/api/v1/tickets/{self._ticket_id}/approvals/"
            f"{approval_id}/resolve",
            json={
                "decision": decision,
                "reason": reason,
                "validation_id": approval.get("validation_id"),
                "presented_run_file_digest": approval.get("presented_run_file_digest"),
                "execution_intent_digest": approval.get("execution_intent_digest"),
            },
        )
        response.raise_for_status()
        if decision == "approved":
            return (
                f"Approval granted for request {approval_id}; pass approval "
                f"request ID {approval_id} to install_kernel, select_default_kernel "
                "and reboot_hosts_and_verify."
            )
        return (
            f"Approval {decision} for request {approval_id}. Do not change "
            "kernels or reboot."
        )

    # ------------------------------------------------------------------
    # Deterministic kernel transition step (no LLM reasoning)
    # ------------------------------------------------------------------

    _KERNEL_APPROVE_RE = re.compile(
        r"(approve[ds]?|yes|ok(ay)?|go( ahead)?|lgtm|proceed|continue)[.!]?", re.I
    )
    _KERNEL_INSTALL_OK = frozenset({"installed", "already_installed"})
    _KERNEL_SELECT_OK = frozenset({"ok", "already_default"})

    @staticmethod
    def _kernel_step_is_deterministic(cf: dict[str, Any]) -> bool:
        from agents.provisioning.kernel import current_kernel_step

        if current_kernel_step({"custom_fields": cf}) is None:
            return False
        mode = (cf.get("directives") or {}).get("kernel_step_mode", "deterministic")
        return mode != "llm"

    async def _kernel_tool(
        self, mcp: Any, name: str, **arguments: Any
    ) -> dict[str, Any]:
        """Call one kernel tool through the audited MCP client; always a dict.

        Tool arguments must match the tool's schema exactly (the MCP layer
        validates them); a transport or validation error becomes an error
        dict so the step pauses with the detail instead of crashing.
        """
        from agents.mcp_client import MCPToolCallError

        try:
            raw = await mcp.call_tool(name, arguments)
        except MCPToolCallError as exc:
            return {"status": "error", "error": f"{name}: {exc}"}
        try:
            decoded = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            decoded = None
        if not isinstance(decoded, dict):
            return {
                "status": "error",
                "error": f"non-JSON result from {name}",
                "raw": raw,
            }
        return decoded

    async def _kernel_step_stop(
        self, ticket_id: str, headline: str, detail: Any
    ) -> None:
        """Pause for the user unless a tool already paused the ticket."""
        body = (
            f"**Kernel transition stopped:** {headline}\n\n"
            f"```json\n{json.dumps(detail, indent=2)[:3000]}\n```"
        )
        await self._add_comment(ticket_id, body)
        ticket = await self._get_ticket(ticket_id)
        if ticket.get("status") == "awaiting_customer_guidance":
            return
        await self._transition_ticket(
            ticket_id,
            "awaiting_customer_guidance",
            comment=(
                "Kernel transition needs a decision. Fix the host or the plan "
                "and reply; a new prepare_kernel_change + approval is needed "
                "for any new reboot."
            ),
        )

    async def _run_kernel_step(
        self, ticket_id: str, ticket: dict[str, Any], mcp: Any
    ) -> None:
        """Drive a kernel transition step with fixed tool calls.

        Order: inventory -> prepare (persisted intent) -> approval pause ->
        the intent's actions (install, select_default, reboot) -> submit.
        The human reply to the approval is interpreted by a strict pattern,
        never by a model; anything that is not a clear yes is a rejection.
        """
        from agents.provisioning.kernel import (
            authorized_kernel_hosts,
            current_kernel_step,
        )

        cf = ticket.get("custom_fields", {})
        step = current_kernel_step(ticket)
        assert step is not None
        step_id, spec = step
        hosts = sorted(authorized_kernel_hosts(ticket).values())
        harness = self._effective_harness(
            cf.get("directives", {}), getattr(self, "_skill_provider", None)
        )
        reboot_policy = (cf.get("directives") or {}).get(
            "kernel_reboot_policy", "always"
        )
        if not hosts:
            await self._kernel_step_stop(
                ticket_id,
                "no assigned benchmark hosts for the kernel step",
                cf.get("assigned_hardware_ips"),
            )
            return

        await self._add_comment(
            ticket_id,
            f"**Kernel transition (deterministic)** — plan step {step_id}: "
            f"`{spec.nevra}` on {', '.join(hosts)}. No model reasoning is used "
            "for this step; the only decision is your approval.",
        )

        inventory = await self._kernel_tool(
            mcp, "get_kernel_inventory", hosts=hosts, kernel=spec.release
        )
        if inventory.get("status") in ("rejected", "error"):
            await self._kernel_step_stop(ticket_id, "inventory rejected", inventory)
            return
        not_ok = {
            host: record
            for host, record in inventory.get("results", {}).items()
            if not isinstance(record, dict) or record.get("state") != "ok"
        }
        if not_ok:
            await self._kernel_step_stop(
                ticket_id, "some hosts could not be inventoried", not_ok
            )
            return
        missing = {
            host: record.get("requested")
            for host, record in inventory["results"].items()
            if record.get("requested", {}).get("state") == "not_found"
        }
        # not_found is expected on a fresh host when the step supplies a
        # repo_url: install_kernel bootstraps the repository before dnf.
        # Without a repo_url the operator must add one first — stopping is
        # the documented behaviour.
        if missing and not spec.repo_url:
            await self._kernel_step_stop(
                ticket_id,
                f"{spec.nevra} is not installed or available on every host "
                "(add the repository first)",
                missing,
            )
            return
        if missing and spec.repo_url:
            await self._add_comment(
                ticket_id,
                f"{spec.nevra} is not yet available on "
                f"{', '.join(sorted(missing))}; the step supplies a repo_url "
                "so install_kernel will bootstrap the repository first.",
            )

        # The tools take the release string; the plan step supplies the
        # package, and each tool re-derives the full spec from the step.
        prepared = await self._kernel_tool(
            mcp, "prepare_kernel_change", hosts=hosts, kernel=spec.release
        )
        if prepared.get("status") != "ok":
            await self._kernel_step_stop(
                ticket_id, "prepare_kernel_change refused", prepared
            )
            return
        intent_id = prepared["intent_id"]
        actions = list(prepared.get("actions_required", []))

        summary = (
            f"Plan step {step_id}: {spec.nevra} on {', '.join(hosts)}; "
            f"actions: {', '.join(actions)}; reboot policy: {reboot_policy}."
            + (
                f" Package source: {spec.repo_url} (approved_repositories; "
                "GPG+TLS verified)."
                if spec.repo_url
                else ""
            )
        )
        # The approval endpoint requires a trusted fence context with an
        # invocation id AND a tool-call id.  The model loop derives one per
        # tool call; this runner has no tool call, so it binds a child
        # context of the dispatcher's invocation with a synthetic id.
        from providers.tracing import (
            bind_trace_context,
            child_context,
            current_trace_context,
            new_trace_context,
            reset_trace_context,
        )

        parent = current_trace_context() or getattr(self, "trace_context", None)
        if parent is None:
            parent = new_trace_context(ticket_id=ticket_id, agent_id=self.agent_name)
            self.trace_context = parent
        approval_context = child_context(parent, tool_call_id=f"kernel-step-{step_id}")
        token = bind_trace_context(approval_context)
        try:
            reply = await self._request_kernel_approval(
                intent_id=intent_id, summary=summary
            )
            approval_id = self._active_kernel_approval_id or ""
            if reply.startswith("Approval already granted for request "):
                pass
            elif reply.startswith("The user replied:"):
                user_text = getattr(self, "_kernel_human_reply", None)
                if user_text is None:
                    user_text = reply.split("The user replied:", 1)[1].rsplit(
                        "\n\nInterpret this response.", 1
                    )[0]
                if self._KERNEL_APPROVE_RE.fullmatch(" ".join(user_text.split())):
                    await self._resolve_kernel_approval(
                        decision="approved", reason=None
                    )
                else:
                    await self._resolve_kernel_approval(
                        decision="rejected", reason="reply was not a clear approval"
                    )
                    await self._kernel_step_stop(
                        ticket_id,
                        "the reply was not a clear approval (say 'approve' to proceed)",
                        {"reply": user_text.strip()},
                    )
                    return
            else:
                await self._kernel_step_stop(
                    ticket_id, "approval not granted", {"reply": reply}
                )
                return
        finally:
            reset_trace_context(token)

        for action in actions:
            if action == "install":
                result = await self._kernel_tool(
                    mcp,
                    "install_kernel",
                    hosts=hosts,
                    kernel=spec.release,
                    approval_request_id=approval_id,
                )
                ok_states = self._KERNEL_INSTALL_OK
            elif action == "select_default":
                result = await self._kernel_tool(
                    mcp,
                    "select_default_kernel",
                    hosts=hosts,
                    kernel=spec.release,
                    approval_request_id=approval_id,
                )
                ok_states = self._KERNEL_SELECT_OK
            elif action == "reboot":
                result = await self._kernel_tool(
                    mcp,
                    "reboot_hosts_and_verify",
                    hosts=hosts,
                    expected_kernel=spec.release,
                    approval_request_id=approval_id,
                    reboot_policy=reboot_policy,
                )
                if result.get("status") != "verified":
                    # The reboot tool pauses the ticket itself on failure.
                    await self._kernel_step_stop(
                        ticket_id,
                        f"reboot/verify ended in {result.get('status')}",
                        result,
                    )
                    return
                continue
            else:
                await self._kernel_step_stop(
                    ticket_id, f"unknown action {action!r}", prepared
                )
                return
            if result.get("status") in ("rejected", "paused", "error"):
                await self._kernel_step_stop(ticket_id, f"{action} refused", result)
                return
            failed = {
                host: record
                for host, record in result.get("results", {}).items()
                if host in hosts
                and (
                    not isinstance(record, dict) or record.get("state") not in ok_states
                )
            }
            if failed:
                await self._kernel_step_stop(
                    ticket_id, f"{action} failed on some hosts", failed
                )
                return

        # Repository cleanup: the step-supplied repo_url is a
        # ticket-scoped package source that must not outlive the
        # ticket. Remove it when the LAST kernel step of the plan
        # completes — later kernel steps may still need it to install
        # their own release (review major on #1230; round 2 hardened
        # the failure path).
        plan = cf.get("execution_plan") or {}
        later_kernel_steps = [
            s
            for s in (plan.get("steps") or [])
            if isinstance(s, dict)
            and s.get("id", -1) > step_id
            and (s.get("params") or {}).get("kernel")
        ]
        if spec.repo_url and not later_kernel_steps:
            cleanup = await self._kernel_tool(
                mcp,
                "run_repo_cleanup",
                hosts=hosts,
            )
            if cleanup.get("status") not in ("ok", "already_clean"):
                # Fail closed (review round 2): an unremoved repository
                # is an unverified package source left behind for the
                # next ticket on these hosts. Stop the step instead of
                # warning and proceeding — the kernel is verified, but
                # the ticket cannot complete until an operator removes
                # the repo or a cleanup retry succeeds.
                await self._kernel_step_stop(
                    ticket_id,
                    "repository cleanup failed after the kernel "
                    "transition; the ticket repo file may remain on "
                    f"{', '.join(hosts)}",
                    cleanup,
                )
                return

        # Hand the result to the normal completion path: it derives the
        # verification from the checkpoint and advances the plan.
        completion = LLMResponse(
            text="",
            tool_calls=[
                ToolCall(
                    id="kernel-step",
                    name="submit_provisioning_result",
                    input={
                        "provisioning_complete": True,
                        "hosts_provisioned": hosts,
                        "harness_name": harness,
                        "harness_version": "unchanged by kernel transition",
                        "configuration_applied": {
                            "kernel_transition": {
                                "step_id": step_id,
                                "kernel": spec.to_dict(),
                                "actions": actions,
                                "intent_id": intent_id,
                                "approval_request_id": approval_id,
                            }
                        },
                        "verification": {
                            "status": "verified",
                            "details": "derived from the kernel transition checkpoint",
                        },
                        "notes": f"Deterministic kernel transition to {spec.nevra}.",
                    },
                )
            ],
        )
        await self._handle_completion(ticket_id, completion)

    async def _kernel_step_verification(
        self, ticket_id: str, ticket: dict[str, Any] | None = None
    ) -> tuple[bool, bool, dict[str, str]]:
        """Return (is_kernel_step, verified, verification) from the checkpoint.

        A kernel step's completion evidence is the reboot tool's checkpoint
        (written after live probes), never the LLM's own summary.
        """
        from agents.provisioning.kernel import (
            current_kernel_step,
            verification_from_transition,
        )

        if ticket is None:
            ticket = await self._get_ticket(ticket_id)
        step = current_kernel_step(ticket)
        if step is None:
            return False, False, {}
        ok, verification = verification_from_transition(ticket, step[0])
        return True, ok, verification

    async def _validate_submit_call(
        self, ticket_id: str, submit_call: ToolCall
    ) -> str | None:
        """Reject malformed provisioning results while the agent can still retry."""
        payload = submit_call.input
        errors: list[str] = []
        if not isinstance(payload, dict):
            payload = {}
            errors.append("the submission must be a JSON object")
        else:
            allowed_fields = {
                "provisioning_complete",
                "hosts_provisioned",
                "verification",
                "harness_version",
                "harness_name",
                "configuration_applied",
                "k3s_installed",
                "k3s_version",
                "notes",
            }
            unexpected_fields = sorted(set(payload) - allowed_fields)
            if unexpected_fields:
                errors.append("unsupported fields: " + ", ".join(unexpected_fields))

        complete = payload.get("provisioning_complete")
        if type(complete) is not bool:
            errors.append("provisioning_complete must be a boolean")

        hosts = payload.get("hosts_provisioned")
        valid_hosts = isinstance(hosts, list) and all(
            isinstance(host, str) and host.strip() and host == host.strip()
            for host in hosts
        )
        if not valid_hosts:
            errors.append(
                "hosts_provisioned must be a list of non-empty host strings "
                "without surrounding whitespace"
            )
        elif complete is True and not hosts:
            errors.append(
                "a completed result must include at least one provisioned host"
            )

        string_fields = (
            "harness_name",
            "harness_version",
            "k3s_version",
            "notes",
        )
        for field in string_fields:
            if field in payload and not isinstance(payload[field], str):
                errors.append(f"{field} must be a string")

        if "configuration_applied" in payload and not isinstance(
            payload["configuration_applied"], dict
        ):
            errors.append("configuration_applied must be an object")
        if "k3s_installed" in payload and type(payload["k3s_installed"]) is not bool:
            errors.append("k3s_installed must be a boolean")

        verification = payload.get("verification")
        if complete is True or "verification" in payload:
            if not isinstance(verification, dict):
                errors.append("verification must be an object with status and details")
            else:
                status = verification.get("status")
                details = verification.get("details")
                if not isinstance(status, str) or status not in {
                    "verified",
                    "failed",
                    "partial",
                }:
                    errors.append(
                        "verification.status must be 'verified', 'failed', or 'partial'"
                    )
                if not isinstance(details, str) or not details.strip():
                    errors.append("verification.details must be a non-empty string")
                if complete is True and status != "verified":
                    errors.append(
                        "a completed result requires verification.status='verified'"
                    )

        if complete is False:
            notes = payload.get("notes")
            if not isinstance(notes, str) or not notes.strip():
                errors.append(
                    "an incomplete result must include actionable, non-empty notes"
                )

        if valid_hosts and hosts and not errors:
            try:
                ticket = await self._get_ticket(ticket_id)
                custom_fields = ticket.get("custom_fields", {})
                allocation = custom_fields.get("assigned_hardware_ips", {})
            except Exception as exc:
                logger.warning(
                    "[provisioning] %s: unable to verify submitted hosts against "
                    "ticket allocation: %s",
                    ticket_id,
                    exc,
                )
                errors.append(
                    "could not verify hosts_provisioned against the ticket allocation"
                )
            else:
                allocated_hosts: set[str] = set()
                if isinstance(allocation, dict):
                    controller = allocation.get("controller")
                    targets = allocation.get("targets", [])
                    if isinstance(controller, str) and controller.strip():
                        allocated_hosts.add(controller)
                    if isinstance(targets, list):
                        allocated_hosts.update(
                            target
                            for target in targets
                            if isinstance(target, str) and target.strip()
                        )
                if not allocated_hosts:
                    errors.append(
                        "the ticket has no assigned_hardware_ips host identities; "
                        "request a resource or platform allocation before completing"
                    )
                else:
                    unallocated_hosts = sorted(set(hosts) - allocated_hosts)
                    if unallocated_hosts:
                        errors.append(
                            "hosts_provisioned contains hosts not allocated to this "
                            "ticket: "
                            + ", ".join(unallocated_hosts)
                            + ". Use only assigned_hardware_ips identities"
                        )

        if not errors and complete is True:
            (
                is_kernel_step,
                verified,
                verification,
            ) = await self._kernel_step_verification(ticket_id)
            if is_kernel_step and not verified:
                errors.append(
                    "this is a kernel transition step and its checkpoint is not "
                    f"verified ({verification.get('details', 'no checkpoint')}); "
                    "run reboot_hosts_and_verify (or verify_kernel_state to "
                    "reconcile) until every host is verified, or submit "
                    "provisioning_complete=false"
                )

        if not errors:
            return None

        # Invalidate stale provisioning output before returning the error to the
        # same agent. Keep hosts and SSH/allocation identity intact because the
        # resource and platform agents may own those fields.
        await self._update_fields(
            ticket_id,
            {
                "provisioning_complete": False,
                "provisioning_verification": {
                    "status": "not_verified",
                    "details": "The latest provisioning submission was rejected.",
                },
                "harness_version": "unknown",
                "configuration_applied": {},
                "k3s_installed": False,
                "k3s_version": "",
            },
        )
        return (
            "Invalid provisioning result: "
            + "; ".join(errors)
            + ". Correct the submission and call submit_provisioning_result again. "
            + "If provisioning is incomplete, set provisioning_complete=false and "
            + "include actionable notes."
        )

    async def _is_self_installing(self, harness: str) -> bool:
        """Check whether a harness needs host-side installation.

        Queries the skill provider's BenchmarkSuite metadata.
        Self-installing harnesses (containers, built-in tools)
        need no provisioning beyond flash + boot.
        """
        provider = getattr(self, "_skill_provider", None)
        if provider is not None:
            harness_provider = None
            if hasattr(provider, "get_provider"):
                harness_provider = provider.get_provider(harness)
            if harness_provider is not None:
                try:
                    suites = await harness_provider.list_benchmarks()
                    if suites:
                        return suites[0].self_installing
                except Exception:
                    pass
        # Standalone benchmarks (boot-time) are checked via
        # the catalog's STANDALONE_BENCHMARKS tuple.
        from providers.skills.catalog import STANDALONE_BENCHMARKS

        for suite in STANDALONE_BENCHMARKS:
            if suite.harness == harness:
                return suite.self_installing
        return False

    _PROVISIONING_DENY_TOOLS: frozenset[str] = frozenset(
        {
            "deploy_secret",
            "get_private_config",
            "install_harness",
            "install_packages",
            "install_k3s",
            "verify_harness_install",
            "check_existing_install",
            "update_install",
            "uninstall_harness",
            "ensure_harness_installed",
            "ensure_prerequisites",
            "check_platform_contract",
        }
    )

    async def _apply_system_config(
        self,
        ticket_id: str,
        hosts: list[str],
        config_ops: list[dict[str, Any]],
        cf: dict[str, Any],
    ) -> None:
        """Apply structured system configuration operations.

        Runs deterministic operations on provisioned hosts
        before the benchmark starts. No LLM involved — code
        executes each operation directly via SSH.

        Supported actions:
            write_file: Write content to a file on the host
            run_command: Execute a shell command on the host
        """
        import asyncio as _asyncio

        ssh_user = cf.get("ssh_user", "root")
        ssh_password = cf.get("ssh_password", "password")

        async def _ssh_run(
            host: str,
            cmd: str,
            timeout: int = 30,
        ) -> tuple[int, str, str]:
            """Run a command via sshpass for password auth."""
            proc = await _asyncio.create_subprocess_exec(
                "sshpass",
                "-p",
                ssh_password,
                "ssh",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
                f"{ssh_user}@{host}",
                cmd,
                stdout=_asyncio.subprocess.PIPE,
                stderr=_asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await _asyncio.wait_for(
                    proc.communicate(),
                    timeout=timeout,
                )
                return (
                    proc.returncode or 0,
                    stdout.decode(errors="replace"),
                    stderr.decode(errors="replace"),
                )
            except _asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
                raise

        # Use first host (controller) for config
        host = hosts[0] if hosts else None
        if not host:
            logger.warning(f"[provisioning] {ticket_id}: no hosts for system_config")
            return

        # Strip jumpstarter: prefix if present
        if host.startswith("jumpstarter:"):
            # Resolve via ticket's assigned IPs
            assigned = cf.get(
                "assigned_hardware_ips",
                {},
            )
            host = assigned.get("controller", host)
            if host.startswith("jumpstarter:"):
                logger.warning(
                    f"[provisioning] {ticket_id}: cannot "
                    f"resolve jumpstarter host for "
                    f"system_config"
                )
                return

        if not isinstance(config_ops, list):
            logger.warning(
                f"[provisioning] {ticket_id}: system_config "
                f"must be a list, got {type(config_ops).__name__}"
            )
            await self._add_comment(
                ticket_id,
                f"**System Configuration Error:** system_config must be a list, got `{type(config_ops).__name__}`",
            )
            return

        applied: list[str] = []
        errors: list[str] = []

        for i, op in enumerate(config_ops):
            if not isinstance(op, dict):
                errors.append(
                    f"Op {i}: expected dictionary configuration, got {type(op).__name__}"
                )
                continue

            action = op.get("action", "")
            try:
                if action == "write_file":
                    path = op.get("path", "")
                    content = op.get("content", "")
                    if not path:
                        errors.append(f"Op {i}: write_file missing path")
                        continue

                    import base64

                    escaped_path = path.replace("'", "'\\''")

                    # Create parent directory
                    mkdir_cmd = f"mkdir -p $(dirname '{escaped_path}')"
                    await _ssh_run(
                        host,
                        mkdir_cmd,
                        timeout=10,
                    )
                    # Write file via base64 to prevent heredoc/quoting issues
                    encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")
                    write_cmd = f"echo '{encoded}' | base64 -d > '{escaped_path}'"
                    rc, out, err = await _ssh_run(
                        host,
                        write_cmd,
                        timeout=10,
                    )
                    if rc == 0:
                        applied.append(f"write_file: {path}")
                        logger.info(f"[provisioning] {ticket_id}: wrote {path}")
                    else:
                        errors.append(f"Op {i}: write_file {path} failed: {err}")

                elif action == "run_command":
                    command = op.get("command", "")
                    if not command:
                        errors.append(f"Op {i}: run_command missing command")
                        continue
                    timeout = op.get("timeout", 120)
                    rc, out, err = await _ssh_run(
                        host,
                        command,
                        timeout=timeout,
                    )
                    # Capture output for visibility
                    output_summary = out.strip()
                    if len(output_summary) > 500:
                        output_summary = output_summary[-500:]
                    if rc == 0:
                        entry = f"run_command: {command}"
                        if output_summary:
                            entry += f" -> {output_summary}"
                        applied.append(entry)
                        logger.info(f"[provisioning] {ticket_id}: ran: {command}")
                    else:
                        errors.append(
                            f"Op {i}: run_command '{command}' failed (exit {rc}): {err}"
                        )

                else:
                    errors.append(f"Op {i}: unknown action '{action}'")

            except _asyncio.TimeoutError:
                errors.append(
                    f"Op {i}: {action} timed out after {op.get('timeout', 120)}s"
                )
            except Exception as e:
                errors.append(f"Op {i}: {action} error: {e}")

        # Report results
        summary_parts = []
        if applied:
            summary_parts.append(
                "**System Configuration Applied:**\n"
                + "\n".join(f"- {a}" for a in applied)
            )
        if errors:
            summary_parts.append(
                "**System Configuration Errors:**\n"
                + "\n".join(f"- {e}" for e in errors)
            )
        if summary_parts:
            await self._add_comment(
                ticket_id,
                "\n\n".join(summary_parts),
            )

        # Store applied config on the ticket
        await self._update_fields(
            ticket_id,
            {
                "system_config_applied": applied,
                "system_config_errors": errors,
            },
        )

    async def _auto_complete_jumpstarter(
        self,
        ticket_id: str,
        cf: dict[str, Any],
    ) -> None:
        """Auto-complete provisioning for Jumpstarter boards.

        When the platform agent has already flashed and
        verified the board, and the harness is self-
        installing, there's nothing for the provisioning
        agent to do. Set provisioning_complete and advance.
        """
        hosts = cf.get("hosts_provisioned", [])
        harness = cf.get("directives", {}).get("harness", "unknown")
        fields = {
            "provisioning_complete": True,
            "harness_name": harness,
            "harness_version": "platform-provisioned",
            "provisioning_verification": {
                "status": "verified",
                "details": "The platform agent flashed and verified the board.",
            },
        }
        if cf.get("ssh_user"):
            fields["ssh_user"] = cf["ssh_user"]
        if cf.get("ssh_key_path"):
            fields["ssh_key_path"] = cf["ssh_key_path"]

        await self._update_fields(ticket_id, fields)

        summary = (
            "**Provisioning Complete** "
            "(platform-provisioned)\n\n"
            f"- **Hosts:** {', '.join(str(h) for h in hosts)}\n"
            f"- **Harness:** {harness}\n"
            "- **Note:** Board flashed and verified by "
            "platform agent. Self-installing harness "
            "requires no additional provisioning.\n"
        )
        await self._add_comment(ticket_id, summary)

        if not await self._plan_controls_next_transition(ticket_id):
            await self._transition_ticket(
                ticket_id,
                "executing_benchmark",
                comment="Provisioning complete (auto)",
            )

    def _apply_tool_scoping(self, ticket: dict[str, Any]) -> None:
        """Hide install/config tools for self-installing harnesses."""
        if getattr(self, "_harness_self_installing", False):
            self.tools = [
                t for t in self.tools if t.name not in self._PROVISIONING_DENY_TOOLS
            ]

    async def run(self, ticket_id: str) -> None:
        self._ticket_id = ticket_id

        prov_server = str(Path(__file__).with_name("server.py"))
        infra_server = str(Path(__file__).parent.parent / "infra" / "server.py")

        mcp = AgentMCPClient()
        await mcp.connect_ticket_server(
            prov_server,
            name="provisioning",
            ticket_id=ticket_id,
            state_store_url=self.store_url,
            agent_name=self.agent_name,
        )
        await mcp.connect_ticket_server(
            infra_server,
            name="infra",
            ticket_id=ticket_id,
            state_store_url=self.store_url,
            agent_name=self.agent_name,
        )

        self._mcp = mcp

        # Resolve whether this harness is self-installing
        # (no host-side provisioning needed). Cache it for
        # use by _apply_tool_scoping and the auto-complete check.
        ticket = await self._get_ticket(ticket_id)
        cf = ticket.get("custom_fields", {})
        harness = self._effective_harness(
            cf.get("directives", {}), getattr(self, "_skill_provider", None)
        )
        self._harness_self_installing = await self._is_self_installing(harness)
        is_jumpstarter = cf.get("resource_provider") == "jumpstarter"

        # A kernel transition step has no decisions left for a model to make:
        # the plan names the kernel, the ticket names the hosts, and every
        # action is a fixed, audited tool call.  Run it in code (the same
        # pattern as deterministic Jumpstarter provisioning) unless the
        # ticket explicitly asks for the LLM path.
        if self._kernel_step_is_deterministic(cf):
            try:
                await self._run_kernel_step(ticket_id, ticket, mcp)
            finally:
                await mcp.disconnect()
                self._mcp = None
            return

        if is_jumpstarter and (not harness or self._harness_self_installing):
            # Platform agent already provisioned the
            # board. For self-installing harnesses,
            # there's nothing more to do — unless
            # system_config directives require post-
            # flash configuration.
            if cf.get("platform_ready") and cf.get("hosts_provisioned"):
                directives = cf.get("directives", {})
                system_config = directives.get("system_config")
                # Fall back to custom_fields.system_config when
                # directives has no system_config, or triage
                # stored a description string instead of a list.
                if not isinstance(system_config, list) or not system_config:
                    system_config = cf.get("system_config", [])
                if system_config:
                    hosts = cf["hosts_provisioned"]
                    await self._apply_system_config(
                        ticket_id,
                        hosts,
                        system_config,
                        cf,
                    )
                    # Abort if any system_config operation
                    # failed — proceeding to benchmark with
                    # incomplete configuration is meaningless.
                    updated = await self._get_ticket(ticket_id)
                    config_errors = updated.get("custom_fields", {}).get(
                        "system_config_errors", []
                    )
                    if config_errors:
                        await self._add_comment(
                            ticket_id,
                            "**Provisioning aborted:** "
                            "system_config errors prevent "
                            "meaningful benchmark execution.",
                        )
                        await self._transition_ticket(
                            ticket_id,
                            "awaiting_customer_guidance",
                            comment=(
                                "System configuration failed. "
                                "Review errors above and "
                                "decide whether to retry "
                                "or abort."
                            ),
                        )
                        await mcp.disconnect()
                        self._mcp = None
                        return
                from agents.provisioning.kernel import current_kernel_step

                if current_kernel_step({"custom_fields": cf}) is None:
                    await self._auto_complete_jumpstarter(
                        ticket_id,
                        cf,
                    )
                    await mcp.disconnect()
                    self._mcp = None
                    return

        mcp_tools = await mcp.list_tools()
        self.tools = mcp_tools + self.tools

        # For Jumpstarter with non-self-installing
        # harnesses, keep install tools but don't
        # attach Jumpstarter MCP (platform agent
        # handled device operations).
        # For non-Jumpstarter, apply harness-based
        # scoping as before.
        if not is_jumpstarter:
            self._apply_tool_scoping(ticket)

        try:
            await super().run(ticket_id)
        finally:
            await mcp.disconnect()
            self._mcp = None

    def _system_prompt(self, ticket: dict[str, Any]) -> str:
        cf = ticket.get("custom_fields", {})
        directives = cf.get("directives", {})
        provider = cf.get("resource_provider") or directives.get("resource_provider")
        endpoint = directives.get("endpoint_type", "remotehosts")
        harness = self._effective_harness(
            directives, getattr(self, "_skill_provider", None)
        )

        # Cloud access requirements for this subject are configured guidance;
        # provider fragments must not duplicate them or SSH phase ownership.
        gateway_cloud_access = harness == "crucible" and provider == "aws"
        fragments = self._load_prompt_fragments(
            Path(__file__).parent,
            resource_provider=None if gateway_cloud_access else provider,
            endpoint_type=None if harness == "crucible" else endpoint,
        )

        # Load harness-specific provisioning fragment.
        harness_fragment = ""
        harness_fragment = self._load_prompt_fragment(
            Path(__file__).parent / "prompts", harness
        )

        prompt = PROVISIONING_BASE_PROMPT
        if getattr(self, "_harness_self_installing", False):
            prompt += (
                "\n\n## Read-Only Checks for Self-Installing Harnesses\n\n"
                "This harness does not need host-side harness installation. "
                "This instruction overrides the base prompt's private "
                "provisioning-config lookup and installation steps: do not "
                "call get_private_config for this check. "
                "Do not call install_harness, ensure_harness_installed, "
                "install_packages, or ensure_prerequisites. Call the "
                "read-only check_host_prerequisites tool for the allocated "
                "target host(s). Use its per-command results to verify only "
                "the runtime required by this harness (for Arcaflow plugins, "
                "Podman). Missing unrelated utilities do not block the run. "
                "If a required runtime is missing, do not install it; report "
                "the specific missing dependency with request_clarification. "
                "Do not try to execute the benchmark in this phase; the "
                "benchmark agent owns workload execution. If the required "
                "runtime is present and SSH connectivity is verified, submit "
                "provisioning as complete with that evidence."
            )
        if harness == "crucible":
            prompt += "\n\n" + skill_context_prompt("harness/crucible")
        if gateway_cloud_access:
            prompt += (
                "\n\n## Provisioning Access Contract\n\n"
                "Before installation, verify that the ticket's current SSH "
                "account can perform the operations required by the approved "
                "settings and software documentation. If account setup is "
                "needed, use an available authorized bootstrap tool and "
                "verify access before continuing. Do not assume a tool's "
                "user argument changes its ticket-bound SSH context. If "
                "the required access cannot be established with available "
                "tools, request clarification and report the missing "
                "capability. Controller-to-endpoint key setup remains the "
                "benchmark phase's responsibility."
            )
        if harness_fragment:
            prompt += f"\n\n{harness_fragment}"
        if fragments:
            prompt += f"\n\n{fragments}"
        return prompt

    def _build_messages(self, ticket: dict[str, Any]) -> list[dict[str, Any]]:
        cf = ticket.get("custom_fields", {})
        scoped = self._get_scoped_context(ticket, "provision")
        if scoped is not None:
            content = (
                f"## Performance Test Request\n\n"
                f"**Ticket ID:** {ticket['id']}\n\n"
                f"{scoped}\n"
            )
        else:
            content = (
                f"## Performance Test Request\n\n"
                f"**Ticket ID:** {ticket['id']}\n"
                f"**Summary:** {ticket['summary']}\n\n"
                f"**Description:**\n{ticket['description']}\n"
            )

        if cf.get("ssh_hardware_ips"):
            content += f"\n## SSH Addresses (use these for SSH/SCP)\n```json\n{json.dumps(cf['ssh_hardware_ips'], indent=2)}\n```\n"
            content += f"\n## Private Addresses (for run-file host entries)\n```json\n{json.dumps(cf.get('assigned_hardware_ips', {}), indent=2)}\n```\n"
        elif cf.get("assigned_hardware_ips"):
            content += f"\n## Assigned Hardware\n```json\n{json.dumps(cf['assigned_hardware_ips'], indent=2)}\n```\n"
        if cf.get("ssh_user"):
            content += f"\n**SSH User:** {cf['ssh_user']}\n"
        if cf.get("ssh_key_path"):
            content += f"**SSH Key:** {cf['ssh_key_path']}\n"
        if cf.get("fresh_host"):
            content += (
                "\n**Fresh Host:** true (freshly provisioned, no existing harness)\n"
            )
        if cf.get("directives"):
            content += f"\n## User Directives\n```json\n{json.dumps(cf['directives'], indent=2)}\n```\n"
        if cf.get("benchmark_suite"):
            content += f"\n**Benchmark Suite:** {cf['benchmark_suite']}\n"
        if cf.get("resource_provider_metadata"):
            metadata = cf["resource_provider_metadata"]
            content += f"\n## Provider Metadata\n```json\n{json.dumps(metadata, indent=2)}\n```\n"

            # Surface Jumpstarter lease info for the agent
            if cf.get("resource_provider") == "jumpstarter":
                lease_id = cf.get("resource_reservation_id") or metadata.get(
                    "lease_id", ""
                )
                content += (
                    f"\n## Jumpstarter Device\n"
                    f"- **Lease ID:** {lease_id}\n"
                    f"- **Board:** {metadata.get('exporter_name', 'unknown')}\n"
                    f"- **Selector:** {metadata.get('selector', 'unknown')}\n"
                    f"- This device needs flashing before use.\n"
                    f"  Follow the Jumpstarter provisioning\n"
                    f"  prompt above.\n"
                )

                flash = cf.get("jumpstarter_flash", {})
                if flash.get("flash_command"):
                    content += (
                        f"\n## Pre-Resolved Flash Command\n"
                        f"```\n{flash['flash_command']}\n```\n"
                        f"Run this via `jmp_run` with "
                        f"timeout_seconds=600.\n"
                    )
                    if flash.get("ssh_public_key"):
                        content += (
                            f"\n## SSH Public Key "
                            f"(for key injection)\n"
                            f"```\n"
                            f"{flash['ssh_public_key']}\n"
                            f"```\n"
                            f"**Key path:** "
                            f"{flash.get('ssh_key_path', '/root/.ssh/id_rsa')}\n"
                        )
                elif flash.get("error"):
                    content += f"\n## Image Resolution Error\n{flash['error']}\n"
                    if flash.get("available_variants"):
                        content += (
                            f"Available variants: "
                            f"{json.dumps(flash['available_variants'])}\n"
                        )

        # Agent/system handoff comments are pipeline noise — only surface
        # user comments so the agent focuses on human intent, not its own
        # prior outputs.
        comments = [
            c for c in (ticket.get("comments") or []) if c.get("author") == "user"
        ]
        if comments:
            content += "\n## Previous Comments\n"
            for comment in comments:
                content += f"\n**{comment['author']}:** {comment['body']}\n"

        return [{"role": "user", "content": content}]

    async def _handle_completion(self, ticket_id: str, response: LLMResponse) -> None:
        result = self._get_submit_result(response)
        if not result:
            result = self._parse_json_response(response.text)
        if not result:
            result = {
                "provisioning_complete": False,
                "notes": "Could not produce structured output",
            }

        # Self-installing harnesses don't need
        # provisioning to install them. If the LLM
        # reports incomplete because install_harness
        # failed, override only when verification succeeded.
        harness = result.get("harness_name", "unknown")
        prov_complete = result.get("provisioning_complete", False)
        verification = result.get("verification")

        # One ticket read serves both the kernel gate and the SSH role
        # projection below.  A completed kernel step is judged by its
        # checkpoint; the submit-time validator already read the live ticket,
        # so a lookup failure here is logged rather than treated as evidence.
        ticket_cache: dict[str, Any] | None = None
        is_kernel_step, kernel_verified, kernel_verification = False, False, {}
        if prov_complete or result.get("hosts_provisioned"):
            try:
                fetched = await self._get_ticket(ticket_id)
                ticket_cache = fetched if isinstance(fetched, dict) else None
            except Exception as exc:
                logger.warning(
                    "[provisioning] %s: ticket lookup failed: %s", ticket_id, exc
                )
        if prov_complete and ticket_cache is not None:
            (
                is_kernel_step,
                kernel_verified,
                kernel_verification,
            ) = await self._kernel_step_verification(ticket_id, ticket_cache)
        if is_kernel_step:
            # Code decides whether a kernel step is complete: the checkpoint,
            # not the model's summary, is the evidence.
            verification = kernel_verification
            if not kernel_verified and prov_complete:
                logger.warning(
                    "[provisioning] %s: forcing provisioning_complete=False; "
                    "kernel transition not verified",
                    ticket_id,
                )
                prov_complete = False
        if (
            not prov_complete
            and getattr(self, "_harness_self_installing", False)
            and result.get("hosts_provisioned")
            and isinstance(verification, dict)
            and verification.get("status") == "verified"
        ):
            prov_complete = True
            logger.info(
                f"[provisioning] Overriding provisioning_complete "
                f"for self-installing harness {harness}"
            )

        fields = {
            "provisioning_complete": prov_complete,
            "harness_version": result.get("harness_version", "unknown"),
            "harness_name": harness,
            "configuration_applied": result.get("configuration_applied", {}),
            "k3s_installed": result.get("k3s_installed", False),
            "k3s_version": result.get("k3s_version", ""),
            "provisioning_verification": verification
            or {
                "status": "incomplete",
                "details": result.get("notes", "Provisioning is incomplete."),
            },
        }
        # hosts_provisioned can be owned by the resource or platform agent.
        # Keep its existing allocation when an incomplete report has no hosts.
        if result.get("hosts_provisioned"):
            fields["hosts_provisioned"] = result["hosts_provisioned"]
        if result.get("k3s_installed"):
            fields["k3s_version"] = result.get("k3s_version", "unknown")

        # Project the resource-owned SSH endpoints onto the
        # assigned host roles reported as provisioned. Submitted
        # host identities never replace the SSH addresses.
        ssh_ips = None
        if fields.get("hosts_provisioned"):
            allocation_loaded = False
            try:
                ticket = (
                    ticket_cache
                    if ticket_cache is not None
                    else await self._get_ticket(ticket_id)
                )
                custom_fields = ticket.get("custom_fields", {})
                assigned = custom_fields.get("assigned_hardware_ips", {})
                ssh_assigned = custom_fields.get("ssh_hardware_ips", {})
                allocation_loaded = True
            except Exception:
                assigned = {}
                ssh_assigned = {}
            hosts = [str(h) for h in fields["hosts_provisioned"]]
            host_set = set(hosts)
            if not allocation_loaded:
                # Preserve the existing SSH identity when the ticket lookup
                # fails; submitted hosts may be private allocation addresses.
                ssh_ips = None
            elif assigned:
                ssh_ips = {"targets": []}
                controller = assigned.get("controller")
                if controller in host_set:
                    ssh_ips["controller"] = (
                        (ssh_assigned.get("controller") or controller)
                        if isinstance(ssh_assigned, dict)
                        else controller
                    )
                assigned_targets = assigned.get("targets", [])
                if not isinstance(assigned_targets, list):
                    assigned_targets = []
                ssh_targets = (
                    ssh_assigned.get("targets", [])
                    if isinstance(ssh_assigned, dict)
                    else []
                )
                if not isinstance(ssh_targets, list):
                    ssh_targets = []
                for index, target in enumerate(assigned_targets):
                    if target in host_set:
                        ssh_target = (
                            ssh_targets[index] if index < len(ssh_targets) else None
                        )
                        ssh_ips["targets"].append(ssh_target or target)
                if not ssh_ips.get("controller") and not ssh_ips["targets"]:
                    ssh_ips = None
            elif isinstance(ssh_assigned, dict) and ssh_assigned:
                # Without private role identities, do not replace a separate
                # resource-owned SSH map with submitted host strings.
                ssh_ips = None
            else:
                # No role map available — fall back to first
                # provisioned host as controller (role-blind
                # but self-consistent; better than stale data).
                first_ip = hosts[0] if hosts else ""
                if first_ip:
                    ssh_ips = {
                        "controller": first_ip,
                        "targets": [first_ip],
                    }
        if ssh_ips:
            fields["ssh_hardware_ips"] = ssh_ips

        # Never write assigned_hardware_ips — that field is
        # owned by the resource agent (and the platform agent
        # for Jumpstarter).  The submit tool schema does not
        # include it, so the LLM never provides it.  If a
        # deterministic caller somehow does, log and discard.
        if "assigned_hardware_ips" in result:
            logger.warning(
                "[provisioning] %s: ignoring unexpected "
                "assigned_hardware_ips in provisioning result "
                "(field owned by resource/platform agent)",
                ticket_id,
            )
        await self._update_fields(ticket_id, fields)

        try:
            from providers.workspace.manager import WorkspaceManager

            ws_mgr = WorkspaceManager(ticket_id=ticket_id)
            summary_payload = {
                "provisioning_complete": prov_complete,
                "hosts_provisioned": fields.get("hosts_provisioned", []),
                "harness_name": harness,
                "harness_version": fields.get("harness_version", "unknown"),
                "configuration_applied": fields.get("configuration_applied", {}),
                "verification": fields["provisioning_verification"],
                "ssh_hardware_ips": fields.get("ssh_hardware_ips", {}),
                "notes": result.get("notes", ""),
            }
            ws_mgr.save_file(
                "provisioning_summary.json",
                json.dumps(summary_payload, indent=2),
                overwrite=True,
            )
        except Exception as e:
            logger.debug(
                f"[provisioning] Failed to save workspace provisioning summary: {e}"
            )

        hosts = [
            str(h) if not isinstance(h, dict) else h.get("host", h.get("ip", str(h)))
            for h in fields.get("hosts_provisioned", [])
        ]
        if prov_complete:
            summary = (
                f"**Provisioning Complete**\n\n"
                f"- **Hosts:** {', '.join(hosts)}\n"
                f"- **Harness:** {fields['harness_name']} "
                f"(version: {fields['harness_version']})\n"
                f"- **Verification:** {verification['details']}\n"
            )
        else:
            summary = (
                "**Provisioning Incomplete**\n\n"
                f"- **Hosts:** {', '.join(hosts) if hosts else 'none reported'}\n"
                f"- **Harness:** {fields['harness_name']}\n"
            )
        config = fields["configuration_applied"]
        if config:
            summary += "- **Configuration:**\n"
            for host, items in config.items():
                if isinstance(items, list):
                    summary += f"  - {host}: {', '.join(str(i) for i in items)}\n"
                else:
                    summary += f"  - {host}: {items}\n"
        if result.get("notes"):
            summary += f"- **Notes:** {result['notes']}\n"

        await self._add_comment(ticket_id, summary)
        if not prov_complete:
            await self._transition_ticket(
                ticket_id,
                "awaiting_customer_guidance",
                comment="Provisioning is incomplete. Review the notes and decide how to proceed.",
            )
            return
        if await self._plan_controls_next_transition(ticket_id):
            return
        await self._transition_ticket(
            ticket_id,
            "executing_benchmark",
            comment="Provisioning complete, ready for benchmark execution",
        )
