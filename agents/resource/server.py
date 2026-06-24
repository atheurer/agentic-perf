"""FastMCP server for resource agent tools.

Exposes resource allocation tools (parse hosts, list/check/reserve providers,
validate hosts) over stdio.  The ResourceProviderRegistry and SSHExecutor are
constructed lazily on first tool call from environment variables and ticket
data, so credentials and provider internals never cross the LLM boundary.

Run directly:  python agents/resource/server.py
Connected via: AgentMCPClient (agents/mcp_client.py)
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

_project_root = str(Path(__file__).resolve().parents[2])
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from fastmcp import FastMCP

from agents.server_utils import build_secrets_provider, build_ssh_from_ticket

logger = logging.getLogger(__name__)

mcp = FastMCP("resource-agent")

# Module-level globals -- lazily initialized by _ensure_init()
_initialized = False
_ssh = None
_ticket: dict[str, Any] = {}
_registry = None

# Accumulates provider metadata across multiple reserve_resources calls
# (e.g., separate calls for controller and endpoints).
_last_reservation: dict[str, Any] = {}


async def _ensure_init():
    """Lazily initialize providers and SSH from env vars on first tool call."""
    global _initialized, _ssh, _ticket, _registry
    if _initialized:
        return
    _ssh, _ticket = await build_ssh_from_ticket()
    secrets = build_secrets_provider()
    from providers.resource.registry import ResourceProviderRegistry
    _registry = ResourceProviderRegistry(secrets)
    _initialized = True


# ---------------------------------------------------------------------------
# Regex helpers (from mcp_server.py)
# ---------------------------------------------------------------------------

IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
FQDN_RE = re.compile(
    r"\b[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
    r"(?:\.[a-zA-Z]{2,})+\b"
)


# ---------------------------------------------------------------------------
# MCP Tools (6 tools -- everything except submit_resource_result)
# ---------------------------------------------------------------------------

@mcp.tool()
async def parse_host_config(text: str) -> str:
    """Extract structured host configuration from free-form text. Parses IP addresses, hostnames, roles (controller/target/client/server), SSH user, and SSH key path."""
    result: dict[str, Any] = {
        "controller": None,
        "targets": [],
        "ssh_user": "root",
        "ssh_key_path": "~/.ssh/id_rsa",
    }

    lines = text.split("\n")
    all_hosts: list[str] = []

    for line in lines:
        lower = line.lower().strip()

        user_match = re.search(
            r"(?:user|ssh_user|ssh-user)\s*[:=]\s*(\S+)", lower
        )
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

    return json.dumps(result)


@mcp.tool()
async def list_resource_providers() -> str:
    """List resource providers that are configured and available. Returns provider names and types (bare_metal, cloud). Call this first if no resource_provider directive is set."""
    await _ensure_init()
    providers = await _registry.list_configured_providers()
    return json.dumps({
        "configured_providers": providers,
        "count": len(providers),
    })


@mcp.tool()
async def check_available_resources(
    provider: str, requirements: dict | None = None
) -> str:
    """Check what resources are available from a specific provider. For bare-metal providers (quads), returns available hosts with CPU, memory, disk, and NIC details. For cloud providers (aws), returns recommended instance types. For GPU cluster providers (psap-cc), returns available clusters with GPU type, count, and cluster details. Use filters to narrow results."""
    await _ensure_init()
    prov = await _registry.get_provider(provider)
    result = await prov.check_available(requirements or {})
    return json.dumps(result)


@mcp.tool()
async def reserve_resources(
    provider: str,
    selection: dict,
    description: str,
    ticket_id: str | None = None,
    duration_hours: int = 36,
) -> str:
    """Reserve resources from a provider. For bare-metal (quads), this creates an assignment, schedules hosts, waits for validation (~30-45 min), and sets up SSH access. For cloud (aws), this launches instances, waits until running, and verifies SSH connectivity. For GPU cluster (psap-cc), this creates a cluster reservation -- returns cluster access info in provider_metadata (no SSH hosts). Returns a reservation ID for teardown."""
    await _ensure_init()
    prov = await _registry.get_provider(provider)
    result = await prov.reserve(
        selection, description, duration_hours, ticket_id=ticket_id
    )

    # Accumulate provider_metadata across multiple reserve calls
    # (e.g., separate calls for controller and endpoints).
    prev_meta = _last_reservation.get("provider_metadata", {})
    _last_reservation.clear()
    _last_reservation.update(result)
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
        _last_reservation["provider_metadata"] = new_meta

    return json.dumps(result)


@mcp.tool()
async def get_reservation_status(
    provider: str, reservation_id: str
) -> str:
    """Check the status of an existing resource reservation."""
    await _ensure_init()
    prov = await _registry.get_provider(provider)
    result = await prov.get_reservation_status(reservation_id)
    return json.dumps(result)


@mcp.tool()
async def validate_host(host: str) -> str:
    """Validate that a host is reachable via SSH. Returns connectivity status, FQDN, and basic system info (OS, CPU count, RAM). Uses SSH credentials from the ticket context. This is for connectivity verification only -- for submit_resource_result, use the IPs from the reserve_resources result, not the FQDN from this tool."""
    await _ensure_init()
    result = await _ssh.run(host, "echo SSH_OK", timeout=15)

    if result.exit_code != 0 or "SSH_OK" not in result.stdout:
        return json.dumps({
            "host": host,
            "reachable": False,
            "message": f"SSH failed: {result.stderr.strip() or 'no response'}",
        })

    info_cmd = (
        "hostname -f 2>/dev/null || hostname; "
        "cat /etc/redhat-release 2>/dev/null || head -1 /etc/os-release; "
        "nproc; "
        "awk '/MemTotal/{printf \"%.0f\", $2/1024/1024}' /proc/meminfo"
    )
    info = await _ssh.run(host, info_cmd, timeout=15)
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

    return json.dumps({
        "host": host,
        "fqdn": fqdn,
        "reachable": True,
        "os": os_info,
        "cpu_count": cpu_count,
        "ram_gb": ram_gb,
        "message": f"Host {host} validated via SSH",
    })


if __name__ == "__main__":
    mcp.run()
