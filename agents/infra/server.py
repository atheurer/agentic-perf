"""Shared infrastructure MCP server.

Provides SSH execution, file transfer, and secrets management for all
agents. Credentials stay server-side — the LLM never sees SSH keys,
API tokens, or secret file contents.

Run directly:  python agents/infra/server.py
Connected via: AgentMCPClient (agents/mcp_client.py)
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

_project_root = str(Path(__file__).resolve().parents[2])
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from fastmcp import FastMCP

from agents.infra.command_policy import (
    CommandPolicy,
    check_command,
    load_policy,
)
from agents.server_utils import build_secrets_provider as _build_secrets
from providers.ssh import SSHExecutor, SSHResult

logger = logging.getLogger(__name__)

mcp = FastMCP("infra")

_ssh: SSHExecutor | None = None
_agent_name: str | None = None
_policy: CommandPolicy | None = None
_secrets_provider = None
_state_store_url: str | None = None
_ticket_id: str | None = None

_APPROVAL_POLL_INTERVAL = 3
_APPROVAL_TIMEOUT = 300


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
async def set_ssh_context(ticket_id: str, agent_name: str = "") -> str:
    """Set SSH credentials by reading them from a ticket's custom_fields.

    Must be called before any SSH operations. Resolves ssh_key_path and
    ssh_user from the ticket so credentials never appear in tool inputs.
    """
    global _ssh, _agent_name, _policy, _state_store_url, _ticket_id

    _ticket_id = ticket_id

    _state_store_url = os.environ.get("STATE_STORE_URL", "http://localhost:8090")
    import httpx

    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(f"{_state_store_url}/api/v1/tickets/{ticket_id}")
        r.raise_for_status()
        ticket = r.json()

    fields = ticket.get("custom_fields", {})
    ssh_key = fields.get("ssh_key_path")
    ssh_user = fields.get("ssh_user", "root")

    _ssh = SSHExecutor(user=ssh_user, key_path=ssh_key)

    if agent_name:
        _agent_name = agent_name
        _policy = load_policy(agent_name)
    else:
        _agent_name = None
        _policy = None

    return json.dumps(
        {
            "status": "ok",
            "ssh_user": ssh_user,
            "has_key": ssh_key is not None,
            "agent_policy": agent_name or "none",
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

    mkdir_result = await ssh.run(
        host, f"mkdir -p $(dirname {remote_path!r})", timeout=15
    )
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
    result = await ssh.run(host, f"head -c {max_bytes} {remote_path!r}", timeout=30)
    return _format_result(result)


@mcp.tool()
async def deploy_secret(host: str, secret_path: str, remote_path: str) -> str:
    """Deploy a secret file to a remote host.

    Resolves the secret locally via the secrets provider, then SCPs it.
    The LLM never sees the secret content — only the logical path.
    """
    ssh = _get_ssh()
    sp = _get_secrets()

    local_path = await sp.get_secret_file(secret_path)
    if local_path is None:
        return json.dumps(
            {
                "success": False,
                "error": f"Secret not found: {secret_path}",
            }
        )

    mkdir_result = await ssh.run(
        host, f"mkdir -p $(dirname {remote_path!r})", timeout=15
    )
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

        import asyncio

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


def _extract_binary(command: str) -> str:
    """Extract the primary binary name from a command string."""
    import shlex

    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    for token in tokens:
        if "=" in token and not token.startswith("-"):
            continue
        return Path(token).name
    return ""


async def _get_ticket_approvals() -> list[str]:
    """Read the per-ticket command_approvals list from custom_fields."""
    if not _state_store_url or not _ticket_id:
        return []
    import httpx

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{_state_store_url}/api/v1/tickets/{_ticket_id}"
            )
            r.raise_for_status()
            fields = r.json().get("custom_fields", {})
            return fields.get("command_approvals", [])
    except Exception:
        return []


async def _request_approval(
    command: str, binary: str, host: str
) -> str:
    """Request user approval for a command not in the allowlist.

    Writes a pending_approval request to the ticket's custom_fields,
    then polls until the user responds or the timeout expires.

    Returns: "approved_once", "approved_ticket", or "denied".
    """
    import asyncio
    import uuid

    import httpx

    if not _state_store_url or not _ticket_id:
        return "denied"

    approval_id = f"appr-{uuid.uuid4().hex[:8]}"
    pending = {
        "id": approval_id,
        "agent": _agent_name or "unknown",
        "command": command[:500],
        "binary": binary,
        "host": host,
        "requested_at": (
            __import__("datetime")
            .datetime.now(__import__("datetime").timezone.utc)
            .isoformat()
        ),
        "status": "pending",
    }

    logger.info(
        "Requesting approval for %s: %s on %s",
        _agent_name,
        command[:120],
        host,
    )

    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.patch(
            f"{_state_store_url}/api/v1/tickets/{_ticket_id}/fields",
            json={"fields": {"pending_approval": pending}},
        )

        elapsed = 0
        while elapsed < _APPROVAL_TIMEOUT:
            await asyncio.sleep(_APPROVAL_POLL_INTERVAL)
            elapsed += _APPROVAL_POLL_INTERVAL

            try:
                r = await client.get(
                    f"{_state_store_url}/api/v1/tickets/{_ticket_id}"
                )
                r.raise_for_status()
                fields = r.json().get("custom_fields", {})
                pa = fields.get("pending_approval", {})
                if pa.get("id") != approval_id:
                    return "denied"
                status = pa.get("status", "pending")
                if status != "pending":
                    logger.info(
                        "Approval response for %s: %s",
                        command[:80],
                        status,
                    )
                    return status
            except Exception:
                logger.exception("Error polling for approval")

    logger.warning("Approval timeout for: %s", command[:120])
    return "denied"


@mcp.tool()
async def execute_command(host: str, command: str, timeout: int = 300) -> str:
    """Execute a command on a remote host via SSH.

    Subject to per-agent command policy: the command's binary must be in
    the agent's allowlist, and the command must not match any blocked pattern.
    If the binary is not in the allowlist but is otherwise safe, the user
    will be prompted for approval.

    Call set_ssh_context() with agent_name to load the policy.
    """
    ssh = _get_ssh()

    if _policy is not None:
        allowed, reason = check_command(command, _policy)
        if not allowed:
            if "not in allowlist" in reason:
                binary = _extract_binary(command)
                ticket_approvals = await _get_ticket_approvals()
                if binary in ticket_approvals:
                    logger.info(
                        "Binary %r pre-approved for ticket, executing",
                        binary,
                    )
                else:
                    decision = await _request_approval(
                        command, binary, host
                    )
                    if decision not in (
                        "approved_once",
                        "approved_ticket",
                    ):
                        logger.warning(
                            "Command denied by user for %s: %s",
                            _agent_name,
                            command[:120],
                        )
                        return json.dumps(
                            {
                                "exit_code": -1,
                                "stdout": "",
                                "stderr": (
                                    f"Command denied by user: "
                                    f"binary {binary!r} not approved"
                                ),
                                "blocked": True,
                            }
                        )
            else:
                logger.warning(
                    "Command blocked for %s: %s — %s",
                    _agent_name,
                    command[:120],
                    reason,
                )
                return json.dumps(
                    {
                        "exit_code": -1,
                        "stdout": "",
                        "stderr": (
                            f"Command blocked by policy: {reason}"
                        ),
                        "blocked": True,
                    }
                )

        if timeout > _policy.max_timeout:
            timeout = _policy.max_timeout

    result = await ssh.run(host, command, timeout=timeout)
    return _format_result(result)


if __name__ == "__main__":
    mcp.run()
