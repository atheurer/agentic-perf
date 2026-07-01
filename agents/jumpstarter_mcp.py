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
from typing import Any

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
) -> set[str] | None:
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
        return None


async def suspend_for_device_ready(
    agent: Any,
    ticket_id: str,
    store_url: str,
    lease_id: str = "",
    expected_duration_mins: int = 10,
) -> bool:
    """Suspend the agent while waiting for a Jumpstarter device.

    Checks if the ticket uses Jumpstarter hardware. If so,
    suspends the agent via _suspend_for_async() and returns
    True. The orchestrator will resume the agent when a
    CloudEvent is received at POST /api/v1/tickets/{id}/signal
    with an event ID matching the operation_id.

    The operation_id is set to the lease ID so that the
    Jumpstarter webhook (or polling adapter) can signal
    completion by posting a CloudEvent with that ID.

    Returns False if the ticket doesn't use Jumpstarter or
    if the device is already signaled ready.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{store_url}/api/v1/tickets/{ticket_id}")
            if r.status_code != 200:
                return False
            cf = r.json().get("custom_fields", {})

        if cf.get("resource_provider") != "jumpstarter":
            return False

        # Skip if already signaled (resuming from suspension)
        async_ctx = cf.get("async_context", {})
        if async_ctx.get("signal_received"):
            return False

        # Check if device is already connected/ready.
        # If the lease is active and we can get a TCP address,
        # the device is ready — no need to suspend.
        metadata = cf.get("resource_provider_metadata", {})
        if metadata.get("device_ready"):
            return False

        # Get the lease ID from resource allocation
        op_id = lease_id or cf.get("resource_provider_metadata", {}).get(
            "lease_id", f"jmp-{ticket_id.lower()}"
        )

        await agent._suspend_for_async(
            ticket_id=ticket_id,
            wait_type="jumpstarter_device_ready",
            operation_id=op_id,
            resume_to_status="awaiting_provision",
            resume_context={
                "lease_id": op_id,
                "device_ready": True,
            },
            expected_duration_mins=expected_duration_mins,
        )
        return True

    except Exception:
        logger.debug(
            "[jumpstarter-mcp] Async suspension not available or not needed",
            exc_info=True,
        )
        return False
