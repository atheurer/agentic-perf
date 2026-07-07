from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from agents.base import AgentBase
from agents.infra.server import cleanup_passwordless_ssh
from agents.mcp_client import AgentMCPClient
from agents.provisioning.mcp_server import cleanup_harness
from providers.events import EventBus
from providers.llm.base import LLMProvider, LLMResponse
from providers.resource.registry import ResourceProviderRegistry
from providers.secrets.base import SecretsProvider
from providers.ssh import SSHExecutor

from .mcp_server import get_resource_tools
from .prompts import RESOURCE_BASE_PROMPT

logger = logging.getLogger(__name__)

_INTERNAL_TOOLS = frozenset({"submit_resource_result", "get_accumulated_metadata"})
_MCP_TOOL_NAMES = frozenset(
    t.name for t in get_resource_tools() if t.name not in _INTERNAL_TOOLS
)


def _match_to_provider_ip(
    host: str, ip_mapping: dict[str, str]
) -> tuple[str, str] | None:
    """Match a host identifier to its (public_ip, private_ip) pair.

    Handles raw IPs and AWS-style hostnames (ip-W-X-Y-Z.*.compute.internal).
    Returns None if no match is found.
    """
    if host in ip_mapping:
        return host, ip_mapping[host]
    reverse = {v: k for k, v in ip_mapping.items()}
    if host in reverse:
        return reverse[host], host
    m = re.match(r"ip-(\d+)-(\d+)-(\d+)-(\d+)", host)
    if m:
        extracted = f"{m.group(1)}.{m.group(2)}.{m.group(3)}.{m.group(4)}"
        if extracted in reverse:
            return reverse[extracted], extracted
    return None


class ResourceAgent(AgentBase):
    def __init__(
        self,
        llm_provider: LLMProvider,
        state_store_url: str,
        mode: str = "create",
        secrets_provider: SecretsProvider | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self._mode = mode
        self._ticket_id: str | None = None
        self._secrets = secrets_provider
        self._registry = (
            ResourceProviderRegistry(secrets_provider) if secrets_provider else None
        )

        self._ssh: SSHExecutor | None = None

        # Only keep local tools (submit_resource_result) -- MCP tools
        # are added dynamically in run() for create mode.
        local_tools = (
            [t for t in get_resource_tools() if t.name not in _MCP_TOOL_NAMES]
            if mode == "create"
            else []
        )

        super().__init__(
            agent_name="resource-agent",
            llm_provider=llm_provider,
            state_store_url=state_store_url,
            tools=local_tools,
            tool_handlers={},
            event_bus=event_bus,
        )

    async def _do_request_clarification(self, question: str) -> str:
        if self._ticket_id:
            return await self._request_human_input(self._ticket_id, question)
        return "No ticket context available."

    async def run(self, ticket_id: str) -> None:
        if self._mode == "teardown":
            await self._run_teardown(ticket_id)
            return
        self._ticket_id = ticket_id

        resource_server = str(Path(__file__).with_name("server.py"))

        mcp = AgentMCPClient()
        await mcp.connect(
            resource_server,
            name="resource",
            env={"TICKET_ID": ticket_id, "STATE_STORE_URL": self.store_url},
        )
        self._mcp = mcp

        mcp_tools = [
            t for t in await mcp.list_tools() if t.name != "get_accumulated_metadata"
        ]
        self.tools = mcp_tools + self.tools

        try:
            await super().run(ticket_id)
        finally:
            await mcp.disconnect()
            self._mcp = None

    async def _run_teardown(self, ticket_id: str) -> None:
        logger.info(f"[resource-agent] Teardown for ticket {ticket_id}")
        ticket = await self._get_ticket(ticket_id)
        fields = ticket.get("custom_fields", {})
        directives = fields.get("directives", {})
        host_cleanup = directives.get(
            "host_cleanup", fields.get("host_cleanup", "required")
        )

        if host_cleanup == "required":
            await self._run_host_cleanup(ticket_id, fields)

        provider_name = fields.get("resource_provider")
        reservation_id = fields.get("resource_reservation_id")
        provider_metadata = fields.get("resource_provider_metadata", {})

        # Backward compat: infer provider from legacy QUADS fields
        if not provider_name and fields.get("quads_assignment_id"):
            provider_name = "quads"
            reservation_id = str(fields["quads_assignment_id"])
            provider_metadata = {
                "assignment_id": fields["quads_assignment_id"],
                "cloud_name": fields.get("quads_cloud_name"),
            }

        if provider_name and provider_name != "user_provided" and reservation_id:
            await self._terminate_provider_resources(
                ticket_id, provider_name, reservation_id, provider_metadata
            )
        else:
            await self._add_comment(
                ticket_id,
                "Resources released (no managed reservation to terminate).",
            )

        await self._transition_ticket(
            ticket_id,
            "retrospective_pending",
            comment="Resource teardown complete, starting retrospective",
        )
        logger.info(f"[resource-agent] Teardown complete for {ticket_id}")

    async def _terminate_provider_resources(
        self,
        ticket_id: str,
        provider_name: str,
        reservation_id: str,
        provider_metadata: dict[str, Any],
    ) -> None:
        if not self._registry:
            await self._add_comment(
                ticket_id,
                f"Cannot terminate {provider_name} reservation {reservation_id}: "
                f"no secrets provider configured. Manual cleanup required.",
            )
            return

        try:
            provider = await self._registry.get_provider(provider_name)
            result = await provider.terminate(reservation_id, provider_metadata)
            await self._add_comment(
                ticket_id,
                f"{provider_name} reservation {reservation_id} terminated.",
            )
            logger.info(
                f"[resource-agent] {provider_name} reservation "
                f"{reservation_id} terminated: {result}"
            )
        except Exception as e:
            logger.exception(
                f"[resource-agent] Failed to terminate {provider_name} "
                f"reservation {reservation_id}"
            )
            await self._add_comment(
                ticket_id,
                f"Failed to terminate {provider_name} reservation "
                f"{reservation_id}: {e}",
            )

    async def _run_host_cleanup(self, ticket_id: str, fields: dict) -> None:
        hw = fields.get("ssh_hardware_ips") or fields.get("assigned_hardware_ips", {})
        controller = hw.get("controller")
        targets = hw.get("targets", [])
        ssh_key_path = fields.get("ssh_key_path")
        harness_name = fields.get("harness_name")
        all_hosts = ([controller] if controller else []) + targets

        if not all_hosts:
            logger.info("[resource-agent] No hosts to clean up")
            return

        ssh = SSHExecutor(user="root", key_path=ssh_key_path)
        cleanup_summary = []

        if controller and targets:
            try:
                result = await cleanup_passwordless_ssh(ssh, controller, targets)
                cleanup_summary.append(f"Controller SSH keys: {result['status']}")
                logger.info(f"[resource-agent] Controller key cleanup: {result}")
            except Exception as e:
                cleanup_summary.append(f"Controller SSH keys: failed ({e})")
                logger.exception("[resource-agent] Controller key cleanup failed")

        if harness_name:
            for host in all_hosts:
                try:
                    result = await cleanup_harness(ssh, host, harness_name)
                    cleanup_summary.append(f"Harness on {host}: {result['status']}")
                    logger.info(f"[resource-agent] Harness cleanup on {host}: {result}")
                except Exception as e:
                    cleanup_summary.append(f"Harness on {host}: failed ({e})")
                    logger.exception(
                        f"[resource-agent] Harness cleanup on {host} failed"
                    )

        # Provider-specific SSH key cleanup
        provider_name = fields.get("resource_provider")
        if not provider_name and fields.get("quads_assignment_id"):
            provider_name = "quads"

        if provider_name and provider_name != "user_provided" and self._registry:
            try:
                provider = await self._registry.get_provider(provider_name)
                result = await provider.cleanup_ssh_keys(all_hosts)
                cleanup_summary.append(
                    f"{provider_name} SSH keys: {result.get('status', 'done')}"
                )
                logger.info(f"[resource-agent] {provider_name} key cleanup: {result}")
            except Exception as e:
                cleanup_summary.append(f"{provider_name} SSH keys: failed ({e})")
                logger.exception(f"[resource-agent] {provider_name} key cleanup failed")

        if cleanup_summary:
            await self._add_comment(
                ticket_id,
                "**Host Cleanup**\n\n" + "\n".join(f"- {s}" for s in cleanup_summary),
            )

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
            return f"{RESOURCE_BASE_PROMPT}\n\n{fragments}"
        return RESOURCE_BASE_PROMPT

    def _build_messages(self, ticket: dict[str, Any]) -> list[dict[str, Any]]:
        scoped = self._get_scoped_context(ticket, "resource")
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

        fields = ticket.get("custom_fields", {})
        specs = fields.get("parsed_specs")
        if specs:
            content += f"\n## Parsed Specifications\n```json\n{specs}\n```\n"

        directives = fields.get("directives", {})
        if directives:
            content += "\n## Directives\n"
            for key, val in directives.items():
                content += f"- **{key}:** {val}\n"

        required_hosts = fields.get("required_hosts", [])
        endpoint_type = directives.get("endpoint_type", "remotehosts")
        if required_hosts:
            content += "\n## Resource Requirements\n"
            roles_str = ", ".join(
                "+".join(h.get("roles", ["?"])) for h in required_hosts
            )
            content += f"- **Required hosts:** {len(required_hosts)} ({roles_str})\n"
            if endpoint_type == "kube":
                content += "- **Endpoint type:** kube (workloads run as pods)\n"
                content += "- **Total hosts to provision:** 1 (single host: controller + K8s cluster)\n"

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
                "assigned_hardware_ips": {},
                "ssh_user": "root",
                "ssh_key_path": "~/.ssh/id_rsa",
                "notes": "Could not produce structured output",
            }

        fields: dict[str, Any] = {
            "assigned_hardware_ips": result.get("assigned_hardware_ips", {}),
            "ssh_user": result.get("ssh_user", "root"),
            "ssh_key_path": result.get("ssh_key_path", "~/.ssh/id_rsa"),
            "lease_expiration": result.get("lease_expiration"),
            "resource_provider": result.get("resource_provider", "user_provided"),
        }

        reservation_id = result.get("resource_reservation_id")
        if reservation_id:
            fields["resource_reservation_id"] = reservation_id

        provider_metadata = result.get("resource_provider_metadata") or {}
        reservation_metadata: dict[str, Any] = {}
        if self._mcp:
            try:
                raw = await self._mcp.call_tool("get_accumulated_metadata", {})
                reservation_metadata = json.loads(raw) if raw else {}
            except Exception:
                logger.debug("get_accumulated_metadata unavailable, skipping")
        for key in (
            "public_ips",
            "private_ips",
            "ip_mapping",
            "ami",
            "cloud_login_user",
        ):
            if key in reservation_metadata and key not in provider_metadata:
                provider_metadata[key] = reservation_metadata[key]
        if provider_metadata:
            fields["resource_provider_metadata"] = provider_metadata

        if reservation_metadata.get("ssh_user"):
            fields["ssh_user"] = reservation_metadata["ssh_user"]
        if reservation_metadata.get("ssh_key_path"):
            fields["ssh_key_path"] = reservation_metadata["ssh_key_path"]

        if result.get("fresh_host"):
            fields["fresh_host"] = True

        ip_mapping = reservation_metadata.get("ip_mapping", {})
        hw = fields["assigned_hardware_ips"]

        if ip_mapping and hw:
            ssh_hw: dict[str, Any] = {}
            private_hw: dict[str, Any] = {}

            ctrl = hw.get("controller", "")
            if ctrl:
                match = _match_to_provider_ip(ctrl, ip_mapping)
                if match:
                    ssh_hw["controller"] = match[0]
                    private_hw["controller"] = match[1]
                else:
                    ssh_hw["controller"] = ctrl
                    private_hw["controller"] = ctrl

            targets = hw.get("targets", [])
            ssh_targets = []
            private_targets = []
            for t in targets:
                match = _match_to_provider_ip(t, ip_mapping)
                if match:
                    ssh_targets.append(match[0])
                    private_targets.append(match[1])
                else:
                    ssh_targets.append(t)
                    private_targets.append(t)
            ssh_hw["targets"] = ssh_targets
            private_hw["targets"] = private_targets

            fields["ssh_hardware_ips"] = ssh_hw
            fields["assigned_hardware_ips"] = private_hw

        # Backward compat: write legacy QUADS fields when provider is quads
        provider = fields.get("resource_provider")
        if provider == "quads":
            meta = provider_metadata or {}
            if meta.get("assignment_id"):
                fields["quads_assignment_id"] = meta["assignment_id"]
            elif result.get("quads_assignment_id"):
                fields["quads_assignment_id"] = result["quads_assignment_id"]
            if meta.get("cloud_name"):
                fields["quads_cloud_name"] = meta["cloud_name"]
            elif result.get("quads_cloud_name"):
                fields["quads_cloud_name"] = result["quads_cloud_name"]

        await self._update_fields(ticket_id, fields)

        hw = fields["assigned_hardware_ips"]
        summary = (
            f"**Resource Allocation Complete**\n\n"
            f"- **Provider:** {fields.get('resource_provider', 'unknown')}\n"
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
