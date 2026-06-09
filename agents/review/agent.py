from __future__ import annotations

import json
import logging
from typing import Any

from agents.base import AgentBase
from providers.llm.base import LLMProvider, LLMResponse

from .mcp_server import create_review_tool_handlers, get_review_tools
from .prompts import REVIEW_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


class ReviewAgent(AgentBase):
    def __init__(
        self,
        llm_provider: LLMProvider,
        state_store_url: str,
    ) -> None:
        self._hitl_triggered = False
        self._hitl_ticket_id: str | None = None

        tools = get_review_tools()
        tool_handlers = create_review_tool_handlers(
            request_clarification_fn=self._do_request_clarification,
        )

        super().__init__(
            agent_name="review-agent",
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
        return REVIEW_SYSTEM_PROMPT

    def _build_messages(self, ticket: dict[str, Any]) -> list[dict[str, Any]]:
        cf = ticket.get("custom_fields", {})
        content = (
            f"## Performance Test Request\n\n"
            f"**Summary:** {ticket['summary']}\n\n"
            f"**Description:**\n{ticket['description']}\n"
        )

        if cf.get("hypothesis"):
            content += f"\n## Hypothesis\n{cf['hypothesis']}\n"
        if cf.get("run_id"):
            content += f"\n**Run ID:** {cf['run_id']}\n"
        if cf.get("benchmark_status"):
            content += f"**Benchmark Status:** {cf['benchmark_status']}\n"
        if cf.get("benchmark_suite"):
            content += f"**Benchmark Suite:** {cf['benchmark_suite']}\n"
        if cf.get("benchmark_duration"):
            content += f"**Duration:** {cf['benchmark_duration']}s\n"
        if cf.get("parsed_specs"):
            content += f"\n## Specifications\n```json\n{json.dumps(cf['parsed_specs'], indent=2)}\n```\n"
        if cf.get("run_file_used"):
            content += f"\n## Run File\n```json\n{json.dumps(cf['run_file_used'], indent=2)}\n```\n"

        if ticket.get("comments"):
            content += "\n## Previous Comments\n"
            for comment in ticket["comments"]:
                content += f"\n**{comment['author']}:** {comment['body']}\n"

        return [{"role": "user", "content": content}]

    async def _handle_completion(
        self, ticket_id: str, response: LLMResponse
    ) -> None:
        if self._hitl_triggered:
            logger.info(f"[review-agent] HITL triggered for {ticket_id}")
            return

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
        await self._transition_ticket(
            ticket_id,
            "awaiting_teardown",
            comment="Review complete, ready for teardown",
        )
