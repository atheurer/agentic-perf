from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from agents.base import AgentBase
from agents.mcp_client import AgentMCPClient
from providers.events import EventBus
from providers.llm.base import LLMProvider, LLMResponse

from .mcp_server import get_retrospective_tools
from .prompts import RETROSPECTIVE_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

_MCP_TOOL_NAMES = frozenset(
    t.name for t in get_retrospective_tools() if t.name != "submit_retrospective"
)


class RetrospectiveAgent(AgentBase):
    def __init__(
        self,
        llm_provider: LLMProvider,
        state_store_url: str,
        event_bus: EventBus | None = None,
    ) -> None:
        local_tools = [
            t for t in get_retrospective_tools() if t.name not in _MCP_TOOL_NAMES
        ]

        super().__init__(
            agent_name="retrospective-agent",
            llm_provider=llm_provider,
            state_store_url=state_store_url,
            tools=local_tools,
            tool_handlers={},
            event_bus=event_bus,
        )

    async def run(self, ticket_id: str) -> None:
        retro_server = str(Path(__file__).with_name("server.py"))

        mcp = AgentMCPClient()
        await mcp.connect(
            retro_server,
            name="retrospective",
            env={
                "TICKET_ID": ticket_id,
                "STATE_STORE_URL": self.store_url,
            },
        )
        self._mcp = mcp
        mcp_tools = await mcp.list_tools()
        self.tools = mcp_tools + self.tools

        try:
            await super().run(ticket_id)
        except Exception:
            logger.exception(
                f"[retrospective-agent] Failed on {ticket_id}, closing ticket anyway"
            )
            await self._add_comment(
                ticket_id,
                "Retrospective analysis failed — see orchestrator logs.",
            )
            await self._transition_ticket(
                ticket_id,
                "closed",
                comment="Retrospective failed, closing ticket",
            )
        finally:
            await mcp.disconnect()
            self._mcp = None

    def _system_prompt(self, ticket: dict[str, Any]) -> str:
        return RETROSPECTIVE_SYSTEM_PROMPT

    def _build_messages(self, ticket: dict[str, Any]) -> list[dict[str, Any]]:
        cf = ticket.get("custom_fields", {})
        content = (
            f"## Ticket Retrospective\n\n"
            f"**Ticket ID:** {ticket['id']}\n"
            f"**Summary:** {ticket['summary']}\n\n"
        )

        if cf.get("harness_name"):
            content += f"- **Harness:** {cf['harness_name']}\n"
        if cf.get("resource_provider"):
            content += f"- **Provider:** {cf['resource_provider']}\n"
        if cf.get("verdict"):
            content += f"- **Verdict:** {cf['verdict']}\n"
        if cf.get("review_summary"):
            content += f"- **Review:** {cf['review_summary']}\n"

        content += (
            f"\nAnalyze the transcript for ticket {ticket['id']} "
            f"using get_transcript_analysis, then classify and submit "
            f"your findings."
        )

        return [{"role": "user", "content": content}]

    async def _handle_completion(self, ticket_id: str, response: LLMResponse) -> None:
        result = self._get_submit_result(response)
        if not result:
            result = self._parse_json_response(response.text)

        findings = result.get("findings", [])
        summary = result.get("summary", "No findings.")
        stats = result.get("stats", {})

        await self._update_fields(
            ticket_id,
            {
                "retrospective": {
                    "findings": findings,
                    "summary": summary,
                    "stats": stats,
                },
            },
        )

        if findings:
            categories = {}
            for f in findings:
                cat = f.get("category", "unknown")
                categories[cat] = categories.get(cat, 0) + 1
            breakdown = ", ".join(f"{count} {cat}" for cat, count in categories.items())
            comment = (
                f"**Retrospective:** {len(findings)} finding(s) — "
                f"{breakdown}\n\n{summary}"
            )
        else:
            comment = f"**Retrospective:** Clean run.\n\n{summary}"

        await self._add_comment(ticket_id, comment)
        await self._transition_ticket(
            ticket_id,
            "closed",
            comment="Retrospective complete",
        )
