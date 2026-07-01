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

import logging

import httpx

from agents.mcp_client import AgentMCPClient

logger = logging.getLogger(__name__)

# Tools that agents should see for device interaction.
# Lease management tools are excluded — the resource
# provider handles that, not agents.
AGENT_DEVICE_TOOLS = frozenset(
    {
        "jmp_run",
        "jmp_explore",
        "jmp_connect",
        "jmp_disconnect",
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


async def attach_jumpstarter_mcp(
    mcp_client: AgentMCPClient,
    ticket_id: str,
    store_url: str,
) -> bool:
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
        True if Jumpstarter MCP was attached, False otherwise.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{store_url}/api/v1/tickets/{ticket_id}")
            if r.status_code != 200:
                return False
            cf = r.json().get("custom_fields", {})

        if cf.get("resource_provider") != "jumpstarter":
            return False

        await mcp_client.connect_command(
            command="jmp",
            args=["mcp", "serve"],
            name="jumpstarter",
        )
        logger.info(f"[jumpstarter-mcp] Attached to agent for ticket {ticket_id}")
        return True

    except Exception:
        logger.debug(
            "[jumpstarter-mcp] Not attached (not configured or not available)",
            exc_info=True,
        )
        return False
