"""Platform agent — system provisioning via MCP tool pattern.

Prepares hardware for benchmark use by getting the OS running
and SSH accessible. The LLM calls a provision_platform MCP tool
which runs deterministic code internally (flash, boot, verify).

The LLM adds value by:
- Reading investigation context to adjust provisioning params
- Interpreting failure diagnostics for recovery decisions
- Escalating to HITL with actionable context

For providers that return ready hosts (AWS, pre-provisioned
QUADS), the LLM verifies and submits immediately.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from agents.base import AgentBase
from agents.mcp_client import AgentMCPClient
from providers.events import EventBus
from providers.llm.base import LLMProvider, LLMResponse

from .prompts import PLATFORM_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


class PlatformAgent(AgentBase):
    """Platform setup via standard LLM + MCP tool pattern."""

    def __init__(
        self,
        llm_provider: LLMProvider,
        state_store_url: str,
        event_bus: EventBus | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            agent_name="platform-agent",
            llm_provider=llm_provider,
            state_store_url=state_store_url,
            event_bus=event_bus,
            tool_handlers={},
            max_iterations=10,
            **kwargs,
        )

    async def run(self, ticket_id: str) -> None:
        """Standard agent run with MCP server."""
        self._ticket_id = ticket_id

        ticket = await self._get_ticket(ticket_id)
        ticket.get("custom_fields", {})

        platform_server = str(Path(__file__).with_name("server.py"))
        mcp = AgentMCPClient()
        await mcp.connect(
            platform_server,
            name="platform",
            env={
                "TICKET_ID": ticket_id,
                "STATE_STORE_URL": self.store_url,
            },
        )

        self._mcp = mcp
        self.tools = await mcp.list_tools()

        try:
            await super().run(ticket_id)
        finally:
            await mcp.disconnect()
            self._mcp = None

    def _system_prompt(self, ticket: dict[str, Any]) -> str:
        return PLATFORM_SYSTEM_PROMPT

    def _build_messages(self, ticket: dict[str, Any]) -> list[dict[str, Any]]:
        cf = ticket.get("custom_fields", {})
        provider = cf.get("resource_provider", "unknown")
        metadata = cf.get("resource_provider_metadata", {})
        flash_info = cf.get("jumpstarter_flash", {})
        cf.get("directives", {})

        content = f"## Platform Setup for {ticket['id']}\n\n"
        content += f"- **Provider:** {provider}\n"

        if provider == "jumpstarter":
            content += (
                f"- **Board:** "
                f"{metadata.get('exporter_name', 'unknown')}\n"
                f"- **Lease:** "
                f"{metadata.get('lease_id', 'unknown')}\n"
            )
            if flash_info.get("error"):
                content += f"\n## Image Resolution Error\n{flash_info['error']}\n"
                if flash_info.get("available_variants"):
                    content += (
                        f"Available variants: "
                        f"{json.dumps(flash_info['available_variants'])}\n"
                    )
                content += (
                    "\nCall submit_platform_result with "
                    "platform_ready=false and include this error.\n"
                )
            elif flash_info.get("flash_command"):
                content += (
                    f"\n## Flash Command (pre-resolved)\n"
                    f"```\n{flash_info['flash_command']}\n```\n"
                    f"\nCall provision_platform to flash "
                    f"and boot the board.\n"
                )
        else:
            ips = cf.get("assigned_hardware_ips", {})
            content += (
                f"- **Hosts:** {json.dumps(ips)}\n"
                f"\nHosts are already provisioned. Call "
                f"provision_platform to verify, then "
                f"submit_platform_result.\n"
            )

        # Include investigation context if present
        if cf.get("anomaly_context"):
            content += (
                f"\n## Investigation Context\n"
                f"{json.dumps(cf['anomaly_context'], indent=2)}\n"
            )

        # Include relevant comments for context
        if ticket.get("comments"):
            relevant = [
                c
                for c in ticket["comments"]
                if c["author"]
                in (
                    "gathering-context-agent",
                    "resource-agent",
                    "orchestrator",
                )
            ]
            if relevant:
                content += "\n## Prior Agent Notes\n"
                for c in relevant[-3:]:
                    content += f"\n**{c['author']}:** {c['body'][:300]}\n"

        return [{"role": "user", "content": content}]

    async def _handle_completion(
        self,
        ticket_id: str,
        response: LLMResponse,
    ) -> None:
        """Process submit_platform_result."""
        result = self._get_submit_result(response)
        if not result:
            result = self._parse_json_response(response.text)
        if not result:
            result = {
                "platform_ready": False,
                "diagnostics": "No structured output",
            }

        platform_ready = result.get("platform_ready", False)
        hosts = result.get("hosts_provisioned", [])

        fields: dict[str, Any] = {
            "platform_ready": platform_ready,
        }
        if hosts:
            fields["hosts_provisioned"] = hosts
            fields["assigned_hardware_ips"] = {
                "controller": hosts[0],
                "targets": hosts,
            }
        if result.get("ssh_user"):
            fields["ssh_user"] = result["ssh_user"]
        if result.get("ssh_key_path"):
            fields["ssh_key_path"] = result["ssh_key_path"]
        if result.get("board_name"):
            fields["platform_board"] = result["board_name"]
        if result.get("flash_duration_s"):
            fields["platform_flash_duration_s"] = result["flash_duration_s"]
        if result.get("boot_duration_s"):
            fields["platform_boot_duration_s"] = result["boot_duration_s"]

        await self._update_fields(ticket_id, fields)

        if platform_ready:
            summary = "**Platform Ready**\n\n"
            if hosts:
                summary += f"- **Hosts:** {', '.join(hosts)}\n"
            if result.get("board_name"):
                summary += f"- **Board:** {result['board_name']}\n"
            await self._add_comment(ticket_id, summary)

            if not await self._plan_controls_next_transition(ticket_id):
                await self._transition_ticket(
                    ticket_id,
                    "awaiting_provision",
                    comment="Platform ready",
                )
        else:
            diag = result.get("diagnostics", "No details")
            summary = f"**Platform Setup Failed**\n\n- **Diagnostics:** {diag}\n"
            if result.get("board_name"):
                summary += f"- **Board:** {result['board_name']}\n"
            await self._add_comment(ticket_id, summary)

            await self._transition_ticket(
                ticket_id,
                "awaiting_customer_guidance",
                comment="Platform setup failed",
            )
