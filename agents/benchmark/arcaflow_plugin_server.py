"""Standalone Arcaflow plugin runner MCP server.

Handles the arcaflow-plugins harness: running a single Arcaflow
plugin as a containerized benchmark via podman on a target host.

This is distinct from the arcaflow-workflows harness which uses
the Arcaflow MCP engine to orchestrate multi-step workflows.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import uuid
from typing import Any

from agents.mcp_audit import create_ticket_mcp

logger = logging.getLogger(__name__)

mcp = create_ticket_mcp("arcaflow-plugin-runner")

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_ARCAFLOW_PLUGIN_IMAGE_RE = re.compile(
    r"^quay\.io/arcalot/arcaflow-plugin-[a-z0-9][a-z0-9._-]*"
    r"(?::[a-zA-Z0-9][a-zA-Z0-9._-]*|@sha256:[0-9a-fA-F]{64})?$"
)


def _validate_plugin_image(plugin_image: str) -> tuple[bool, str]:
    """Validate an Arcaflow plugin image before using it in a remote command."""
    if not isinstance(plugin_image, str) or not plugin_image.strip():
        return False, "plugin_image must be a non-empty string"
    if not _ARCAFLOW_PLUGIN_IMAGE_RE.fullmatch(plugin_image):
        return (
            False,
            "plugin_image must be a quay.io/arcalot/arcaflow-plugin image "
            "with an optional tag or sha256 digest",
        )
    return True, "OK"


# ---------------------------------------------------------------------------
# Module state (initialized on first tool call)
# ---------------------------------------------------------------------------

_ssh: Any = None
_ticket: dict[str, Any] | None = None
_initialized = False


def _controller_host() -> str:
    if _ticket is None:
        return ""
    cf = _ticket.get("custom_fields", {})
    ips = cf.get("assigned_hardware_ips", {})
    # Direct model: use first target
    targets = ips.get("targets", [])
    if targets:
        return targets[0]
    return ips.get("controller", "")


def _authorized_plugin_hosts() -> set[str]:
    """Return the set of hosts this ticket is authorized to run plugins on."""
    hosts: set[str] = set()
    if _ticket is None:
        return hosts
    cf = _ticket.get("custom_fields", {})
    ips = cf.get("assigned_hardware_ips", {})

    def add(h: str) -> None:
        if isinstance(h, str) and h.strip():
            hosts.add(h.strip().rstrip(".").lower())

    controller = ips.get("controller", "")
    if controller:
        add(controller)
    for t in ips.get("targets", []):
        add(t)
    for h in cf.get("hosts_provisioned", []):
        add(h)
    return hosts


def _validate_plugin_host(host: str) -> tuple[bool, str]:
    """Check that a schema target is an assigned controller/inventory host."""
    if not isinstance(host, str) or not host.strip():
        return False, "No target host available"
    normalized = host.strip().rstrip(".").lower()
    if normalized not in _authorized_plugin_hosts():
        return False, "Target host is not assigned to this ticket"
    return True, "OK"


async def _ensure_init() -> None:
    global _ssh, _ticket, _initialized
    if _initialized:
        return
    _initialized = True

    from providers.ssh import SSHExecutor

    ticket_id = os.environ.get("TICKET_ID", "")
    store_url = os.environ.get("STATE_STORE_URL", "http://localhost:8090")
    token = os.environ.get("AGENTIC_PERF_API_TOKEN", "")

    _ssh = SSHExecutor()

    if ticket_id and store_url:
        try:
            from providers.execution import AuditedAsyncHTTPClient

            headers = {"Authorization": f"Bearer {token}"} if token else {}
            async with AuditedAsyncHTTPClient(timeout=10.0, headers=headers) as client:
                r = await client.get(f"{store_url}/api/v1/tickets/{ticket_id}")
                _ticket = r.json()
        except Exception:
            logger.debug("Could not fetch ticket for arcaflow server init")

    # Set SSH context from ticket
    if _ticket:
        cf = _ticket.get("custom_fields", {})
        ssh_key = cf.get("ssh_key_path")
        ssh_user = cf.get("ssh_user", "root")
        if ssh_key:
            _ssh.set_key_path(ssh_key)
        if ssh_user:
            _ssh.set_user(ssh_user)


# ---------------------------------------------------------------------------
# Progress callback
# ---------------------------------------------------------------------------


def _plugin_progress(line: str) -> None:
    """Log plugin execution output for observability."""
    logger.info("[arcaflow/execute] %s", line.rstrip())


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_plugin_schema(
    plugin_image: str,
    host: str = "",
) -> str:
    """Query an Arcaflow plugin container for its full schema.

    Runs the plugin with --schema on the target host via podman.
    Returns the complete schema (all steps, inputs, outputs) so
    the caller can discover available steps and their parameters.

    Args:
        plugin_image: Full container image ref
            (e.g., quay.io/arcalot/arcaflow-plugin-fio:0.5.0)
        host: Target host IP. Uses the ticket's target
            if not specified.
    """
    await _ensure_init()
    if _ssh is None:
        return json.dumps({"error": "SSH not initialized"})

    image_valid, image_error = _validate_plugin_image(plugin_image)
    if not image_valid:
        return json.dumps({"error": image_error})

    target = host or _controller_host() or ""
    host_valid, host_error = _validate_plugin_host(target)
    if not host_valid:
        return json.dumps({"error": host_error})

    # Use --schema: returns the complete plugin schema (all steps,
    # inputs, outputs) as YAML without needing a step selector.
    # --json-schema requires both -s <step> and input|output args
    # which we don't know yet at discovery time.
    cmd = shlex.join(["podman", "run", "--rm", plugin_image, "--schema"])
    result = await _ssh.run(target, cmd, timeout=120)

    if result.exit_code != 0:
        return json.dumps(
            {
                "error": (f"Failed to query plugin schema (exit {result.exit_code})"),
                "stderr": result.stderr[:500] if result.stderr else "",
                "hint": (
                    "The plugin image may not exist or may not "
                    "support --schema. Check the image ref."
                ),
            }
        )

    stdout = result.stdout or ""
    try:
        import yaml

        schema = yaml.safe_load(stdout)
        return json.dumps(
            {
                "plugin_image": plugin_image,
                "schema": schema,
            }
        )
    except ImportError:
        pass
    except Exception:
        pass

    try:
        schema = json.loads(stdout)
        return json.dumps(
            {
                "plugin_image": plugin_image,
                "schema": schema,
            }
        )
    except json.JSONDecodeError:
        pass

    return json.dumps(
        {
            "plugin_image": plugin_image,
            "raw_schema": stdout[:4000],
            "format": "yaml",
        }
    )


@mcp.tool()
async def execute_arcaflow_plugin(
    plugin_image: str,
    plugin_step: str,
    input: dict[str, Any],
    host: str = "",
) -> str:
    """Execute an Arcaflow plugin container on the target host.

    Runs 'podman run -i --rm <image> -s <step> -f -' with the
    input piped to stdin. Collects stdout/stderr and exit code.

    Args:
        plugin_image: Full container image ref.
        plugin_step: Step name (e.g. 'sysbenchcpu', 'uperf').
        input: Plugin input parameters as a dict.
        host: Target host IP. Uses the ticket's target
            if not specified.
    """
    await _ensure_init()
    if _ssh is None:
        return json.dumps({"error": "SSH not initialized"})

    run_uuid = uuid.uuid4().hex[:8]

    image_valid, image_error = _validate_plugin_image(plugin_image)
    if not image_valid:
        return json.dumps(
            {
                "status": "failed",
                "exit_code": -1,
                "run_id": f"arcaflow-{run_uuid}",
                "error": image_error,
            }
        )

    target = host or _controller_host() or ""
    host_valid, host_error = _validate_plugin_host(target)
    if not host_valid:
        return json.dumps(
            {
                "status": "failed",
                "exit_code": -1,
                "run_id": f"arcaflow-{run_uuid}",
                "error": host_error,
            }
        )

    # Serialize input as YAML if pyyaml available, else JSON
    try:
        import yaml

        input_content = yaml.dump(input, default_flow_style=False)
    except ImportError:
        input_content = json.dumps(input, indent=2)

    # Container args: -s step, -f - for stdin
    container_args = ["-s", plugin_step, "-f", "-"]

    # Local execution path: run podman directly when the
    # target is localhost (dev/CI without SSH keys).
    is_local = target in ("localhost", "127.0.0.1", "::1")

    if is_local:
        import shutil

        from providers.execution import AuditedSubprocessRunner

        logger.info("[arcaflow] Local execution: podman run %s", plugin_image)
        podman_path = shutil.which("podman")
        if not podman_path:
            return json.dumps(
                {
                    "status": "failed",
                    "exit_code": -1,
                    "run_id": f"arcaflow-{run_uuid}",
                    "error": "podman not found locally",
                }
            )

        proc = await AuditedSubprocessRunner().start(
            [
                podman_path,
                "run",
                "-i",
                "--rm",
                "--network=host",
                plugin_image,
                *container_args,
            ],
            stdin=input_content.encode(),
            mutating=True,
        )
        stdout_bytes, stderr_bytes = await proc.communicate()
        exit_code = proc.returncode or 0
        stdout_str = stdout_bytes.decode(errors="replace")
        stderr_str = stderr_bytes.decode(errors="replace")
    else:
        # Remote execution via SSH
        podman_check = await _ssh.run(target, "which podman", timeout=10)
        if podman_check.exit_code != 0:
            return json.dumps(
                {
                    "status": "failed",
                    "exit_code": -1,
                    "run_id": f"arcaflow-{run_uuid}",
                    "error": "podman not found on target host",
                }
            )

        input_path = f"/tmp/arcaflow-input-{run_uuid}.yaml"
        await _ssh.run(
            target,
            f"cat > {input_path} << 'ARCAEOF'\n{input_content}\nARCAEOF",
        )

        podman_args = [
            "podman",
            "run",
            "-i",
            "--rm",
            plugin_image,
            *container_args,
        ]
        cmd = f"cat {shlex.quote(input_path)} | {shlex.join(podman_args)} 2>&1"
        logger.info("[arcaflow] Executing plugin via SSH: %s", cmd)
        result = await _ssh.run_with_progress(
            target,
            cmd,
            progress_callback=_plugin_progress,
        )
        exit_code = result.exit_code
        stdout_str = result.stdout or ""
        stderr_str = result.stderr or ""

        # Clean up
        await _ssh.run(target, f"rm -f {input_path}", timeout=10)

    response: dict[str, Any] = {
        "status": "completed" if exit_code == 0 else "failed",
        "exit_code": exit_code,
        "run_id": f"arcaflow-{run_uuid}",
        "harness": "arcaflow-plugins",
        "plugin_image": plugin_image,
        "plugin_step": plugin_step,
        "message": (
            "Arcaflow plugin completed"
            if exit_code == 0
            else f"Arcaflow plugin failed (exit {exit_code})"
        ),
    }
    if exit_code == 0 and stdout_str:
        try:
            response["result_summary"] = json.loads(stdout_str)
        except json.JSONDecodeError:
            try:
                import yaml

                response["result_summary"] = yaml.safe_load(stdout_str)
            except Exception:
                response["result_summary"] = stdout_str[:3000]
    if exit_code != 0:
        response["output"] = stdout_str[-3000:] if stdout_str else ""
        response["error"] = stderr_str[-1000:] if stderr_str else ""
    return json.dumps(response)
