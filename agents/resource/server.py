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

# Accumulates validate_host results keyed by host IP/hostname.
_host_inventory: dict[str, dict[str, Any]] = {}


async def _ensure_init():
    """Lazily initialize providers and SSH from env vars on first tool call."""
    global _initialized, _ssh, _ticket, _registry
    if _initialized:
        return
    _ssh, _ticket = await build_ssh_from_ticket()
    secrets = build_secrets_provider()
    from paths import get_instance_name
    from providers.resource.registry import ResourceProviderRegistry

    _registry = ResourceProviderRegistry(secrets, instance_name=get_instance_name())
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
    default_key = "~/.ssh/id_ed25519"
    config_path = Path.home() / ".agentic-perf" / "config.json"
    if config_path.exists():
        with open(config_path) as _f:
            _cfg = json.load(_f)
        default_key = _cfg.get("ssh_key_path", _cfg.get("ssh_key", default_key))

    result: dict[str, Any] = {
        "controller": None,
        "targets": [],
        "ssh_user": "root",
        "ssh_key_path": default_key,
    }

    lines = text.split("\n")
    all_hosts: list[str] = []

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

    return json.dumps(result)


@mcp.tool()
async def list_resource_providers() -> str:
    """List resource providers that are configured and available. Returns provider names and types (bare_metal, cloud). Call this first if no resource_provider directive is set."""
    await _ensure_init()
    providers = await _registry.list_configured_providers()
    return json.dumps(
        {
            "configured_providers": providers,
            "count": len(providers),
        }
    )


@mcp.tool()
async def check_available_resources(
    provider: str,
    requirements: dict | None = None,
    required_hosts: list[dict] | None = None,
) -> str:
    """Check what resources are available from a specific provider. Use required_hosts (preferred) to get per-host recommendations based on the ticket's required_hosts entries with hardware specs, or requirements for a single uniform recommendation."""
    await _ensure_init()
    prov = await _registry.get_provider(provider)
    if required_hosts:
        recommendations = []
        for host_req in required_hosts:
            result = await prov.check_available(host_req)
            rec = dict(host_req)
            if result.get("options"):
                rec["recommended"] = result["options"][0]
            recommendations.append(rec)
        return json.dumps(
            {
                "provider": prov.provider_name,
                "per_host_recommendations": recommendations,
            }
        )
    result = await prov.check_available(requirements or {})
    return json.dumps(result)


def _infer_os_from_ticket() -> str:
    """Extract the OS from the ticket's required_hosts.

    Returns the OS if all non-controller hosts share the same value,
    otherwise returns empty string.
    """
    required_hosts = _ticket.get("custom_fields", {}).get("required_hosts", [])
    os_values = set()
    for h in required_hosts:
        os_val = h.get("os", "")
        if os_val and "controller" not in h.get("roles", []):
            os_values.add(os_val)
    if len(os_values) == 1:
        return os_values.pop()
    return ""


@mcp.tool()
async def reserve_resources(
    provider: str,
    selection: dict,
    description: str,
    ticket_id: str | None = None,
    duration_hours: int = 36,
) -> str:
    """Reserve resources from a provider. For bare-metal (quads), this creates an assignment, schedules hosts, waits for validation (~30-45 min), and sets up SSH access. For cloud (aws), this launches instances, waits until running, and verifies SSH connectivity. Pass {instance_type, count} for uniform instances or {instance_specs: [{instance_type, count, role}, ...]} for per-role instance types. For GPU cluster (psap-cc), this creates a cluster reservation -- returns cluster access info in provider_metadata (no SSH hosts). Returns a reservation ID for teardown."""
    await _ensure_init()
    # Inject OS from ticket required_hosts when the LLM doesn't
    # include it in the selection — ensures AMI resolution fires
    # in the provider regardless of LLM behavior.
    if not selection.get("ami") and not selection.get("os"):
        os_name = _infer_os_from_ticket()
        if os_name:
            selection = dict(selection)
            selection["os"] = os_name
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
async def get_reservation_status(provider: str, reservation_id: str) -> str:
    """Check the status of an existing resource reservation."""
    await _ensure_init()
    prov = await _registry.get_provider(provider)
    result = await prov.get_reservation_status(reservation_id)
    return json.dumps(result)


@mcp.tool()
async def validate_host(
    host: str, ssh_key_path: str = "", ssh_user: str = "root"
) -> str:
    """Validate that a host is reachable via SSH. Returns connectivity status, FQDN, basic system info (OS, CPU count, RAM), and NIC details (interface names and link speeds from ethtool). Pass ssh_key_path from the reserve_resources result to use the correct key."""
    await _ensure_init()
    from providers.ssh import SSHExecutor

    if ssh_key_path:
        ssh = SSHExecutor(user=ssh_user, key_path=ssh_key_path)
    else:
        ssh = _ssh
    result = await ssh.run(host, "echo SSH_OK", timeout=15)

    if result.exit_code != 0 or "SSH_OK" not in result.stdout:
        return json.dumps(
            {
                "host": host,
                "reachable": False,
                "message": f"SSH failed: {result.stderr.strip() or 'no response'}",
            }
        )

    info_cmd = (
        "hostname -f 2>/dev/null || hostname; "
        "cat /etc/redhat-release 2>/dev/null || head -1 /etc/os-release; "
        "nproc; "
        "awk '/MemTotal/{printf \"%.0f\", $2/1024/1024}' /proc/meminfo"
    )
    info = await ssh.run(host, info_cmd, timeout=15)
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

    nic_cmd = (
        "for iface in $(ip -o link show "
        "| awk -F'[ :]+' '/^[0-9]+: (eth|ens|eno|enp)/"
        "{print $2}'); do "
        'speed=$(ethtool "$iface" 2>/dev/null '
        "| awk '/Speed:/{print $2}'); "
        "numa=$(cat /sys/class/net/$iface/device/numa_node "
        "2>/dev/null || echo -1); "
        'echo "${iface}:${speed:-unknown}:${numa}"; '
        "done"
    )
    nic_result = await ssh.run(host, nic_cmd, timeout=15)
    nic_info = []
    if nic_result.exit_code == 0 and nic_result.stdout.strip():
        for nic_line in nic_result.stdout.strip().splitlines():
            parts = nic_line.split(":", 2)
            if len(parts) >= 2:
                entry: dict[str, Any] = {
                    "name": parts[0],
                    "speed": parts[1],
                }
                if len(parts) == 3:
                    try:
                        entry["numa_node"] = int(parts[2])
                    except ValueError:
                        entry["numa_node"] = -1
                nic_info.append(entry)

    numa_cmd = (
        "for node in /sys/devices/system/node/node[0-9]*; do "
        "n=${node##*node}; "
        "cpus=$(cat $node/cpulist); "
        'echo "${n}:${cpus}"; '
        "done"
    )
    numa_result = await ssh.run(host, numa_cmd, timeout=15)
    numa_topology = []
    if numa_result.exit_code == 0 and numa_result.stdout.strip():
        for numa_line in numa_result.stdout.strip().splitlines():
            parts = numa_line.split(":", 1)
            if len(parts) == 2:
                try:
                    numa_topology.append({"node": int(parts[0]), "cpus": parts[1]})
                except ValueError:
                    pass

    inventory = {
        "host": host,
        "fqdn": fqdn,
        "reachable": True,
        "os": os_info,
        "cpu_count": cpu_count,
        "ram_gb": ram_gb,
        "nic_info": nic_info,
        "numa_topology": numa_topology,
        "message": f"Host {host} validated via SSH",
    }
    _host_inventory[host] = inventory
    return json.dumps(inventory)


@mcp.tool()
async def get_host_inventory() -> str:
    """Return accumulated host inventory from prior validate_host calls. Keyed by host IP/hostname, includes OS, CPU, RAM, NIC info with NUMA mapping, and NUMA topology."""
    return json.dumps(_host_inventory)


@mcp.tool()
async def get_accumulated_metadata() -> str:
    """Return accumulated provider metadata from prior reserve_resources calls. Includes public_ips, private_ips, ip_mapping, ssh_user, and ssh_key_path."""
    result = dict(_last_reservation.get("provider_metadata", {}))
    for key in ("ssh_user", "ssh_key_path"):
        if key in _last_reservation:
            result[key] = _last_reservation[key]
    return json.dumps(result)


if __name__ == "__main__":
    mcp.run()
