from __future__ import annotations

import logging
from typing import Any

from agents.base import AgentBase
from providers.llm.base import LLMProvider, LLMResponse

from .mcp_server import create_triage_tool_handlers, get_triage_tools
from .prompts import TRIAGE_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


class TriageAgent(AgentBase):
    def __init__(
        self,
        llm_provider: LLMProvider,
        state_store_url: str,
        skill_provider,
    ) -> None:
        self._skill_provider = skill_provider
        self._hitl_triggered = False
        self._hitl_ticket_id: str | None = None

        tools = get_triage_tools()
        tool_handlers = create_triage_tool_handlers(
            skill_provider=skill_provider,
            request_clarification_fn=self._do_request_clarification,
        )

        super().__init__(
            agent_name="triage-agent",
            llm_provider=llm_provider,
            state_store_url=state_store_url,
            tools=tools,
            tool_handlers=tool_handlers,
        )

    async def _do_request_clarification(self, question: str) -> None:
        if self._hitl_ticket_id:
            self._hitl_triggered = True
            await self._request_human_input(self._hitl_ticket_id, question)

    async def run(self, ticket_id: str) -> None:
        self._hitl_ticket_id = ticket_id
        self._hitl_triggered = False
        await super().run(ticket_id)

    def _system_prompt(self) -> str:
        return TRIAGE_SYSTEM_PROMPT

    def _build_messages(self, ticket: dict[str, Any]) -> list[dict[str, Any]]:
        content = (
            f"## Performance Test Request\n\n"
            f"**Summary:** {ticket['summary']}\n\n"
            f"**Description:**\n{ticket['description']}\n"
        )

        if ticket.get("comments"):
            content += "\n## Previous Comments\n"
            for comment in ticket["comments"]:
                content += f"\n**{comment['author']}:** {comment['body']}\n"

        return [{"role": "user", "content": content}]

    async def _handle_completion(
        self, ticket_id: str, response: LLMResponse
    ) -> None:
        if self._hitl_triggered:
            logger.info(f"[triage-agent] HITL triggered for {ticket_id}, pausing")
            return

        result = self._get_submit_result(response)
        if not result:
            result = self._parse_json_response(response.text)
        if not result:
            await self._add_comment(ticket_id, "Triage agent could not produce structured output.")
            return

        roles = result.get("roles", [])
        min_hosts = result.get("min_hosts", 1)
        fields = {
            "parsed_specs": result.get("parsed_specs", {}),
            "hypothesis": result.get("hypothesis", ""),
            "benchmark_suite": result.get("benchmark_suite", ""),
            "absent_suite": result.get("absent_suite", False),
            "required_roles": roles,
            "min_hosts": min_hosts,
        }
        await self._update_fields(ticket_id, fields)

        summary = (
            f"**Triage Complete**\n\n"
            f"- **Hypothesis:** {fields['hypothesis']}\n"
            f"- **Benchmark Suite:** {fields['benchmark_suite']}\n"
            f"- **Required Hosts:** {min_hosts} ({', '.join(roles) if roles else 'unknown'})\n"
            f"- **Absent Suite:** {fields['absent_suite']}\n"
        )
        if result.get("notes"):
            summary += f"- **Notes:** {result['notes']}\n"

        await self._add_comment(ticket_id, summary)
        await self._transition_ticket(
            ticket_id,
            "awaiting_hardware",
            comment="Triage complete, requesting hardware",
        )
