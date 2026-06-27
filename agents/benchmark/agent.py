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
from .prompts import BENCHMARK_SYSTEM_PROMPT

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
            env={"TICKET_ID": ticket_id, "STATE_STORE_URL": self.store_url},
        )
        await mcp.connect(infra_server, name="infra")
        self._mcp = mcp

        mcp_tools = await mcp.list_tools()
        self.tools = mcp_tools + self.tools

        try:
            ticket = await self._get_ticket(ticket_id)
            ssh_key = ticket.get("custom_fields", {}).get("ssh_key_path")
            if ssh_key:
                # SSH key is now handled server-side via ticket data
                pass
            await super().run(ticket_id)
        finally:
            await mcp.disconnect()
            self._mcp = None

    def _system_prompt(self) -> str:
        return BENCHMARK_SYSTEM_PROMPT

    def _build_messages(self, ticket: dict[str, Any]) -> list[dict[str, Any]]:
        cf = ticket.get("custom_fields", {})
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
            content += f"\n## SSH Addresses (use these for SSH/SCP and setup_controller_ssh_keys)\n```json\n{json.dumps(cf['ssh_hardware_ips'], indent=2)}\n```\n"
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

        general_dir = Path(__file__).resolve().parent.parent.parent / "skills" / "general"
        if general_dir.is_dir():
            general_files = sorted(general_dir.glob("*.md"))
            if general_files:
                content += "\n## General Skills\n"
                for f in general_files:
                    content += f"- `{f.name}`\n"
                content += "\nUse `read_skill(harness='general', filename='...')` to read.\n"

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
            await self._transition_ticket(
                ticket_id,
                "awaiting_review",
                comment="Benchmark completed, ready for review",
            )
