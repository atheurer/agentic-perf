"""Deterministic enrichment for webhook-triggered tickets.

When a ticket is created by a webhook (trigger_source is set),
this module resolves run metadata from the Domain MCP and writes
it to the ticket so downstream agents have the context they need.

This is a data bridge, not a mapping layer. The raw metadata
from get_run_info is written to the ticket as-is. Downstream
agents and deterministic code (image resolution, resource
selection) use the metadata directly.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def _ap_home() -> Path:
    return Path(
        os.environ.get(
            "AGENTIC_PERF_HOME",
            str(Path.home() / ".agentic-perf"),
        )
    )


def _load_config() -> dict[str, Any]:
    try:
        return json.loads((_ap_home() / "config.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}


async def enrich_webhook_ticket(
    store_url: str,
    ticket_id: str,
    ticket: dict[str, Any],
) -> bool:
    """Enrich a webhook ticket with run metadata from Domain MCP.

    Calls get_run_info to resolve the run's target, OS, build,
    and labels, then writes them to the ticket's custom_fields
    as run_metadata for downstream agents to use.

    Returns True if enrichment succeeded, False if skipped/failed.
    """
    cf = ticket.get("custom_fields", {})

    # Only enrich webhook-triggered tickets
    if not cf.get("trigger_source"):
        return False

    # Skip if already enriched
    if cf.get("run_metadata"):
        logger.debug(f"[enrich] {ticket_id}: already enriched")
        return True

    # Get run/dataset ID from anomaly context
    anomaly = cf.get("anomaly_context", {})
    run_id = anomaly.get("run_id")
    dataset_id = anomaly.get("dataset_id")
    if not run_id and not dataset_id:
        logger.info(
            f"[enrich] {ticket_id}: no run_id or dataset_id, skipping enrichment"
        )
        return False

    # Call Domain MCP get_run_info
    run_info = await _call_get_run_info(run_id, dataset_id)
    if not run_info or run_info.get("error"):
        logger.warning(
            f"[enrich] {ticket_id}: get_run_info failed:"
            f" {run_info.get('error', 'no response') if run_info else 'no response'}"
        )
        return False

    # Write raw metadata and derived directives to ticket.
    # board_selector uses the Jumpstarter target label which
    # matches run_metadata.target exactly — no mapping needed.
    headers = {}
    api_token = os.environ.get("AGENTIC_PERF_API_TOKEN", "")
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"

    fields: dict[str, Any] = {"run_metadata": run_info}

    # Set board_selector from target if not already in directives
    target = run_info.get("target", "")
    if target:
        existing_directives = cf.get("directives", {})
        if not existing_directives.get("board_selector"):
            merged = dict(existing_directives)
            merged["board_selector"] = f"board-type={target}"
            fields["directives"] = merged

    async with httpx.AsyncClient(
        timeout=10.0,
        headers=headers,
    ) as client:
        await client.patch(
            f"{store_url}/api/v1/tickets/{ticket_id}/fields",
            json={"fields": fields},
        )

    logger.info(
        f"[enrich] {ticket_id}: wrote run_metadata"
        f" (target={run_info.get('target')},"
        f" os_id={run_info.get('os_id')})"
    )
    return True


async def _call_get_run_info(
    run_id: Any | None,
    dataset_id: Any | None,
) -> dict[str, Any] | None:
    """Call Domain MCP get_run_info via StreamableHTTP.

    Manages the MCP session lifecycle (initialize → call).
    Returns the parsed result or None on failure.
    """
    config = _load_config()

    # Find a Domain MCP server with get_run_info access
    servers = config.get("external_mcp_servers", [])
    mcp_server = None
    for s in servers:
        agents = s.get("agents", {})
        for _agent_name, agent_cfg in agents.items():
            enabled = agent_cfg.get("enabled_tools", [])
            if enabled == "all" or "get_run_info" in enabled:
                mcp_server = s
                break
        if mcp_server:
            break

    if not mcp_server:
        logger.info("[enrich] No Domain MCP server with get_run_info")
        return None

    url = mcp_server.get("url", "")
    if not url:
        return None

    # Auth token
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    secret_path = mcp_server.get("secret", "")
    if secret_path:
        token_file = _ap_home() / "secrets" / secret_path
        if token_file.exists():
            token = token_file.read_text().strip()
            if token:
                headers["Authorization"] = f"Bearer {token}"

    try:
        async with httpx.AsyncClient(
            timeout=30.0,
            headers=headers,
            verify=not mcp_server.get("trust", False),
        ) as client:
            # Initialize MCP session
            init_resp = await client.post(
                url,
                json={
                    "jsonrpc": "2.0",
                    "id": 0,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {
                            "name": "webhook-enrichment",
                            "version": "1.0",
                        },
                    },
                },
            )
            init_data = init_resp.json()
            if "error" in init_data:
                logger.warning(f"[enrich] MCP init error: {init_data['error']}")
                return None

            session_id = init_resp.headers.get(
                "mcp-session-id",
                "",
            )

            # Build arguments
            args: dict[str, str] = {}
            if run_id:
                args["run_id"] = str(run_id)
            elif dataset_id:
                args["dataset_id"] = str(dataset_id)

            # Call tool
            call_headers = dict(headers)
            if session_id:
                call_headers["mcp-session-id"] = session_id

            resp = await client.post(
                url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "get_run_info",
                        "arguments": args,
                    },
                },
                headers=call_headers,
            )
            data = resp.json()
            if "error" in data:
                logger.warning(f"[enrich] get_run_info error: {data['error']}")
                return None

            content = data.get("result", {}).get("content", [])
            if content and content[0].get("text"):
                return json.loads(content[0]["text"])

    except Exception:
        logger.warning(
            "[enrich] Failed to call Domain MCP",
            exc_info=True,
        )

    return None
