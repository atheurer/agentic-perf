from __future__ import annotations

import re
from typing import Any

from providers.llm.base import ToolDefinition


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
            name="request_clarification",
            description=(
                "Ask the user for clarification when host information is missing or invalid. "
                "This pauses the ticket for human input."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The specific question to ask the user",
                    }
                },
                "required": ["question"],
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
                    "notes": {"type": "string"},
                },
                "required": ["assigned_hardware_ips", "ssh_user"],
            },
        ),
    ]


IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
FQDN_RE = re.compile(r"\b[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z]{2,})+\b")


def create_resource_tool_handlers(
    request_clarification_fn,
) -> dict[str, Any]:

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

    async def request_clarification(question: str) -> str:
        await request_clarification_fn(question)
        return "Clarification requested. Ticket paused for human input."

    return {
        "parse_host_config": parse_host_config,
        "validate_host": validate_host,
        "request_clarification": request_clarification,
    }
