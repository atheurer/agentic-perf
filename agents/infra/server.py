"""Shared infrastructure MCP server.

Provides SSH execution, file transfer, and secrets management for all
agents. Credentials stay server-side — the LLM never sees SSH keys,
API tokens, or secret file contents.

Run directly:  python agents/infra/server.py
Connected via: AgentMCPClient (agents/mcp_client.py)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import sys
import tempfile
from contextlib import AsyncExitStack
from pathlib import Path, PurePosixPath
from typing import Any

_project_root = str(Path(__file__).resolve().parents[2])
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from fastmcp import FastMCP

from agents.infra.topology import discover_cache_topology
from agents.server_utils import (
    _resolve_vault_secret_name,
    resolve_ssh_key,
)
from agents.server_utils import (
    build_secrets_provider as _build_secrets,
)
from providers.ssh import _PID_SENTINEL, SSHExecutor, SSHResult, parse_pid_sentinel

logger = logging.getLogger(__name__)

mcp = FastMCP("infra")

CONTROLLER_KEY_COMMENT = "agentic-perf-controller-key"

_ssh: SSHExecutor | None = None
_ssh_key_stack: AsyncExitStack | None = None
_secrets_provider = None
_state_store_url: str | None = None
_ticket_id: str | None = None


def _store_headers() -> dict[str, str]:
    token = os.environ.get("AGENTIC_PERF_API_TOKEN", "")
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


def _get_ssh() -> SSHExecutor:
    if _ssh is None:
        raise RuntimeError("SSH context not set. Call set_ssh_context() first.")
    return _ssh


def _get_secrets():
    global _secrets_provider
    if _secrets_provider is None:
        _secrets_provider = _build_secrets()
    return _secrets_provider


def _format_result(result: SSHResult) -> str:
    out: dict[str, Any] = {
        "exit_code": result.exit_code,
        "stdout": result.stdout,
    }
    if result.stderr:
        out["stderr"] = result.stderr
    return json.dumps(out, indent=2)


@mcp.tool()
async def set_ssh_context(ticket_id: str) -> str:
    """Set SSH credentials by reading them from a ticket's custom_fields.

    Must be called before any SSH operations. Resolves ssh_key_path and
    ssh_user from the ticket so credentials never appear in tool inputs.
    """
    global _ssh, _ssh_key_stack, _state_store_url, _ticket_id

    _ticket_id = ticket_id

    _state_store_url = os.environ.get("STATE_STORE_URL", "http://localhost:8090")
    import httpx

    async with httpx.AsyncClient(timeout=15.0, headers=_store_headers()) as client:
        r = await client.get(f"{_state_store_url}/api/v1/tickets/{ticket_id}")
        r.raise_for_status()
        ticket = r.json()

    fields = ticket.get("custom_fields", {})
    ssh_key = fields.get("ssh_key_path")
    ssh_user = fields.get("ssh_user", "root")

    # Jumpstarter boards get reflashed — host keys
    # change every time. Disable strict checking.
    strict = "no" if fields.get("resource_provider") == "jumpstarter" else "accept-new"

    vault_secret_name = _resolve_vault_secret_name(fields)
    resolved_key = ssh_key
    if vault_secret_name:
        if _ssh_key_stack is not None:
            await _ssh_key_stack.aclose()
        _ssh_key_stack = AsyncExitStack()
        sp = _get_secrets()
        resolved_key = await _ssh_key_stack.enter_async_context(
            resolve_ssh_key(ssh_key, sp, vault_secret_name),
        )

    _ssh = SSHExecutor(user=ssh_user, key_path=resolved_key, strict_host_key=strict)

    return json.dumps(
        {
            "status": "ok",
            "ssh_user": ssh_user,
            "has_key": ssh_key is not None,
        }
    )


@mcp.tool()
async def check_host(host: str) -> str:
    """Test SSH connectivity and gather system info (OS, CPU, RAM, hostname)."""
    ssh = _get_ssh()

    result = await ssh.run(host, "echo SSH_OK", timeout=15)
    if result.exit_code != 0:
        return json.dumps(
            {
                "reachable": False,
                "error": result.stderr or result.stdout,
            }
        )

    info_cmd = (
        "hostname -f 2>/dev/null || hostname; "
        "cat /etc/os-release 2>/dev/null | grep -E '^(NAME|VERSION)=' | head -2; "
        "nproc; "
        "grep MemTotal /proc/meminfo 2>/dev/null | awk '{printf \"%.0f\\n\", $2/1024/1024}'"
    )
    info = await ssh.run(host, info_cmd, timeout=15)
    return json.dumps(
        {
            "reachable": True,
            "host": host,
            "system_info": info.stdout.strip(),
            "exit_code": info.exit_code,
        },
        indent=2,
    )


@mcp.tool()
async def write_remote_file(host: str, remote_path: str, content: str) -> str:
    """Write content to a file on a remote host. Creates parent directories."""
    ssh = _get_ssh()

    parent_dir = shlex.quote(str(PurePosixPath(remote_path).parent))
    mkdir_result = await ssh.run(host, f"mkdir -p {parent_dir}", timeout=15)
    if mkdir_result.exit_code != 0:
        return json.dumps(
            {
                "success": False,
                "error": f"mkdir failed: {mkdir_result.stderr}",
            }
        )

    with tempfile.NamedTemporaryFile(mode="w", suffix=".tmp", delete=False) as f:
        f.write(content)
        local_path = f.name

    try:
        scp_result = await ssh.copy_to(host, local_path, remote_path)
    finally:
        Path(local_path).unlink(missing_ok=True)

    return json.dumps(
        {
            "success": scp_result.exit_code == 0,
            "remote_path": remote_path,
            "bytes_written": len(content),
            "error": scp_result.stderr if scp_result.exit_code != 0 else None,
        }
    )


@mcp.tool()
async def read_remote_file(host: str, remote_path: str, max_bytes: int = 10000) -> str:
    """Read a file from a remote host. Truncates to max_bytes."""
    ssh = _get_ssh()
    result = await ssh.run(
        host, f"head -c {max_bytes} {shlex.quote(remote_path)}", timeout=30
    )
    return _format_result(result)


@mcp.tool()
async def read_remote_dir(host: str, remote_path: str, max_mb: int = 100) -> str:
    """Copy a remote directory to a local temp directory.

    Returns the local path for the agent to read files from directly.
    Use for multi-file data like crucible tool-data directories.
    """
    ssh = _get_ssh()
    local_dir = tempfile.mkdtemp(prefix="remote-dir-")
    result = await ssh.copy_from(host, remote_path, local_dir)
    if result.exit_code != 0:
        return json.dumps(
            {
                "success": False,
                "error": result.stderr or result.stdout,
            }
        )
    return json.dumps({"success": True, "local_path": local_dir})


def _parse_ethtool_output(stdout: str, mode: str) -> dict[str, Any]:
    """Parse ethtool output (either native JSON or text format) into structured dict."""
    try:
        parsed = json.loads(stdout)
        if isinstance(parsed, list) and len(parsed) == 1 and isinstance(parsed[0], dict):
            return parsed[0]
        return parsed
    except Exception:
        pass

    data: dict[str, Any] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("NIC statistics:") or line.startswith("Features for"):
            continue
        if ":" in line:
            key, val = line.split(":", 1)
            key = key.strip()
            val = val.strip()
            if mode == "stats":
                try:
                    if "." in val:
                        data[key] = float(val)
                    else:
                        data[key] = int(val)
                except ValueError:
                    data[key] = val
            elif mode == "features":
                is_on = val.startswith("on")
                is_fixed = "[fixed]" in val
                data[key] = {
                    "active": is_on,
                    "fixed": is_fixed,
                    "raw": val,
                }
            else:
                data[key] = val
    return data


@mcp.tool()
async def get_ethtool_info(host: str, iface: str, mode: str = "features") -> str:
    """Get ethtool information for a network interface as structured JSON.

    mode='features' returns offload feature flags (ethtool -k / ethtool --json -k),
    mode='stats' returns NIC statistics (ethtool -S / ethtool --json -S).
    """
    ssh = _get_ssh()
    quoted = shlex.quote(iface)
    if mode == "features":
        flag = "-k"
    elif mode == "stats":
        flag = "-S"
    else:
        return json.dumps(
            {
                "success": False,
                "error": f"Unknown mode {mode!r}. Use 'features' or 'stats'.",
            }
        )

    # Try native JSON output first
    result = await ssh.run(host, f"ethtool --json {flag} {quoted}", timeout=30)
    if result.exit_code != 0 or not result.stdout.strip().startswith(("{", "[")):
        # Fallback to standard ethtool
        result = await ssh.run(host, f"ethtool {flag} {quoted}", timeout=30)

    if result.exit_code != 0:
        return _format_result(result)

    parsed_data = _parse_ethtool_output(result.stdout, mode)
    return json.dumps(
        {
            "host": host,
            "iface": iface,
            "mode": mode,
            "exit_code": result.exit_code,
            "data": parsed_data,
            "stdout": result.stdout,
        },
        indent=2,
    )


@mcp.tool()
async def get_sysctl_values(host: str, params: list[str]) -> str:
    """Read sysctl parameter values from a remote host as structured JSON.

    Pass a list of sysctl key names
    (e.g. ['net.core.rmem_max', 'net.ipv4.tcp_wmem']).
    """
    ssh = _get_ssh()
    _PARAM_RE = re.compile(r"^[a-zA-Z0-9._/-]+$")
    for p in params:
        if not _PARAM_RE.match(p):
            return json.dumps(
                {
                    "success": False,
                    "error": f"Invalid sysctl key {p!r}: must match [a-zA-Z0-9._/-]+",
                }
            )
    cmd = "sysctl " + " ".join(shlex.quote(p) for p in params)
    result = await ssh.run(host, cmd, timeout=30)
    if result.exit_code != 0 and not result.stdout.strip():
        return _format_result(result)

    values = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip()

    return json.dumps(
        {
            "host": host,
            "exit_code": result.exit_code,
            "values": values,
            "stdout": result.stdout,
        },
        indent=2,
    )


@mcp.tool()
async def query_numa_topology(host: str, iface: str) -> str:
    """Query NUMA topology for a host.

    Returns which NUMA node a NIC belongs to, and the CPU list for each
    NUMA node.
    """
    ssh = _get_ssh()
    quoted = shlex.quote(iface)

    node_result = await ssh.run(
        host,
        f"cat /sys/class/net/{quoted}/device/numa_node",
        timeout=15,
    )
    if node_result.exit_code != 0:
        return json.dumps(
            {
                "error": (
                    f"Failed to read NIC NUMA node: "
                    f"{node_result.stderr or node_result.stdout}"
                ),
            }
        )

    cpulist_result = await ssh.run(
        host,
        'for f in /sys/devices/system/node/node*/cpulist; do echo "$f: $(cat $f)"; done',
        timeout=15,
    )
    if cpulist_result.exit_code != 0:
        return json.dumps(
            {
                "error": (
                    f"Failed to read per-node CPU lists: "
                    f"{cpulist_result.stderr or cpulist_result.stdout}"
                ),
            }
        )

    node_cpu_lists = {}
    for line in cpulist_result.stdout.splitlines():
        m = re.search(r"node(\d+)/cpulist:\s*(.*)", line)
        if m:
            nid = m.group(1)
            cpus = m.group(2).strip()
            node_cpu_lists[nid] = cpus
            node_cpu_lists[f"node{nid}"] = cpus

    nic_numa_node = node_result.stdout.strip()
    try:
        nic_numa_node_int = int(nic_numa_node)
    except ValueError:
        nic_numa_node_int = nic_numa_node

    return json.dumps(
        {
            "host": host,
            "iface": iface,
            "nic_numa_node": nic_numa_node,
            "nic_numa_node_int": nic_numa_node_int,
            "node_cpu_lists": node_cpu_lists,
            "node_cpu_lists_raw": cpulist_result.stdout.strip(),
        },
        indent=2,
    )


@mcp.tool()
async def get_cache_topology(
    host: str,
    socket: int | None = None,
) -> str:
    """Discover CPU cache / CCD (Core Complex Die) topology for a host.

    Discovers L3 cache domains (or LLC) and which CPUs belong to each CCD/cache slice.
    Supports filtering by socket.

    Returns structured CCD map and cache domain details:
      {
        "host": host,
        "socket": socket,
        "ccds": {"0": [192, 193, ...], "1": [208, 209, ...]},
        "domains": [
          {
            "ccd_id": 0,
            "cache_id": 0,
            "socket_id": 1,
            "numa_node": 1,
            "cpus": [192, 193, ...],
            "cpu_list": "192-207,576-591",
            "core_count": 16,
            "thread_count": 32,
            "size": "32M",
            ...
          }
        ],
        "total_ccds": 24,
        "total_cpus": 768,
        "source": "sysfs"
      }
    """
    ssh = _get_ssh()
    result = await discover_cache_topology(ssh, host, socket=socket)
    return json.dumps(result, indent=2)


@mcp.tool()
async def verify_ssh_path(host: str, target_host: str) -> str:
    """Verify SSH reachability from one host to another.

    For example, use this to validate that crucible's controller can reach
    its endpoints before starting a benchmark.
    """
    ssh = _get_ssh()
    cmd = (
        f"ssh -o ConnectTimeout=5 -o BatchMode=yes "
        f"-o StrictHostKeyChecking=accept-new "
        f"{shlex.quote(target_host)} hostname"
    )
    result = await ssh.run(host, cmd, timeout=20)
    reachable = result.exit_code == 0
    out: dict[str, Any] = {
        "reachable": reachable,
        "from": host,
        "to": target_host,
    }
    if reachable:
        out["hostname"] = result.stdout.strip()
    return json.dumps(out)


@mcp.tool()
async def list_interfaces(host: str) -> str:
    """List network interfaces that are UP with their assigned IP addresses."""
    ssh = _get_ssh()
    result = await ssh.run(host, "ip -br addr show", timeout=15)
    if result.exit_code != 0:
        return _format_result(result)

    interfaces = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        iface = parts[0]
        state = parts[1]
        if state != "UP":
            continue
        addresses = parts[2:] if len(parts) > 2 else []
        interfaces.append({"iface": iface, "state": state, "addresses": addresses})

    return json.dumps(interfaces)


_IFACE_SCRIPT = (
    "import json,os,subprocess\n"
    "net='/sys/class/net'\n"
    "result=[]\n"
    "for iface in sorted(os.listdir(net)):\n"
    " if iface=='lo':continue\n"
    " d={'name':iface}\n"
    " def rd(p,dfl=None,iface=iface):\n"
    "  try:return open(f'{net}/{iface}/{p}').read().strip()\n"
    "  except:return dfl\n"
    " d['link']=rd('operstate','unknown')\n"
    " try:d['speed_mbps']=int(rd('speed','-1'))\n"
    " except:d['speed_mbps']=-1\n"
    " try:d['mtu']=int(rd('mtu','-1'))\n"
    " except:d['mtu']=-1\n"
    " try:d['numa_node']=int(rd('device/numa_node','-1'))\n"
    " except:d['numa_node']=-1\n"
    " d['mac']=rd('address','')\n"
    " result.append(d)\n"
    "proc=subprocess.run(['ip','-j','addr','show'],capture_output=True,text=True)\n"
    "addrs={a['ifname']:a for a in json.loads(proc.stdout or '[]')}\n"
    "for d in result:\n"
    " ai=addrs.get(d['name'],{}).get('addr_info',[])\n"
    " d['ipv4']=[x['local']+'/'+str(x['prefixlen']) for x in ai if x.get('family')=='inet']\n"
    " d['ipv6']=[x['local']+'/'+str(x['prefixlen']) for x in ai"
    " if x.get('family')=='inet6' and not x.get('local','').startswith('fe80')]\n"
    " d['ipv6_link_local']=[x['local']+'/'+str(x['prefixlen']) for x in ai"
    " if x.get('family')=='inet6' and x.get('local','').startswith('fe80')]\n"
    "print(json.dumps(result))"
)


def _apply_iface_filters(
    interfaces: list[dict[str, Any]],
    name_regex: str,
    link: str,
    min_speed_gbps: float,
    max_speed_gbps: float,
    min_mtu: int,
    max_mtu: int,
    ipv4: str,
    ipv6: str,
    numa_node: int,
) -> list[dict[str, Any]]:
    out = []
    for iface in interfaces:
        if name_regex and not re.search(name_regex, iface["name"]):
            continue
        if link and iface["link"].lower() != link.lower():
            continue
        spd = iface["speed_mbps"]
        if min_speed_gbps > 0:
            if spd < 0 or spd < min_speed_gbps * 1000:
                continue
        if max_speed_gbps > 0:
            if spd < 0 or spd > max_speed_gbps * 1000:
                continue
        if min_mtu > 0 and iface["mtu"] < min_mtu:
            continue
        if max_mtu > 0 and iface["mtu"] > max_mtu:
            continue
        if ipv4:
            addrs = iface.get("ipv4", [])
            if ipv4 == "present" and not addrs:
                continue
            if ipv4 == "absent" and addrs:
                continue
            if ipv4 not in ("present", "absent"):
                if not any(re.search(ipv4, a) for a in addrs):
                    continue
        if ipv6:
            addrs = iface.get("ipv6", [])
            if ipv6 == "present" and not addrs:
                continue
            if ipv6 == "absent" and addrs:
                continue
            if ipv6 not in ("present", "absent"):
                if not any(re.search(ipv6, a) for a in addrs):
                    continue
        # -2 sentinel means no filter; -1 is a valid numa_node value
        if numa_node != -2 and iface["numa_node"] != numa_node:
            continue
        iface["speed_gbps"] = round(spd / 1000, 3) if spd >= 0 else -1
        out.append(iface)
    return out


@mcp.tool()
async def get_interface_inventory(
    host: str,
    name_regex: str = "",
    link: str = "",
    min_speed_gbps: float = 0,
    max_speed_gbps: float = 0,
    min_mtu: int = 0,
    max_mtu: int = 0,
    ipv4: str = "",
    ipv6: str = "",
    numa_node: int = -2,
) -> str:
    """Get a comprehensive inventory of network interfaces on a remote host.

    Returns per-interface: name, link state, speed (Mbps and Gbps), MTU,
    NUMA node, MAC, IPv4 addresses, IPv6 addresses (global and link-local).

    All data is collected in a single SSH call. Optional server-side filters
    let the caller narrow results without multiple round-trips:

      name_regex        — regex matched against interface name
      link              — "up" or "down"
      min/max_speed_gbps — speed range in Gbps (0 = no limit)
      min/max_mtu       — MTU range (0 = no limit)
      ipv4              — "present", "absent", or regex against address strings
      ipv6              — same for non-link-local IPv6
      numa_node         — exact NUMA node match (-2 = no filter)

    Example — find all UP 400G interfaces on NUMA node 1:
      get_interface_inventory(host, link="up", min_speed_gbps=400,
                              max_speed_gbps=400, numa_node=1)
    """
    ssh = _get_ssh()
    cmd = f"python3 -c {shlex.quote(_IFACE_SCRIPT)}"
    result = await ssh.run(host, cmd, timeout=20)
    if result.exit_code != 0:
        return json.dumps({"error": result.stderr or result.stdout, "host": host})
    try:
        interfaces = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return json.dumps({"error": f"parse error: {exc}", "host": host})

    filters_applied: dict[str, Any] = {}
    if name_regex:
        filters_applied["name_regex"] = name_regex
    if link:
        filters_applied["link"] = link
    if min_speed_gbps:
        filters_applied["min_speed_gbps"] = min_speed_gbps
    if max_speed_gbps:
        filters_applied["max_speed_gbps"] = max_speed_gbps
    if min_mtu:
        filters_applied["min_mtu"] = min_mtu
    if max_mtu:
        filters_applied["max_mtu"] = max_mtu
    if ipv4:
        filters_applied["ipv4"] = ipv4
    if ipv6:
        filters_applied["ipv6"] = ipv6
    if numa_node != -2:
        filters_applied["numa_node"] = numa_node

    filtered = _apply_iface_filters(
        interfaces,
        name_regex,
        link,
        min_speed_gbps,
        max_speed_gbps,
        min_mtu,
        max_mtu,
        ipv4,
        ipv6,
        numa_node,
    )
    # Ensure speed_gbps on unfiltered items too
    for iface in filtered:
        if "speed_gbps" not in iface:
            spd = iface["speed_mbps"]
            iface["speed_gbps"] = round(spd / 1000, 3) if spd >= 0 else -1

    return json.dumps(
        {
            "host": host,
            "interfaces": filtered,
            "count": len(filtered),
            "filters_applied": filters_applied,
        }
    )


@mcp.tool()
async def deploy_secret(host: str, secret_path: str, remote_path: str) -> str:
    """Deploy a secret file to a remote host.

    Resolves the secret locally via the secrets provider, then SCPs it.
    The LLM never sees the secret content — only the logical path.
    """
    ssh = _get_ssh()
    sp = _get_secrets()

    async with sp.secret_file(secret_path) as local_path:
        if local_path is None:
            return json.dumps(
                {
                    "success": False,
                    "error": f"Secret not found: {secret_path}",
                }
            )

        parent_dir = shlex.quote(str(PurePosixPath(remote_path).parent))
        mkdir_result = await ssh.run(host, f"mkdir -p {parent_dir}", timeout=15)
        if mkdir_result.exit_code != 0:
            return json.dumps(
                {
                    "success": False,
                    "error": f"mkdir failed: {mkdir_result.stderr}",
                }
            )

        result = await ssh.copy_to(host, str(local_path), remote_path)
        return json.dumps(
            {
                "success": result.exit_code == 0,
                "secret_path": secret_path,
                "remote_path": remote_path,
                "error": result.stderr if result.exit_code != 0 else None,
            }
        )


@mcp.tool()
async def transfer_file(
    host: str,
    local_path: str,
    remote_path: str,
    direction: str = "push",
) -> str:
    """Transfer a file to or from a remote host via SCP.

    direction: "push" (local→remote) or "pull" (remote→local).
    """
    ssh = _get_ssh()

    if direction == "push":
        result = await ssh.copy_to(host, local_path, remote_path)
    elif direction == "pull":
        args = [
            "scp",
            "-r",
            "-o",
            f"ConnectTimeout={ssh.connect_timeout}",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
        ]
        if ssh.key_path:
            args.extend(["-i", ssh.key_path])
        args.extend([f"{ssh.user}@{host}:{remote_path}", local_path])

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        result = SSHResult(
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            exit_code=proc.returncode or 0,
        )
    else:
        return json.dumps(
            {"success": False, "error": f"Unknown direction: {direction}"}
        )

    return json.dumps(
        {
            "success": result.exit_code == 0,
            "direction": direction,
            "local_path": local_path,
            "remote_path": remote_path,
            "error": result.stderr if result.exit_code != 0 else None,
        }
    )


@mcp.tool()
async def check_hosts(hosts: list[str]) -> str:
    """Test SSH connectivity and gather system info for multiple hosts at once.

    Accepts a list of IPs or hostnames and checks them all concurrently.
    Use this instead of calling check_host repeatedly — it saves iterations.
    """
    ssh = _get_ssh()

    async def _check_one(host: str) -> dict[str, Any]:
        result = await ssh.run(host, "echo SSH_OK", timeout=15)
        if result.exit_code != 0:
            return {
                "host": host,
                "reachable": False,
                "error": result.stderr or result.stdout or "SSH failed",
            }
        info_cmd = (
            "hostname -f 2>/dev/null || hostname; "
            "cat /etc/os-release 2>/dev/null | grep -E '^(NAME|VERSION)=' | head -2; "
            "nproc; "
            "grep MemTotal /proc/meminfo 2>/dev/null "
            "| awk '{printf \"%.0f\\n\", $2/1024/1024}'"
        )
        info = await ssh.run(host, info_cmd, timeout=15)
        return {
            "host": host,
            "reachable": True,
            "system_info": info.stdout.strip(),
        }

    results = await asyncio.gather(
        *[_check_one(h) for h in hosts], return_exceptions=True
    )
    per_host = {}
    reachable = []
    unreachable = []
    for r in results:
        if isinstance(r, Exception):
            continue
        host = r["host"]
        per_host[host] = r
        if r["reachable"]:
            reachable.append(host)
        else:
            unreachable.append(host)

    return json.dumps(
        {
            "results": per_host,
            "reachable": reachable,
            "unreachable": unreachable,
        }
    )


@mcp.tool()
async def test_port_connectivity(
    server_ssh_host: str,
    client_ssh_host: str,
    server_test_ip: str,
    port: int,
    client_test_ip: str = "",
    timeout: int = 10,
) -> str:
    """Test TCP port connectivity between two hosts.

    This is harness-agnostic — it works for any benchmark that needs to
    verify that a client can reach a server on a specific TCP port.

    The SSH hosts are how we reach the machines (may be public IPs). The
    test IPs are what we actually test connectivity on (may be private IPs
    that the hosts use to talk to each other).

    Args:
        server_ssh_host: IP to SSH into the server machine
        client_ssh_host: IP to SSH into the client machine
        server_test_ip: IP the server listens on (the IP being tested)
        port: TCP port to test
        client_test_ip: Optional — if provided, also tests reverse
            connectivity (server connecting to client on the same port)
        timeout: Seconds to wait for the connection test
    """
    ssh = _get_ssh()
    results = []

    async def _test_direction(
        listener_ssh: str,
        connector_ssh: str,
        listen_ip: str,
        label: str,
    ) -> dict[str, Any]:
        bg_cmd = (
            f"nohup nc -l {listen_ip} {port} > /dev/null 2>&1 & echo {_PID_SENTINEL}$!"
        )
        start = await ssh.run(listener_ssh, bg_cmd, timeout=10)
        pid = parse_pid_sentinel(start.stdout or "")

        if pid is None:
            return {
                "direction": label,
                "port": port,
                "reachable": False,
                "error": "Failed to start nc listener",
            }
        try:
            test_cmd = f"nc -z -w {timeout} {listen_ip} {port}"
            test = await ssh.run(connector_ssh, test_cmd, timeout=timeout + 5)
            return {
                "direction": label,
                "port": port,
                "reachable": test.exit_code == 0,
                "error": test.stderr.strip() if test.exit_code != 0 else "",
            }
        finally:
            await ssh.run(listener_ssh, f"kill {pid} 2>/dev/null", timeout=5)

    forward = await _test_direction(
        server_ssh_host,
        client_ssh_host,
        server_test_ip,
        f"client({client_ssh_host}) -> server({server_test_ip}:{port})",
    )
    results.append(forward)

    if client_test_ip:
        reverse = await _test_direction(
            client_ssh_host,
            server_ssh_host,
            client_test_ip,
            f"server({server_ssh_host}) -> client({client_test_ip}:{port})",
        )
        results.append(reverse)

    all_reachable = all(r["reachable"] for r in results)
    return json.dumps(
        {
            "all_reachable": all_reachable,
            "tests": results,
        }
    )


async def cleanup_passwordless_ssh(
    ssh: SSHExecutor,
    controller: str,
    endpoints: list[str],
) -> dict:
    """Remove agentic-perf SSH keys from endpoints and the controller."""
    logger.info(f"[infra] Cleaning up SSH keys: {controller} -> {endpoints}")
    results = {}

    for endpoint in endpoints:
        result = await ssh.run(
            endpoint,
            f"sed -i '/{CONTROLLER_KEY_COMMENT}/d' /root/.ssh/authorized_keys",
        )
        results[endpoint] = (
            "cleaned" if result.exit_code == 0 else f"failed: {result.stderr}"
        )

    check = await ssh.run(
        controller,
        f"grep -q '{CONTROLLER_KEY_COMMENT}' /root/.ssh/id_rsa.pub 2>/dev/null && "
        f"rm -f /root/.ssh/id_rsa /root/.ssh/id_rsa.pub && echo REMOVED || echo SKIPPED",
    )
    controller_key = check.stdout.strip()
    results[f"{controller} (key pair)"] = (
        "removed" if controller_key == "REMOVED" else "skipped (not ours)"
    )

    return {
        "status": "success",
        "results": results,
    }


if __name__ == "__main__":
    mcp.run()
