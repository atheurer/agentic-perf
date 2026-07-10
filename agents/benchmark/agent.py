from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from agents.base import AgentBase
from agents.mcp_client import AgentMCPClient
from providers.events import EventBus
from providers.llm.base import LLMProvider, LLMResponse
from providers.skills.repo_cache import RepoCache

from .mcp_server import get_benchmark_tools
from .prompts import BENCHMARK_BASE_PROMPT

logger = logging.getLogger(__name__)

_LOCAL_TOOL_NAMES = frozenset(
    {"request_clarification", "present_runfile_for_approval", "submit_benchmark_result"}
)

_MCP_TOOL_NAMES = frozenset(
    t.name for t in get_benchmark_tools() if t.name not in _LOCAL_TOOL_NAMES
) | {"list_harness_docs", "read_harness_doc"}


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

        local_tools = [
            t for t in get_benchmark_tools() if t.name not in _MCP_TOOL_NAMES
        ]

        async def _request_clarification(question: str) -> str:
            return await self._do_request_clarification(question)

        async def _present_runfile_for_approval(
            run_file: dict,
            benchmark: str | None = None,
            summary: str | None = None,
        ) -> str:
            bench_label = f" for {benchmark}" if benchmark else ""
            summary_line = f"\n\n{summary}" if summary else ""
            question = (
                f"Please review this run-file{bench_label}{summary_line}\n\n"
                f"```json\n{json.dumps(run_file, indent=2)}\n```\n\n"
                "Do you approve this configuration? (approve / request changes / reject)"
            )
            return await self._do_request_clarification(question)

        local_handlers = {
            "request_clarification": _request_clarification,
            "present_runfile_for_approval": _present_runfile_for_approval,
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
            # Collect Jumpstarter diagnostics before any
            # clarification or fleet redirect. Serial logs
            # and tunnel data are critical for diagnosing
            # node failures.
            ticket = await self._get_ticket(self._ticket_id)
            cf = ticket.get("custom_fields", {})
            diag = ""
            if cf.get("resource_provider") == "jumpstarter":
                diag = await self._collect_jumpstarter_diagnostics()
                if diag:
                    # Store diagnostics on the ticket so
                    # evaluate agent can see them.
                    await self._update_fields(
                        self._ticket_id,
                        {"node_diagnostics": diag[:5000]},
                    )
                    question = f"{question}\n\n## Node Diagnostics\n{diag}"
            # Fleet investigation: failures are data
            # points. Submit failed result instead of
            # parking. Diagnostics are already collected
            # and attached above.
            from providers.fleet import is_fleet_investigation

            if is_fleet_investigation(cf):
                return (
                    "Fleet investigation: this failure is "
                    "a critical data point, not a blocker. "
                    "Submit your benchmark result with "
                    "benchmark_status=failed and include "
                    "the failure details and diagnostics "
                    "in the notes. The system will record "
                    "the failure and move to the next "
                    "host."
                )
            return await self._request_human_input(self._ticket_id, question)
        return "No ticket context available."

    async def run(self, ticket_id: str) -> None:
        self._ticket_id = ticket_id

        bench_server = str(Path(__file__).with_name("server.py"))
        infra_server = str(Path(__file__).parent.parent / "infra" / "server.py")

        mcp = AgentMCPClient()
        await mcp.connect(
            bench_server,
            name="benchmark",
            env={
                "TICKET_ID": ticket_id,
                "STATE_STORE_URL": self.store_url,
                "AGENT_NAME": self.agent_name,
            },
        )
        await mcp.connect(infra_server, name="infra")

        # Attach Jumpstarter MCP if ticket uses Jumpstarter hardware.
        # Returns allowed tool names for filtering, or None.
        from agents.jumpstarter_mcp import attach_jumpstarter_mcp

        jmp_tools = await attach_jumpstarter_mcp(mcp, ticket_id, self.store_url)

        self._mcp = mcp

        # Get all tools, but if Jumpstarter is attached,
        # exclude lease management tools (resource provider's
        # job, not the agent's).
        all_tools = await mcp.list_tools()
        if jmp_tools is not None:
            from agents.jumpstarter_mcp import _PROVIDER_ONLY_TOOLS

            all_tools = [t for t in all_tools if t.name not in _PROVIDER_ONLY_TOOLS]
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
        "boot-time": {
            "read_skill",
            "set_ssh_context",
            "check_host",
            "execute_boot_time_test",
            "submit_benchmark_result",
            "request_clarification",
        },
    }

    def _apply_tool_scoping(self, ticket: dict[str, Any]) -> None:
        """Filter tools based on harness type.

        Harnesses listed in _HARNESS_TOOLS get a reduced
        tool set. Unlisted harnesses keep all tools.
        """
        harness = (
            ticket.get("custom_fields", {}).get("directives", {}).get("harness", "")
        )
        allowed = self._HARNESS_TOOLS.get(harness)
        if allowed is not None:
            self.tools = [t for t in self.tools if t.name in allowed]

    def _system_prompt(self, ticket: dict[str, Any]) -> str:
        cf = ticket.get("custom_fields", {})
        directives = cf.get("directives", {})
        provider = cf.get("resource_provider") or directives.get("resource_provider")
        endpoint = directives.get("endpoint_type", "remotehosts")

        fragments = self._load_prompt_fragments(
            Path(__file__).parent,
            resource_provider=provider,
            endpoint_type=endpoint,
        )
        if fragments:
            return f"{BENCHMARK_BASE_PROMPT}\n\n{fragments}"
        return BENCHMARK_BASE_PROMPT

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

        if cf.get("parsed_specs"):
            content += f"\n## Parsed Specifications\n```json\n{json.dumps(cf['parsed_specs'], indent=2)}\n```\n"
        if cf.get("benchmark_suite"):
            content += f"\n**Benchmark Suite:** {cf['benchmark_suite']}\n"
        if cf.get("absent_suite"):
            content += f"\n**Absent Suite:** {cf['absent_suite']} (no standard automation available)\n"
        if cf.get("hypothesis"):
            content += f"\n**Hypothesis:** {cf['hypothesis']}\n"
        if cf.get("ssh_hardware_ips"):
            content += f"\n## SSH Addresses (use these for SSH/SCP and setup_passwordless_ssh)\n```json\n{json.dumps(cf['ssh_hardware_ips'], indent=2)}\n```\n"
            content += f"\n## Private Addresses (use these for run-file host entries and controller-ip-address)\n```json\n{json.dumps(cf.get('assigned_hardware_ips', {}), indent=2)}\n```\n"
        elif cf.get("assigned_hardware_ips"):
            content += f"\n## Assigned Hardware\n```json\n{json.dumps(cf['assigned_hardware_ips'], indent=2)}\n```\n"
        if cf.get("ssh_user"):
            content += f"\n**SSH User:** {cf['ssh_user']}\n"
        if cf.get("directives"):
            content += f"\n## User Directives\n```json\n{json.dumps(cf['directives'], indent=2)}\n```\n"
        if cf.get("resource_provider_metadata"):
            content += f"\n## Provider Metadata (raw)\n```json\n{json.dumps(cf['resource_provider_metadata'], indent=2)}\n```\n"

        harness = cf.get("directives", {}).get("harness", "crucible")

        skills_dir = Path(__file__).resolve().parent.parent.parent / "skills" / harness
        if skills_dir.is_dir():
            content += f"\n## {harness} Skills (read these first)\n"
            content += "These contain critical lessons from prior runs:\n\n"
            for f in sorted(skills_dir.glob("*.md")):
                content += f"- `{f.name}`\n"
            content += "\nUse `read_skill` to read each one.\n"

        general_dir = (
            Path(__file__).resolve().parent.parent.parent / "skills" / "general"
        )
        if general_dir.is_dir():
            general_files = sorted(general_dir.glob("*.md"))
            if general_files:
                content += "\n## General Skills\n"
                for f in general_files:
                    content += f"- `{f.name}`\n"
                content += (
                    "\nUse `read_skill(harness='general', filename='...')` to read.\n"
                )

        if self._repo_cache:
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

        if ticket.get("comments"):
            content += "\n## Previous Comments\n"
            for comment in ticket["comments"]:
                content += f"\n**{comment['author']}:** {comment['body']}\n"

        return [{"role": "user", "content": content}]

    async def _collect_jumpstarter_diagnostics(
        self,
    ) -> str:
        """Collect diagnostics via Jumpstarter tunnel.

        Called deterministically when a fleet benchmark
        fails. The tunnel may still work even when direct
        SSH doesn't. Captures serial output, power state,
        and tunnel SSH — the only data available to
        diagnose node failures.
        """
        if self._mcp is None:
            return "No MCP connection available"

        import json as _json

        diag: list[str] = []

        try:
            conns_raw = await self._mcp.call_tool("jmp_list_connections", {})
            conns = _json.loads(conns_raw)
            conn_list = conns.get("connections", [])
            if not conn_list:
                return "No active Jumpstarter connection"
            conn_id = conn_list[0]["connection_id"]
        except Exception as exc:
            return f"Could not list connections: {exc}"

        # Serial capture — most critical diagnostic
        try:
            serial = await self._mcp.call_tool(
                "jmp_run",
                {
                    "connection_id": conn_id,
                    "command": ["serial", "pipe"],
                    "timeout_seconds": 15,
                },
            )
            sd = _json.loads(serial)
            stdout = sd.get("stdout", "")
            if stdout:
                diag.append(f"Serial output:\n{stdout[:3000]}")
            else:
                diag.append("Serial: no output (board may be hung or powered off)")
        except Exception as exc:
            diag.append(f"Serial capture failed: {exc}")

        # Power state
        try:
            power = await self._mcp.call_tool(
                "jmp_run",
                {
                    "connection_id": conn_id,
                    "command": ["power", "read"],
                },
            )
            pd = _json.loads(power)
            diag.append(f"Power: {pd.get('stdout', 'unknown').strip()}")
        except Exception as exc:
            diag.append(f"Power check failed: {exc}")

        # SSH via tunnel
        try:
            ssh = await self._mcp.call_tool(
                "jmp_run",
                {
                    "connection_id": conn_id,
                    "command": [
                        "ssh",
                        "--",
                        "uptime",
                    ],
                    "timeout_seconds": 15,
                },
            )
            sd = _json.loads(ssh)
            if sd.get("exit_code") == 0:
                diag.append(f"Tunnel SSH: OK — {sd.get('stdout', '').strip()}")
            else:
                diag.append(
                    f"Tunnel SSH: failed — {sd.get('stderr', '').strip()[:200]}"
                )
        except Exception as exc:
            diag.append(f"Tunnel SSH failed: {exc}")

        return "\n".join(diag)

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

        fields = {
            "run_id": result.get("run_id", "UNKNOWN"),
            "benchmark_status": result.get("benchmark_status", "unknown"),
            "run_file_used": result.get("run_file_used", {}),
            "benchmark_duration": result.get("benchmark_duration"),
            "output_dir": result.get("output_dir", ""),
        }
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
            await self._transition_ticket(
                ticket_id,
                "awaiting_customer_guidance",
                comment="Benchmark failed — needs investigation",
            )
        else:
            # Route based on whether this is an investigation
            # ticket. Same code-enforced pattern as triage.
            ticket = await self._get_ticket(ticket_id)
            cf = ticket.get("custom_fields", {})
            if cf.get("investigation_ledger") or cf.get("anomaly_context"):
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
