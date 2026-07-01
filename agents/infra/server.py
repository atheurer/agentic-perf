"""Shared infrastructure MCP server.

Provides SSH execution, file transfer, and secrets management for all
agents. Credentials stay server-side — the LLM never sees SSH keys,
API tokens, or secret file contents.

Run directly:  python agents/infra/server.py
Connected via: AgentMCPClient (agents/mcp_client.py)
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import sys
import tempfile
import uuid
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
_background_pids: dict[str, dict[str, Any]] = {}


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


def _validate_host(host: str) -> str | None:
    """Reject ticket IDs used as SSH targets.

    Returns an error message if the host looks like a ticket
    ID (e.g., PERF-ABC123), or None if it's valid.
    """
    import re

    if re.match(r"^PERF-[A-F0-9]+$", host, re.IGNORECASE):
        return (
            f"ERROR: '{host}' is a ticket ID, not a "
            f"hostname or IP address. Use the actual "
            f"host IP from the ticket's SSH Access "
            f"section (e.g., 10.x.x.x) instead."
        )
    return None


@mcp.tool()
async def check_host(host: str) -> str:
    """Test SSH connectivity and gather system info (OS, CPU, RAM, hostname)."""
    err = _validate_host(host)
    if err:
        return err
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
            r = await client.get(f"{_state_store_url}/api/v1/tickets/{_ticket_id}")
            r.raise_for_status()
            fields = r.json().get("custom_fields", {})
            return fields.get("command_approvals", [])
    except Exception:
        return []


async def _request_approval(command: str, binary: str, host: str) -> str:
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
                r = await client.get(f"{_state_store_url}/api/v1/tickets/{_ticket_id}")
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
async def execute_command(
    host: str,
    command: str,
    timeout: int = 300,
    background: bool = False,
) -> str:
    """Execute a command on a remote host via SSH.

    Subject to per-agent command policy: the command's binary must be in
    the agent's allowlist, and the command must not match any blocked pattern.
    If the binary is not in the allowlist but is otherwise safe, the user
    will be prompted for approval.

    Set background=True (or end the command with &) to run the command in
    the background. The response will include a bg_id and pid. You MUST
    call stop_background_command(bg_id) when you no longer need the
    background process — for example, after using nc to test port
    connectivity, stop the listener before the benchmark needs that port.

    Call set_ssh_context() with agent_name to load the policy.
    """
    err = _validate_host(host)
    if err:
        return err
    ssh = _get_ssh()

    command = html.unescape(command)

    stripped = command.rstrip()
    if stripped.endswith("&"):
        background = True
        command = stripped[:-1].rstrip()

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
                    decision = await _request_approval(command, binary, host)
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
                        "stderr": (f"Command blocked by policy: {reason}"),
                        "blocked": True,
                    }
                )

        if timeout > _policy.max_timeout:
            timeout = _policy.max_timeout

    if background:
        bg_id = f"bg-{uuid.uuid4().hex[:8]}"
        bg_cmd = f"nohup {command} > /tmp/{bg_id}.out 2>&1 & echo $!"
        result = await ssh.run(host, bg_cmd, timeout=10)
        pid_str = result.stdout.strip().splitlines()[-1] if result.stdout else ""
        if result.exit_code == 0 and pid_str.isdigit():
            pid = int(pid_str)
            _background_pids[bg_id] = {
                "host": host,
                "pid": pid,
                "command": command,
            }
            return json.dumps(
                {
                    "status": "backgrounded",
                    "bg_id": bg_id,
                    "pid": pid,
                    "host": host,
                }
            )
        return json.dumps(
            {
                "status": "failed",
                "exit_code": result.exit_code,
                "stdout": result.stdout or "",
                "stderr": result.stderr or "",
            }
        )

    result = await ssh.run(host, command, timeout=timeout)
    return _format_result(result)


@mcp.tool()
async def stop_background_command(bg_id: str) -> str:
    """Stop a background command started by execute_command.

    Pass the bg_id returned by execute_command when background=True.
    Always call this when you no longer need the background process
    — for example, after a connectivity test, stop the nc listener
    before the benchmark needs that port.
    """
    ssh = _get_ssh()
    entry = _background_pids.pop(bg_id, None)
    if entry is None:
        return json.dumps({"status": "error", "message": f"Unknown bg_id: {bg_id}"})

    host = entry["host"]
    pid = entry["pid"]

    await ssh.run(host, f"kill {pid} 2>/dev/null", timeout=5)
    check = await ssh.run(host, f"kill -0 {pid} 2>/dev/null", timeout=5)
    if check.exit_code == 0:
        await ssh.run(host, f"kill -9 {pid} 2>/dev/null", timeout=5)

    return json.dumps({"status": "stopped", "bg_id": bg_id, "pid": pid})


@mcp.tool()
async def check_background_command(bg_id: str) -> str:
    """Check whether a background command is still running.

    Returns the running status and recent output (last 20 lines).
    """
    ssh = _get_ssh()
    entry = _background_pids.get(bg_id)
    if entry is None:
        return json.dumps({"status": "error", "message": f"Unknown bg_id: {bg_id}"})

    host = entry["host"]
    pid = entry["pid"]

    check = await ssh.run(host, f"kill -0 {pid} 2>/dev/null", timeout=5)
    running = check.exit_code == 0

    tail = await ssh.run(host, f"tail -20 /tmp/{bg_id}.out 2>/dev/null", timeout=5)
    output = tail.stdout.strip() if tail.stdout else ""

    return json.dumps(
        {
            "bg_id": bg_id,
            "running": running,
            "pid": pid,
            "output": output,
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
        bg_cmd = f"nohup nc -l {listen_ip} {port} > /dev/null 2>&1 & echo $!"
        start = await ssh.run(listener_ssh, bg_cmd, timeout=5)
        pid_str = start.stdout.strip().splitlines()[-1] if start.stdout else ""

        if not pid_str.isdigit():
            return {
                "direction": label,
                "port": port,
                "reachable": False,
                "error": "Failed to start nc listener",
            }

        pid = int(pid_str)
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


if __name__ == "__main__":
    mcp.run()
