from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from agents.base import AgentBase
from agents.mcp_client import AgentMCPClient
from providers.events import EventBus
from providers.llm.base import LLMProvider, LLMResponse

from .mcp_server import get_triage_tools
from .prompts import TRIAGE_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

_MCP_TOOL_NAMES = frozenset(
    {"list_benchmarks", "get_benchmark_details", "resolve_benchmark"}
)


class TriageAgent(AgentBase):
    def __init__(
        self,
        llm_provider: LLMProvider,
        state_store_url: str,
        skill_provider,
        event_bus: EventBus | None = None,
    ) -> None:
        self._skill_provider = skill_provider
        self._ticket_id: str | None = None

        local_tools = [t for t in get_triage_tools() if t.name not in _MCP_TOOL_NAMES]

        async def _request_clarification(question: str) -> str:
            return await self._do_request_clarification(question)

        local_handlers = {
            "request_clarification": _request_clarification,
        }

        super().__init__(
            agent_name="triage-agent",
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

        triage_server = str(Path(__file__).with_name("server.py"))
        infra_server = str(Path(__file__).parent.parent / "infra" / "server.py")

        mcp = AgentMCPClient()
        await mcp.connect(triage_server, name="triage")
        await mcp.connect(infra_server, name="infra")
        self._mcp = mcp

        mcp_tools = await mcp.list_tools()
        self.tools = mcp_tools + self.tools

        try:
            await super().run(ticket_id)
        finally:
            await mcp.disconnect()
            self._mcp = None

    def _system_prompt(self) -> str:
        return TRIAGE_SYSTEM_PROMPT

    def _build_messages(self, ticket: dict[str, Any]) -> list[dict[str, Any]]:
        content = (
            f"## Performance Test Request\n\n"
            f"**Ticket ID:** {ticket['id']}\n"
            f"**Summary:** {ticket['summary']}\n\n"
            f"**Description:**\n{ticket['description']}\n"
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
            await self._add_comment(
                ticket_id, "Triage agent could not produce structured output."
            )
            return

        roles = result.get("roles", [])
        min_hosts = result.get("min_hosts", 1)
        directives = result.get("directives", {})
        # Backward compat: top-level host_cleanup moves into directives
        if "host_cleanup" in result and "host_cleanup" not in directives:
            directives["host_cleanup"] = result["host_cleanup"]
        fields: dict[str, Any] = {
            "parsed_specs": result.get("parsed_specs", {}),
            "hypothesis": result.get("hypothesis", ""),
            "benchmark_suite": result.get("benchmark_suite", ""),
            "absent_suite": result.get("absent_suite", False),
            "required_roles": roles,
            "min_hosts": min_hosts,
            "directives": directives,
        }

        raw_plan = result.get("execution_plan")
        if raw_plan and isinstance(raw_plan, list) and len(raw_plan) > 1:
            steps = []
            for i, s in enumerate(raw_plan):
                steps.append(
                    {
                        "id": i,
                        "agent_type": s.get("agent_type", "benchmark"),
                        "status": "in_progress" if i == 0 else "pending",
                        "params": s.get("params", {}),
                        "results": {},
                    }
                )
            fields["execution_plan"] = {
                "current_step": 0,
                "run_ids": [],
                "steps": steps,
            }

        await self._update_fields(ticket_id, fields)

        summary = (
            f"**Triage Complete**\n\n"
            f"- **Hypothesis:** {fields['hypothesis']}\n"
            f"- **Benchmark Suite:** {fields['benchmark_suite']}\n"
            f"- **Required Hosts:** {min_hosts} ({', '.join(roles) if roles else 'unknown'})\n"
            f"- **Absent Suite:** {fields['absent_suite']}\n"
        )
        if directives:
            summary += f"- **Directives:** {', '.join(f'{k}={v}' for k, v in directives.items())}\n"
        if result.get("notes"):
            summary += f"- **Notes:** {result['notes']}\n"

        await self._add_comment(ticket_id, summary)

        # Route based on whether anomaly_context is present.
        # Set by alert seeds, CLI, or API — not inferred by
        # the LLM. Code enforces the routing invariant.
        ticket = await self._get_ticket(ticket_id)
        cf = ticket.get("custom_fields", {})
        if cf.get("anomaly_context"):
            await self._transition_ticket(
                ticket_id,
                "gathering_context",
                comment=(
                    "Triage complete, anomaly context present"
                    " — routing to investigation"
                ),
            )
        else:
            await self._transition_ticket(
                ticket_id,
                "awaiting_hardware",
                comment="Triage complete, requesting hardware",
            )
