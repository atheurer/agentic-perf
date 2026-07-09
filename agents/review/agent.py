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

from .mcp_server import get_review_tools
from .prompts import REVIEW_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

_LOCAL_TOOL_NAMES = frozenset({"request_clarification", "submit_review_result"})

_MCP_TOOL_NAMES = frozenset(
    t.name for t in get_review_tools() if t.name not in _LOCAL_TOOL_NAMES
) | {"list_harness_docs", "read_harness_doc"}


class ReviewAgent(AgentBase):
    def __init__(
        self,
        llm_provider: LLMProvider,
        state_store_url: str,
        skill_provider=None,
        event_bus: EventBus | None = None,
        repo_cache: RepoCache | None = None,
    ) -> None:
        self._skill_provider = skill_provider
        self._repo_cache = repo_cache
        self._ticket_id: str | None = None

        local_tools = [
            t
            for t in get_review_tools(repo_cache=repo_cache)
            if t.name not in _MCP_TOOL_NAMES
        ]

        async def _request_clarification(question: str) -> str:
            return await self._do_request_clarification(question)

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
        )

    async def _do_request_clarification(self, question: str) -> str:
        if self._ticket_id:
            return await self._request_human_input(self._ticket_id, question)
        return "No ticket context available."

    async def run(self, ticket_id: str) -> None:
        self._ticket_id = ticket_id

        review_server = str(Path(__file__).with_name("server.py"))

        mcp = AgentMCPClient()
        await mcp.connect(
            review_server,
            name="review",
            env={"TICKET_ID": ticket_id, "STATE_STORE_URL": self.store_url},
        )
        self._mcp = mcp

        mcp_tools = await mcp.list_tools()
        self.tools = mcp_tools + self.tools

        try:
            await super().run(ticket_id)
        finally:
            await mcp.disconnect()
            self._mcp = None

    def _system_prompt(self, ticket: dict[str, Any]) -> str:
        return REVIEW_SYSTEM_PROMPT

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

        harness = cf.get("harness_name") or cf.get("directives", {}).get(
            "harness", "crucible"
        )
        content += f"**Harness:** {harness}\n"

        if cf.get("benchmark_duration"):
            content += f"**Duration:** {cf['benchmark_duration']}s\n"
        if cf.get("parsed_specs"):
            content += f"\n## Specifications\n```json\n{json.dumps(cf['parsed_specs'], indent=2)}\n```\n"
        if cf.get("run_file_used"):
            content += f"\n## Run File\n```json\n{json.dumps(cf['run_file_used'], indent=2)}\n```\n"

        ssh_ips = cf.get("ssh_hardware_ips") or cf.get("assigned_hardware_ips") or {}
        if ssh_ips.get("controller"):
            content += "\n## Connection Details\n"
            content += f"**Controller (SSH):** {ssh_ips['controller']}\n"
            if cf.get("ssh_key_path"):
                content += f"**SSH Key:** {cf['ssh_key_path']}\n"

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
