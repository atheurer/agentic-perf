from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from agents.base import AgentBase
from agents.mcp_client import AgentMCPClient
from providers.events import EventBus
from providers.llm.base import LLMProvider, LLMResponse, ToolDefinition
from providers.skills.base import EXECUTION_MODEL_DIRECT
from providers.skills.repo_cache import RepoCache
from providers.tracing import current_trace_context

from .prompts import BENCHMARK_BASE_PROMPT

logger = logging.getLogger(__name__)


def _filter_external_tools(
    tools: list[ToolDefinition],
    routing: dict[str, str],
    connected_external: list[str],
    enabled_external: dict[str, set[str] | None],
) -> list[ToolDefinition]:
    """Apply each external MCP server's independent visibility policy."""
    external = set(connected_external)
    # Accept the pre-per-server set shape for callers that provide a mocked
    # connector; production ``connect_external_servers`` always returns the
    # mapping below.
    if isinstance(enabled_external, set):
        return [
            tool
            for tool in tools
            if routing.get(tool.name) not in external or tool.name in enabled_external
        ]
    return [
        tool
        for tool in tools
        if routing.get(tool.name) not in external
        or enabled_external.get(routing.get(tool.name)) is None
        or tool.name in (enabled_external.get(routing.get(tool.name)) or set())
    ]


_LOCAL_TOOLS = [
    ToolDefinition(
        name="request_clarification",
        description="Ask the user for clarification. Pauses the ticket for human input.",
        input_schema={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "Question to ask",
                },
            },
            "required": ["question"],
        },
    ),
    ToolDefinition(
        name="present_runfile_for_approval",
        description=(
            "Present the immutable run-file identified by a successful validation "
            "to the user for review and approval. The user can approve, request "
            "changes, or reject. Returns a status string."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "benchmark": {
                    "type": "string",
                    "description": "Benchmark name for context",
                },
                "summary": {
                    "type": "string",
                    "description": "Brief summary of what this run-file will do",
                },
                "validation_id": {
                    "type": "string",
                    "description": "Immutable validation record to approve",
                },
            },
            "required": ["validation_id"],
        },
    ),
    ToolDefinition(
        name="resolve_benchmark_approval",
        description=(
            "Resolve the active immutable benchmark approval after interpreting "
            "the user's natural-language reply. Use approved only when the user "
            "clearly authorizes execution; use changes_requested or rejected "
            "otherwise. Never execute a benchmark while approval is pending."
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
    ToolDefinition(
        name="submit_benchmark_result",
        description=(
            "Submit the benchmark execution result when the run completes or fails."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "validation_id": {
                    "type": "string",
                    "description": "Validation identity returned by execute_benchmark",
                },
                "benchmark_status": {
                    "type": "string",
                    "enum": ["completed", "failed"],
                },
                "run_file_used": {"type": "object"},
                "benchmark_duration": {"type": ["integer", "null"]},
                "notes": {"type": "string"},
            },
            "required": ["run_id", "benchmark_status"],
        },
    ),
]


class BenchmarkAgent(AgentBase):
    def __init__(
        self,
        llm_provider: LLMProvider,
        state_store_url: str,
        skill_provider=None,
        secrets_provider=None,
        event_bus: EventBus | None = None,
        repo_cache: RepoCache | None = None,
    ) -> None:
        self._skill_provider = skill_provider
        self._secrets_provider = secrets_provider
        self._repo_cache = repo_cache
        self._ticket_id: str | None = None
        self._active_validation_id: str | None = None
        self._active_approval_request_id: str | None = None
        self._active_approval: dict[str, Any] | None = None

        local_tools = list(_LOCAL_TOOLS)

        async def _request_clarification(question: str) -> str:
            return await self._do_request_clarification(question)

        async def _present_runfile_for_approval(
            benchmark: str | None = None,
            summary: str | None = None,
            validation_id: str = "",
        ) -> str:
            return await self._request_benchmark_approval(
                benchmark=benchmark,
                summary=summary,
                validation_id=validation_id,
            )

        async def _resolve_benchmark_approval(
            decision: str,
            reason: str | None = None,
        ) -> str:
            return await self._resolve_benchmark_approval(
                decision=decision,
                reason=reason,
            )

        local_handlers = {
            "request_clarification": _request_clarification,
            "present_runfile_for_approval": _present_runfile_for_approval,
            "resolve_benchmark_approval": _resolve_benchmark_approval,
        }

        super().__init__(
            agent_name="benchmark-agent",
            llm_provider=llm_provider,
            state_store_url=state_store_url,
            tools=local_tools,
            tool_handlers=local_handlers,
            event_bus=event_bus,
        )

    async def _do_request_clarification(self, question: str) -> str:
        if self._ticket_id:
            # Fleet investigation: failures are data points.
            # Auto-submit as failed instead of escalating
            # to HITL — the fleet coordinator will record
            # the result and route to the next board.
            from providers.fleet import is_fleet_investigation

            ticket = await self._get_ticket(self._ticket_id)
            cf = ticket.get("custom_fields", {})
            if is_fleet_investigation(cf):
                await self._add_comment(
                    self._ticket_id,
                    "**Fleet: auto-routing to coordinator**"
                    f"\n\nAgent wanted clarification:"
                    f"\n{question[:500]}",
                )
                await self._transition_ticket(
                    self._ticket_id,
                    "coordinating_fleet",
                    comment=("Fleet: benchmark issue, routing to coordinator"),
                )
                # Raise HITLDriftError to exit the LLM
                # loop cleanly. The base class catches
                # this and returns without error.
                from agents.base import HITLDriftError

                raise HITLDriftError("Fleet: routed to coordinator")

            # Collect Jumpstarter diagnostics before
            # clarification. Serial logs and tunnel data
            # are critical for diagnosing node failures.
            cf = ticket.get("custom_fields", {})
            if cf.get("resource_provider") == "jumpstarter":
                diag = await self._collect_jumpstarter_diagnostics()
                if diag:
                    await self._update_fields(
                        self._ticket_id,
                        {"node_diagnostics": diag[:5000]},
                    )
                    question = f"{question}\n\n## Node Diagnostics\n{diag}"
            return await self._request_human_input(self._ticket_id, question)
        return "No ticket context available."

    async def _request_benchmark_approval(
        self,
        *,
        benchmark: str | None,
        summary: str | None,
        validation_id: str | None,
    ) -> str:
        """Create and wait on one immutable approval request.

        Execution integration remains owned by the benchmark operation work
        (#788); this method only binds the human decision to validation and
        execution intent before returning to the LLM.
        """
        if not self._ticket_id:
            return "No ticket context available."
        ticket = await self._get_ticket(self._ticket_id)
        cf = ticket.get("custom_fields", {})
        manifest = cf.get("benchmark_validations", {})
        records = manifest.get("records", {}) if isinstance(manifest, dict) else {}
        validation = records.get(validation_id) if validation_id else None
        if not isinstance(validation, dict):
            validation = cf.get("validated_run_file") or {}
        validation_id = validation_id or validation.get("validation_id")
        self._active_validation_id = validation_id
        intent = validation.get("execution_intent_digest")
        immutable_run_file = validation.get("run_file")
        if not validation_id or not intent or not isinstance(immutable_run_file, dict):
            return "Approval rejected: run-file has no immutable validation record."
        digest = hashlib.sha256(
            json.dumps(
                immutable_run_file, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        if digest != validation.get("runfile_fingerprint"):
            return (
                "Approval rejected: validation record has an invalid run-file digest."
            )
        approval_context = current_trace_context() or self.trace_context
        fence_headers = {
            "X-Agentic-Perf-Orchestrator-Session": os.environ.get(
                "AGENTIC_PERF_ORCHESTRATOR_SESSION_ID", ""
            ),
            "X-Agentic-Perf-Orchestrator-Epoch": os.environ.get(
                "AGENTIC_PERF_ORCHESTRATOR_EPOCH", ""
            ),
            "X-Agentic-Perf-Claim-Id": os.environ.get("AGENTIC_PERF_CLAIM_ID", "")
            or (cf.get("claim") or {}).get("claim_id", ""),
            "X-Agentic-Perf-Invocation-Id": str(approval_context.invocation_id)
            if approval_context and approval_context.invocation_id
            else "",
            "X-Agentic-Perf-Tool-Call-Id": approval_context.tool_call_id
            if approval_context and approval_context.tool_call_id
            else "",
        }
        request = await self._client.post(
            f"{self.store_url}/api/v1/tickets/{self._ticket_id}/approvals",
            headers={key: value for key, value in fence_headers.items() if value},
            json={
                "validation_id": validation_id,
                "presented_run_file_digest": digest,
                "execution_intent_digest": intent,
                "execution_intent_id": validation.get("execution_intent_id"),
                "attempt_id": validation.get("attempt_id"),
                "summary": summary or benchmark or "Benchmark run-file approval",
                "invocation_id": str(approval_context.invocation_id)
                if approval_context and approval_context.invocation_id
                else None,
                "tool_call_id": approval_context.tool_call_id
                if approval_context
                else None,
                "session_id": os.environ.get("AGENTIC_PERF_ORCHESTRATOR_SESSION_ID"),
                "session_epoch": os.environ.get("AGENTIC_PERF_ORCHESTRATOR_EPOCH"),
                "waiter_owner": (
                    str(approval_context.invocation_id)
                    if approval_context and approval_context.invocation_id
                    else None
                ),
                "ticket_attempt": os.environ.get("AGENTIC_PERF_CLAIM_ID")
                or (cf.get("claim") or {}).get("claim_id"),
                "claim_id": os.environ.get("AGENTIC_PERF_CLAIM_ID")
                or (cf.get("claim") or {}).get("claim_id"),
            },
        )
        request.raise_for_status()
        approval = request.json()
        approval_id = approval["approval_request_id"]
        self._active_approval_request_id = approval_id
        self._active_approval = approval
        bench_label = f" for {benchmark}" if benchmark else ""
        question = (
            f"Approval request {approval_id} for run-file{bench_label}.\n"
            f"Validation ID: {validation_id}\n"
            f"Presented digest: {digest}\n\n"
            f"{summary or ''}\n\n"
            f"```json\n{json.dumps(immutable_run_file, indent=2)}\n```\n\n"
            "Reply approve / request changes / reject, or use the structured approval action."
        )
        await self._add_comment(self._ticket_id, f"**Approval requested:**\n{question}")
        return await self._wait_for_benchmark_approval(approval_id)

    async def _wait_for_benchmark_approval(self, approval_id: str) -> str:
        reply = await self._request_human_input(
            self._ticket_id,
            "Review the immutable run-file above and reply naturally with your "
            "decision. I will interpret your response before execution.",
        )
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
                f"approval request ID {approval_id} to execute_benchmark."
            )
        if record and record.get("status") != "pending":
            return f"Approval {record.get('status')} for request {approval_id}."
        return (
            f"The user replied: {reply}\n\n"
            f"Interpret this response. If it clearly authorizes the benchmark, "
            f"call resolve_benchmark_approval(decision='approved') for request "
            f"{approval_id}; otherwise resolve it as changes_requested or rejected. "
            "Do not call execute_benchmark while approval remains pending."
        )

    async def _resolve_benchmark_approval(
        self,
        *,
        decision: str,
        reason: str | None = None,
    ) -> str:
        if decision not in {"approved", "changes_requested", "rejected"}:
            return f"Unsupported approval decision: {decision}"
        approval_id = self._active_approval_request_id
        approval = self._active_approval or {}
        if not approval_id:
            return "No active benchmark approval request is awaiting a decision."
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
                f"request ID {approval_id} to execute_benchmark."
            )
        return f"Approval {decision} for request {approval_id}. Do not execute the benchmark."

    async def run(self, ticket_id: str) -> None:
        self._ticket_id = ticket_id

        bench_server = str(Path(__file__).with_name("server.py"))
        infra_server = str(Path(__file__).parent.parent / "infra" / "server.py")

        mcp = AgentMCPClient(trace_context=self.trace_context)
        await mcp.connect_ticket_server(
            bench_server,
            name="benchmark",
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

        # Workflow harnesses may expose their discovery and execution tools
        # through a configured external MCP server (for example the Arcaflow
        # stdio server). Keep those tools on this agent's MCP client so normal
        # AgentBase dispatch can route calls to the owning external server.
        from agents.mcp_client import connect_external_servers

        connected_ext, ext_tools = await connect_external_servers(mcp, "benchmark")

        self._mcp = mcp

        all_tools = await mcp.list_tools()
        all_tools = _filter_external_tools(
            all_tools, mcp._tool_routing, connected_ext, ext_tools
        )
        self.tools = all_tools + self.tools

        try:
            ticket = await self._get_ticket(ticket_id)

            # Scope tools to the harness. Standalone
            # harnesses need only a few tools — hiding
            # the rest prevents the agent from exploring
            # harness-specific tools (runfile schemas,
            # example configs) or running diagnostic SSH
            # commands instead of its one job.
            self._apply_tool_scoping(ticket)

            ssh_key = ticket.get("custom_fields", {}).get("ssh_key_path")
            if ssh_key:
                # SSH key is now handled server-side via ticket data
                pass
            await super().run(ticket_id)
        finally:
            await mcp.disconnect()
            self._mcp = None

    # Tool sets per harness. Each harness declares the
    # tools its benchmark agent needs. Tools not listed
    # are hidden from the LLM to prevent exploration and
    # scope creep (upstream #201).
    _HARNESS_TOOLS: dict[str, set[str]] = {
        "crucible": {
            "read_skills",
            "list_harness_docs",
            "read_harness_doc",
            "get_execution_config",
            "get_runfile_schema",
            "get_benchmark_params",
            "get_tool_params",
            "get_example_runfile",
            "setup_passwordless_ssh",
            "validate_benchmark",
            "execute_benchmark",
            "get_run_logs",
            "submit_benchmark_result",
            "present_runfile_for_approval",
            "resolve_benchmark_approval",
            "request_clarification",
        },
        "boot-time": {
            "read_skills",
            "execute_boot_time_test",
            "submit_benchmark_result",
            "request_clarification",
            # Workspace tools needed when execute_boot_time_test
            # spills its output to a workspace file.
            "jq_file_from_workspace",
            "read_file_from_workspace",
            "list_files_from_workspace",
        },
        "arcaflow-plugins": {
            "read_skills",
            "set_ssh_context",
            "check_host",
            "get_execution_config",
            "get_runfile_schema",
            "get_benchmark_params",
            "get_plugin_schema",
            "plugin_list",
            "plugin_describe",
            "workflow_load",
            "workflow_list",
            "workflow_input_build",
            "workflow_input_validate",
            "workflow_input_export",
            "workflow_execute",
            "workflow_execution_status",
            "workflow_execution_cancel",
            "workflow_execution_output",
            "execute_benchmark",
            "submit_benchmark_result",
            "request_clarification",
        },
    }
    _HARNESS_EXCLUDED_TOOLS: dict[str, set[str]] = {
        "crucible": {
            "read_skills",
            "list_harness_docs",
            "read_harness_doc",
            "get_execution_config",
            "get_runfile_schema",
            "get_benchmark_params",
            "get_tool_params",
            "get_example_runfile",
        },
    }

    def _apply_tool_scoping(self, ticket: dict[str, Any]) -> None:
        """Filter tools based on harness type.

        Harnesses listed in _HARNESS_TOOLS get a reduced
        tool set. Unlisted harnesses keep all tools.
        """
        harness = self._effective_harness(
            ticket.get("custom_fields", {}).get("directives", {}),
            self._skill_provider,
        )
        excluded = self._HARNESS_EXCLUDED_TOOLS.get(harness)
        if excluded is not None:
            self.tools = [t for t in self.tools if t.name not in excluded]
            return
        allowed = self._HARNESS_TOOLS.get(harness)
        if allowed is not None:
            directives = ticket.get("custom_fields", {}).get("directives", {})
            if harness == "arcaflow-plugins" and directives.get("workflow_source"):
                # Workflow tickets execute through the configured Arcaflow MCP
                # workflow tools. The direct plugin runner requires
                # ``plugin_image`` and is intentionally unavailable here.
                allowed = allowed - {
                    "execute_benchmark",
                    "get_plugin_schema",
                    "plugin_list",
                    "plugin_describe",
                }
            self.tools = [t for t in self.tools if t.name in allowed]

    def _system_prompt(self, ticket: dict[str, Any]) -> str:
        cf = ticket.get("custom_fields", {})
        directives = cf.get("directives", {})
        provider = cf.get("resource_provider") or directives.get("resource_provider")
        endpoint = directives.get("endpoint_type", "remotehosts")
        harness = self._effective_harness(directives, self._skill_provider)

        fragments = self._load_prompt_fragments(
            Path(__file__).parent,
            resource_provider=provider,
            endpoint_type=endpoint,
        )

        # Load harness-specific prompt fragment (e.g., crucible.md,
        # jumpstarter.md).  These contain harness-specific execution
        # instructions that don't belong in the base prompt.
        harness_fragment = ""
        prompts_dir = Path(__file__).parent / "prompts"
        # For controller harnesses, also try the harness name
        harness_fragment = self._load_prompt_fragment(prompts_dir, harness)

        prompt = BENCHMARK_BASE_PROMPT

        if cf.get("execution_model") == EXECUTION_MODEL_DIRECT:
            prompt += (
                "\n\n## Direct Execution Model\n\n"
                "This benchmark uses the **direct** execution model. "
                "There is no dedicated controller host \u2014 the "
                "orchestrator runs benchmark tools directly. The "
                "assigned targets are the systems under test (SUTs). "
                "Use `targets[0]` as the `sut_host` parameter."
            )
        if harness_fragment:
            prompt += f"\n\n{harness_fragment}"
        if directives.get("workflow_source"):
            prompt += "\n\n" + self._workflow_instructions(directives)
        if fragments:
            prompt += f"\n\n{fragments}"
        return prompt

    @staticmethod
    def _workflow_instructions(directives: dict[str, Any]) -> str:
        """Describe the required Arcaflow MCP execution path.

        Workflow MCP tools are intentionally dispatched by the model because
        their input schemas are supplied by the configured external server.
        Keeping the directive here makes the ticket fields operational rather
        than merely displaying them in the initial message.
        """
        source = directives.get("workflow_source", "")
        name = directives.get("workflow_name")
        name_line = f"\n- Workflow name/path: `{name}`" if name else ""
        return (
            "## Arcaflow Workflow Execution (mandatory)\n"
            "This ticket supplies an Arcaflow workflow. Do not construct a "
            "plugin-image run-file and do not call `execute_benchmark` for "
            "this ticket. Use the configured Arcaflow MCP tools in this "
            "order:\n"
            "1. Call `workflow_load` for the supplied source (and workflow "
            "name/path when present).\n"
            "2. Use `workflow_input_build` to construct inputs from the "
            "workflow schema and the ticket's requested parameters.\n"
            "3. Call `workflow_input_validate`; correct any reported input "
            "errors before continuing.\n"
            "4. Call `workflow_input_export` to obtain the immutable input "
            "payload, then call `workflow_execute` with the loaded workflow "
            "and exported input.\n"
            "5. Use the workflow status/output tools until execution reaches "
            "a terminal state, then submit the result with its workflow run "
            "ID.\n\n"
            f"- Workflow source: `{source}`{name_line}"
        )

    @staticmethod
    def _compute_params_fingerprint(cf: dict[str, Any]) -> str:
        """SHA-256 fingerprint of the current execution plan step's mv_params."""
        plan = cf.get("execution_plan")
        if not plan:
            return "no-plan"
        steps = plan.get("steps", [])
        idx = plan.get("current_step", 0)
        if idx >= len(steps):
            return "no-plan"
        mv_params = steps[idx].get("params", {}).get("mv_params")
        if not mv_params:
            return "no-mv-params"
        return hashlib.sha256(
            json.dumps(mv_params, sort_keys=True).encode()
        ).hexdigest()

    def _build_messages(self, ticket: dict[str, Any]) -> list[dict[str, Any]]:
        cf = ticket.get("custom_fields", {})
        scoped = self._get_scoped_context(ticket, "benchmark")
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

        if cf.get("benchmark_suite"):
            content += f"\n**Benchmark Suite:** {cf['benchmark_suite']}\n"
        if cf.get("absent_suite"):
            content += f"\n**Absent Suite:** {cf['absent_suite']} (no standard automation available)\n"
        if cf.get("hypothesis"):
            content += f"\n**Hypothesis:** {cf['hypothesis']}\n"
        is_direct = cf.get("execution_model") == EXECUTION_MODEL_DIRECT
        if is_direct:
            # Show full assigned_hardware_ips so fragments
            # can reference targets[0] by path.  Also show
            # ssh_hardware_ips when they differ (cloud envs).
            hw = cf.get("assigned_hardware_ips", {})
            if hw:
                content += (
                    f"\n## Assigned Hardware\n"
                    f"Target hosts for this benchmark. Use "
                    f"`targets[0]` as the SUT.\n"
                    f"```json\n{json.dumps(hw, indent=2)}\n```\n"
                )
            ssh_hw = cf.get("ssh_hardware_ips", {})
            if ssh_hw and ssh_hw != hw:
                content += (
                    f"\n## SSH Addresses\n"
                    f"Use these for SSH access (may differ from "
                    f"assigned addresses in cloud environments).\n"
                    f"```json\n{json.dumps(ssh_hw, indent=2)}\n```\n"
                )
        elif cf.get("ssh_hardware_ips"):
            content += f"\n## Controller SSH Addresses\nUse these addresses for Crucible remotehost `config.host` values only after verifying controller-to-host SSH reachability. They may be hostnames or IPs and are independent from benchmark dataplane addresses.\n```json\n{json.dumps(cf['ssh_hardware_ips'], indent=2)}\n```\n"
            content += f"\n## Assigned Network Addresses\nThese are candidates for benchmark dataplane connectivity. Do not use them as Crucible remotehost `config.host` values unless the controller independently verifies SSH access through them.\n```json\n{json.dumps(cf.get('assigned_hardware_ips', {}), indent=2)}\n```\n"
        elif cf.get("assigned_hardware_ips"):
            content += f"\n## Assigned Hardware\n```json\n{json.dumps(cf['assigned_hardware_ips'], indent=2)}\n```\n"
        if cf.get("ssh_user"):
            content += f"\n**SSH User:** {cf['ssh_user']}\n"
        if cf.get("directives"):
            content += f"\n## User Directives\n```json\n{json.dumps(cf['directives'], indent=2)}\n```\n"

        directives = cf.get("directives", {})
        if directives.get("workflow_source"):
            content += "\n" + self._workflow_instructions(directives) + "\n"
        if cf.get("resource_provider_metadata"):
            content += f"\n## Provider Metadata (raw)\n```json\n{json.dumps(cf['resource_provider_metadata'], indent=2)}\n```\n"

        harness = cf.get("directives", {}).get("harness", "crucible")

        skills_dir = Path(__file__).resolve().parent.parent.parent / "skills" / harness
        if harness != "crucible" and skills_dir.is_dir():
            content += f"\n## {harness} Skills (read these first)\n"
            content += "These contain critical lessons from prior runs:\n\n"
            for f in sorted(skills_dir.glob("*.md")):
                content += f"- `{f.name}`\n"
            content += (
                "\nUse `read_skills(docs=[{'harness': '"
                + harness
                + "', 'filename': '...'}])` to read.\n"
            )

        general_dir = (
            Path(__file__).resolve().parent.parent.parent / "skills" / "general"
        )
        if harness != "crucible" and general_dir.is_dir():
            general_files = sorted(general_dir.glob("*.md"))
            if general_files:
                content += "\n## General Skills\n"
                for f in general_files:
                    content += f"- `{f.name}`\n"
                content += "\nUse `read_skills(docs=[{'harness': 'general', 'filename': '...'}])` to read.\n"

        # Crucible documentation is served by the source-aware gateway.  Keep
        # the generic cache path for harnesses that have not adopted it yet.
        if self._repo_cache and harness != "crucible":
            docs = self._repo_cache.list_docs(harness, subdirs=["docs", "config"])
            if docs:
                content += f"\n## Available {harness} Documentation\n"
                content += (
                    "Use `read_harness_doc` to read any of these before "
                    "constructing the run file:\n\n"
                )
                for doc in docs:
                    content += f"- `{doc['path']}`\n"

        plan = cf.get("execution_plan")
        if plan:
            current_idx = plan.get("current_step", 0)
            steps = plan.get("steps", [])
            if current_idx < len(steps):
                step = steps[current_idx]
                step_params = step.get("params", {})
                content += (
                    f"\n## Execution Plan — Step {current_idx}\n"
                    f"**Label:** {step_params.get('label', 'unnamed')}\n"
                )
                if step_params.get("mv_params"):
                    content += (
                        f"**Parameter overrides for this run:**\n"
                        f"```json\n{json.dumps(step_params['mv_params'], indent=2)}\n```\n"
                        f"Apply these values in the run-file's mv-params.\n"
                    )
                content += (
                    f"\nThis is step {current_idx + 1} of {len(steps)} "
                    f"in a multi-step plan.\n"
                )
            if plan.get("run_ids"):
                content += (
                    f"\n**Previous run IDs from earlier steps:** "
                    f"{', '.join(plan['run_ids'])}\n"
                )

        validated = cf.get("validated_run_file")
        if validated:
            current_fp = self._compute_params_fingerprint(cf)
            stored_fp = validated.get("params_fingerprint", "")
            if current_fp == stored_fp:
                content += (
                    "\n## Previously Validated Run-File\n"
                    "A prior agent run validated this run-file for the "
                    "current parameters. You may reuse it only with the "
                    "matching validation_id when calling `execute_benchmark`. "
                    "If you modify it, validate the new run-file first.\n\n"
                    f"**Harness:** {validated.get('harness', 'unknown')}\n"
                    f"**Validation ID:** {validated.get('validation_id', 'unknown')}\n"
                    f"```json\n"
                    f"{json.dumps(validated.get('run_file', {}), indent=2)}"
                    f"\n```\n"
                )
            else:
                content += (
                    "\n*Note: A previously validated run-file exists "
                    "but its parameters fingerprint does not match the "
                    "current execution plan step. Build a fresh "
                    "run-file.*\n"
                )

        user_comments = self._user_comments(ticket)
        if user_comments:
            content += "\n## Previous Comments\n"
            for comment in user_comments:
                content += f"\n**{comment['author']}:** {comment['body']}\n"

        return [{"role": "user", "content": content}]

    async def _collect_jumpstarter_diagnostics(self) -> str:
        """Collect diagnostics for Jumpstarter boards.

        The platform agent uses the Jumpstarter Python SDK
        which doesn't leave a persistent socket. Board-level
        diagnostics (serial, power state) are captured by
        the platform agent during provisioning and stored
        in ticket fields. The benchmark agent reports what
        is available from the ticket.
        """
        if not self._ticket_id:
            return "No ticket context for diagnostics"
        try:
            ticket = await self._get_ticket(self._ticket_id)
            cf = ticket.get("custom_fields", {})
            diag = []
            hosts = cf.get("hosts_provisioned", [])
            if hosts:
                diag.append(f"Hosts: {', '.join(str(h) for h in hosts)}")
            board = cf.get("platform_board", "")
            if board:
                diag.append(f"Board: {board}")
            flash_dur = cf.get("platform_flash_duration_s")
            if flash_dur:
                diag.append(f"Flash duration: {flash_dur:.0f}s")
            return "\n".join(diag) if diag else "No diagnostics available"
        except Exception:
            return "Could not retrieve diagnostics from ticket"

    async def _handle_budget_pause(self, ticket_id: str) -> None:
        """Route budget-exhausted investigation tickets
        to evaluating_convergence so partial results can
        be assessed. Non-investigation tickets get the
        default behavior (awaiting_customer_guidance).
        """
        ticket = await self._get_ticket(ticket_id)
        cf = ticket.get("custom_fields", {})
        if cf.get("investigation_ledger") or cf.get("anomaly_context"):
            await self._add_comment(
                ticket_id,
                "**Budget exhausted during benchmark "
                "iteration.** Routing to convergence "
                "assessment with partial results.",
            )
            await self._transition_ticket(
                ticket_id,
                "evaluating_convergence",
                comment=(
                    "Budget exhausted — evaluating convergence with partial results"
                ),
            )
        else:
            await super()._handle_budget_pause(ticket_id)

    async def _handle_completion(self, ticket_id: str, response: LLMResponse) -> None:
        result = self._get_submit_result(response)
        if not result:
            result = self._parse_json_response(response.text)
        if not result:
            result = {
                "run_id": "UNKNOWN",
                "benchmark_status": "failed",
                "notes": "Could not produce structured output",
            }

        fields: dict[str, Any] = {
            "run_id": result.get("run_id", "UNKNOWN"),
            "benchmark_status": result.get("benchmark_status", "unknown"),
            "benchmark_duration": result.get("benchmark_duration"),
        }
        validation_id = result.get("validation_id") or self._active_validation_id
        run_file_used = result.get("run_file_used", {})
        if validation_id:
            ticket = await self._get_ticket(ticket_id)
            records = (
                ticket.get("custom_fields", {})
                .get("benchmark_validations", {})
                .get("records", {})
            )
            validation = records.get(validation_id, {})
            if isinstance(validation, dict) and isinstance(
                validation.get("run_file"), dict
            ):
                run_file_used = validation["run_file"]
        fields["run_file_used"] = run_file_used
        # Accumulate output_dirs across loop-back runs
        # so the evaluate agent can access all artifacts.
        if result.get("output_dir"):
            ticket = await self._get_ticket(ticket_id)
            existing = ticket.get("custom_fields", {}).get("output_dirs", [])
            if result["output_dir"] not in existing:
                existing.append(result["output_dir"])
            fields["output_dirs"] = existing
            # Keep output_dir as latest for backward compat
            fields["output_dir"] = result["output_dir"]
        await self._update_fields(ticket_id, fields)

        status = fields["benchmark_status"]
        summary = (
            f"**Benchmark Execution {'Complete' if status == 'completed' else 'Failed'}**\n\n"
            f"- **Run ID:** {fields['run_id']}\n"
            f"- **Status:** {status}\n"
        )
        if fields["benchmark_duration"]:
            summary += f"- **Duration:** {fields['benchmark_duration']}s\n"
        if result.get("notes"):
            summary += f"- **Notes:** {result['notes']}\n"

        await self._add_comment(ticket_id, summary)

        if status == "failed":
            from providers.fleet import is_fleet_investigation

            ticket = await self._get_ticket(ticket_id)
            cf = ticket.get("custom_fields", {})
            if is_fleet_investigation(cf):
                # Fleet: coordinator handles recording and routing.
                await self._transition_ticket(
                    ticket_id,
                    "coordinating_fleet",
                    comment="Fleet: benchmark failed, coordinating",
                )
            else:
                # Non-fleet: failed benchmarks need human
                # review to determine next steps.
                await self._transition_ticket(
                    ticket_id,
                    "awaiting_customer_guidance",
                    comment="Benchmark failed — needs investigation",
                )
        else:
            # Route based on whether this is an investigation
            # or fleet ticket. Code-enforced pattern.
            from providers.fleet import is_fleet_investigation

            ticket = await self._get_ticket(ticket_id)
            cf = ticket.get("custom_fields", {})
            if is_fleet_investigation(cf):
                # Fleet: coordinator handles recording and routing.
                await self._transition_ticket(
                    ticket_id,
                    "coordinating_fleet",
                    comment="Fleet: benchmark completed, coordinating",
                )
            elif cf.get("investigation_ledger") or cf.get("anomaly_context"):
                await self._transition_ticket(
                    ticket_id,
                    "evaluating_convergence",
                    comment=("Benchmark completed, evaluating convergence"),
                )
            elif await self._plan_controls_next_transition(ticket_id):
                return
            else:
                await self._transition_ticket(
                    ticket_id,
                    "awaiting_review",
                    comment=("Benchmark completed, ready for review"),
                )
