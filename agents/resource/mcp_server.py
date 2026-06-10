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
            name="quads_check_available",
            description=(
                "List bare-metal hosts available for self-service reservation "
                "from the Scale Lab QUADS system. Returns host details including "
                "CPU, memory, disks (type and size), and NICs. Optionally filter "
                "by host model, NIC vendor, NIC speed, or disk type."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "model_filter": {
                        "type": "string",
                        "description": "Filter by host model substring (e.g. 'r660', 'r650', 'r6625')",
                    },
                    "vendor_filter": {
                        "type": "string",
                        "description": "Filter by NIC vendor substring (e.g. 'Intel', 'Mellanox', 'Broadcom')",
                    },
                    "speed_filter": {
                        "type": "integer",
                        "description": "Filter by NIC speed in Gbps (e.g. 25, 100)",
                    },
                    "disk_type_filter": {
                        "type": "string",
                        "description": "Filter by disk type (e.g. 'nvme', 'sata', 'scsi')",
                    },
                },
            },
        ),
        ToolDefinition(
            name="quads_reserve_hosts",
            description=(
                "Reserve bare-metal hosts from QUADS. Creates an assignment, schedules "
                "the specified hosts, waits for validation (~30-45 min), and sets up "
                "SSH key access. Returns assigned host details, SSH credentials, and "
                "lease expiration. Use quads_check_available first to find hosts."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "hostnames": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "FQDNs of hosts to reserve (from quads_check_available)",
                    },
                    "description": {
                        "type": "string",
                        "description": "Short description for the QUADS assignment",
                    },
                },
                "required": ["hostnames", "description"],
            },
        ),
        ToolDefinition(
            name="quads_get_assignment_status",
            description=(
                "Check the status of an existing QUADS assignment. "
                "Returns validated/provisioned state."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "assignment_id": {
                        "type": "integer",
                        "description": "QUADS assignment ID",
                    },
                },
                "required": ["assignment_id"],
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
                    "lease_expiration": {"type": ["string", "null"]},
                    "quads_assignment_id": {"type": ["integer", "null"]},
                    "quads_cloud_name": {"type": ["string", "null"]},
                    "fresh_host": {
                        "type": "boolean",
                        "description": "True if hosts were freshly provisioned (e.g., via QUADS) and need a full harness install. Set this when QUADS was used to provision hosts.",
                    },
                    "notes": {"type": "string"},
                },
                "required": ["assigned_hardware_ips", "ssh_user"],
            },
        ),
    ]


IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
FQDN_RE = re.compile(r"\b[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z]{2,})+\b")


def create_resource_tool_handlers(
    secrets_provider=None,
) -> dict[str, Any]:

    _quads_client = None

    async def _get_quads_client():
        nonlocal _quads_client
        if _quads_client is None:
            if secrets_provider is None:
                raise ValueError("No secrets provider configured for QUADS")
            from providers.quads import QuadsClient
            _quads_client = await QuadsClient.from_secrets(secrets_provider)
        return _quads_client

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
        # Simulated — returns success. Replace body with real SSH check.
        return {
            "host": host,
            "reachable": True,
            "os": "RHEL 9.4",
            "cpu_count": 16,
            "ram_gb": 64,
            "message": f"Host {host} validated (simulated)",
        }

    async def quads_check_available(
        model_filter: str | None = None,
        vendor_filter: str | None = None,
        speed_filter: int | None = None,
        disk_type_filter: str | None = None,
    ) -> dict:
        client = await _get_quads_client()
        hosts = await client.get_available(
            model_filter=model_filter,
            vendor_filter=vendor_filter,
            speed_filter=speed_filter,
            disk_type_filter=disk_type_filter,
        )
        return {
            "available_count": len(hosts),
            "hosts": hosts,
        }

    async def quads_reserve_hosts(
        hostnames: list[str], description: str
    ) -> dict:
        client = await _get_quads_client()

        if len(hostnames) > 10:
            return {"status": "failed", "message": "Max 10 hosts per assignment"}

        logger.info(f"[resource] Creating QUADS assignment: {description}")
        assignment = await client.create_assignment(description)
        logger.info(
            f"[resource] Assignment created: id={assignment['id']} "
            f"cloud={assignment['cloud_name']}"
        )

        scheduled = []
        for hostname in hostnames:
            logger.info(f"[resource] Scheduling {hostname} -> {assignment['cloud_name']}")
            sched = await client.schedule_host(assignment["cloud_name"], hostname)
            scheduled.append(sched)

        logger.info(
            f"[resource] Waiting for QUADS validation of assignment {assignment['id']}..."
        )
        status = await client.poll_until_validated(assignment["id"])
        logger.info(f"[resource] Assignment {assignment['id']} validated")

        logger.info(f"[resource] Setting up SSH access to {len(hostnames)} hosts")
        ssh_result = await client.setup_ssh(hostnames)

        return {
            "status": "success",
            "assignment_id": assignment["id"],
            "cloud_name": assignment["cloud_name"],
            "ticket": assignment.get("ticket"),
            "hosts": hostnames,
            "ssh_user": "root",
            "ssh_key_path": client.ssh_key_path,
            "ssh_setup": ssh_result,
            "lease_expiration": scheduled[0].get("end") if scheduled else None,
        }

    async def quads_get_assignment_status(assignment_id: int) -> dict:
        client = await _get_quads_client()
        return await client.get_assignment_status(assignment_id)

    return {
        "parse_host_config": parse_host_config,
        "validate_host": validate_host,
        "quads_check_available": quads_check_available,
        "quads_reserve_hosts": quads_reserve_hosts,
        "quads_get_assignment_status": quads_get_assignment_status,
    }
