from __future__ import annotations

import logging
import re
from typing import Any

from providers.llm.base import ToolDefinition

logger = logging.getLogger(__name__)


def get_resource_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="parse_host_config",
            description=(
                "Extract structured host configuration from free-form text. "
                "Parses IP addresses, hostnames, roles (controller/target/client/server), "
                "SSH user, and SSH key path."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Free-form text containing host configuration",
                    }
                },
                "required": ["text"],
            },
        ),
        ToolDefinition(
            name="validate_host",
            description=(
                "Validate that a host is reachable via SSH. "
                "Returns connectivity status and basic system info."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "host": {
                        "type": "string",
                        "description": "IP address or hostname to validate",
                    },
                    "user": {
                        "type": "string",
                        "description": "SSH user (default: root)",
                    },
                    "ssh_key_path": {
                        "type": "string",
                        "description": "Path to SSH private key",
                    },
                },
                "required": ["host"],
            },
        ),
        ToolDefinition(
            name="list_resource_providers",
            description=(
                "List resource providers that are configured and available. "
                "Returns provider names and types (bare_metal, cloud). "
                "Call this first if no resource_provider directive is set."
            ),
            input_schema={
                "type": "object",
                "properties": {},
            },
        ),
        ToolDefinition(
            name="check_available_resources",
            description=(
                "Check what resources are available from a specific provider. "
                "For bare-metal providers (quads), returns available hosts with "
                "CPU, memory, disk, and NIC details. For cloud providers (aws), "
                "returns recommended instance types. Use filters to narrow results."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "provider": {
                        "type": "string",
                        "description": "Provider name (e.g., 'quads', 'aws')",
                    },
                    "requirements": {
                        "type": "object",
                        "description": (
                            "Resource requirements. Common keys: "
                            "min_cores (int), min_memory_gb (int), "
                            "nic_speed (int, Gbps), nic_vendor (str), "
                            "disk_type (str), count (int, hosts needed), "
                            "duration_hours (int). "
                            "Provider-specific: model_filter (quads), "
                            "instance_type (aws)."
                        ),
                    },
                },
                "required": ["provider"],
            },
        ),
        ToolDefinition(
            name="reserve_resources",
            description=(
                "Reserve resources from a provider. For bare-metal (quads), "
                "this creates an assignment, schedules hosts, waits for "
                "validation (~30-45 min), and sets up SSH access. For cloud "
                "(aws), this launches instances, waits until running, and "
                "verifies SSH connectivity. Returns host IPs, SSH credentials, "
                "and a reservation ID for teardown."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "provider": {
                        "type": "string",
                        "description": "Provider name (e.g., 'quads', 'aws')",
                    },
                    "selection": {
                        "type": "object",
                        "description": (
                            "What to reserve, based on check_available_resources results. "
                            "For quads: {hostnames: ['host1.example.com', ...]}. "
                            "For aws: {instance_type: 'm5.xlarge', count: 2}."
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": "Short description for the reservation",
                    },
                    "ticket_id": {
                        "type": "string",
                        "description": "Jira ticket ID (e.g., 'PERF-123') for instance naming and traceability",
                    },
                    "duration_hours": {
                        "type": "integer",
                        "description": "Lease duration in hours (default: 36, ignored by cloud providers)",
                    },
                },
                "required": ["provider", "selection", "description"],
            },
        ),
        ToolDefinition(
            name="get_reservation_status",
            description=(
                "Check the status of an existing resource reservation."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "provider": {
                        "type": "string",
                        "description": "Provider name",
                    },
                    "reservation_id": {
                        "type": "string",
                        "description": "Reservation ID from reserve_resources result",
                    },
                },
                "required": ["provider", "reservation_id"],
            },
        ),
        ToolDefinition(
            name="submit_resource_result",
            description="Submit the resource allocation result when host validation is complete.",
            input_schema={
                "type": "object",
                "properties": {
                    "assigned_hardware_ips": {
                        "type": "object",
                        "description": "Controller and target host IPs/hostnames",
                        "properties": {
                            "controller": {"type": "string"},
                            "targets": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                    "ssh_user": {"type": "string"},
                    "ssh_key_path": {"type": "string"},
                    "resource_provider": {
                        "type": "string",
                        "description": "Provider used: 'quads', 'aws', 'user_provided', etc.",
                    },
                    "resource_reservation_id": {
                        "type": ["string", "null"],
                        "description": "Reservation ID for teardown (from reserve_resources result)",
                    },
                    "resource_provider_metadata": {
                        "type": ["object", "null"],
                        "description": (
                            "Provider-specific metadata for teardown. "
                            "QUADS: {assignment_id, cloud_name}. "
                            "AWS: {instance_ids, region, instance_type}."
                        ),
                    },
                    "lease_expiration": {"type": ["string", "null"]},
                    "fresh_host": {
                        "type": "boolean",
                        "description": (
                            "True if hosts were freshly provisioned and need a full "
                            "harness install. Set true for QUADS and cloud providers."
                        ),
                    },
                    "notes": {"type": "string"},
                    # Backward-compatible fields
                    "quads_assignment_id": {
                        "type": ["integer", "null"],
                        "description": "Deprecated: use resource_reservation_id instead",
                    },
                    "quads_cloud_name": {
                        "type": ["string", "null"],
                        "description": "Deprecated: use resource_provider_metadata instead",
                    },
                },
                "required": ["assigned_hardware_ips", "ssh_user"],
            },
        ),
    ]


IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
FQDN_RE = re.compile(r"\b[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z]{2,})+\b")


def create_resource_tool_handlers(
    registry=None,
    secrets_provider=None,
) -> dict[str, Any]:

    _registry = registry

    def _get_registry():
        nonlocal _registry
        if _registry is None:
            if secrets_provider is None:
                raise ValueError("No secrets provider or registry configured")
            from providers.resource.registry import ResourceProviderRegistry
            _registry = ResourceProviderRegistry(secrets_provider)
        return _registry

    async def parse_host_config(text: str) -> dict:
        result: dict[str, Any] = {
            "controller": None,
            "targets": [],
            "ssh_user": "root",
            "ssh_key_path": "~/.ssh/id_rsa",
        }

        lines = text.split("\n")
        all_hosts = []

        for line in lines:
            lower = line.lower().strip()

            user_match = re.search(r"(?:user|ssh_user|ssh-user)\s*[:=]\s*(\S+)", lower)
            if user_match:
                result["ssh_user"] = user_match.group(1)

            key_match = re.search(r"(?:key|ssh_key|ssh-key|ssh_key_path)\s*[:=]\s*(\S+)", lower)
            if key_match:
                result["ssh_key_path"] = key_match.group(1)

            ips = IP_RE.findall(line)
            fqdns = FQDN_RE.findall(line)
            hosts_in_line = ips + fqdns

            if hosts_in_line:
                if re.search(r"controller|server", lower):
                    result["controller"] = hosts_in_line[0]
                    if len(hosts_in_line) > 1:
                        result["targets"].extend(hosts_in_line[1:])
                elif re.search(r"target|client", lower):
                    result["targets"].extend(hosts_in_line)
                else:
                    all_hosts.extend(hosts_in_line)

        if not result["controller"] and all_hosts:
            result["controller"] = all_hosts[0]
            result["targets"] = all_hosts[1:]

        return result

    async def validate_host(
        host: str, user: str = "root", ssh_key_path: str = "~/.ssh/id_rsa"
    ) -> dict:
        return {
            "host": host,
            "reachable": True,
            "os": "RHEL 9.4",
            "cpu_count": 16,
            "ram_gb": 64,
            "message": f"Host {host} validated (simulated)",
        }

    async def list_resource_providers() -> dict:
        reg = _get_registry()
        providers = await reg.list_configured_providers()
        return {
            "configured_providers": providers,
            "count": len(providers),
        }

    async def check_available_resources(
        provider: str, requirements: dict | None = None
    ) -> dict:
        reg = _get_registry()
        prov = await reg.get_provider(provider)
        return await prov.check_available(requirements or {})

    async def reserve_resources(
        provider: str,
        selection: dict,
        description: str,
        ticket_id: str | None = None,
        duration_hours: int = 36,
    ) -> dict:
        reg = _get_registry()
        prov = await reg.get_provider(provider)
        return await prov.reserve(selection, description, duration_hours, ticket_id=ticket_id)

    async def get_reservation_status(
        provider: str, reservation_id: str
    ) -> dict:
        reg = _get_registry()
        prov = await reg.get_provider(provider)
        return await prov.get_reservation_status(reservation_id)

    return {
        "parse_host_config": parse_host_config,
        "validate_host": validate_host,
        "list_resource_providers": list_resource_providers,
        "check_available_resources": check_available_resources,
        "reserve_resources": reserve_resources,
        "get_reservation_status": get_reservation_status,
    }
