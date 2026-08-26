from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from agents.base import AgentBase
from agents.mcp_client import AgentMCPClient
from providers.events import EventBus
from providers.llm.base import LLMProvider, LLMResponse, ToolDefinition
from providers.skills.repo_cache import RepoCache

from .prompts import REVIEW_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

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
        name="submit_review_result",
        description="Submit the performance review analysis when complete.",
        input_schema={
            "type": "object",
            "properties": {
                "review_summary": {
                    "type": "string",
                    "description": "1-2 sentence summary",
                },
                "verdict": {
                    "type": "string",
                    "enum": [
                        "hypothesis_confirmed",
                        "hypothesis_refuted",
                        "inconclusive",
                    ],
                },
                "detailed_analysis": {
                    "type": "string",
                    "description": "Multi-paragraph markdown analysis",
                },
                "key_metrics": {
                    "type": "object",
                    "description": "Key metric values and assessments",
                },
                "recommendations": {"type": "array", "items": {"type": "string"}},
                "follow_up_needed": {"type": "boolean"},
                "chart_data": {
                    "type": "object",
                    "description": (
                        "Optional chart for the web dashboard. Visualize the "
                        "single most informative finding from your analysis."
                    ),
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "Chart title, e.g. 'Throughput by Thread Count'",
                        },
                        "type": {
                            "type": "string",
                            "enum": ["bar", "line", "doughnut"],
                        },
                        "labels": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "X-axis labels or segment names",
                        },
                        "datasets": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {
                                        "type": "string",
                                        "description": "Dataset label, e.g. 'Gbps' or 'IOPS'",
                                    },
                                    "values": {
                                        "type": "array",
                                        "items": {"type": "number"},
                                    },
                                },
                                "required": ["label", "values"],
                            },
                        },
                    },
                    "required": ["title", "type", "labels", "datasets"],
                },
                "results_url": {
                    "type": "string",
                    "description": (
                        "Optional URL to a harness-specific results viewer "
                        "for deeper analysis"
                    ),
                },
            },
            "required": ["review_summary", "verdict", "detailed_analysis"],
        },
    ),
]


def _is_approved(reply: str) -> bool:
    cleaned = reply.strip().lower().lstrip("/")
    return cleaned in (
        "done",
        "submit",
        "submit the review",
        "that's enough",
        "wrap it up",
    ) or cleaned.startswith("done")


class ReviewAgent(AgentBase):
    def __init__(
        self,
        llm_provider: LLMProvider,
        state_store_url: str,
        skill_provider=None,
        event_bus: EventBus | None = None,
        repo_cache: RepoCache | None = None,
        tool_spill_threshold: int | None = None,
    ) -> None:
        self._skill_provider = skill_provider
        self._repo_cache = repo_cache
        self._ticket_id: str | None = None
        self._user_approved_submit: bool = True

        local_tools = list(_LOCAL_TOOLS)

        async def _request_clarification(question: str) -> str:
            reply = await self._do_request_clarification(question)
            if _is_approved(reply):
                self._user_approved_submit = True
            return reply

        local_handlers = {
            "request_clarification": _request_clarification,
        }

        super().__init__(
            agent_name="review-agent",
            llm_provider=llm_provider,
            state_store_url=state_store_url,
            tools=local_tools,
            tool_handlers=local_handlers,
            event_bus=event_bus,
            max_iterations=50,
            tool_spill_threshold=tool_spill_threshold,
        )

    async def _do_request_clarification(self, question: str) -> str:
        if self._ticket_id:
            return await self._request_human_input(self._ticket_id, question)
        return "No ticket context available."

    async def _handle_slash_command(self, ticket_id: str, command: str) -> str | None:
        """Handle review-agent slash commands.

        /submit — unlock the submission gate and instruct the LLM to submit
                  immediately without further clarification.
        """
        cmd = command.split()[0].lower()

        if cmd == "/submit":
            self._user_approved_submit = True
            logger.info(
                f"[review-agent] /submit command received for {ticket_id} "
                "— submission gate unlocked"
            )
            return (
                "SLASH COMMAND /submit received. The user has force-approved "
                "submission. You MUST immediately call submit_review_result with "
                "your current findings. Do NOT call request_clarification again. "
                "Submit now."
            )

        # Delegate everything else to the base class (unknown-command guard etc.)
        return await super()._handle_slash_command(ticket_id, command)

    def _should_block_submit(self, ticket_id: str) -> str | None:
        if self._user_approved_submit:
            return None
        return (
            "REJECTED: You cannot submit a review yet. The user has not "
            "approved submission. You MUST call request_clarification to "
            "present your findings and ask the user for guidance. The "
            "iterative investigation loop requires you to present findings, "
            "receive user direction, investigate further, and repeat until "
            "the user explicitly says 'done' or 'submit the review'. Only "
            "then will submit_review_result be accepted. Call "
            "request_clarification now with your current findings."
        )

    async def run(self, ticket_id: str) -> None:
        self._ticket_id = ticket_id

        # Block auto-submit only when the ticket
        # explicitly requests interactive review.
        ticket = await self._get_ticket(ticket_id)
        directives = ticket.get("custom_fields", {}).get("directives", {})
        if directives.get("review_mode") == "interactive":
            self._user_approved_submit = False

        review_server = str(Path(__file__).with_name("server.py"))
        infra_server = str(Path(__file__).parent.parent / "infra" / "server.py")
        eval_server = str(Path(__file__).parent.parent / "evaluate" / "server.py")
        workspace_server = str(Path(__file__).parent.parent / "workspace" / "server.py")

        mcp = AgentMCPClient()
        await mcp.connect(
            review_server,
            name="review",
            env={"TICKET_ID": ticket_id, "STATE_STORE_URL": self.store_url},
        )
        await mcp.connect(infra_server, name="infra")
        await mcp.connect(eval_server, name="evaluate-tools")
        await mcp.connect(
            workspace_server,
            name="workspace",
            env={"TICKET_ID": ticket_id},
        )

        # Connect any configured external MCP servers
        # (e.g., historical baselines for comparison).
        from agents.mcp_client import connect_external_servers

        connected_ext, ext_tools = await connect_external_servers(mcp, "review")

        self._mcp = mcp

        mcp_tools = await mcp.list_tools()
        if ext_tools is not None:
            mcp_tools = [
                t
                for t in mcp_tools
                if mcp._tool_routing.get(t.name) not in connected_ext
                or t.name in ext_tools
            ]
        self.tools = mcp_tools + self.tools

        try:
            await super().run(ticket_id)
        finally:
            await mcp.disconnect()
            self._mcp = None

    def _system_prompt(self, ticket: dict[str, Any]) -> str:
        cf = ticket.get("custom_fields", {})
        directives = cf.get("directives", {})
        prompt = REVIEW_SYSTEM_PROMPT
        if directives.get("review_mode") == "interactive":
            prompt += (
                "\n\n## Interactive Review Mode\n\n"
                "This ticket has review_mode=interactive. "
                "OVERRIDE the default auto-submit behavior. "
                "Instead:\n\n"
                "1. Present your initial findings to the "
                "user via request_clarification. Include: "
                "the primary metric result, any apparent "
                "bottlenecks, and what you'd like to "
                "investigate next.\n"
                "2. The user provides direction (e.g., "
                "'check per-CPU usage', 'look at TCP "
                "tuning').\n"
                "3. Perform the requested analysis and "
                "present findings via "
                "request_clarification.\n"
                "4. Repeat until the user says 'done', "
                "'submit', or 'wrap it up'.\n\n"
                "Do NOT call submit_review_result until "
                "the user explicitly ends the "
                "investigation."
            )
        return prompt

    def _build_messages(self, ticket: dict[str, Any]) -> list[dict[str, Any]]:
        cf = ticket.get("custom_fields", {})
        scoped = self._get_scoped_context(ticket, "review")
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

        if cf.get("hypothesis"):
            content += f"\n## Hypothesis\n{cf['hypothesis']}\n"

        plan = cf.get("execution_plan")
        if plan and plan.get("run_ids"):
            content += "\n## Multi-Run Execution Plan Results\n"
            for step in plan.get("steps", []):
                if (
                    step.get("status") == "completed"
                    and step.get("agent_type") == "benchmark"
                ):
                    results = step.get("results", {})
                    label = step.get("params", {}).get("label", f"Step {step['id']}")
                    content += (
                        f"\n### {label}\n"
                        f"- **Run ID:** {results.get('run_id', 'UNKNOWN')}\n"
                        f"- **Status:** "
                        f"{results.get('benchmark_status', 'unknown')}\n"
                    )
            content += (
                f"\n**All Run IDs for comparison:** "
                f"{', '.join(plan['run_ids'])}\n"
                f"Use these run IDs to retrieve and compare results.\n"
            )
        elif cf.get("run_id"):
            content += f"\n**Run ID:** {cf['run_id']}\n"

        if cf.get("benchmark_status"):
            content += f"**Benchmark Status:** {cf['benchmark_status']}\n"
        if cf.get("benchmark_suite"):
            content += f"**Benchmark Suite:** {cf['benchmark_suite']}\n"

        # Pre-scan local artifacts so the LLM doesn't need
        # to discover them via SSH trial-and-error.
        output_dir = cf.get("output_dir", "")
        if output_dir:
            odir = Path(output_dir)
            if odir.is_dir():
                try:
                    files = [
                        str(f.relative_to(odir))
                        for f in sorted(odir.rglob("*"))
                        if f.is_file()
                    ]
                except OSError:
                    files = []
                # Show top-level files first (metadata,
                # merged results, serial capture), then
                # a sample of per-sample files.
                top_level = [f for f in files if "/" not in f]
                nested = [f for f in files if "/" in f]
                content += (
                    f"\n## Local Artifacts\n"
                    f"Results are stored locally at "
                    f"`{output_dir}`.\n"
                    f"Use `read_benchmark_artifact` with "
                    f"this output_dir to read files. "
                    f"Do NOT use SSH.\n"
                    f"\nTop-level files:\n"
                )
                for fn in top_level[:20]:
                    content += f"- `{fn}`\n"
                if len(top_level) > 20:
                    content += f"- ... and {len(top_level) - 20} more top-level files\n"
                if nested:
                    content += f"\nPer-sample files ({len(nested)} total):\n"
                    for fn in nested[:10]:
                        content += f"- `{fn}`\n"
                    if len(nested) > 10:
                        content += (
                            f"- ... and "
                            f"{len(nested) - 10} more "
                            f"— use "
                            f"`list_benchmark_artifacts` "
                            f"to see all files\n"
                        )

        harness = cf.get("harness_name") or cf.get("directives", {}).get(
            "harness", "crucible"
        )
        content += f"**Harness:** {harness}\n"

        if cf.get("benchmark_duration"):
            content += f"**Duration:** {cf['benchmark_duration']}s\n"
        if cf.get("run_file_used"):
            content += f"\n## Run File\n```json\n{json.dumps(cf['run_file_used'], indent=2)}\n```\n"

        ssh_ips = cf.get("ssh_hardware_ips") or cf.get("assigned_hardware_ips") or {}
        if ssh_ips.get("controller"):
            content += "\n## Connection Details\n"
            content += f"**Controller (SSH):** {ssh_ips['controller']}\n"
            if cf.get("ssh_key_path"):
                content += f"**SSH Key:** {cf['ssh_key_path']}\n"

        host_inventory = cf.get("host_inventory")
        if host_inventory:
            content += "\n## Host Inventory\n"
            content += (
                "This data was collected during host validation. "
                "Use it for NUMA locality analysis.\n"
            )
            for host_ip, inv in host_inventory.items():
                content += f"\n### {inv.get('fqdn', host_ip)} ({host_ip})\n"
                content += (
                    f"- **OS:** {inv.get('os', 'unknown')}\n"
                    f"- **CPUs:** {inv.get('cpu_count', '?')}\n"
                    f"- **RAM:** {inv.get('ram_gb', '?')} GB\n"
                )
                numa = inv.get("numa_topology", [])
                if numa:
                    content += f"- **NUMA nodes:** {len(numa)}\n"
                    for node in numa:
                        content += f"  - Node {node['node']}: CPUs {node['cpus']}\n"
                nics = inv.get("nic_info", [])
                if nics:
                    content += "- **NICs:**\n"
                    for nic in nics:
                        numa_str = ""
                        if "numa_node" in nic:
                            numa_str = f", NUMA node {nic['numa_node']}"
                        content += (
                            f"  - {nic['name']}: {nic.get('speed', '?')}{numa_str}\n"
                        )

        if cf.get("resource_provider_metadata"):
            content += f"\n## Provider Metadata (raw)\n```json\n{json.dumps(cf['resource_provider_metadata'], indent=2)}\n```\n"

        skills_dir = Path(__file__).resolve().parent.parent.parent / "skills" / harness
        if skills_dir.is_dir():
            content += f"\n## {harness} Skills\n"
            content += "These contain lessons from prior runs that may help interpret results:\n\n"
            for f in sorted(skills_dir.glob("*.md")):
                content += f"- `{f.name}`\n"
            content += "\nUse `read_skill` to read any of these.\n"

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
                content += "Use `read_harness_doc` to read any of these:\n\n"
                for doc in docs:
                    content += f"- `{doc['path']}`\n"

        user_comments = self._user_comments(ticket)
        if user_comments:
            content += "\n## Previous Comments\n"
            for comment in user_comments:
                content += f"\n**{comment['author']}:** {comment['body']}\n"

        return [{"role": "user", "content": content}]

    async def _handle_completion(self, ticket_id: str, response: LLMResponse) -> None:
        result = self._get_submit_result(response)
        if not result:
            result = self._parse_json_response(response.text)
        if not result:
            result = {
                "review_summary": "Review could not produce structured output",
                "verdict": "inconclusive",
                "detailed_analysis": response.text or "No analysis available",
            }

        fields = {
            "review_summary": result.get("review_summary", ""),
            "verdict": result.get("verdict", "inconclusive"),
            "detailed_analysis": result.get("detailed_analysis", ""),
            "key_metrics": result.get("key_metrics", {}),
            "recommendations": result.get("recommendations", []),
            "follow_up_needed": result.get("follow_up_needed", False),
            "review_submitted": True,
        }
        if result.get("chart_data"):
            fields["chart_data"] = result["chart_data"]
        if result.get("results_url"):
            fields["results_url"] = result["results_url"]
        await self._update_fields(ticket_id, fields)

        analysis = result.get("detailed_analysis", "")
        verdict = fields["verdict"]
        summary_line = result.get("review_summary", "")

        comment = f"**Performance Review — {verdict.replace('_', ' ').title()}**\n\n"
        if summary_line:
            comment += f"*{summary_line}*\n\n"
        if analysis:
            comment += f"{analysis}\n\n"

        recs = fields["recommendations"]
        if recs:
            comment += "### Recommendations\n"
            for r in recs:
                comment += f"- {r}\n"

        await self._add_comment(ticket_id, comment)
        if await self._plan_controls_next_transition(ticket_id):
            return
        await self._transition_ticket(
            ticket_id,
            "awaiting_teardown",
            comment="Review complete, ready for teardown",
        )
