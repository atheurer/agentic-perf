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
import logging
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
        return True

    except Exception:
        logger.debug(
            "[jumpstarter-mcp] Not attached (not configured or not available)",
            exc_info=True,
        )
        return None


# Provisioning operations shorter than this threshold run
# synchronously (the LLM waits for the tool result). Longer
# operations trigger async suspension to save LLM compute.
SUSPEND_THRESHOLD_MINS = 10


async def suspend_for_device_ready(
    agent: Any,
    ticket_id: str,
    store_url: str,
    lease_id: str = "",
    expected_duration_mins: int = 0,
) -> bool:
    """Conditionally suspend the agent during provisioning.

    Decision logic:
    - If device_ready is set (provisioning already done,
      e.g., investigation loop-back), skip.
    - If already resumed from a previous suspension
      (signal_received present), skip.
    - If expected provisioning duration exceeds
      SUSPEND_THRESHOLD_MINS, suspend and wait for a
      CloudEvents signal.
    - Otherwise, let the agent proceed synchronously.

    The expected duration comes from jumpstarter_flash
    metadata (set by the orchestrator's image resolution)
    or from the caller's estimate.

    Returns True if the agent was suspended (caller should
    return immediately), False otherwise.
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

        # Skip if device is already provisioned (loop-back)
        metadata = cf.get("resource_provider_metadata", {})
        if metadata.get("device_ready"):
            return False

        # Determine expected duration. The orchestrator's
        # image resolution may set an estimate, or the
        # caller can provide one.
        flash_info = cf.get("jumpstarter_flash", {})
        est_mins = expected_duration_mins or flash_info.get("expected_duration_mins", 0)

        # Short operations: let the agent handle it
        # synchronously. The LLM waits for the tool result
        # but for < 10 minutes that's acceptable.
        if est_mins < SUSPEND_THRESHOLD_MINS:
            logger.info(
                f"[jumpstarter-mcp] Provisioning "
                f"{ticket_id} estimated at {est_mins}m "
                f"(< {SUSPEND_THRESHOLD_MINS}m threshold) "
                f"— proceeding synchronously"
            )
            return False

        # Long operations: suspend to save LLM compute.
        op_id = lease_id or metadata.get("lease_id", f"jmp-{ticket_id.lower()}")

        logger.info(
            f"[jumpstarter-mcp] Suspending {ticket_id} "
            f"for provisioning (~{est_mins}m expected)"
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
            expected_duration_mins=est_mins,
        )
        return True

    except Exception:
        logger.debug(
            "[jumpstarter-mcp] Async suspension check "
            "failed — proceeding synchronously",
            exc_info=True,
        )
        return False
