from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from agents.base import AgentBase
from agents.mcp_client import AgentMCPClient
from providers.events import EventBus
from providers.llm.base import LLMProvider, LLMResponse

from .mcp_server import get_provisioning_tools
from .prompts import PROVISIONING_BASE_PROMPT

logger = logging.getLogger(__name__)

_MCP_TOOL_NAMES = frozenset(
    t.name
    for t in get_provisioning_tools()
    if t.name not in ("request_clarification", "submit_provisioning_result")
)


class ProvisioningAgent(AgentBase):
    def __init__(
        self,
        llm_provider: LLMProvider,
        state_store_url: str,
        skill_provider=None,
        secrets_provider=None,
        event_bus: EventBus | None = None,
    ) -> None:
        self._skill_provider = skill_provider
        self._secrets_provider = secrets_provider
        self._ticket_id: str | None = None

        local_tools = [
            t for t in get_provisioning_tools() if t.name not in _MCP_TOOL_NAMES
        ]

        async def _request_clarification(question: str) -> str:
            return await self._do_request_clarification(question)

        local_handlers = {
            "request_clarification": _request_clarification,
        }

        super().__init__(
            agent_name="provisioning-agent",
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

        prov_server = str(Path(__file__).with_name("server.py"))
        infra_server = str(Path(__file__).parent.parent / "infra" / "server.py")

        mcp = AgentMCPClient()
        await mcp.connect(
            prov_server,
            name="provisioning",
            env={"TICKET_ID": ticket_id, "STATE_STORE_URL": self.store_url},
        )
        await mcp.connect(infra_server, name="infra")

        # Attach Jumpstarter MCP if ticket uses Jumpstarter hardware
        from agents.jumpstarter_mcp import (
            attach_jumpstarter_mcp,
            suspend_for_device_ready,
        )

        jmp_tools = await attach_jumpstarter_mcp(mcp, ticket_id, self.store_url)

        self._mcp = mcp

        # Exclude lease management tools if Jumpstarter attached
        all_tools = await mcp.list_tools()
        if jmp_tools is not None:
            from agents.jumpstarter_mcp import _PROVIDER_ONLY_TOOLS

            all_tools = [t for t in all_tools if t.name not in _PROVIDER_ONLY_TOOLS]
        self.tools = all_tools + self.tools

        # Suspend while Jumpstarter device boots, unless
        # already resumed from a previous suspension.
        if jmp_tools is not None:
            suspended = await suspend_for_device_ready(self, ticket_id, self.store_url)
            if suspended:
                # Agent exits here. Orchestrator will
                # re-dispatch when CloudEvent arrives.
                return

        try:
            await super().run(ticket_id)
        finally:
            await mcp.disconnect()
            self._mcp = None

    def _system_prompt(self, ticket: dict[str, Any]) -> str:
        cf = ticket.get("custom_fields", {})
        directives = cf.get("directives", {})
        provider = cf.get("resource_provider") or directives.get("resource_provider")
        endpoint = directives.get("endpoint_type", "remotehosts")

        fragments = self._load_prompt_fragments(
            Path(__file__).parent,
            resource_provider=provider,
            endpoint_type=endpoint,
        )
        if fragments:
            return f"{PROVISIONING_BASE_PROMPT}\n\n{fragments}"
        return PROVISIONING_BASE_PROMPT

    def _build_messages(self, ticket: dict[str, Any]) -> list[dict[str, Any]]:
        cf = ticket.get("custom_fields", {})
        scoped = self._get_scoped_context(ticket, "provisioning")
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

        if cf.get("ssh_hardware_ips"):
            content += f"\n## SSH Addresses (use these for SSH/SCP)\n```json\n{json.dumps(cf['ssh_hardware_ips'], indent=2)}\n```\n"
            content += f"\n## Private Addresses (for run-file host entries)\n```json\n{json.dumps(cf.get('assigned_hardware_ips', {}), indent=2)}\n```\n"
        elif cf.get("assigned_hardware_ips"):
            content += f"\n## Assigned Hardware\n```json\n{json.dumps(cf['assigned_hardware_ips'], indent=2)}\n```\n"
        if cf.get("ssh_user"):
            content += f"\n**SSH User:** {cf['ssh_user']}\n"
        if cf.get("ssh_key_path"):
            content += f"**SSH Key:** {cf['ssh_key_path']}\n"
        if cf.get("fresh_host"):
            content += (
                "\n**Fresh Host:** true (freshly provisioned, no existing harness)\n"
            )
        if cf.get("directives"):
            content += f"\n## User Directives\n```json\n{json.dumps(cf['directives'], indent=2)}\n```\n"
        if cf.get("parsed_specs"):
            content += f"\n## Parsed Specifications\n```json\n{json.dumps(cf['parsed_specs'], indent=2)}\n```\n"
        if cf.get("benchmark_suite"):
            content += f"\n**Benchmark Suite:** {cf['benchmark_suite']}\n"
        if cf.get("resource_provider_metadata"):
            metadata = cf["resource_provider_metadata"]
            content += f"\n## Provider Metadata\n```json\n{json.dumps(metadata, indent=2)}\n```\n"

            # Surface Jumpstarter lease info for the skill
            if cf.get("resource_provider") == "jumpstarter":
                lease_id = cf.get("resource_reservation_id") or metadata.get(
                    "lease_id", ""
                )
                content += (
                    f"\n## Jumpstarter Device\n"
                    f"- **Lease ID:** {lease_id}\n"
                    f"- **Board:** {metadata.get('exporter_name', 'unknown')}\n"
                    f"- **Selector:** {metadata.get('selector', 'unknown')}\n"
                    f"- This is a physical embedded board that needs\n"
                    f"  flashing before use. Follow the Jumpstarter\n"
                    f"  provisioning skill above.\n"
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
                "provisioning_complete": False,
                "notes": "Could not produce structured output",
            }

        fields = {
            "provisioning_complete": result.get("provisioning_complete", False),
            "hosts_provisioned": result.get("hosts_provisioned", []),
            "harness_version": result.get("harness_version", "unknown"),
            "harness_name": result.get("harness_name", "unknown"),
            "configuration_applied": result.get("configuration_applied", {}),
        }
        if result.get("k3s_installed"):
            fields["k3s_installed"] = True
            fields["k3s_version"] = result.get("k3s_version", "unknown")

        # Jumpstarter: the provisioning agent discovers the
        # SSH IP during flashing. Update ticket fields so
        # downstream agents can SSH directly.
        if result.get("ssh_hardware_ips"):
            fields["ssh_hardware_ips"] = result["ssh_hardware_ips"]
            fields["assigned_hardware_ips"] = result.get(
                "assigned_hardware_ips",
                result["ssh_hardware_ips"],
            )
        if result.get("ssh_user"):
            fields["ssh_user"] = result["ssh_user"]
        if result.get("ssh_key_path"):
            fields["ssh_key_path"] = result["ssh_key_path"]

        await self._update_fields(ticket_id, fields)

        summary = (
            f"**Provisioning Complete**\n\n"
            f"- **Hosts:** {', '.join(fields['hosts_provisioned'])}\n"
            f"- **Harness:** {fields['harness_name']} (version: {fields['harness_version']})\n"
        )
        config = fields["configuration_applied"]
        if config:
            summary += "- **Configuration:**\n"
            for host, items in config.items():
                summary += f"  - {host}: {', '.join(items) if isinstance(items, list) else items}\n"
        if result.get("notes"):
            summary += f"- **Notes:** {result['notes']}\n"

        await self._add_comment(ticket_id, summary)
        await self._transition_ticket(
            ticket_id,
            "executing_benchmark",
            comment="Provisioning complete, ready for benchmark execution",
        )
