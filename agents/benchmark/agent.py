from __future__ import annotations

import json
import logging
from typing import Any

from agents.base import AgentBase
from providers.events import EventBus
from providers.llm.base import LLMProvider, LLMResponse

from .mcp_server import create_benchmark_tool_handlers, get_benchmark_tools
from .prompts import BENCHMARK_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


class BenchmarkAgent(AgentBase):
    def __init__(
        self,
        llm_provider: LLMProvider,
        state_store_url: str,
        skill_provider=None,
        event_bus: EventBus | None = None,
    ) -> None:
        self._skill_provider = skill_provider
        self._hitl_triggered = False
        self._hitl_ticket_id: str | None = None

        tools = get_benchmark_tools()
        tool_handlers = create_benchmark_tool_handlers(
            skill_provider=skill_provider,
            request_clarification_fn=self._do_request_clarification,
        )

        super().__init__(
            agent_name="benchmark-agent",
            llm_provider=llm_provider,
            state_store_url=state_store_url,
            tools=tools,
            tool_handlers=tool_handlers,
            event_bus=event_bus,
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
        return BENCHMARK_SYSTEM_PROMPT

    def _build_messages(self, ticket: dict[str, Any]) -> list[dict[str, Any]]:
        cf = ticket.get("custom_fields", {})
        content = (
            f"## Performance Test Request\n\n"
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
        if cf.get("assigned_hardware_ips"):
            content += f"\n## Assigned Hardware\n```json\n{json.dumps(cf['assigned_hardware_ips'], indent=2)}\n```\n"
        if cf.get("ssh_user"):
            content += f"\n**SSH User:** {cf['ssh_user']}\n"

        if ticket.get("comments"):
            content += "\n## Previous Comments\n"
            for comment in ticket["comments"]:
                content += f"\n**{comment['author']}:** {comment['body']}\n"

        return [{"role": "user", "content": content}]

    async def _handle_completion(
        self, ticket_id: str, response: LLMResponse
    ) -> None:
        if self._hitl_triggered:
            logger.info(f"[benchmark-agent] HITL triggered for {ticket_id}")
            return

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
