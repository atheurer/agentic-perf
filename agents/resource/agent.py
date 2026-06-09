from __future__ import annotations

import logging
from typing import Any

from agents.base import AgentBase
from providers.llm.base import LLMProvider, LLMResponse

from .mcp_server import create_resource_tool_handlers, get_resource_tools
from .prompts import RESOURCE_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


class ResourceAgent(AgentBase):
    def __init__(
        self,
        llm_provider: LLMProvider,
        state_store_url: str,
        mode: str = "create",
    ) -> None:
        self._mode = mode
        self._hitl_triggered = False
        self._hitl_ticket_id: str | None = None

        tools = get_resource_tools() if mode == "create" else []
        tool_handlers = (
            create_resource_tool_handlers(
                request_clarification_fn=self._do_request_clarification,
            )
            if mode == "create"
            else {}
        )

        super().__init__(
            agent_name="resource-agent",
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
        if self._mode == "teardown":
            await self._run_teardown(ticket_id)
            return
        self._hitl_ticket_id = ticket_id
        self._hitl_triggered = False
        await super().run(ticket_id)

    async def _run_teardown(self, ticket_id: str) -> None:
        logger.info(f"[resource-agent] Teardown for ticket {ticket_id}")
        await self._add_comment(ticket_id, "Resources released (simulated teardown).")
        await self._transition_ticket(
            ticket_id, "closed", comment="Resource teardown complete"
        )
        logger.info(f"[resource-agent] Teardown complete for {ticket_id}")

    def _system_prompt(self) -> str:
        return RESOURCE_SYSTEM_PROMPT

    def _build_messages(self, ticket: dict[str, Any]) -> list[dict[str, Any]]:
        content = (
            f"## Performance Test Request\n\n"
            f"**Summary:** {ticket['summary']}\n\n"
            f"**Description:**\n{ticket['description']}\n"
        )

        specs = ticket.get("custom_fields", {}).get("parsed_specs")
        if specs:
            content += f"\n## Parsed Specifications\n```json\n{specs}\n```\n"

        if ticket.get("comments"):
            content += "\n## Previous Comments\n"
            for comment in ticket["comments"]:
                content += f"\n**{comment['author']}:** {comment['body']}\n"

        return [{"role": "user", "content": content}]

    async def _handle_completion(
        self, ticket_id: str, response: LLMResponse
    ) -> None:
        if self._hitl_triggered:
            logger.info(f"[resource-agent] HITL triggered for {ticket_id}")
            return

        result = self._get_submit_result(response)
        if not result:
            result = self._parse_json_response(response.text)
        if not result:
            result = {
                "assigned_hardware_ips": {},
                "ssh_user": "root",
                "ssh_key_path": "~/.ssh/id_rsa",
                "notes": "Could not produce structured output",
            }

        fields = {
            "assigned_hardware_ips": result.get("assigned_hardware_ips", {}),
            "ssh_user": result.get("ssh_user", "root"),
            "ssh_key_path": result.get("ssh_key_path", "~/.ssh/id_rsa"),
            "lease_expiration": result.get("lease_expiration"),
        }
        await self._update_fields(ticket_id, fields)

        hw = fields["assigned_hardware_ips"]
        summary = (
            f"**Resource Allocation Complete**\n\n"
            f"- **Controller:** {hw.get('controller', 'N/A')}\n"
            f"- **Targets:** {', '.join(hw.get('targets', []))}\n"
            f"- **SSH User:** {fields['ssh_user']}\n"
        )
        if result.get("notes"):
            summary += f"- **Notes:** {result['notes']}\n"

        await self._add_comment(ticket_id, summary)
        await self._transition_ticket(
            ticket_id,
            "awaiting_provision",
            comment="Hardware validated, ready for provisioning",
        )
