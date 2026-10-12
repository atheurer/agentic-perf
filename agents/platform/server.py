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

from agents.mcp_audit import create_ticket_mcp
from providers.llm.base import ToolDefinition

logger = logging.getLogger(__name__)

mcp = create_ticket_mcp("platform-agent")

# Module-level state — lazily initialized. PlatformAgent launches this server
# as a fresh stdio subprocess for each ticket, so this state is process-local
# and does not leak between tickets.
_ticket: dict[str, Any] = {}
_initialized = False

# Track infrastructure failures to block speculative variant retries.
# Once a provisioning attempt fails with an infrastructure error, further
# calls with a different image_variant are rejected immediately because
# the root cause is the environment, not the OS image.
_last_infra_error: str | None = None
_last_infra_variant: str | None = None


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
        # Block speculative variant changes after infrastructure errors.
        # If the previous attempt failed due to infrastructure (TaskGroup,
        # U-Boot timeout, connection error), changing the image variant will
        # not help — fail immediately.
        if _last_infra_error and image_variant != (_last_infra_variant or ""):
            return json.dumps(
                {
                    "success": False,
                    "infrastructure_error": True,
                    "error": (
                        "Previous provisioning failed with an infrastructure "
                        "error. Changing the image variant will not fix it. "
                        f"Original error: {_last_infra_error}"
                    ),
                }
            )
        return await _provision_jumpstarter(
            cf,
            ticket_id=_ticket.get("id", ""),
            image_variant=image_variant,
            flash_timeout=flash_timeout_seconds,
            boot_wait=boot_wait_seconds,
        )
    else:
        return await _provision_ready_host(cf)


async def _provision_jumpstarter(
    cf: dict[str, Any],
    ticket_id: str = "",
    image_variant: str = "",
    flash_timeout: int = 600,
    boot_wait: int = 60,
) -> str:
    """Run deterministic Jumpstarter provisioning."""
    from providers.resource.jumpstarter_provision import (
        prepare_and_provision,
    )

    ticket_id = (
        ticket_id
        or cf.get("ticket_id", "")
        or (cf.get("resource_provider_metadata") or {}).get("ticket_id", "")
    )
    result, artifact_dir = await prepare_and_provision(cf, ticket_id=ticket_id)

    # Track infrastructure failures to block speculative variant retries.
    global _last_infra_error, _last_infra_variant
    if result.infrastructure_error:
        _last_infra_error = next(
            (d for d in result.diagnostics if "INFRASTRUCTURE_ERROR" in d),
            "infrastructure failure (see diagnostics)",
        )
        _last_infra_variant = image_variant
    else:
        _last_infra_error = None
        _last_infra_variant = None

    return json.dumps(result.to_tool_response(artifact_dir=artifact_dir))


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


@mcp.tool()
async def read_artifact_file(
    path: str,
    offset: int = 0,
    limit: int = 200,
) -> str:
    """Read lines from a local artifact file (flash diagnostics,
    serial capture, provision logs).  Use this to inspect full
    output when the tool result was truncated.

    Args:
        path: Absolute path to the artifact file, or a path relative
            to the ticket's artifact directory.
        offset: 0-based line number to start reading from.
        limit: Maximum number of lines to return (max 500).
    """
    from agents.server_utils import read_artifact_file_impl

    tid = _ticket.get("id", "") if _ticket else ""
    return read_artifact_file_impl(path, offset, limit, ticket_id=tid)


if __name__ == "__main__":
    mcp.run()
