"""FastMCP server for platform agent tools.

Exposes the provision_platform tool which runs deterministic
provisioning (flash, boot, verify) internally. The LLM calls
this tool; the tool handles the fixed procedure.

Run directly:  python agents/platform/server.py
Connected via: AgentMCPClient (agents/mcp_client.py)
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

_project_root = str(Path(__file__).resolve().parents[2])
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from fastmcp import FastMCP

from providers.llm.base import ToolDefinition

logger = logging.getLogger(__name__)

mcp = FastMCP("platform-agent")

# Module-level state — lazily initialized
_ticket: dict[str, Any] = {}
_initialized = False


async def _ensure_init():
    """Lazily initialize from env vars on first tool call."""
    global _initialized, _ticket
    if _initialized:
        return

    from agents.server_utils import build_ssh_from_ticket

    _, _ticket = await build_ssh_from_ticket()
    _initialized = True


@mcp.tool()
async def provision_platform(
    provider: str = "",
    image_variant: str = "",
    flash_timeout_seconds: int = 600,
    boot_wait_seconds: int = 60,
) -> str:
    """Provision the platform — flash OS, boot, verify SSH.

    Runs the deterministic provisioning sequence for the
    board type. For Jumpstarter boards: flash image, power
    on, wait for boot, discover IP, inject SSH key.

    Args:
        provider: Resource provider name. Auto-detected
            from ticket if empty.
        image_variant: Override the default image variant
            (e.g., "qa", "ps-ostree"). Uses the pre-resolved
            flash command by default.
        flash_timeout_seconds: Timeout for the flash step.
        boot_wait_seconds: Seconds to wait after power on.

    Returns:
        JSON with provisioning result including success,
        IP address, diagnostics, and timing.
    """
    await _ensure_init()

    cf = _ticket.get("custom_fields", {})
    prov = provider or cf.get("resource_provider", "")

    if prov == "jumpstarter":
        return await _provision_jumpstarter(
            cf,
            image_variant=image_variant,
            flash_timeout=flash_timeout_seconds,
            boot_wait=boot_wait_seconds,
        )
    else:
        return await _provision_ready_host(cf)


async def _provision_jumpstarter(
    cf: dict[str, Any],
    image_variant: str = "",
    flash_timeout: int = 600,
    boot_wait: int = 60,
) -> str:
    """Run deterministic Jumpstarter provisioning."""
    flash_info = cf.get("jumpstarter_flash", {})
    metadata = cf.get("resource_provider_metadata", {})

    if flash_info.get("error"):
        return json.dumps(
            {
                "success": False,
                "error": flash_info["error"],
                "available_variants": flash_info.get("available_variants", []),
            }
        )

    # Get the flash URL from flash_targets (structured)
    # or fall back to parsing flash_command (string).
    flash_targets = flash_info.get("flash_targets", [])
    if flash_targets:
        # Use the first target's URL — for single-
        # partition boards (EBBR) this is the image.
        # Multi-partition boards pass the full command.
        flash_url = flash_targets[0].get("url", "")
    else:
        flash_command = flash_info.get("flash_command", "")
        if not flash_command:
            return json.dumps(
                {
                    "success": False,
                    "error": (
                        "No flash_targets or flash_command "
                        "in jumpstarter_flash"
                    ),
                }
            )
        # Fallback: extract URL from command string
        flash_url = ""
        for part in flash_command.split():
            if part.startswith("http"):
                flash_url = part
                break
        if not flash_url:
            flash_url = flash_command.replace(
                "j storage flash ", ""
            ).strip()

    ssh_public_key = flash_info.get("ssh_public_key", "")
    ssh_key_path = flash_info.get("ssh_key_path", "")
    board_name = metadata.get("exporter_name", "")
    lease_id = metadata.get("lease_id", "")

    from providers.resource.jumpstarter_provision import (
        provision_jumpstarter,
    )

    result = await provision_jumpstarter(
        lease_name=lease_id,
        flash_url=flash_url,
        ssh_public_key=ssh_public_key,
        ssh_key_path=ssh_key_path,
        board_name=board_name,
    )

    return json.dumps(
        {
            "success": result.success,
            "ip": result.ip,
            "board_name": result.board_name,
            "ssh_user": result.ssh_user,
            "ssh_key_path": result.ssh_key_path,
            "diagnostics": result.diagnostics,
            "flash_duration_s": result.flash_duration_s,
            "boot_duration_s": result.boot_duration_s,
        }
    )


async def _provision_ready_host(cf: dict[str, Any]) -> str:
    """Handle providers that return SSH-ready hosts."""
    ips = cf.get("assigned_hardware_ips", {})
    controller = ips.get("controller", "")
    targets = ips.get("targets", [])
    hosts = []
    if controller:
        hosts.append(controller)
    hosts.extend(t for t in targets if t not in hosts)

    if not hosts:
        return json.dumps(
            {
                "success": False,
                "error": "No hosts in assigned_hardware_ips",
            }
        )

    return json.dumps(
        {
            "success": True,
            "hosts": hosts,
            "ssh_user": cf.get("ssh_user", "root"),
            "ssh_key_path": cf.get("ssh_key_path", ""),
            "diagnostics": ["Hosts already provisioned by resource provider"],
        }
    )


@mcp.tool()
async def submit_platform_result(
    platform_ready: bool,
    hosts_provisioned: list[str] | None = None,
    ssh_user: str = "root",
    ssh_key_path: str = "",
    board_name: str = "",
    diagnostics: str = "",
    flash_duration_s: float = 0.0,
    boot_duration_s: float = 0.0,
) -> str:
    """Submit the platform provisioning result.

    Call this after provision_platform completes (or when
    the platform is already ready for non-Jumpstarter
    providers).

    Args:
        platform_ready: Whether the platform is ready.
        hosts_provisioned: List of IP addresses.
        ssh_user: SSH user for the hosts.
        ssh_key_path: Path to the SSH private key.
        board_name: Board/exporter name.
        diagnostics: Diagnostic notes.
        flash_duration_s: Time spent flashing.
        boot_duration_s: Time spent booting.
    """
    return json.dumps(
        {
            "submitted": True,
            "platform_ready": platform_ready,
            "hosts_provisioned": hosts_provisioned or [],
        }
    )


def get_platform_tools() -> list[ToolDefinition]:
    """Return tool definitions for local handler registration."""
    return [
        ToolDefinition(
            name="provision_platform",
            description=(
                "Provision the platform — flash OS, boot, "
                "verify SSH. Runs deterministic code internally."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "provider": {
                        "type": "string",
                        "description": ("Resource provider. Auto-detected if empty."),
                    },
                    "image_variant": {
                        "type": "string",
                        "description": (
                            "Override image variant (e.g., 'qa', 'ps-ostree')"
                        ),
                    },
                    "flash_timeout_seconds": {
                        "type": "integer",
                        "description": "Flash timeout in seconds",
                        "default": 600,
                    },
                    "boot_wait_seconds": {
                        "type": "integer",
                        "description": "Boot wait in seconds",
                        "default": 60,
                    },
                },
            },
        ),
        ToolDefinition(
            name="submit_platform_result",
            description="Submit the platform provisioning result.",
            input_schema={
                "type": "object",
                "properties": {
                    "platform_ready": {"type": "boolean"},
                    "hosts_provisioned": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "ssh_user": {"type": "string", "default": "root"},
                    "ssh_key_path": {"type": "string"},
                    "board_name": {"type": "string"},
                    "diagnostics": {"type": "string"},
                    "flash_duration_s": {"type": "number"},
                    "boot_duration_s": {"type": "number"},
                },
                "required": ["platform_ready"],
            },
        ),
        ToolDefinition(
            name="request_clarification",
            description=(
                "Ask the user for guidance when provisioning "
                "fails and recovery is unclear."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "Question for the user",
                    },
                },
                "required": ["question"],
            },
        ),
    ]


if __name__ == "__main__":
    mcp.run()
