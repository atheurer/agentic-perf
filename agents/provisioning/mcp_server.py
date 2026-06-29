from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from providers.llm.base import ToolDefinition
from providers.ssh import SSHExecutor

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

        await ssh.run(host, "rm -rf /var/lib/crucible", timeout=120)
        cleanup_details.append("data: /var/lib/crucible")

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
            name="configure_host",
            description=(
                "Apply OS-level configuration for optimal benchmark performance "
                "on multiple hosts. Each target specifies a host and its config. "
                "Supports CPU isolation, hugepages, IRQ affinity, tuned profiles."
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
                                "config": {
                                    "type": "object",
                                    "properties": {
                                        "cpu_isolation": {"type": "string"},
                                        "hugepages": {"type": "integer"},
                                        "irq_affinity": {"type": "string"},
                                        "tuned_profile": {"type": "string"},
                                    },
                                },
                            },
                            "required": ["host", "config"],
                        },
                        "description": "List of {host, config} targets",
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

        for entry in secret_files:
            secret_key = entry["secret_key"]
            secret_path = secrets_map.get(secret_key)
            required = entry.get("required", True)
            description = entry.get("description", secret_key)

            if not secret_path:
                if required:
                    missing.append(f"{secret_key}: no path in secrets config")
                continue

            if secrets_provider is None:
                if required:
                    missing.append(f"{secret_key}: no secrets provider configured")
                continue

            local_path = await secrets_provider.get_secret_file(secret_path)
            if local_path is None:
                if required:
                    missing.append(
                        f"{description} ({secret_path}): not found in secrets store"
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
                host, item["local_path"], item["remote_path"]
            )
            if scp_result.exit_code != 0:
                return {
                    "status": "failed",
                    "message": (
                        f"Failed to deploy {item['description']} to "
                        f"{host}:{item['remote_path']}: {scp_result.stderr}"
                    ),
                }
            deployed.append(f"{item['secret_key']} -> {item['remote_path']}")
            logger.info(
                f"[provision] Deployed {item['secret_key']} to "
                f"{host}:{item['remote_path']}"
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
    ) -> dict:
        private_config = await skill_provider.get_all_private_config(harness_name)
        provisioning = private_config.get("provisioning", {})
        results = await _gather_for_hosts(
            hosts,
            _check_existing_install_one,
            harness_name,
            provisioning,
            install_path,
        )
        return _summarize(results)

    async def update_install(
        hosts: list[str],
        harness_name: str,
        install_path: str = "",
        user: str = "root",
    ) -> dict:
        private_config = await skill_provider.get_all_private_config(harness_name)
        provisioning = private_config.get("provisioning", {})
        results = await _gather_for_hosts(
            hosts,
            _update_install_one,
            harness_name,
            provisioning,
            install_path,
        )
        return _summarize(results)

    async def uninstall_harness(
        hosts: list[str],
        harness_name: str,
        user: str = "root",
    ) -> dict:
        private_config = await skill_provider.get_all_private_config(harness_name)
        provisioning = private_config.get("provisioning", {})
        results: dict[str, dict] = {}
        coros = [
            cleanup_harness(
                ssh,
                h,
                harness_name,
                install_path=provisioning.get("install_target_path"),
                pre_uninstall_commands=provisioning.get("pre_uninstall_commands"),
            )
            for h in hosts
        ]
        raw = await asyncio.gather(*coros, return_exceptions=True)
        for host, result in zip(hosts, raw):
            if isinstance(result, Exception):
                results[host] = {"status": "error", "message": str(result)}
            else:
                results[host] = result
        return _summarize(results)

    async def install_harness(
        hosts: list[str],
        harness_name: str,
        user: str = "root",
        branch: str = "",
    ) -> dict:
        private_config = await skill_provider.get_all_private_config(harness_name)
        provisioning = private_config.get("provisioning", {})
        constraints = private_config.get("constraints", {})
        results = await _gather_for_hosts(
            hosts,
            _install_harness_one,
            harness_name,
            private_config,
            provisioning,
            constraints,
            branch,
        )
        return _summarize(results)

    async def install_k3s(hosts: list[str], user: str = "root") -> dict:
        results = await _gather_for_hosts(hosts, _install_k3s_one)
        return _summarize(results)

    async def verify_harness_install(
        hosts: list[str],
        harness_name: str,
        user: str = "root",
        install_path: str = "",
    ) -> dict:
        private_config = await skill_provider.get_all_private_config(harness_name)
        provisioning = private_config.get("provisioning", {})
        results = await _gather_for_hosts(
            hosts,
            _verify_harness_install_one,
            harness_name,
            provisioning,
            install_path,
        )
        return _summarize(results)

    async def configure_host(targets: list[dict], user: str = "root") -> dict:
        results: dict[str, dict] = {}
        for t in targets:
            host = t["host"]
            config = t.get("config", {})
            results[host] = {
                "host": host,
                "config_applied": config,
                "status": "success",
                "reboot_required": False,
                "message": f"Configuration applied on {host} (simulated)",
            }
        return _summarize(results)

    async def ensure_harness_installed(
        hosts: list[str],
        harness_name: str,
        user: str = "root",
        branch: str = "",
        install_path: str = "",
    ) -> dict:
        private_config = await skill_provider.get_all_private_config(harness_name)
        provisioning = private_config.get("provisioning", {})
        constraints = private_config.get("constraints", {})
        results = await _gather_for_hosts(
            hosts,
            _ensure_harness_one,
            harness_name,
            private_config,
            provisioning,
            constraints,
            branch,
            install_path,
        )
        return _summarize(results)

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

    handlers = {
        "check_platform_contract": check_platform_contract,
        "check_host_prerequisites": check_host_prerequisites,
        "install_packages": install_packages,
        "check_existing_install": check_existing_install,
        "update_install": update_install,
        "uninstall_harness": uninstall_harness,
        "install_harness": install_harness,
        "install_k3s": install_k3s,
        "verify_harness_install": verify_harness_install,
        "configure_host": configure_host,
        "ensure_harness_installed": ensure_harness_installed,
        "get_private_config": get_private_config,
        "request_clarification": request_clarification,
    }
    return handlers, ssh
