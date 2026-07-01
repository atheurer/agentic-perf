from __future__ import annotations

import logging
import re
from typing import Any

from providers.llm.base import ToolDefinition
from providers.ssh import SSHExecutor

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
                "Returns connectivity status, FQDN, and basic system info "
                "(OS, CPU count, RAM). This is for connectivity verification "
                "only — for submit_resource_result, use the IPs from the "
                "reserve_resources result, not the FQDN from this tool."
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
                "returns recommended instance types. For GPU cluster providers "
                "(psap-cc), returns available clusters with GPU type, count, "
                "and cluster details. Use filters to narrow results."
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
                            "instance_type (aws), min_gpus (int, psap-cc), "
                            "gpu_type (str, e.g. 'H100', psap-cc)."
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
                "verifies SSH connectivity. For GPU cluster (psap-cc), this "
                "creates a cluster reservation — returns cluster access info "
                "in provider_metadata (no SSH hosts). Returns a reservation "
                "ID for teardown."
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
                            "For aws: {instance_type: 'm5.xlarge', count: 2} for uniform "
                            "instances, or {instance_specs: [{instance_type: 'm5.4xlarge', "
                            "count: 1, role: 'controller'}, {instance_type: 'm5n.4xlarge', "
                            "count: 2, role: 'client'}]} for per-role instance types. "
                            "For psap-cc: {cluster_id: '<uuid>'}."
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
            description=("Check the status of an existing resource reservation."),
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
            name="get_accumulated_metadata",
            description=(
                "Return accumulated provider metadata from prior "
                "reserve_resources calls. Includes public_ips, private_ips, "
                "and ip_mapping needed for splitting SSH vs benchmark IPs."
            ),
            input_schema={
                "type": "object",
                "properties": {},
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
FQDN_RE = re.compile(
    r"\b[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z]{2,})+\b"
)


def create_resource_tool_handlers(
    registry=None,
    secrets_provider=None,
) -> tuple[dict[str, Any], dict[str, Any], SSHExecutor]:
    _registry = registry
    ssh = SSHExecutor(user="root")

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

            key_match = re.search(
                r"(?:key|ssh_key|ssh-key|ssh_key_path)\s*[:=]\s*(\S+)", lower
            )
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
        effective_key = ssh_key_path if ssh_key_path != "~/.ssh/id_rsa" else None
        result = await ssh.run(
            host,
            "echo SSH_OK",
            timeout=15,
            key_path=effective_key,
        )

        if result.exit_code != 0 or "SSH_OK" not in result.stdout:
            return {
                "host": host,
                "reachable": False,
                "message": f"SSH failed: {result.stderr.strip() or 'no response'}",
            }

        info_cmd = (
            "hostname -f 2>/dev/null || hostname; "
            "cat /etc/redhat-release 2>/dev/null || head -1 /etc/os-release; "
            "nproc; "
            "awk '/MemTotal/{printf \"%.0f\", $2/1024/1024}' /proc/meminfo"
        )
        info = await ssh.run(host, info_cmd, timeout=15, key_path=effective_key)
        lines = info.stdout.strip().splitlines()

        fqdn = lines[0].strip() if len(lines) > 0 else host
        os_info = lines[1].strip() if len(lines) > 1 else "unknown"
        try:
            cpu_count = int(lines[2].strip()) if len(lines) > 2 else 0
        except ValueError:
            cpu_count = 0
        try:
            ram_gb = int(lines[3].strip()) if len(lines) > 3 else 0
        except ValueError:
            ram_gb = 0

        return {
            "host": host,
            "fqdn": fqdn,
            "reachable": True,
            "os": os_info,
            "cpu_count": cpu_count,
            "ram_gb": ram_gb,
            "message": f"Host {host} validated via SSH",
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

    last_reservation: dict[str, Any] = {}

    async def reserve_resources(
        provider: str,
        selection: dict,
        description: str,
        ticket_id: str | None = None,
        duration_hours: int = 36,
    ) -> dict:
        reg = _get_registry()
        prov = await reg.get_provider(provider)
        result = await prov.reserve(
            selection, description, duration_hours, ticket_id=ticket_id
        )

        # Accumulate provider_metadata across multiple reserve calls
        # (e.g., separate calls for controller and endpoints).
        prev_meta = last_reservation.get("provider_metadata", {})
        last_reservation.clear()
        last_reservation.update(result)
        if prev_meta:
            new_meta = result.get("provider_metadata", {})
            for key in ("public_ips", "private_ips"):
                if key in prev_meta:
                    merged = list(prev_meta[key])
                    merged.extend(new_meta.get(key, []))
                    new_meta[key] = merged
            if "ip_mapping" in prev_meta:
                merged_map = dict(prev_meta["ip_mapping"])
                merged_map.update(new_meta.get("ip_mapping", {}))
                new_meta["ip_mapping"] = merged_map
            last_reservation["provider_metadata"] = new_meta

        return result

    async def get_reservation_status(provider: str, reservation_id: str) -> dict:
        reg = _get_registry()
        prov = await reg.get_provider(provider)
        return await prov.get_reservation_status(reservation_id)

    async def get_accumulated_metadata() -> dict:
        result = dict(last_reservation.get("provider_metadata", {}))
        for key in ("ssh_user", "ssh_key_path", "device_ready"):
            if key in last_reservation:
                result[key] = last_reservation[key]
        return result

    handlers = {
        "parse_host_config": parse_host_config,
        "validate_host": validate_host,
        "list_resource_providers": list_resource_providers,
        "check_available_resources": check_available_resources,
        "reserve_resources": reserve_resources,
        "get_reservation_status": get_reservation_status,
        "get_accumulated_metadata": get_accumulated_metadata,
    }
    return handlers, last_reservation, ssh
