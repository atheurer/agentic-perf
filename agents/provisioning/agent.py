from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from agents.base import AgentBase
from agents.mcp_client import AgentMCPClient
from providers.events import EventBus
from providers.llm.base import LLMProvider, LLMResponse, ToolDefinition

from .prompts import PROVISIONING_BASE_PROMPT

logger = logging.getLogger(__name__)

# The only two tools NOT served by the provisioning MCP server (server.py) —
# everything else the agent can call comes from that live connection.
_LOCAL_TOOLS = [
    ToolDefinition(
        name="request_clarification",
        description="Ask the user for clarification. Pauses the ticket for human input.",
        input_schema={
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "Question to ask"},
            },
            "required": ["question"],
        },
    ),
    ToolDefinition(
        name="submit_provisioning_result",
        description="Submit the provisioning result when all hosts are prepared.",
        input_schema={
            "type": "object",
            "properties": {
                "provisioning_complete": {"type": "boolean"},
                "hosts_provisioned": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "harness_version": {"type": "string"},
                "harness_name": {"type": "string"},
                "configuration_applied": {"type": "object"},
                "k3s_installed": {
                    "type": "boolean",
                    "description": "Whether K3s was installed",
                },
                "k3s_version": {
                    "type": "string",
                    "description": "K3s version string (if installed)",
                },
                "notes": {"type": "string"},
            },
            "required": ["provisioning_complete", "hosts_provisioned"],
        },
    ),
]


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

        local_tools = list(_LOCAL_TOOLS)

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

    # Harnesses that need no provisioning setup beyond
    # flash + boot. These get a reduced tool set and
    # provisioning_complete override. Extend this set
    # when adding new self-contained harnesses.
    _SELF_INSTALLING: frozenset[str] = frozenset({"boot-time", "arcaflow-plugins"})

    # Denied for every harness, no exceptions (see #483): analysis of
    # 444 ticket logs showed the agent bypassing structured tools
    # (tune_nic, tune_tcp, pin_irq, disable_firewall, nm_set_mtu) in
    # favor of raw execute_command, causing tuning that doesn't
    # persist, skipped verification, and wasted iteration budget on
    # ad-hoc command discovery. Every legitimate provisioning
    # operation has a structured tool — this is not harness-specific
    # scoping like _PROVISIONING_DENY_TOOLS below.
    _ALWAYS_DENY_TOOLS: frozenset[str] = frozenset({"execute_command"})

    _PROVISIONING_DENY_TOOLS: frozenset[str] = frozenset(
        {
            "deploy_secret",
            "get_private_config",
            "install_harness",
            "install_packages",
            "install_k3s",
            "verify_harness_install",
            "check_existing_install",
            "update_install",
            "uninstall_harness",
            "ensure_harness_installed",
            "ensure_prerequisites",
            "check_host_prerequisites",
            "check_platform_contract",
        }
    )

    async def _auto_complete_jumpstarter(
        self,
        ticket_id: str,
        cf: dict[str, Any],
    ) -> None:
        """Auto-complete provisioning for Jumpstarter boards.

        When the platform agent has already flashed and
        verified the board, and the harness is self-
        installing, there's nothing for the provisioning
        agent to do. Set provisioning_complete and advance.
        """
        hosts = cf.get("hosts_provisioned", [])
        harness = cf.get("directives", {}).get("harness", "unknown")
        fields = {
            "provisioning_complete": True,
            "harness_name": harness,
            "harness_version": "platform-provisioned",
        }
        if cf.get("ssh_user"):
            fields["ssh_user"] = cf["ssh_user"]
        if cf.get("ssh_key_path"):
            fields["ssh_key_path"] = cf["ssh_key_path"]

        await self._update_fields(ticket_id, fields)

        summary = (
            "**Provisioning Complete** "
            "(platform-provisioned)\n\n"
            f"- **Hosts:** {', '.join(str(h) for h in hosts)}\n"
            f"- **Harness:** {harness}\n"
            "- **Note:** Board flashed and verified by "
            "platform agent. Self-installing harness "
            "requires no additional provisioning.\n"
        )
        await self._add_comment(ticket_id, summary)

        if not await self._plan_controls_next_transition(ticket_id):
            await self._transition_ticket(
                ticket_id,
                "executing_benchmark",
                comment="Provisioning complete (auto)",
            )

    def _apply_tool_scoping(self, ticket: dict[str, Any]) -> None:
        """Hide install/config tools for self-installing harnesses."""
        harness = (
            ticket.get("custom_fields", {}).get("directives", {}).get("harness", "")
        )
        if harness in self._SELF_INSTALLING:
            self.tools = [
                t for t in self.tools if t.name not in self._PROVISIONING_DENY_TOOLS
            ]

    def _apply_always_deny(self) -> None:
        """Remove tools denied for every harness, no exceptions (see #483)."""
        self.tools = [t for t in self.tools if t.name not in self._ALWAYS_DENY_TOOLS]

    async def run(self, ticket_id: str) -> None:
        self._ticket_id = ticket_id

        prov_server = str(Path(__file__).with_name("server.py"))
        infra_server = str(Path(__file__).parent.parent / "infra" / "server.py")

        mcp = AgentMCPClient()
        await mcp.connect(
            prov_server,
            name="provisioning",
            env={
                "TICKET_ID": ticket_id,
                "STATE_STORE_URL": self.store_url,
                "AGENT_NAME": self.agent_name,
            },
        )
        await mcp.connect(infra_server, name="infra")

        self._mcp = mcp

        # Check if this is a Jumpstarter ticket with a
        # self-installing harness. The platform agent
        # already handled flash/boot/verify — the
        # provisioning agent just needs to confirm
        # and advance.
        ticket = await self._get_ticket(ticket_id)
        cf = ticket.get("custom_fields", {})
        harness = cf.get("directives", {}).get("harness", "")
        is_jumpstarter = cf.get("resource_provider") == "jumpstarter"

        if is_jumpstarter and (not harness or harness in self._SELF_INSTALLING):
            # Platform agent already provisioned the
            # board. For self-installing harnesses,
            # there's nothing more to do.
            if cf.get("platform_ready") and cf.get("hosts_provisioned"):
                await self._auto_complete_jumpstarter(ticket_id, cf)
                await mcp.disconnect()
                self._mcp = None
                return

        mcp_tools = await mcp.list_tools()
        self.tools = mcp_tools + self.tools

        # For Jumpstarter with non-self-installing
        # harnesses, keep install tools but don't
        # attach Jumpstarter MCP (platform agent
        # handled device operations).
        # For non-Jumpstarter, apply harness-based
        # scoping as before.
        if not is_jumpstarter:
            self._apply_tool_scoping(ticket)

        self._apply_always_deny()

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

            # Surface Jumpstarter lease info for the agent
            if cf.get("resource_provider") == "jumpstarter":
                lease_id = cf.get("resource_reservation_id") or metadata.get(
                    "lease_id", ""
                )
                content += (
                    f"\n## Jumpstarter Device\n"
                    f"- **Lease ID:** {lease_id}\n"
                    f"- **Board:** {metadata.get('exporter_name', 'unknown')}\n"
                    f"- **Selector:** {metadata.get('selector', 'unknown')}\n"
                    f"- This device needs flashing before use.\n"
                    f"  Follow the Jumpstarter provisioning\n"
                    f"  prompt above.\n"
                )

                flash = cf.get("jumpstarter_flash", {})
                if flash.get("flash_command"):
                    content += (
                        f"\n## Pre-Resolved Flash Command\n"
                        f"```\n{flash['flash_command']}\n```\n"
                        f"Run this via `jmp_run` with "
                        f"timeout_seconds=600.\n"
                    )
                    if flash.get("ssh_public_key"):
                        content += (
                            f"\n## SSH Public Key "
                            f"(for key injection)\n"
                            f"```\n"
                            f"{flash['ssh_public_key']}\n"
                            f"```\n"
                            f"**Key path:** "
                            f"{flash.get('ssh_key_path', '/root/.ssh/id_rsa')}\n"
                        )
                elif flash.get("error"):
                    content += f"\n## Image Resolution Error\n{flash['error']}\n"
                    if flash.get("available_variants"):
                        content += (
                            f"Available variants: "
                            f"{json.dumps(flash['available_variants'])}\n"
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

        # Self-installing harnesses don't need
        # provisioning to install them. If the LLM
        # reports incomplete because install_harness
        # failed, override when hosts were provisioned.
        harness = result.get("harness_name", "unknown")
        prov_complete = result.get("provisioning_complete", False)
        if (
            not prov_complete
            and harness in self._SELF_INSTALLING
            and result.get("hosts_provisioned")
        ):
            prov_complete = True
            logger.info(
                f"[provisioning] Overriding provisioning_complete "
                f"for self-installing harness {harness}"
            )

        fields = {
            "provisioning_complete": prov_complete,
            "hosts_provisioned": result.get("hosts_provisioned", []),
            "harness_version": result.get("harness_version", "unknown"),
            "harness_name": harness,
            "configuration_applied": result.get("configuration_applied", {}),
        }
        if result.get("k3s_installed"):
            fields["k3s_installed"] = True
            fields["k3s_version"] = result.get("k3s_version", "unknown")

        # Derive ssh_hardware_ips from hosts_provisioned.
        ssh_ips = result.get("ssh_hardware_ips")
        if not ssh_ips and fields.get("hosts_provisioned"):
            hosts = fields["hosts_provisioned"]
            first_ip = str(hosts[0]) if hosts else ""
            if first_ip:
                ssh_ips = {
                    "controller": first_ip,
                    "targets": [first_ip],
                }
        if ssh_ips:
            fields["ssh_hardware_ips"] = ssh_ips
            fields["assigned_hardware_ips"] = result.get(
                "assigned_hardware_ips",
                ssh_ips,
            )
        if result.get("ssh_user"):
            fields["ssh_user"] = result["ssh_user"]
        if result.get("ssh_key_path"):
            fields["ssh_key_path"] = result["ssh_key_path"]

        await self._update_fields(ticket_id, fields)

        hosts = [
            str(h) if not isinstance(h, dict) else h.get("host", h.get("ip", str(h)))
            for h in fields["hosts_provisioned"]
        ]
        summary = (
            f"**Provisioning Complete**\n\n"
            f"- **Hosts:** {', '.join(hosts)}\n"
            f"- **Harness:** {fields['harness_name']} (version: {fields['harness_version']})\n"
        )
        config = fields["configuration_applied"]
        if config:
            summary += "- **Configuration:**\n"
            for host, items in config.items():
                if isinstance(items, list):
                    summary += f"  - {host}: {', '.join(str(i) for i in items)}\n"
                else:
                    summary += f"  - {host}: {items}\n"
        if result.get("notes"):
            summary += f"- **Notes:** {result['notes']}\n"

        await self._add_comment(ticket_id, summary)
        if await self._plan_controls_next_transition(ticket_id):
            return
        await self._transition_ticket(
            ticket_id,
            "executing_benchmark",
            comment="Provisioning complete, ready for benchmark execution",
        )
