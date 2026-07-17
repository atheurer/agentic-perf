"""Jumpstarter MCP attachment for agents.

Conditionally connects the Jumpstarter MCP server to an agent's
MCP client when the ticket uses Jumpstarter-provisioned hardware.
Tool filtering ensures agents only see relevant device tools.

Usage in an agent's run() method:
    from agents.jumpstarter_mcp import attach_jumpstarter_mcp

    mcp = AgentMCPClient()
    await mcp.connect(...)  # agent's own servers
    await attach_jumpstarter_mcp(mcp, ticket_id, store_url)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import httpx

from agents.mcp_client import AgentMCPClient

logger = logging.getLogger(__name__)

# Tools that agents should see for device interaction.
# Lease management tools are excluded — the resource
# provider handles that, not agents.
# jmp_disconnect is intentionally excluded — the MCP
# connection must stay alive through provisioning
# AND benchmarking. Disconnecting kills the socket
# that boot-timings-test.sh needs for serial capture.
AGENT_DEVICE_TOOLS = frozenset(
    {
        "jmp_run",
        "jmp_explore",
        "jmp_connect",
        "jmp_drivers",
        "jmp_driver_methods",
        "jmp_get_env",
        "jmp_list_connections",
    }
)

# Tools excluded from agents — resource provider's job
_PROVIDER_ONLY_TOOLS = frozenset(
    {
        "jmp_create_lease",
        "jmp_delete_lease",
        "jmp_list_leases",
        "jmp_list_exporters",
    }
)


_JMP_CONNECT_TIMEOUT = 180  # 3 minutes


class _JmpCallHook:
    """Pre-call hook for Jumpstarter-specific tool behavior.

    Installed on the MCP client during attach. Handles:
    - One-connect guard: prevents duplicate jmp_connect calls
    - Connect timeout: fails fast when no exporters available
    """

    def __init__(self, mcp_client: AgentMCPClient) -> None:
        self._mcp = mcp_client
        self._connected = False

    async def pre_call(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> str | None:
        """Return a string to short-circuit; None to proceed."""
        if name != "jmp_connect":
            return None

        if self._connected:
            return json.dumps(
                {
                    "error": (
                        "Already connected to a "
                        "Jumpstarter device in this "
                        "session. You are provisioning "
                        "ONE board. Submit your result."
                    ),
                }
            )

        # Execute jmp_connect with a timeout and track
        # connection state. Returns the result directly
        # so AgentMCPClient.call_tool skips its normal
        # session.call_tool path.
        server_name = self._mcp._tool_routing.get(name)
        if server_name is None:
            return None
        conn = self._mcp._servers[server_name]

        try:
            result = await asyncio.wait_for(
                conn.session.call_tool(name, arguments),
                timeout=_JMP_CONNECT_TIMEOUT,
            )
            # Extract content.
            parts = []
            for block in result.content:
                if hasattr(block, "text"):
                    parts.append(block.text)
                else:
                    parts.append(str(block))
            content = "\n".join(parts) if parts else ""

            if result.isError:
                return content  # Error but don't set connected

            self._connected = True
            return trim_response(name, content)
        except asyncio.TimeoutError:
            return json.dumps(
                {
                    "error": (
                        f"Failed to connect: lease "
                        f"acquisition timed out after "
                        f"{_JMP_CONNECT_TIMEOUT} "
                        f"seconds. No exporter was "
                        f"assigned — the board may be "
                        f"offline or leased by another "
                        f"user."
                    ),
                }
            )


async def attach_jumpstarter_mcp(
    mcp_client: AgentMCPClient,
    ticket_id: str,
    store_url: str,
) -> bool | None:
    """Conditionally attach Jumpstarter MCP to an agent.

    Checks if the ticket uses Jumpstarter-provisioned hardware.
    If so, connects the `jmp mcp serve` server and returns True.
    If not (or on error), returns False silently.

    The tool routing ensures agents can call Jumpstarter device
    tools via the standard call_tool() path. Tool filtering
    (via list_tools(include=...)) should be used by agents to
    control which tools the LLM sees.

    Args:
        mcp_client: The agent's MCP client to attach to.
        ticket_id: Current ticket ID.
        store_url: State store URL for ticket lookup.

    Returns:
        Set of allowed tool names if attached (for use with
        list_tools(include=...)), or None if not attached.
    """
    try:
        token = os.environ.get("AGENTIC_PERF_API_TOKEN", "")
        _headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with httpx.AsyncClient(timeout=10.0, headers=_headers) as client:
            r = await client.get(f"{store_url}/api/v1/tickets/{ticket_id}")
            if r.status_code != 200:
                return False
            cf = r.json().get("custom_fields", {})

        if cf.get("resource_provider") != "jumpstarter":
            return False

        # Timeout protects against hanging when all
        # Jumpstarter exporters are leased — jmp mcp serve
        # blocks waiting for exporter assignment with no
        # built-in timeout.
        try:
            await asyncio.wait_for(
                mcp_client.connect_command(
                    command="jmp",
                    args=["mcp", "serve"],
                    name="jumpstarter",
                ),
                timeout=120,  # 2 min to connect
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"[jumpstarter-mcp] Connection timed "
                f"out for {ticket_id} — all exporters "
                f"may be leased"
            )
            return False
        logger.info(f"[jumpstarter-mcp] Attached to agent for ticket {ticket_id}")

        # Install Jumpstarter-specific call_tool hooks.
        _hook = _JmpCallHook(mcp_client)
        mcp_client.pre_call_hook = _hook.pre_call
        mcp_client.post_call_hook = trim_response

        return True

    except Exception:
        logger.debug(
            "[jumpstarter-mcp] Not attached (not configured or not available)",
            exc_info=True,
        )
        return None


async def collect_diagnostics(
    mcp_client: AgentMCPClient,
    ticket_id: str = "",
    get_ticket: Any = None,
) -> str:
    """Collect diagnostics via the Jumpstarter tunnel.

    Captures serial output, power state, and tunnel SSH
    when a benchmark fails. The tunnel may still work even
    when direct SSH doesn't — these are often the only
    data available to diagnose device failures.

    Args:
        mcp_client: Agent's MCP client (with Jumpstarter attached).
        ticket_id: For lease lookup if no active connection.
        get_ticket: Async callable to fetch ticket data.
    """
    diag: list[str] = []

    # Find or establish a connection.
    try:
        conns_raw = await mcp_client.call_tool("jmp_list_connections", {})
        conns = json.loads(conns_raw)
        conn_list = conns if isinstance(conns, list) else conns.get("connections", [])
        if not conn_list and ticket_id and get_ticket:
            ticket = await get_ticket(ticket_id)
            cf = ticket.get("custom_fields", {})
            lid = cf.get("resource_reservation_id") or cf.get(
                "resource_provider_metadata", {}
            ).get("lease_id", "")
            if not lid:
                return "No lease ID available"
            conn_raw = await mcp_client.call_tool("jmp_connect", {"lease_id": lid})
            conn_data = json.loads(conn_raw)
            if conn_data.get("error"):
                return f"Connect failed: {conn_data['error'][:200]}"
            conn_list = [conn_data]
        if not conn_list:
            return "No active Jumpstarter connection"
        conn_id = conn_list[0].get(
            "connection_id",
            conn_list[0].get("id", ""),
        )
    except Exception as exc:
        return f"Could not list connections: {exc}"

    # Serial capture — most critical diagnostic.
    try:
        serial = await mcp_client.call_tool(
            "jmp_run",
            {
                "connection_id": conn_id,
                "command": ["serial", "pipe"],
                "timeout_seconds": 15,
            },
        )
        sd = json.loads(serial)
        stdout = sd.get("stdout", "")
        if stdout:
            diag.append(f"Serial output:\n{stdout[:3000]}")
        else:
            diag.append("Serial: no output (board may be hung or powered off)")
    except Exception as exc:
        diag.append(f"Serial capture failed: {exc}")

    # Power state.
    try:
        power = await mcp_client.call_tool(
            "jmp_run",
            {
                "connection_id": conn_id,
                "command": ["power", "read"],
            },
        )
        pd = json.loads(power)
        diag.append(f"Power: {pd.get('stdout', 'unknown').strip()}")
    except Exception as exc:
        diag.append(f"Power check failed: {exc}")

    # SSH via tunnel.
    try:
        ssh = await mcp_client.call_tool(
            "jmp_run",
            {
                "connection_id": conn_id,
                "command": ["ssh", "--", "uptime"],
                "timeout_seconds": 15,
            },
        )
        sd = json.loads(ssh)
        if sd.get("exit_code") == 0:
            diag.append(f"Tunnel SSH: OK — {sd.get('stdout', '').strip()}")
        else:
            diag.append(f"Tunnel SSH: failed — {sd.get('stderr', '').strip()[:200]}")
    except Exception as exc:
        diag.append(f"Tunnel SSH failed: {exc}")

    return "\n".join(diag)


def trim_response(tool_name: str, content: str) -> str:
    """Trim verbose Jumpstarter tool responses.

    jmp_connect returns cli_tree (~10K chars) and
    drivers (~4K chars) that the agent never uses.
    jmp_run for storage flash returns full progress
    logs (~18K chars) on success.

    Returns the content unchanged for non-Jumpstarter
    tools or when no trimming is needed.
    """
    if tool_name == "jmp_connect":
        try:
            data = json.loads(content)
            if "connection_id" in data:
                trimmed = {
                    "connection_id": data["connection_id"],
                    "lease_name": data.get("lease_name", ""),
                    "exporter_name": data.get("exporter_name", ""),
                    "socket_path": data.get("socket_path", ""),
                }
                return json.dumps(trimmed, indent=2)
        except (ValueError, KeyError):
            pass

    if tool_name == "jmp_run":
        try:
            data = json.loads(content)
            stdout = data.get("stdout", "")
            if data.get("exit_code") == 0 and len(stdout) > 2000:
                lines = stdout.strip().split("\n")
                if len(lines) > 10:
                    summary = (
                        "\n".join(lines[:3]) + f"\n... ({len(lines) - 6} lines "
                        f"trimmed) ...\n" + "\n".join(lines[-3:])
                    )
                    data["stdout"] = summary
                    data["_trimmed"] = True
                    return json.dumps(data, indent=2)
        except (ValueError, KeyError):
            pass

    return content
