from __future__ import annotations

import asyncio
import logging
import re
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from providers.llm.base import ToolDefinition
from providers.ssh import SSHExecutor

_SKILLS_DIR = Path(__file__).parent.parent.parent / "skills"

logger = logging.getLogger(__name__)


def _parse_os_release(text: str) -> str:
    """Parse /etc/os-release output into a normalized OS string like 'rhel9' or 'fedora41'."""
    fields: dict[str, str] = {}
    for line in text.strip().splitlines():
        m = re.match(r'^(\w+)="?([^"]*)"?$', line)
        if m:
            fields[m.group(1)] = m.group(2)

    os_id = fields.get("ID", "unknown").lower()
    version = fields.get("VERSION_ID", "")

    # Normalize: "rhel" stays "rhel", "rocky" -> "rhel" (RHEL-compatible)
    rhel_compat = {"rocky", "almalinux", "ol", "scientific"}
    if os_id in rhel_compat:
        os_id = "rhel"

    major = version.split(".")[0] if version else ""
    return f"{os_id}{major}" if major else os_id


def _os_matches(detected: str, supported: list[str]) -> bool:
    """Check if detected OS matches any entry in supported list (prefix match)."""
    for s in supported:
        if detected.startswith(s) or s.startswith(detected):
            return True
    return False


def _summarize(results: dict[str, dict]) -> dict:
    """Wrap per-host results with a summary line."""
    total = len(results)
    success = sum(
        1
        for r in results.values()
        if r.get("status") in ("success", "ok", "already_installed", "ready")
        or r.get("all_met") is True
        or r.get("installed") is True
        or r.get("verified") is True
    )
    failed = total - success
    return {
        "results": results,
        "summary": f"{total} host(s): {success} success, {failed} failed",
    }


async def _gather_for_hosts(
    hosts: list[str],
    coro_fn,
    *args,
    **kwargs,
) -> dict[str, dict]:
    """Run coro_fn(host, *args, **kwargs) for each host concurrently."""
    coros = [coro_fn(h, *args, **kwargs) for h in hosts]
    raw = await asyncio.gather(*coros, return_exceptions=True)
    results: dict[str, dict] = {}
    for host, result in zip(hosts, raw):
        if isinstance(result, Exception):
            results[host] = {"status": "error", "message": str(result)}
        else:
            results[host] = result
    return results


def _filter_controller_only(
    hosts: list[str],
    controller_host: str,
    provisioning: dict,
    harness_name: str = "",
) -> tuple[list[str], dict[str, dict]]:
    """Filter hosts when controller_only_install is set.

    Returns (filtered_hosts, skipped_results).
    """
    if not provisioning.get("controller_only_install", True) or not controller_host:
        return hosts, {}

    filtered = [h for h in hosts if h == controller_host]
    skipped: dict[str, dict] = {}
    for h in hosts:
        if h != controller_host:
            skipped[h] = {
                "host": h,
                "harness": harness_name,
                "status": "skipped",
                "message": (
                    "controller_only_install: harness only installed on controller"
                ),
            }
    return filtered, skipped


async def validate_platform_contract(
    ssh: SSHExecutor,
    host: str,
    private_config: dict,
) -> dict:
    """Validate host OS, repos, and packages against platform_contract."""
    contract = private_config.get("platform_contract")
    if not contract:
        return {"status": "ok", "message": "No platform contract to validate"}

    result: dict[str, Any] = {
        "status": "ok",
        "detected_os": "",
        "os_match": True,
        "missing_repos": [],
        "missing_packages": [],
        "message": "",
    }
    failures = []

    # OS detection
    supported_os = contract.get("supported_os", [])
    if supported_os:
        os_result = await ssh.run(host, "cat /etc/os-release")
        if os_result.exit_code != 0:
            result["status"] = "failed"
            result["message"] = f"Could not detect OS on {host}: {os_result.stderr}"
            return result

        detected = _parse_os_release(os_result.stdout)
        result["detected_os"] = detected

        if not _os_matches(detected, supported_os):
            result["os_match"] = False
            result["status"] = "failed"
            failures.append(f"OS '{detected}' is not in supported list: {supported_os}")

    # Repo validation
    required_repos = contract.get("required_repos", [])
    if required_repos:
        repo_result = await ssh.run(
            host, "dnf repolist --enabled 2>/dev/null || yum repolist 2>/dev/null"
        )
        repo_output = repo_result.stdout.lower() if repo_result.exit_code == 0 else ""
        for repo in required_repos:
            if repo.lower() not in repo_output:
                result["missing_repos"].append(repo)
        if result["missing_repos"]:
            result["status"] = "failed"
            failures.append(f"Missing required repos: {result['missing_repos']}")

    # Package validation (soft warning, not fatal)
    required_packages = contract.get("required_packages", [])
    if required_packages:
        for pkg in required_packages:
            pkg_result = await ssh.run(
                host, f"which {pkg} 2>/dev/null || rpm -q {pkg} 2>/dev/null"
            )
            if pkg_result.exit_code != 0:
                result["missing_packages"].append(pkg)
        if result["missing_packages"]:
            logger.info(
                f"[provision] Missing packages on {host} (can be installed): "
                f"{result['missing_packages']}"
            )

    if failures:
        result["message"] = "; ".join(failures)
    elif result["missing_packages"]:
        result["message"] = (
            f"Platform compatible. Missing packages (installable): "
            f"{result['missing_packages']}"
        )
    else:
        result["message"] = "Platform contract satisfied"

    return result


async def _discover_crucible_token_files(
    ssh: SSHExecutor, host: str, install_path: str
) -> list[str]:
    """Read registries.json on the host and extract all referenced token file paths."""
    result = await ssh.run(
        host, f"cat {install_path}/config/registries.json 2>/dev/null"
    )
    if result.exit_code != 0 or not result.stdout.strip():
        return []

    try:
        import json as _json

        reg = _json.loads(result.stdout)
    except Exception:
        logger.warning(f"[provision] Could not parse registries.json on {host}")
        return []

    paths = []
    if reg.get("controller", {}).get("pull-token"):
        paths.append(reg["controller"]["pull-token"])
    pub = reg.get("engines", {}).get("public", {})
    if pub.get("push-token"):
        paths.append(pub["push-token"])
    if pub.get("quay", {}).get("refresh-expiration", {}).get("token-file"):
        paths.append(pub["quay"]["refresh-expiration"]["token-file"])
    priv = reg.get("engines", {}).get("private", {})
    if priv.get("tokens", {}).get("push"):
        paths.append(priv["tokens"]["push"])
    if priv.get("tokens", {}).get("pull"):
        paths.append(priv["tokens"]["pull"])
    if priv.get("quay", {}).get("refresh-expiration", {}).get("token-file"):
        paths.append(priv["quay"]["refresh-expiration"]["token-file"])
    for ue in reg.get("userenvs", []):
        if ue.get("pull-token"):
            paths.append(ue["pull-token"])

    return paths


async def cleanup_harness(
    ssh: SSHExecutor,
    host: str,
    harness_name: str,
    install_path: str | None = None,
    pre_uninstall_commands: list[str] | None = None,
) -> dict:
    """Remove a harness installation from a host."""
    path = install_path or f"/opt/{harness_name}"
    cleanup_details = []

    for cmd in pre_uninstall_commands or []:
        logger.info(f"[provision] Pre-uninstall on {host}: {cmd}")
        await ssh.run(host, cmd, timeout=120)

    if harness_name == "crucible":
        token_files = await _discover_crucible_token_files(ssh, host, path)
        if token_files:
            logger.info(
                f"[provision] Found {len(token_files)} token files in "
                f"registries.json on {host}"
            )

        await ssh.run(
            host,
            "podman ps -a --format '{{.Names}}' 2>/dev/null | grep '^crucible-'"
            " | xargs -r podman stop 2>/dev/null"
            " && podman ps -a --format '{{.Names}}' 2>/dev/null | grep '^crucible-'"
            " | xargs -r podman rm 2>/dev/null"
            " ; echo done",
            timeout=120,
        )
        cleanup_details.append("containers: stopped and removed")
        logger.info(f"[provision] Stopped crucible containers on {host}")

        for token_path in token_files:
            await ssh.run(host, f"rm -f {token_path}")
            cleanup_details.append(f"token: {token_path}")
        logger.info(f"[provision] Removed {len(token_files)} token files on {host}")

        for artifact in [
            "/usr/bin/crucible",
            "/etc/sysconfig/crucible",
            "/etc/profile.d/crucible_completions.sh",
        ]:
            await ssh.run(host, f"rm -f {artifact}")
        cleanup_details.append("system: symlinks, sysconfig, profile.d")

        await ssh.run(host, "rm -rf /root/.crucible", timeout=60)
        cleanup_details.append("config: /root/.crucible")

        # Preserve run results — only remove ancillary state
        # (logs, CDM index, container images, etc.), not the
        # run data that operators need for post-hoc analysis.
        await ssh.run(
            host,
            "find /var/lib/crucible -mindepth 1 -maxdepth 1"
            " -not -name 'run' -exec rm -rf {} +",
            timeout=120,
        )
        cleanup_details.append("data: /var/lib/crucible (run/ preserved)")

    logger.info(f"[provision] Removing {harness_name} install dir {path} on {host}")
    result = await ssh.run(host, f"rm -rf {path}", timeout=120)
    if result.exit_code != 0:
        return {
            "host": host,
            "harness": harness_name,
            "status": "failed",
            "cleanup_details": cleanup_details,
            "message": f"Failed to remove {path}: {result.stderr}",
        }

    await ssh.run(host, f"rm -rf {path}-moved-on-*", timeout=60)

    return {
        "host": host,
        "harness": harness_name,
        "status": "success",
        "cleanup_details": cleanup_details,
        "message": f"{harness_name} fully uninstalled from {host}",
    }


def get_provisioning_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="check_platform_contract",
            description=(
                "Check if hosts meet the platform requirements (OS, repos, packages) "
                "for a benchmark harness. Call this before attempting installation to "
                "verify compatibility. Returns detected OS, missing repos, and missing "
                "packages per host. OS or repo mismatches are hard failures; missing "
                "packages are warnings (they can be installed)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "hosts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of IPs or hostnames",
                    },
                    "harness_name": {
                        "type": "string",
                        "description": "Harness name (e.g., 'crucible', 'zathras')",
                    },
                    "user": {
                        "type": "string",
                        "description": "SSH user (default: root)",
                    },
                },
                "required": ["hosts", "harness_name"],
            },
        ),
        ToolDefinition(
            name="check_host_prerequisites",
            description=(
                "Check if hosts have the required software installed "
                "(podman, git, jq, curl). Returns the status of each "
                "prerequisite per host."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "hosts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of IPs or hostnames",
                    },
                    "user": {
                        "type": "string",
                        "description": "SSH user (default: root)",
                    },
                },
                "required": ["hosts"],
            },
        ),
        ToolDefinition(
            name="install_packages",
            description=(
                "Install required packages on multiple hosts via the system "
                "package manager. Each target specifies a host and its packages."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "targets": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "host": {"type": "string"},
                                "packages": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                            },
                            "required": ["host", "packages"],
                        },
                        "description": "List of {host, packages} targets",
                    },
                    "user": {
                        "type": "string",
                        "description": "SSH user (default: root)",
                    },
                },
                "required": ["targets"],
            },
        ),
        ToolDefinition(
            name="install_harness",
            description=(
                "Install the benchmark harness on multiple hosts. Uses private "
                "skill config to determine the install method: 'public_install' "
                "downloads and runs the upstream installer with skill-driven flags; "
                "'git_clone' clones from a URL and runs install.sh. Validates and "
                "deploys required secrets from the install_contract before running "
                "the installer."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "hosts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of target hosts",
                    },
                    "harness_name": {
                        "type": "string",
                        "description": "Harness name (e.g., 'crucible', 'zathras')",
                    },
                    "user": {
                        "type": "string",
                        "description": "SSH user (default: root)",
                    },
                    "branch": {
                        "type": "string",
                        "description": "Specific git branch or release tag.",
                    },
                    "controller_host": {
                        "type": "string",
                        "description": (
                            "The controller IP — when controller_only_install "
                            "is true, only this host gets the harness."
                        ),
                    },
                },
                "required": ["hosts", "harness_name"],
            },
        ),
        ToolDefinition(
            name="verify_harness_install",
            description=(
                "Verify that the benchmark harness is correctly installed and "
                "functional on multiple hosts. Uses private skill config's "
                "verify_command."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "hosts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of target hosts",
                    },
                    "harness_name": {
                        "type": "string",
                        "description": "Harness name (e.g., 'crucible', 'zathras')",
                    },
                    "user": {
                        "type": "string",
                        "description": "SSH user (default: root)",
                    },
                    "install_path": {
                        "type": "string",
                        "description": "Install path override",
                    },
                    "controller_host": {
                        "type": "string",
                        "description": (
                            "The controller IP — when controller_only_install "
                            "is true, only this host gets verified."
                        ),
                    },
                },
                "required": ["hosts", "harness_name"],
            },
        ),
        ToolDefinition(
            name="check_existing_install",
            description=(
                "Check if the benchmark harness is already installed on multiple "
                "hosts. Returns whether an installation exists and its version "
                "info per host."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "hosts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of target hosts",
                    },
                    "harness_name": {
                        "type": "string",
                        "description": "Harness name (e.g., 'crucible', 'zathras')",
                    },
                    "install_path": {
                        "type": "string",
                        "description": "Path to check",
                    },
                    "user": {
                        "type": "string",
                        "description": "SSH user (default: root)",
                    },
                    "controller_host": {
                        "type": "string",
                        "description": (
                            "The controller IP — when controller_only_install "
                            "is true, only this host gets checked."
                        ),
                    },
                },
                "required": ["hosts", "harness_name"],
            },
        ),
        ToolDefinition(
            name="update_install",
            description=(
                "Update an existing benchmark harness installation on multiple "
                "hosts. Runs the harness-specific update command from private config."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "hosts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of target hosts",
                    },
                    "harness_name": {
                        "type": "string",
                        "description": "Harness name (e.g., 'crucible', 'zathras')",
                    },
                    "install_path": {
                        "type": "string",
                        "description": "Install path override",
                    },
                    "user": {
                        "type": "string",
                        "description": "SSH user (default: root)",
                    },
                    "controller_host": {
                        "type": "string",
                        "description": (
                            "The controller IP — when controller_only_install "
                            "is true, only this host gets updated."
                        ),
                    },
                },
                "required": ["hosts", "harness_name"],
            },
        ),
        ToolDefinition(
            name="uninstall_harness",
            description=(
                "Remove an existing benchmark harness installation from multiple "
                "hosts. Must be called BEFORE install_harness when reinstalling."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "hosts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of target hosts",
                    },
                    "harness_name": {
                        "type": "string",
                        "description": "Harness name (e.g., 'crucible', 'zathras')",
                    },
                    "user": {
                        "type": "string",
                        "description": "SSH user (default: root)",
                    },
                    "controller_host": {
                        "type": "string",
                        "description": (
                            "The controller IP — when controller_only_install "
                            "is true, only this host gets uninstalled."
                        ),
                    },
                },
                "required": ["hosts", "harness_name"],
            },
        ),
        ToolDefinition(
            name="install_k3s",
            description=(
                "Install K3s (lightweight Kubernetes) on multiple hosts. K3s "
                "provides a single-node Kubernetes cluster that crucible uses "
                "for kube endpoints."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "hosts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of target host IPs or hostnames",
                    },
                    "user": {
                        "type": "string",
                        "description": "SSH user (default: root)",
                    },
                },
                "required": ["hosts"],
            },
        ),
        ToolDefinition(
            name="ensure_prerequisites",
            description=(
                "Check and install missing prerequisites on all hosts in one "
                "call. Harness prerequisites (podman, git, jq, curl) are "
                "checked and installed only on the controller_host. Extra "
                "packages (e.g., nmap-ncat from user directives) are checked "
                "and installed on ALL hosts. Use this instead of calling "
                "check_host_prerequisites then install_packages separately."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "hosts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of all host IPs to process",
                    },
                    "extra_packages": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Additional packages to install on ALL hosts "
                            "(e.g., ['nmap-ncat'] from user directives)"
                        ),
                    },
                    "controller_host": {
                        "type": "string",
                        "description": (
                            "The controller IP — harness prereqs (podman, "
                            "git, etc.) are only installed here. If empty, "
                            "only extra_packages are installed."
                        ),
                    },
                    "user": {
                        "type": "string",
                        "description": "SSH user (default: root)",
                    },
                },
                "required": ["hosts"],
            },
        ),
        ToolDefinition(
            name="ensure_harness_installed",
            description=(
                "Check if harness is installed on each host, install where "
                "missing, and verify all installations. Combines "
                "check_existing_install + install_harness + verify_harness_install "
                "into one batched call. Returns per-host status: "
                "already_installed, success, or failure details."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "hosts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of target hosts",
                    },
                    "harness_name": {
                        "type": "string",
                        "description": "Harness name (e.g., 'crucible', 'zathras')",
                    },
                    "user": {
                        "type": "string",
                        "description": "SSH user (default: root)",
                    },
                    "branch": {
                        "type": "string",
                        "description": "Specific git branch or release tag.",
                    },
                    "install_path": {
                        "type": "string",
                        "description": "Install path override",
                    },
                    "controller_host": {
                        "type": "string",
                        "description": (
                            "The controller IP — when controller_only_install "
                            "is true, only this host gets the harness."
                        ),
                    },
                },
                "required": ["hosts", "harness_name"],
            },
        ),
        ToolDefinition(
            name="get_private_config",
            description=(
                "Fetch private configuration for a benchmark harness. "
                "Returns organization-specific data like install method, "
                "repo paths, registry URLs, and constraints (supported OS, "
                "prerequisites). Use key='constraints' to check OS and "
                "platform requirements before attempting installation."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "harness_name": {
                        "type": "string",
                        "description": "Harness name (e.g., 'crucible', 'zathras')",
                    },
                    "key": {
                        "type": "string",
                        "description": "Config key to fetch",
                    },
                },
                "required": ["harness_name", "key"],
            },
        ),
        ToolDefinition(
            name="request_clarification",
            description="Ask the user for clarification. Pauses the ticket for human input.",
            input_schema={
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "Question to ask"},
                },
                "required": ["question"],
            },
        ),
        ToolDefinition(
            name="list_skill_docs",
            description=(
                "List available skill documents for a topic. "
                "Use 'general' for host-tuning, connectivity, and network-perf guides. "
                "Use a harness name (e.g. 'crucible') for harness-specific docs."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "harness": {
                        "type": "string",
                        "description": "Topic/harness name (e.g. 'general', 'crucible')",
                    },
                },
                "required": ["harness"],
            },
        ),
        ToolDefinition(
            name="read_skill",
            description=(
                "Read a skill document. Always read 'general/host-tuning.md' "
                "before applying any host tuning — it defines the required tool "
                "ordering, BBR+fq dependency, and irqbalance strategy."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "harness": {
                        "type": "string",
                        "description": "Topic/harness name (e.g. 'general', 'crucible')",
                    },
                    "filename": {
                        "type": "string",
                        "description": "Document filename (e.g. 'host-tuning.md')",
                    },
                },
                "required": ["harness", "filename"],
            },
        ),
        ToolDefinition(
            name="read_skills",
            description=(
                "Read multiple skill documents in one call. Use this instead of calling "
                "read_skill repeatedly — saves iterations when you need several docs at once "
                "(e.g. general/host-tuning.md + general/network-manager.md in one call)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "docs": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "harness": {"type": "string"},
                                "filename": {"type": "string"},
                            },
                            "required": ["harness", "filename"],
                        },
                        "description": "List of {harness, filename} pairs to read",
                    },
                },
                "required": ["docs"],
            },
        ),
        ToolDefinition(
            name="tune_hosts",
            description=(
                "Apply tuning to multiple hosts in one call. Runs tune_nic → tune_tcp → "
                "pin_irq → disable_firewall (if requested) → verify_host_tuning for each "
                "host concurrently. Use this instead of calling individual tuning tools "
                "one host at a time — saves multiple iterations. "
                "Always read general/host-tuning.md before calling this."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "targets": {
                        "type": "array",
                        "description": "Per-host tuning specifications",
                        "items": {
                            "type": "object",
                            "properties": {
                                "host": {"type": "string"},
                                "interface": {"type": "string"},
                                "channels": {"type": "integer"},
                                "congestion_control": {"type": "string"},
                                "qdisc": {"type": "string"},
                                "irq_cpu": {"type": "integer"},
                                "irqbalance_mode": {
                                    "type": "string",
                                    "enum": ["ban_irq", "ban_cpu", "disable"],
                                },
                                "mtu": {"type": "integer"},
                                "disable_firewall": {"type": "boolean"},
                            },
                            "required": ["host", "interface"],
                        },
                    },
                },
                "required": ["targets"],
            },
        ),
        ToolDefinition(
            name="disable_firewall",
            description=(
                "Flush all iptables/ip6tables rules and set default policies to ACCEPT "
                "on a host. Use this on dedicated benchmark hosts before running "
                "connectivity checks or benchmarks — fresh lab hosts often block "
                "benchmark ports (30002/30003 for uperf, etc.) by default. "
                "Do NOT call this on shared or production hosts."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "Hostname or IP"},
                    "user": {
                        "type": "string",
                        "description": "SSH user (default: root)",
                    },
                },
                "required": ["host"],
            },
        ),
        ToolDefinition(
            name="open_firewall_port",
            description=(
                "Open specific TCP/UDP ports in iptables on a host without flushing "
                "all rules. Use this when the host firewall should stay active but "
                "benchmark ports need to be explicitly allowed. "
                "For dedicated benchmark hosts with no firewall requirements, "
                "disable_firewall is simpler."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "Hostname or IP"},
                    "ports": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "TCP port numbers to open (e.g. [30002, 30003] for uperf)",
                    },
                    "protocol": {
                        "type": "string",
                        "enum": ["tcp", "udp", "both"],
                        "description": "Protocol to open (default: tcp)",
                    },
                    "user": {
                        "type": "string",
                        "description": "SSH user (default: root)",
                    },
                },
                "required": ["host", "ports"],
            },
        ),
        ToolDefinition(
            name="tune_nic",
            description=(
                "Apply ethtool NIC settings on a host for benchmark preparation. "
                "Sets queue/channel count and optionally ring buffer sizes and offloads. "
                "MUST be called before pin_irq — changing channel count alters which "
                "IRQ numbers the NIC has. Returns before/after state for each setting."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "Hostname or IP"},
                    "interface": {
                        "type": "string",
                        "description": "NIC interface name (e.g. eno16695np0)",
                    },
                    "channels": {
                        "type": "integer",
                        "description": "Combined queue/channel count (default: 1 for IRQ pinning)",
                    },
                    "ring_rx": {
                        "type": "integer",
                        "description": "RX ring buffer size (optional)",
                    },
                    "ring_tx": {
                        "type": "integer",
                        "description": "TX ring buffer size (optional)",
                    },
                    "offloads": {
                        "type": "object",
                        "description": 'Offload flags to set, e.g. {"gro": "on", "lro": "off"} (optional)',
                    },
                    "user": {
                        "type": "string",
                        "description": "SSH user (default: root)",
                    },
                    "ssh_key_path": {
                        "type": "string",
                        "description": "Path to SSH private key",
                    },
                },
                "required": ["host", "interface"],
            },
        ),
        ToolDefinition(
            name="tune_tcp",
            description=(
                "Apply TCP/network stack settings on a host. "
                "Sets congestion control and qdisc via sysctl AND applies the qdisc "
                "directly to existing interfaces via 'tc qdisc replace' — the sysctl "
                "alone only affects newly-created interfaces. "
                "BBR requires fq (not fq_codel) for per-flow pacing; always set both. "
                "Optionally sets socket buffer sizes (rmem_max, wmem_max)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "Hostname or IP"},
                    "interface": {
                        "type": "string",
                        "description": "NIC interface to apply qdisc to via tc (e.g. eno16695np0). Required when setting qdisc.",
                    },
                    "congestion_control": {
                        "type": "string",
                        "description": "TCP congestion control algorithm (e.g. 'bbr', 'cubic')",
                    },
                    "qdisc": {
                        "type": "string",
                        "description": "Qdisc to set (e.g. 'fq' for BBR). Applied via both sysctl and tc qdisc replace.",
                    },
                    "rmem_max": {
                        "type": "integer",
                        "description": "Max socket receive buffer size in bytes (e.g. 134217728 for 128MB)",
                    },
                    "wmem_max": {
                        "type": "integer",
                        "description": "Max socket send buffer size in bytes (e.g. 134217728 for 128MB)",
                    },
                    "extra_sysctls": {
                        "type": "object",
                        "description": "Additional net.* sysctl key/value pairs to set (optional)",
                    },
                    "user": {
                        "type": "string",
                        "description": "SSH user (default: root)",
                    },
                    "ssh_key_path": {
                        "type": "string",
                        "description": "Path to SSH private key",
                    },
                },
                "required": ["host"],
            },
        ),
        ToolDefinition(
            name="pin_irq",
            description=(
                "Pin NIC IRQ(s) to a specific CPU and coordinate irqbalance so the "
                "pin is not overridden during a run. Must be called after tune_nic. "
                "irqbalance_mode controls how irqbalance is handled: "
                "'ban_irq' (default) adds the IRQ to IRQBALANCE_BANNED_INTERRUPTS so "
                "irqbalance keeps running for other IRQs but won't touch this one; "
                "'ban_cpu' adds the CPU to IRQBALANCE_BANNED_CPUS; "
                "'disable' masks and stops irqbalance entirely."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "Hostname or IP"},
                    "interface": {
                        "type": "string",
                        "description": "NIC interface name",
                    },
                    "cpu": {
                        "type": "integer",
                        "description": "CPU core number to pin IRQ to",
                    },
                    "irqbalance_mode": {
                        "type": "string",
                        "enum": ["ban_irq", "ban_cpu", "disable"],
                        "description": "How to prevent irqbalance from overriding the pin (default: ban_irq)",
                    },
                    "user": {
                        "type": "string",
                        "description": "SSH user (default: root)",
                    },
                    "ssh_key_path": {
                        "type": "string",
                        "description": "Path to SSH private key",
                    },
                },
                "required": ["host", "interface", "cpu"],
            },
        ),
        ToolDefinition(
            name="verify_host_tuning",
            description=(
                "Verify that host tuning settings match expected values. "
                "Re-reads sysctl, ethtool channel count, IRQ CPU affinity, and "
                "irqbalance status. Returns pass/fail per parameter with actual values. "
                "Call after tuning to confirm settings applied, and after benchmarks "
                "to detect drift (e.g. irqbalance overriding a pin mid-run)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "Hostname or IP"},
                    "interface": {
                        "type": "string",
                        "description": "NIC interface name",
                    },
                    "expected": {
                        "type": "object",
                        "description": (
                            "Expected values to check. Supported keys: "
                            "congestion_control, qdisc, channels, irq_cpu, irqbalance_mode. "
                            "Omit keys you don't want checked."
                        ),
                    },
                    "user": {
                        "type": "string",
                        "description": "SSH user (default: root)",
                    },
                    "ssh_key_path": {
                        "type": "string",
                        "description": "Path to SSH private key",
                    },
                },
                "required": ["host", "interface"],
            },
        ),
        ToolDefinition(
            name="submit_provisioning_result",
            description="Submit the provisioning result when all hosts are prepared.",
            input_schema={
                "type": "object",
                "properties": {
                    "provisioning_complete": {"type": "boolean"},
                    "hosts_provisioned": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "harness_version": {"type": "string"},
                    "harness_name": {"type": "string"},
                    "configuration_applied": {"type": "object"},
                    "k3s_installed": {
                        "type": "boolean",
                        "description": "Whether K3s was installed",
                    },
                    "k3s_version": {
                        "type": "string",
                        "description": "K3s version string (if installed)",
                    },
                    "notes": {"type": "string"},
                },
                "required": ["provisioning_complete", "hosts_provisioned"],
            },
        ),
        ToolDefinition(
            name="nm_set_mtu",
            description=(
                "Set the MTU on a network interface persistently via NetworkManager. "
                "Using 'ip link set mtu' is NOT persistent — NM overrides it on "
                "connection events. This tool modifies the NM connection profile and "
                "brings it up. Read skills/general/network-manager.md before calling. "
                "MTU 9000 requires end-to-end switch support — do not set it unless "
                "the user explicitly requests jumbo frames."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "Hostname or IP"},
                    "interface": {
                        "type": "string",
                        "description": "Interface name (e.g. eno16695np0)",
                    },
                    "mtu": {
                        "type": "integer",
                        "description": "MTU value (e.g. 1500 or 9000)",
                    },
                    "user": {
                        "type": "string",
                        "description": "SSH user (default: root)",
                    },
                },
                "required": ["host", "interface", "mtu"],
            },
        ),
        ToolDefinition(
            name="nm_set_ip",
            description=(
                "Configure a static IP address on an interface via NetworkManager. "
                "Used when a ticket requests a private test network. "
                "Modifies the NM connection profile and brings it up."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "Hostname or IP"},
                    "interface": {"type": "string", "description": "Interface name"},
                    "ip_cidr": {
                        "type": "string",
                        "description": "IP address with prefix (e.g. 172.16.0.1/24)",
                    },
                    "gateway": {
                        "type": "string",
                        "description": "Gateway IP (optional)",
                    },
                    "dns": {
                        "type": "string",
                        "description": "DNS server IP (optional)",
                    },
                    "user": {
                        "type": "string",
                        "description": "SSH user (default: root)",
                    },
                },
                "required": ["host", "interface", "ip_cidr"],
            },
        ),
        ToolDefinition(
            name="nm_set_dhcp",
            description="Switch an interface to DHCP via NetworkManager.",
            input_schema={
                "type": "object",
                "properties": {
                    "host": {"type": "string"},
                    "interface": {"type": "string"},
                    "user": {"type": "string"},
                },
                "required": ["host", "interface"],
            },
        ),
        ToolDefinition(
            name="nm_show_connection",
            description=(
                "Show the current NetworkManager connection profile for an interface. "
                "Returns IP method, addresses, MTU, and connection name. "
                "Use this to audit actual interface configuration before a benchmark."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "host": {"type": "string"},
                    "interface": {"type": "string"},
                    "user": {"type": "string"},
                },
                "required": ["host", "interface"],
            },
        ),
        ToolDefinition(
            name="nm_verify_interface",
            description=(
                "Verify that a network interface matches expected configuration. "
                "Checks live state (ip link) not just the NM profile. "
                "Returns pass/fail per parameter: mtu, ip_address, state."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "host": {"type": "string"},
                    "interface": {"type": "string"},
                    "expected_mtu": {
                        "type": "integer",
                        "description": "Expected MTU (optional)",
                    },
                    "expected_ip": {
                        "type": "string",
                        "description": "Expected IP address without prefix (optional)",
                    },
                    "user": {"type": "string"},
                },
                "required": ["host", "interface"],
            },
        ),
    ]


def create_provisioning_tool_handlers(
    skill_provider,
    secrets_provider=None,
    request_clarification_fn=None,
) -> tuple[dict[str, Any], SSHExecutor]:
    ssh = SSHExecutor(user="root")

    async def _validate_and_deploy_contract(host: str, private_config: dict) -> dict:
        contract = private_config.get("install_contract")
        if not contract:
            return {
                "status": "ok",
                "deployed_files": [],
                "message": "No install contract",
            }

        secrets_map = private_config.get("secrets", {})
        secret_files = contract.get("secret_files", [])
        missing = []
        resolved = []

        async with AsyncExitStack() as stack:
            for entry in secret_files:
                secret_key = entry["secret_key"]
                secret_path = secrets_map.get(secret_key)
                required = entry.get("required", True)
                description = entry.get("description", secret_key)

                if not secret_path:
                    if required:
                        missing.append(
                            f"{secret_key}: no path in secrets config",
                        )
                    continue

                if secrets_provider is None:
                    if required:
                        missing.append(
                            f"{secret_key}: no secrets provider configured",
                        )
                    continue

                local_path = await stack.enter_async_context(
                    secrets_provider.secret_file(secret_path),
                )
                if local_path is None:
                    if required:
                        missing.append(
                            f"{description} ({secret_path}): "
                            f"not found in secrets store",
                        )
                    continue

                resolved.append(
                    {
                        "secret_key": secret_key,
                        "local_path": str(local_path),
                        "remote_path": entry["remote_path"],
                        "description": description,
                    }
                )

            if missing:
                return {
                    "status": "failed",
                    "message": (
                        f"Install contract validation failed. "
                        f"Missing {len(missing)} required input(s):\n"
                        + "\n".join(f"  - {m}" for m in missing)
                    ),
                    "missing": missing,
                }

            deployed = []
            for item in resolved:
                scp_result = await ssh.copy_to(
                    host,
                    item["local_path"],
                    item["remote_path"],
                )
                if scp_result.exit_code != 0:
                    return {
                        "status": "failed",
                        "message": (
                            f"Failed to deploy {item['description']} to "
                            f"{host}:{item['remote_path']}: "
                            f"{scp_result.stderr}"
                        ),
                    }
                deployed.append(
                    f"{item['secret_key']} -> {item['remote_path']}",
                )
                logger.info(
                    f"[provision] Deployed {item['secret_key']} to "
                    f"{host}:{item['remote_path']}",
                )

        for cmd in contract.get("pre_install_commands", []):
            logger.info(f"[provision] Contract pre-install on {host}: {cmd}")
            await ssh.run(host, cmd, timeout=60)

        return {
            "status": "ok",
            "deployed_files": deployed,
            "message": f"Contract satisfied: {len(deployed)} file(s) deployed",
        }

    # --- Single-host helper functions ---

    async def _check_platform_contract_one(host: str, private_config: dict) -> dict:
        return await validate_platform_contract(ssh, host, private_config)

    async def _check_host_prerequisites_one(host: str) -> dict:
        prereqs = {}
        for cmd in ["podman", "git", "jq", "curl"]:
            result = await ssh.run(
                host,
                f"which {cmd} 2>/dev/null && {cmd} --version 2>/dev/null | head -1",
            )
            if result.exit_code == 0 and result.stdout.strip():
                lines = result.stdout.strip().split("\n")
                prereqs[cmd] = {
                    "installed": True,
                    "version": lines[-1] if len(lines) > 1 else lines[0],
                }
            else:
                prereqs[cmd] = {"installed": False, "version": None}

        all_met = all(p["installed"] for p in prereqs.values())
        return {
            "host": host,
            "prerequisites": prereqs,
            "all_met": all_met,
            "message": f"All prerequisites met on {host}"
            if all_met
            else f"Missing prerequisites on {host}",
        }

    async def _install_packages_one(host: str, packages: list[str]) -> dict:
        pkg_list = " ".join(packages)
        result = await ssh.run(host, f"dnf install -y {pkg_list}", timeout=300)
        return {
            "host": host,
            "packages": packages,
            "status": "success" if result.exit_code == 0 else "failed",
            "exit_code": result.exit_code,
            "output": result.stdout or "",
            "error": result.stderr or "",
        }

    async def _install_harness_one(
        host: str,
        harness_name: str,
        private_config: dict,
        provisioning: dict,
        constraints: dict,
        branch: str = "",
    ) -> dict:
        install_method = provisioning.get("install_method", "git_clone")
        target_path = provisioning.get("install_target_path", f"/opt/{harness_name}")

        platform_result = await validate_platform_contract(ssh, host, private_config)
        if platform_result["status"] == "failed":
            return {
                "host": host,
                "harness": harness_name,
                "status": "platform_incompatible",
                "message": platform_result["message"],
                "detected_os": platform_result.get("detected_os"),
            }

        contract_result = await _validate_and_deploy_contract(host, private_config)
        if contract_result["status"] == "failed":
            return {
                "host": host,
                "harness": harness_name,
                "status": "contract_failed",
                "message": contract_result["message"],
                "missing": contract_result.get("missing", []),
            }

        if install_method == "public_install":
            installer_url = provisioning.get("installer_url")
            if not installer_url:
                return {
                    "host": host,
                    "status": "failed",
                    "message": (
                        f"No installer_url in private config for {harness_name}"
                    ),
                }

            installer_path = "/tmp/harness-install.sh"
            dl_result = await ssh.run(
                host,
                f"curl --fail --silent --output {installer_path} {installer_url}"
                f" && chmod +x {installer_path}",
                timeout=60,
            )
            if dl_result.exit_code != 0:
                return {
                    "host": host,
                    "status": "failed",
                    "message": f"Failed to download installer: {dl_result.stderr}",
                }

            flags = provisioning.get("install_flags", {})
            flag_parts = []
            for flag, value in flags.items():
                if value is None:
                    flag_parts.append(f"--{flag}")
                else:
                    flag_parts.append(f"--{flag} {value}")
            if branch and branch.lower() not in ("latest", "default"):
                flag_parts.append(f"--release {branch}")
            flags_str = " ".join(flag_parts)

            cmd = f"{installer_path} {flags_str}"
            result = await ssh.run(host, cmd, timeout=1800)

            if result.exit_code != 0:
                return {
                    "host": host,
                    "harness": harness_name,
                    "status": "failed",
                    "exit_code": result.exit_code,
                    "install_path": target_path,
                    "output": result.stdout or "",
                    "error": result.stderr or "",
                    "message": f"Install failed (exit {result.exit_code})",
                }

            for post_cmd in provisioning.get("post_install_commands", []):
                post_result = await ssh.run(host, post_cmd, timeout=120)
                if post_result.exit_code != 0:
                    logger.warning(
                        f"[provision] Post-install command failed: {post_result.stderr}"
                    )

            await ssh.run(host, f"rm -f {installer_path}")

            return {
                "host": host,
                "harness": harness_name,
                "status": "success",
                "exit_code": 0,
                "install_path": target_path,
                "constraints": constraints,
                "contract": contract_result.get("deployed_files", []),
                "output": result.stdout or "",
                "message": f"{harness_name} installed via public installer",
            }

        if install_method == "git_clone":
            git_url = provisioning.get("git_url")
            if not git_url:
                return {
                    "host": host,
                    "status": "failed",
                    "message": (f"No git_url in private config for {harness_name}"),
                }

            pre_install_steps = provisioning.get("pre_install_steps", [])
            for step in pre_install_steps:
                pre_result = await ssh.run(host, step, timeout=300)
                if pre_result.exit_code != 0:
                    logger.warning(
                        f"[provision] Pre-install step failed: {pre_result.stderr}"
                    )

            branch_flag = f"-b {branch}" if branch else ""
            await ssh.run(host, f"rm -rf {target_path}")
            result = await ssh.run(
                host,
                f"git clone {branch_flag} {git_url} {target_path}",
                timeout=300,
            )
            if result.exit_code != 0:
                return {
                    "host": host,
                    "status": "failed",
                    "message": f"Git clone failed: {result.stderr}",
                }

            install_cmd = provisioning.get("run_install_as_root")
            if not install_cmd:
                install_script = provisioning.get("install_script", "install.sh")
                install_cmd = f"./{install_script}"
            cmd = f"cd {target_path} && {install_cmd}"
            result = await ssh.run(host, cmd, timeout=900)

            return {
                "host": host,
                "harness": harness_name,
                "status": "success" if result.exit_code == 0 else "failed",
                "exit_code": result.exit_code,
                "install_path": target_path,
                "constraints": constraints,
                "contract": contract_result.get("deployed_files", []),
                "output": result.stdout or "",
                "error": result.stderr or "",
                "message": f"{harness_name} installed"
                if result.exit_code == 0
                else f"Install failed (exit {result.exit_code})",
            }

        return {
            "host": host,
            "status": "failed",
            "message": (
                f"Unknown install_method '{install_method}' for {harness_name}"
            ),
        }

    async def _verify_harness_install_one(
        host: str,
        harness_name: str,
        provisioning: dict,
        install_path: str = "",
    ) -> dict:
        path = install_path or provisioning.get(
            "install_target_path", f"/opt/{harness_name}"
        )
        verify_cmd = provisioning.get(
            "verify_command", f"{path}/bin/{harness_name} help"
        )

        result = await ssh.run(host, verify_cmd)
        return {
            "host": host,
            "harness": harness_name,
            "verified": result.exit_code == 0,
            "install_path": path,
            "output": result.stdout[:500] if result.stdout else "",
            "error": result.stderr[:500] if result.stderr else "",
            "message": f"{harness_name} verified"
            if result.exit_code == 0
            else f"Verification failed: {result.stderr[:200]}",
        }

    async def _check_existing_install_one(
        host: str,
        harness_name: str,
        provisioning: dict,
        install_path: str = "",
    ) -> dict:
        path = install_path or provisioning.get(
            "install_target_path", f"/opt/{harness_name}"
        )
        verify_cmd = provisioning.get(
            "verify_command", f"{path}/bin/{harness_name} help"
        )

        result = await ssh.run(host, f"{verify_cmd} > /dev/null 2>&1")
        if result.exit_code == 0:
            version_result = await ssh.run(
                host, f"cd {path} && git log --oneline -1 2>/dev/null"
            )
            return {
                "host": host,
                "harness": harness_name,
                "installed": True,
                "install_path": path,
                "version": version_result.stdout.strip()
                if version_result.exit_code == 0
                else "unknown",
                "message": f"{harness_name} is already installed at {path}",
            }
        return {
            "host": host,
            "harness": harness_name,
            "installed": False,
            "install_path": path,
            "exit_code": result.exit_code,
            "stderr": result.stderr[:500] if result.stderr else "",
            "message": (
                f"No {harness_name} installation found at {path} "
                f"(exit_code={result.exit_code})"
            ),
        }

    async def _update_install_one(
        host: str,
        harness_name: str,
        provisioning: dict,
        install_path: str = "",
    ) -> dict:
        path = install_path or provisioning.get(
            "install_target_path", f"/opt/{harness_name}"
        )
        update_cmd = provisioning.get("update_command", f"cd {path} && git pull")

        result = await ssh.run(host, update_cmd, timeout=600)
        return {
            "host": host,
            "harness": harness_name,
            "status": "success" if result.exit_code == 0 else "failed",
            "exit_code": result.exit_code,
            "output": result.stdout or "",
            "error": result.stderr or "",
            "message": "Update completed"
            if result.exit_code == 0
            else f"Update failed (exit {result.exit_code})",
        }

    async def _install_k3s_one(host: str) -> dict:
        selinux_result = await ssh.run(host, "getenforce 2>/dev/null")
        if (
            selinux_result.exit_code == 0
            and selinux_result.stdout.strip() == "Enforcing"
        ):
            await ssh.run(host, "setenforce 0")

        result = await ssh.run(host, "curl -sfL https://get.k3s.io | sh -", timeout=300)
        if result.exit_code != 0:
            return {
                "host": host,
                "status": "failed",
                "message": f"K3s install failed: {result.stderr or ''}",
            }

        for _attempt in range(12):
            check = await ssh.run(host, "k3s kubectl cluster-info 2>/dev/null")
            if check.exit_code == 0:
                break
            await ssh.run(host, "sleep 5")
        else:
            return {
                "host": host,
                "status": "failed",
                "message": "K3s API server did not become ready within 60s",
            }

        await ssh.run(
            host,
            "k3s kubectl wait --for=condition=ready pod -l k8s-app=kube-dns "
            "-n kube-system --timeout=120s",
            timeout=150,
        )
        await ssh.run(
            host,
            "mkdir -p /root/.kube && "
            "ln -sf /etc/rancher/k3s/k3s.yaml /root/.kube/config",
        )

        kubectl_check = await ssh.run(host, "test -x /usr/local/bin/kubectl")
        if kubectl_check.exit_code != 0:
            await ssh.run(host, "ln -sf /usr/local/bin/k3s /usr/local/bin/kubectl")

        self_ssh_ok = False
        keygen = await ssh.run(
            host,
            "test -f /root/.ssh/id_rsa || "
            'ssh-keygen -t rsa -b 4096 -f /root/.ssh/id_rsa -C "k3s-self-ssh" -N ""',
        )
        if keygen.exit_code == 0:
            await ssh.run(
                host,
                "cat /root/.ssh/id_rsa.pub >> /root/.ssh/authorized_keys && "
                "chmod 600 /root/.ssh/authorized_keys && "
                "sort -u /root/.ssh/authorized_keys -o /root/.ssh/authorized_keys",
            )
            verify = await ssh.run(
                host,
                "ssh -o StrictHostKeyChecking=accept-new "
                "-o BatchMode=yes localhost hostname",
                timeout=15,
            )
            self_ssh_ok = verify.exit_code == 0

        node_result = await ssh.run(host, "kubectl get nodes -o wide --no-headers")
        version_result = await ssh.run(host, "k3s --version 2>/dev/null | head -1")

        return {
            "host": host,
            "status": "success",
            "k3s_version": version_result.stdout.strip()
            if version_result.exit_code == 0
            else "unknown",
            "node_info": node_result.stdout.strip()
            if node_result.exit_code == 0
            else "",
            "kubeconfig_path": "/root/.kube/config",
            "self_ssh": self_ssh_ok,
            "message": "K3s installed and cluster ready",
        }

    async def _ensure_harness_one(
        host: str,
        harness_name: str,
        private_config: dict,
        provisioning: dict,
        constraints: dict,
        branch: str = "",
        install_path: str = "",
    ) -> dict:
        existing = await _check_existing_install_one(
            host, harness_name, provisioning, install_path
        )
        if existing.get("installed"):
            return {
                "host": host,
                "harness": harness_name,
                "status": "already_installed",
                "install_path": existing.get("install_path", ""),
                "version": existing.get("version", "unknown"),
                "message": f"{harness_name} already installed on {host}",
            }

        install_result = await _install_harness_one(
            host, harness_name, private_config, provisioning, constraints, branch
        )
        if install_result.get("status") not in ("success", "ready"):
            return install_result

        verify_result = await _verify_harness_install_one(
            host, harness_name, provisioning, install_path
        )
        if not verify_result.get("verified"):
            verify_result["status"] = "install_succeeded_verify_failed"
            return verify_result

        verify_result["status"] = "success"
        return verify_result

    # --- Batched handler functions ---

    async def check_platform_contract(
        hosts: list[str], harness_name: str, user: str = "root"
    ) -> dict:
        private_config = await skill_provider.get_all_private_config(harness_name)
        results = await _gather_for_hosts(
            hosts, _check_platform_contract_one, private_config
        )
        return _summarize(results)

    async def check_host_prerequisites(hosts: list[str], user: str = "root") -> dict:
        results = await _gather_for_hosts(hosts, _check_host_prerequisites_one)
        return _summarize(results)

    async def install_packages(targets: list[dict], user: str = "root") -> dict:
        coros = [_install_packages_one(t["host"], t["packages"]) for t in targets]
        raw = await asyncio.gather(*coros, return_exceptions=True)
        results: dict[str, dict] = {}
        for target, result in zip(targets, raw):
            host = target["host"]
            if isinstance(result, Exception):
                results[host] = {"status": "error", "message": str(result)}
            else:
                results[host] = result
        return _summarize(results)

    async def check_existing_install(
        hosts: list[str],
        harness_name: str,
        install_path: str = "",
        user: str = "root",
        controller_host: str = "",
    ) -> dict:
        private_config = await skill_provider.get_all_private_config(harness_name)
        provisioning = private_config.get("provisioning", {})
        filtered, skipped = _filter_controller_only(
            hosts, controller_host, provisioning, harness_name
        )
        results = await _gather_for_hosts(
            filtered,
            _check_existing_install_one,
            harness_name,
            provisioning,
            install_path,
        )
        results.update(skipped)
        return _summarize(results)

    async def update_install(
        hosts: list[str],
        harness_name: str,
        install_path: str = "",
        user: str = "root",
        controller_host: str = "",
    ) -> dict:
        private_config = await skill_provider.get_all_private_config(harness_name)
        provisioning = private_config.get("provisioning", {})
        filtered, skipped = _filter_controller_only(
            hosts, controller_host, provisioning, harness_name
        )
        results = await _gather_for_hosts(
            filtered,
            _update_install_one,
            harness_name,
            provisioning,
            install_path,
        )
        results.update(skipped)
        return _summarize(results)

    async def uninstall_harness(
        hosts: list[str],
        harness_name: str,
        user: str = "root",
        controller_host: str = "",
    ) -> dict:
        private_config = await skill_provider.get_all_private_config(harness_name)
        provisioning = private_config.get("provisioning", {})
        filtered, skipped = _filter_controller_only(
            hosts, controller_host, provisioning, harness_name
        )
        results: dict[str, dict] = {}
        coros = [
            cleanup_harness(
                ssh,
                h,
                harness_name,
                install_path=provisioning.get("install_target_path"),
                pre_uninstall_commands=provisioning.get("pre_uninstall_commands"),
            )
            for h in filtered
        ]
        raw = await asyncio.gather(*coros, return_exceptions=True)
        for host, result in zip(filtered, raw):
            if isinstance(result, Exception):
                results[host] = {"status": "error", "message": str(result)}
            else:
                results[host] = result
        results.update(skipped)
        return _summarize(results)

    async def install_harness(
        hosts: list[str],
        harness_name: str,
        user: str = "root",
        branch: str = "",
        controller_host: str = "",
    ) -> dict:
        private_config = await skill_provider.get_all_private_config(harness_name)
        provisioning = private_config.get("provisioning", {})
        constraints = private_config.get("constraints", {})
        filtered, skipped = _filter_controller_only(
            hosts, controller_host, provisioning, harness_name
        )
        results = await _gather_for_hosts(
            filtered,
            _install_harness_one,
            harness_name,
            private_config,
            provisioning,
            constraints,
            branch,
        )
        results.update(skipped)
        return _summarize(results)

    async def install_k3s(hosts: list[str], user: str = "root") -> dict:
        results = await _gather_for_hosts(hosts, _install_k3s_one)
        return _summarize(results)

    async def verify_harness_install(
        hosts: list[str],
        harness_name: str,
        user: str = "root",
        install_path: str = "",
        controller_host: str = "",
    ) -> dict:
        private_config = await skill_provider.get_all_private_config(harness_name)
        provisioning = private_config.get("provisioning", {})
        filtered, skipped = _filter_controller_only(
            hosts, controller_host, provisioning, harness_name
        )
        results = await _gather_for_hosts(
            filtered,
            _verify_harness_install_one,
            harness_name,
            provisioning,
            install_path,
        )
        results.update(skipped)
        return _summarize(results)

    async def ensure_harness_installed(
        hosts: list[str],
        harness_name: str,
        user: str = "root",
        branch: str = "",
        install_path: str = "",
        controller_host: str = "",
    ) -> dict:
        private_config = await skill_provider.get_all_private_config(harness_name)
        provisioning = private_config.get("provisioning", {})
        constraints = private_config.get("constraints", {})
        filtered, skipped = _filter_controller_only(
            hosts, controller_host, provisioning, harness_name
        )
        results = await _gather_for_hosts(
            filtered,
            _ensure_harness_one,
            harness_name,
            private_config,
            provisioning,
            constraints,
            branch,
            install_path,
        )
        results.update(skipped)
        return _summarize(results)

    async def list_skill_docs(harness: str) -> dict:
        skill_dir = _SKILLS_DIR / harness
        if not skill_dir.is_dir():
            return {"found": False, "message": f"No skill directory for '{harness}'"}
        files = [f.name for f in sorted(skill_dir.iterdir()) if f.suffix == ".md"]
        return {"found": True, "harness": harness, "files": files}

    async def read_skill(harness: str, filename: str) -> dict:
        skill_path = _SKILLS_DIR / harness / filename
        if not skill_path.is_file():
            return {"found": False, "message": f"Skill not found: {harness}/{filename}"}
        resolved = skill_path.resolve()
        if not str(resolved).startswith(str(_SKILLS_DIR.resolve())):
            return {"found": False, "message": "Invalid path"}
        return {"found": True, "filename": filename, "content": skill_path.read_text()}

    async def read_skills(docs: list[dict]) -> list:
        results = []
        for doc in docs:
            harness = doc.get("harness", "")
            filename = doc.get("filename", "")
            skill_path = _SKILLS_DIR / harness / filename
            if not skill_path.is_file():
                results.append(
                    {
                        "harness": harness,
                        "filename": filename,
                        "found": False,
                        "message": f"Skill not found: {harness}/{filename}",
                    }
                )
                continue
            resolved = skill_path.resolve()
            if not str(resolved).startswith(str(_SKILLS_DIR.resolve())):
                results.append(
                    {
                        "harness": harness,
                        "filename": filename,
                        "found": False,
                        "message": "Invalid path",
                    }
                )
                continue
            results.append(
                {
                    "harness": harness,
                    "filename": filename,
                    "found": True,
                    "content": skill_path.read_text(),
                }
            )
        return results

    async def tune_hosts(targets: list[dict]) -> dict:
        results = {}
        coros = []
        hosts = []
        for t in targets:
            host = t.get("host", "")
            hosts.append(host)

            async def _tune_one(t=t) -> dict:
                h = t.get("host", "")
                iface = t.get("interface", "")
                steps = []
                errors = []

                # Step 1: tune_nic (must come first)
                if t.get("channels") is not None:
                    r = await tune_nic(
                        host=h,
                        interface=iface,
                        channels=t.get("channels", 1),
                        ring_rx=t.get("ring_rx"),
                        ring_tx=t.get("ring_tx"),
                        offloads=t.get("offloads"),
                    )
                    steps.append({"tune_nic": r})
                    if r.get("status") == "error":
                        errors.extend(r.get("errors", []))

                # Step 2: tune_tcp
                if t.get("congestion_control") or t.get("qdisc"):
                    r = await tune_tcp(
                        host=h,
                        interface=iface,
                        congestion_control=t.get("congestion_control"),
                        qdisc=t.get("qdisc"),
                        rmem_max=t.get("rmem_max"),
                        wmem_max=t.get("wmem_max"),
                    )
                    steps.append({"tune_tcp": r})
                    if r.get("status") == "error":
                        errors.extend(r.get("errors", []))

                # Step 3: pin_irq (after tune_nic — channel count determines IRQ numbers)
                if t.get("irq_cpu") is not None:
                    r = await pin_irq(
                        host=h,
                        interface=iface,
                        cpu=t["irq_cpu"],
                        irqbalance_mode=t.get("irqbalance_mode", "ban_irq"),
                    )
                    steps.append({"pin_irq": r})
                    if r.get("status") == "error":
                        errors.extend(r.get("errors", []))

                # Step 4: MTU via NetworkManager
                if t.get("mtu") is not None:
                    r = await nm_set_mtu(host=h, interface=iface, mtu=t["mtu"])
                    steps.append({"nm_set_mtu": r})
                    if r.get("status") == "error":
                        errors.append(r.get("error", "nm_set_mtu failed"))

                # Step 5: disable firewall
                if t.get("disable_firewall"):
                    r = await disable_firewall(host=h)
                    steps.append({"disable_firewall": r})
                    if r.get("status") == "error":
                        errors.extend(r.get("errors", []))

                # Step 6: verify
                expected = {}
                if t.get("congestion_control"):
                    expected["congestion_control"] = t["congestion_control"]
                if t.get("qdisc"):
                    expected["qdisc"] = t["qdisc"]
                if t.get("channels") is not None:
                    expected["channels"] = t["channels"]
                if t.get("irq_cpu") is not None:
                    expected["irq_cpu"] = t["irq_cpu"]
                if expected:
                    r = await verify_host_tuning(
                        host=h, interface=iface, expected=expected
                    )
                    steps.append({"verify": r})
                    if not r.get("all_ok"):
                        errors.append(f"Verification failed: {r.get('checks', {})}")

                return {
                    "host": h,
                    "status": "error" if errors else "ok",
                    "steps": steps,
                    "errors": errors,
                }

            coros.append(_tune_one())

        raw = await asyncio.gather(*coros, return_exceptions=True)
        for host, result in zip(hosts, raw):
            if isinstance(result, Exception):
                results[host] = {"status": "error", "errors": [str(result)]}
            else:
                results[host] = result

        success = sum(1 for r in results.values() if r.get("status") == "ok")
        return {
            "results": results,
            "summary": f"{len(results)} host(s): {success} ok, {len(results) - success} failed",
        }

    async def disable_firewall(
        host: str,
        user: str = "root",
    ) -> dict:
        results = []
        errors = []
        for cmd, label in [
            ("iptables -F", "iptables flush rules"),
            ("iptables -X", "iptables delete chains"),
            ("iptables -P INPUT ACCEPT", "iptables INPUT accept"),
            ("iptables -P FORWARD ACCEPT", "iptables FORWARD accept"),
            ("iptables -P OUTPUT ACCEPT", "iptables OUTPUT accept"),
            ("ip6tables -F 2>/dev/null", "ip6tables flush rules"),
            ("ip6tables -X 2>/dev/null", "ip6tables delete chains"),
            ("ip6tables -P INPUT ACCEPT 2>/dev/null", "ip6tables INPUT accept"),
            ("ip6tables -P FORWARD ACCEPT 2>/dev/null", "ip6tables FORWARD accept"),
            ("ip6tables -P OUTPUT ACCEPT 2>/dev/null", "ip6tables OUTPUT accept"),
            (
                "systemctl stop firewalld 2>/dev/null; systemctl mask firewalld 2>/dev/null; true",
                "firewalld stop+mask",
            ),
        ]:
            r = await ssh.run(host, cmd + " 2>&1")
            if r.exit_code == 0:
                results.append(label)
            else:
                errors.append(f"{label}: {r.stdout.strip()}")

        # Verify connectivity port is open
        r_verify = await ssh.run(host, "iptables -L INPUT -n | head -5")
        return {
            "host": host,
            "status": "error" if errors else "ok",
            "applied": results,
            "errors": errors,
            "iptables_input": r_verify.stdout.strip(),
        }

    async def open_firewall_port(
        host: str,
        ports: list[int],
        protocol: str = "tcp",
        user: str = "root",
    ) -> dict:
        protos = ["tcp", "udp"] if protocol == "both" else [protocol]
        applied = []
        errors = []
        for port in ports:
            for proto in protos:
                cmd = (
                    f"iptables -C INPUT -p {proto} --dport {port} -j ACCEPT 2>/dev/null"
                    f" || iptables -I INPUT -p {proto} --dport {port} -j ACCEPT"
                )
                r = await ssh.run(host, cmd + " 2>&1")
                if r.exit_code == 0:
                    applied.append(f"{proto}/{port}")
                else:
                    errors.append(f"{proto}/{port}: {r.stdout.strip()}")

        return {
            "host": host,
            "status": "error" if errors else "ok",
            "opened": applied,
            "errors": errors,
        }

    async def tune_nic(
        host: str,
        interface: str,
        channels: int = 1,
        ring_rx: int | None = None,
        ring_tx: int | None = None,
        offloads: dict | None = None,
        user: str = "root",
        ssh_key_path: str = "",
    ) -> dict:
        _ssh = ssh
        applied = []
        errors = []

        # Read current channel count
        r = await _ssh.run(host, f"ethtool -l {interface} 2>&1")
        before_channels: int | None = None
        for line in r.stdout.splitlines():
            if line.strip().startswith("Combined:") and "Current" not in "".join(
                r.stdout.splitlines()[: r.stdout.splitlines().index(line)]
            ):
                try:
                    before_channels = int(line.split()[-1])
                except ValueError:
                    pass
                break

        # Parse current hardware/software sections properly
        sections = r.stdout.split("Current hardware settings:")
        if len(sections) == 2:
            for line in sections[1].splitlines():
                if line.strip().startswith("Combined:"):
                    try:
                        before_channels = int(line.split()[-1])
                    except ValueError:
                        pass
                    break

        if channels != before_channels:
            r2 = await _ssh.run(
                host, f"ethtool -L {interface} combined {channels} 2>&1"
            )
            if r2.exit_code == 0:
                applied.append(f"channels: {before_channels} → {channels}")
            else:
                errors.append(f"ethtool -L failed: {r2.stdout.strip()}")
        else:
            applied.append(f"channels: already {channels}")

        # Ring buffers
        if ring_rx is not None or ring_tx is not None:
            parts = []
            if ring_rx is not None:
                parts.append(f"rx {ring_rx}")
            if ring_tx is not None:
                parts.append(f"tx {ring_tx}")
            r3 = await _ssh.run(host, f"ethtool -G {interface} {' '.join(parts)} 2>&1")
            if r3.exit_code == 0:
                applied.append(f"ring buffers: {' '.join(parts)}")
            else:
                errors.append(f"ethtool -G failed: {r3.stdout.strip()}")

        # Offloads
        for flag, value in (offloads or {}).items():
            r4 = await _ssh.run(host, f"ethtool -K {interface} {flag} {value} 2>&1")
            if r4.exit_code == 0:
                applied.append(f"offload {flag}={value}")
            else:
                errors.append(f"ethtool -K {flag}={value} failed: {r4.stdout.strip()}")

        return {
            "host": host,
            "interface": interface,
            "status": "error" if errors else "ok",
            "applied": applied,
            "errors": errors,
        }

    async def tune_tcp(
        host: str,
        interface: str | None = None,
        congestion_control: str | None = None,
        qdisc: str | None = None,
        rmem_max: int | None = None,
        wmem_max: int | None = None,
        extra_sysctls: dict | None = None,
        user: str = "root",
        ssh_key_path: str = "",
    ) -> dict:
        _ssh = ssh
        results = {}
        errors = []

        sysctls: dict[str, str] = {}
        if congestion_control:
            sysctls["net.ipv4.tcp_congestion_control"] = congestion_control
        if qdisc:
            # net.core.default_qdisc only affects newly-created interfaces;
            # set it for future interfaces AND apply tc qdisc to existing ones.
            sysctls["net.core.default_qdisc"] = qdisc
        if rmem_max is not None:
            sysctls["net.core.rmem_max"] = str(rmem_max)
            sysctls["net.core.rmem_default"] = str(rmem_max)
        if wmem_max is not None:
            sysctls["net.core.wmem_max"] = str(wmem_max)
            sysctls["net.core.wmem_default"] = str(wmem_max)
        if extra_sysctls:
            sysctls.update({str(k): str(v) for k, v in extra_sysctls.items()})

        for key, value in sysctls.items():
            rb = await _ssh.run(host, f"sysctl -n {key} 2>&1")
            before = rb.stdout.strip()
            rw = await _ssh.run(host, f"sysctl -w {key}={value} 2>&1")
            if rw.exit_code != 0:
                errors.append(f"{key}: {rw.stdout.strip()}")
                results[key] = {"before": before, "requested": value, "ok": False}
                continue
            rv = await _ssh.run(host, f"sysctl -n {key} 2>&1")
            after = rv.stdout.strip()
            results[key] = {"before": before, "after": after, "ok": after == value}

        # Apply the qdisc directly to existing interfaces via tc.
        # sysctl net.core.default_qdisc only affects newly-created interfaces;
        # tc qdisc is required to change the qdisc on an interface already up.
        tc_result: dict = {}
        if qdisc and interface:
            rt = await _ssh.run(
                host,
                f"tc qdisc replace dev {interface} root {qdisc} 2>&1",
            )
            if rt.exit_code == 0:
                # Verify
                rv2 = await _ssh.run(
                    host,
                    f"tc qdisc show dev {interface} 2>&1",
                )
                tc_result = {
                    "interface": interface,
                    "qdisc": qdisc,
                    "ok": qdisc in rv2.stdout,
                    "output": rv2.stdout.strip(),
                }
            else:
                errors.append(f"tc qdisc replace {interface}: {rt.stdout.strip()}")
                tc_result = {
                    "interface": interface,
                    "qdisc": qdisc,
                    "ok": False,
                    "error": rt.stdout.strip(),
                }

        return {
            "host": host,
            "status": "error" if errors else "ok",
            "sysctls": results,
            "tc_qdisc": tc_result,
            "errors": errors,
        }

    async def pin_irq(
        host: str,
        interface: str,
        cpu: int,
        irqbalance_mode: str = "ban_irq",
        user: str = "root",
        ssh_key_path: str = "",
    ) -> dict:
        _ssh = ssh
        errors = []
        applied = []

        # Discover IRQ number(s) for the interface
        r = await _ssh.run(host, "cat /proc/interrupts 2>&1")
        irq_numbers = []
        for line in r.stdout.splitlines():
            if interface in line:
                try:
                    irq_numbers.append(int(line.split(":")[0].strip()))
                except ValueError:
                    pass

        if not irq_numbers:
            return {
                "host": host,
                "interface": interface,
                "status": "error",
                "errors": [f"No IRQ found for {interface} in /proc/interrupts"],
            }

        cpu_mask = hex(1 << cpu)

        for irq in irq_numbers:
            r2 = await _ssh.run(
                host,
                f"echo {cpu_mask} > /proc/irq/{irq}/smp_affinity 2>&1",
            )
            if r2.exit_code == 0:
                applied.append(f"IRQ {irq} → CPU {cpu} (mask {cpu_mask})")
            else:
                errors.append(
                    f"smp_affinity write failed for IRQ {irq}: {r2.stdout.strip()}"
                )

        # irqbalance coordination
        ib_result = {"mode": irqbalance_mode}
        if irqbalance_mode == "disable":
            r3 = await _ssh.run(
                host, "systemctl mask irqbalance && systemctl stop irqbalance 2>&1"
            )
            ib_result["status"] = "masked" if r3.exit_code == 0 else "error"
            if r3.exit_code != 0:
                errors.append(f"irqbalance disable failed: {r3.stdout.strip()}")

        elif irqbalance_mode == "ban_irq":
            banned = " ".join(str(i) for i in irq_numbers)
            # Read existing banned interrupts
            rb = await _ssh.run(
                host,
                "grep -s IRQBALANCE_BANNED_INTERRUPTS /etc/sysconfig/irqbalance || echo ''",
            )
            existing = ""
            for line in rb.stdout.splitlines():
                if "IRQBALANCE_BANNED_INTERRUPTS" in line:
                    existing = line.split("=", 1)[-1].strip().strip('"')
            new_val = f"{existing} {banned}".strip()
            r4 = await _ssh.run(
                host,
                f"sed -i '/IRQBALANCE_BANNED_INTERRUPTS/d' /etc/sysconfig/irqbalance 2>/dev/null; "
                f"echo 'IRQBALANCE_BANNED_INTERRUPTS=\"{new_val}\"' >> /etc/sysconfig/irqbalance; "
                f"systemctl restart irqbalance 2>&1",
            )
            ib_result["banned_interrupts"] = new_val
            ib_result["status"] = "restarted" if r4.exit_code == 0 else "error"
            if r4.exit_code != 0:
                errors.append(f"irqbalance ban_irq failed: {r4.stdout.strip()}")

        elif irqbalance_mode == "ban_cpu":
            cpu_mask_ib = hex(1 << cpu)
            rb = await _ssh.run(
                host,
                "grep -s IRQBALANCE_BANNED_CPUS /etc/sysconfig/irqbalance || echo ''",
            )
            existing = ""
            for line in rb.stdout.splitlines():
                if "IRQBALANCE_BANNED_CPUS" in line:
                    existing = line.split("=", 1)[-1].strip().strip('"')
            # Merge masks
            try:
                merged = (
                    hex(int(existing, 16) | (1 << cpu)) if existing else cpu_mask_ib
                )
            except ValueError:
                merged = cpu_mask_ib
            r5 = await _ssh.run(
                host,
                f"sed -i '/IRQBALANCE_BANNED_CPUS/d' /etc/sysconfig/irqbalance 2>/dev/null; "
                f"echo 'IRQBALANCE_BANNED_CPUS=\"{merged}\"' >> /etc/sysconfig/irqbalance; "
                f"systemctl restart irqbalance 2>&1",
            )
            ib_result["banned_cpus_mask"] = merged
            ib_result["status"] = "restarted" if r5.exit_code == 0 else "error"
            if r5.exit_code != 0:
                errors.append(f"irqbalance ban_cpu failed: {r5.stdout.strip()}")

        return {
            "host": host,
            "interface": interface,
            "irq_numbers": irq_numbers,
            "cpu": cpu,
            "cpu_mask": cpu_mask,
            "irqbalance": ib_result,
            "status": "error" if errors else "ok",
            "applied": applied,
            "errors": errors,
        }

    async def verify_host_tuning(
        host: str,
        interface: str,
        expected: dict | None = None,
        user: str = "root",
        ssh_key_path: str = "",
    ) -> dict:
        _ssh = ssh
        exp = expected or {}
        checks: dict[str, dict] = {}
        all_ok = True

        # TCP congestion control (sysctl)
        r = await _ssh.run(host, "sysctl -n net.ipv4.tcp_congestion_control 2>&1")
        actual_cc = r.stdout.strip()
        expected_cc = exp.get("congestion_control")
        cc_ok = (expected_cc is None) or (actual_cc == expected_cc)
        all_ok = all_ok and cc_ok
        checks["net.ipv4.tcp_congestion_control"] = {
            "actual": actual_cc,
            "expected": expected_cc,
            "ok": cc_ok,
        }

        # Qdisc: check via tc on the interface (authoritative) not sysctl
        # (sysctl net.core.default_qdisc only affects new interfaces)
        expected_qdisc = exp.get("qdisc")
        r_tc = await _ssh.run(host, f"tc qdisc show dev {interface} 2>&1")
        tc_output = r_tc.stdout.strip()
        # tc output format: "qdisc fq 8001: root refcnt 2 ..."
        actual_qdisc = None
        for part in tc_output.split():
            if part not in ("qdisc", "noqueue", "root", "refcnt"):
                actual_qdisc = part
                break
        qdisc_ok = (expected_qdisc is None) or (actual_qdisc == expected_qdisc)
        all_ok = all_ok and qdisc_ok
        checks["qdisc"] = {
            "interface": interface,
            "actual": actual_qdisc,
            "expected": expected_qdisc,
            "ok": qdisc_ok,
            "tc_output": tc_output,
        }

        # NIC channel count
        r = await _ssh.run(host, f"ethtool -l {interface} 2>&1")
        actual_channels: int | None = None
        sections = r.stdout.split("Current hardware settings:")
        if len(sections) == 2:
            for line in sections[1].splitlines():
                if line.strip().startswith("Combined:"):
                    try:
                        actual_channels = int(line.split()[-1])
                    except ValueError:
                        pass
                    break
        expected_channels = exp.get("channels")
        ch_ok = (expected_channels is None) or (actual_channels == expected_channels)
        all_ok = all_ok and ch_ok
        checks["channels"] = {
            "actual": actual_channels,
            "expected": expected_channels,
            "ok": ch_ok,
        }

        # IRQ affinity
        ri = await _ssh.run(host, "cat /proc/interrupts 2>&1")
        irq_numbers = []
        for line in ri.stdout.splitlines():
            if interface in line:
                try:
                    irq_numbers.append(int(line.split(":")[0].strip()))
                except ValueError:
                    pass

        irq_check: dict = {"irq_numbers": irq_numbers}
        expected_cpu = exp.get("irq_cpu")
        if irq_numbers:
            irq = irq_numbers[0]
            ra = await _ssh.run(host, f"cat /proc/irq/{irq}/smp_affinity_list 2>&1")
            actual_affinity = ra.stdout.strip()
            irq_check["cpu_affinity"] = actual_affinity
            if expected_cpu is not None:
                irq_ok = str(expected_cpu) in actual_affinity.split(",")
                irq_check["expected_cpu"] = expected_cpu
                irq_check["ok"] = irq_ok
                all_ok = all_ok and irq_ok
        checks["irq"] = irq_check

        # irqbalance status
        rib = await _ssh.run(host, "systemctl is-active irqbalance 2>&1")
        ib_active = rib.stdout.strip() == "active"
        expected_ib_mode = exp.get("irqbalance_mode")
        ib_check: dict = {"active": ib_active}
        if expected_ib_mode == "disable":
            ib_ok = not ib_active
            ib_check["ok"] = ib_ok
            all_ok = all_ok and ib_ok
        checks["irqbalance"] = ib_check

        return {
            "host": host,
            "interface": interface,
            "all_ok": all_ok,
            "checks": checks,
        }

    async def get_private_config(harness_name: str, key: str) -> Any:
        result = await skill_provider.get_private_config(harness_name, key)
        if result is None:
            return {
                "key": key,
                "value": None,
                "message": f"No private config for {harness_name}.{key}",
            }
        return {"key": key, "value": result}

    async def request_clarification(question: str) -> str:
        await request_clarification_fn(question)
        return "Clarification requested. Ticket paused for human input."

    # -----------------------------------------------------------------------
    # NetworkManager tools
    # -----------------------------------------------------------------------

    async def _nm_find_connection(host: str, interface: str) -> str:
        """Return the NM connection name that owns the interface, or the interface name."""
        r = await ssh.run(
            host,
            f"nmcli -t -f NAME,DEVICE connection show --active 2>/dev/null | grep ':{interface}$' | cut -d: -f1",
        )
        name = r.stdout.strip()
        return name if name else interface

    async def nm_set_mtu(
        host: str,
        interface: str,
        mtu: int,
        user: str = "root",
    ) -> dict:
        conn = await _nm_find_connection(host, interface)
        r_before = await ssh.run(
            host, f"ip link show {interface} 2>/dev/null | grep -o 'mtu [0-9]*'"
        )
        before_mtu = r_before.stdout.strip()

        r = await ssh.run(
            host,
            f"nmcli connection modify '{conn}' 802-3-ethernet.mtu {mtu} 2>&1"
            f" && nmcli connection up '{conn}' 2>&1",
        )
        if r.exit_code != 0:
            return {
                "host": host,
                "interface": interface,
                "status": "error",
                "error": r.stdout.strip(),
            }

        r_after = await ssh.run(
            host, f"ip link show {interface} 2>/dev/null | grep -o 'mtu [0-9]*'"
        )
        after_mtu = r_after.stdout.strip()
        actual = int(after_mtu.split()[-1]) if after_mtu else None
        return {
            "host": host,
            "interface": interface,
            "connection": conn,
            "before": before_mtu,
            "after": after_mtu,
            "status": "ok" if actual == mtu else "error",
            "ok": actual == mtu,
        }

    async def nm_set_ip(
        host: str,
        interface: str,
        ip_cidr: str,
        gateway: str | None = None,
        dns: str | None = None,
        user: str = "root",
    ) -> dict:
        conn = await _nm_find_connection(host, interface)
        cmds = [
            f"nmcli connection modify '{conn}' ipv4.method manual ipv4.addresses '{ip_cidr}'",
        ]
        if gateway:
            cmds.append(f"nmcli connection modify '{conn}' ipv4.gateway '{gateway}'")
        if dns:
            cmds.append(f"nmcli connection modify '{conn}' ipv4.dns '{dns}'")
        cmds.append(f"nmcli connection up '{conn}'")

        errors = []
        for cmd in cmds:
            r = await ssh.run(host, cmd + " 2>&1")
            if r.exit_code != 0:
                errors.append(r.stdout.strip())

        r_verify = await ssh.run(
            host, f"ip addr show {interface} 2>/dev/null | grep 'inet '"
        )
        return {
            "host": host,
            "interface": interface,
            "connection": conn,
            "ip_cidr": ip_cidr,
            "gateway": gateway,
            "live_addresses": r_verify.stdout.strip(),
            "status": "error" if errors else "ok",
            "errors": errors,
        }

    async def nm_set_dhcp(
        host: str,
        interface: str,
        user: str = "root",
    ) -> dict:
        conn = await _nm_find_connection(host, interface)
        r = await ssh.run(
            host,
            f"nmcli connection modify '{conn}' ipv4.method auto ipv4.addresses '' ipv4.gateway '' 2>&1"
            f" && nmcli connection up '{conn}' 2>&1",
        )
        return {
            "host": host,
            "interface": interface,
            "connection": conn,
            "status": "ok" if r.exit_code == 0 else "error",
            "output": r.stdout.strip(),
        }

    async def nm_show_connection(
        host: str,
        interface: str,
        user: str = "root",
    ) -> dict:
        conn = await _nm_find_connection(host, interface)
        r = await ssh.run(
            host,
            f"nmcli connection show '{conn}' 2>/dev/null"
            f" | grep -E 'ipv4\\.method|ipv4\\.addresses|802-3-ethernet\\.mtu|GENERAL\\.STATE'",
        )
        live = await ssh.run(
            host,
            f"ip link show {interface} 2>/dev/null | grep -o 'mtu [0-9]*';"
            f" ip addr show {interface} 2>/dev/null | grep 'inet '",
        )
        return {
            "host": host,
            "interface": interface,
            "connection": conn,
            "profile": r.stdout.strip(),
            "live": live.stdout.strip(),
        }

    async def nm_verify_interface(
        host: str,
        interface: str,
        expected_mtu: int | None = None,
        expected_ip: str | None = None,
        user: str = "root",
    ) -> dict:
        checks: dict[str, dict] = {}
        all_ok = True

        r_link = await ssh.run(host, f"ip link show {interface} 2>/dev/null")
        link_output = r_link.stdout

        # MTU check
        actual_mtu: int | None = None
        for token in link_output.split():
            if (
                token.isdigit()
                and "mtu"
                in link_output[
                    max(0, link_output.find(token) - 5) : link_output.find(token)
                ]
            ):
                actual_mtu = int(token)
                break
        import re as _re

        m = _re.search(r"mtu (\d+)", link_output)
        if m:
            actual_mtu = int(m.group(1))
        mtu_ok = (expected_mtu is None) or (actual_mtu == expected_mtu)
        all_ok = all_ok and mtu_ok
        checks["mtu"] = {"actual": actual_mtu, "expected": expected_mtu, "ok": mtu_ok}

        # IP check
        r_addr = await ssh.run(
            host, f"ip addr show {interface} 2>/dev/null | grep 'inet '"
        )
        actual_ips = [
            line.strip().split()[1].split("/")[0]
            for line in r_addr.stdout.splitlines()
            if "inet " in line
        ]
        ip_ok = (expected_ip is None) or (expected_ip in actual_ips)
        all_ok = all_ok and ip_ok
        checks["ip"] = {"actual": actual_ips, "expected": expected_ip, "ok": ip_ok}

        # State check
        state_ok = "UP" in link_output and "state UP" in link_output
        checks["state"] = {"up": state_ok, "ok": state_ok}
        all_ok = all_ok and state_ok

        return {
            "host": host,
            "interface": interface,
            "all_ok": all_ok,
            "checks": checks,
        }

    handlers = {
        "list_skill_docs": list_skill_docs,
        "read_skill": read_skill,
        "read_skills": read_skills,
        "tune_hosts": tune_hosts,
        "disable_firewall": disable_firewall,
        "open_firewall_port": open_firewall_port,
        "tune_nic": tune_nic,
        "tune_tcp": tune_tcp,
        "pin_irq": pin_irq,
        "verify_host_tuning": verify_host_tuning,
        "nm_set_mtu": nm_set_mtu,
        "nm_set_ip": nm_set_ip,
        "nm_set_dhcp": nm_set_dhcp,
        "nm_show_connection": nm_show_connection,
        "nm_verify_interface": nm_verify_interface,
        "check_platform_contract": check_platform_contract,
        "check_host_prerequisites": check_host_prerequisites,
        "install_packages": install_packages,
        "check_existing_install": check_existing_install,
        "update_install": update_install,
        "uninstall_harness": uninstall_harness,
        "install_harness": install_harness,
        "install_k3s": install_k3s,
        "verify_harness_install": verify_harness_install,
        "ensure_harness_installed": ensure_harness_installed,
        "get_private_config": get_private_config,
        "request_clarification": request_clarification,
    }
    return handlers, ssh
