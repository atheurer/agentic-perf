"""FastMCP server for provisioning agent tools.

Exposes host provisioning tools (install, verify, configure, uninstall)
over stdio.  The SkillProvider, SecretsProvider, and SSHExecutor are
constructed lazily on first tool call from environment variables and
ticket data, so credentials and provider internals never cross the LLM
boundary.

Run directly:  python agents/provisioning/server.py
Connected via: AgentMCPClient (agents/mcp_client.py)
"""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

_project_root = str(Path(__file__).resolve().parents[2])
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from fastmcp import FastMCP

from agents.server_utils import (
    build_secrets_provider,
    build_skill_provider,
    build_ssh_from_ticket,
)

logger = logging.getLogger(__name__)

mcp = FastMCP("provisioning-agent")

# Module-level globals — lazily initialized by _ensure_init()
_ssh = None
_skill_provider = None
_secrets_provider = None
_ticket: dict[str, Any] = {}
_initialized = False


async def _ensure_init():
    """Lazily initialize providers and SSH from env vars on first tool call."""
    global _initialized, _ssh, _skill_provider, _secrets_provider, _ticket
    if _initialized:
        return
    _ssh, _ticket = await build_ssh_from_ticket()
    _skill_provider = build_skill_provider()
    _secrets_provider = build_secrets_provider()
    _initialized = True


# ---------------------------------------------------------------------------
# Helper functions (verbatim from mcp_server.py)
# ---------------------------------------------------------------------------


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


async def validate_platform_contract(
    ssh,
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
    ssh, host: str, install_path: str
) -> list[str]:
    """Read registries.json on the host and extract all referenced token file paths."""
    result = await ssh.run(
        host, f"cat {install_path}/config/registries.json 2>/dev/null"
    )
    if result.exit_code != 0 or not result.stdout.strip():
        return []

    try:
        reg = json.loads(result.stdout)
    except Exception:
        logger.warning(f"[provision] Could not parse registries.json on {host}")
        return []

    paths = []
    # controller.pull-token
    if reg.get("controller", {}).get("pull-token"):
        paths.append(reg["controller"]["pull-token"])
    # engines.public
    pub = reg.get("engines", {}).get("public", {})
    if pub.get("push-token"):
        paths.append(pub["push-token"])
    if pub.get("quay", {}).get("refresh-expiration", {}).get("token-file"):
        paths.append(pub["quay"]["refresh-expiration"]["token-file"])
    # engines.private
    priv = reg.get("engines", {}).get("private", {})
    if priv.get("tokens", {}).get("push"):
        paths.append(priv["tokens"]["push"])
    if priv.get("tokens", {}).get("pull"):
        paths.append(priv["tokens"]["pull"])
    if priv.get("quay", {}).get("refresh-expiration", {}).get("token-file"):
        paths.append(priv["quay"]["refresh-expiration"]["token-file"])
    # userenvs[].pull-token
    for ue in reg.get("userenvs", []):
        if ue.get("pull-token"):
            paths.append(ue["pull-token"])

    return paths


async def cleanup_harness(
    ssh,
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

    # --- crucible-specific cleanup (TODO: migrate to crucible project) ---
    if harness_name == "crucible":
        # 1. Discover auth token files from registries.json before removing anything
        token_files = await _discover_crucible_token_files(ssh, host, path)
        if token_files:
            logger.info(
                f"[provision] Found {len(token_files)} token files in registries.json on {host}"
            )

        # 2. Stop and remove all crucible containers
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

        # 3. Remove auth token files discovered from registries.json
        for token_path in token_files:
            await ssh.run(host, f"rm -f {token_path}")
            cleanup_details.append(f"token: {token_path}")
        logger.info(f"[provision] Removed {len(token_files)} token files on {host}")

        # 4. Remove system artifacts
        for artifact in [
            "/usr/bin/crucible",
            "/etc/sysconfig/crucible",
            "/etc/profile.d/crucible_completions.sh",
        ]:
            await ssh.run(host, f"rm -f {artifact}")
        cleanup_details.append("system: symlinks, sysconfig, profile.d")

        # 5. Remove user config
        await ssh.run(host, "rm -rf /root/.crucible", timeout=60)
        cleanup_details.append("config: /root/.crucible")

        # 6. Remove run data
        await ssh.run(host, "rm -rf /var/lib/crucible", timeout=120)
        cleanup_details.append("data: /var/lib/crucible")

    # Remove the install directory
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


async def _validate_and_deploy_contract(host: str, private_config: dict) -> dict:
    """Validate install contract secrets and deploy them to the host."""
    contract = private_config.get("install_contract")
    if not contract:
        return {"status": "ok", "deployed_files": [], "message": "No install contract"}

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

        if _secrets_provider is None:
            if required:
                missing.append(f"{secret_key}: no secrets provider configured")
            continue

        local_path = await _secrets_provider.get_secret_file(secret_path)
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
        scp_result = await _ssh.copy_to(host, item["local_path"], item["remote_path"])
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
            f"[provision] Deployed {item['secret_key']} to {host}:{item['remote_path']}"
        )

    for cmd in contract.get("pre_install_commands", []):
        logger.info(f"[provision] Contract pre-install on {host}: {cmd}")
        await _ssh.run(host, cmd, timeout=60)

    return {
        "status": "ok",
        "deployed_files": deployed,
        "message": f"Contract satisfied: {len(deployed)} file(s) deployed",
    }


# ---------------------------------------------------------------------------
# MCP Tools (11 tools)
# ---------------------------------------------------------------------------


@mcp.tool()
async def check_platform_contract(
    host: str, harness_name: str, user: str = "root"
) -> str:
    """Check if a host meets the platform requirements (OS, repos, packages) for a benchmark harness. Call this before attempting installation to verify compatibility. Returns detected OS, missing repos, and missing packages. OS or repo mismatches are hard failures; missing packages are warnings (they can be installed)."""
    await _ensure_init()
    private_config = await _skill_provider.get_all_private_config(harness_name)
    result = await validate_platform_contract(_ssh, host, private_config)
    return json.dumps(result)


@mcp.tool()
async def check_host_prerequisites(host: str, user: str = "root") -> str:
    """Check if a host has the required software installed (podman, git, jq, curl). Returns the status of each prerequisite."""
    await _ensure_init()
    prereqs = {}
    for cmd in ["podman", "git", "jq", "curl"]:
        result = await _ssh.run(
            host, f"which {cmd} 2>/dev/null && {cmd} --version 2>/dev/null | head -1"
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
    return json.dumps(
        {
            "host": host,
            "prerequisites": prereqs,
            "all_met": all_met,
            "message": f"All prerequisites met on {host}"
            if all_met
            else f"Missing prerequisites on {host}",
        }
    )


@mcp.tool()
async def install_packages(host: str, packages: list[str], user: str = "root") -> str:
    """Install required packages on a host via the system package manager."""
    await _ensure_init()
    pkg_list = " ".join(packages)
    result = await _ssh.run(host, f"dnf install -y {pkg_list}", timeout=300)
    response = {
        "host": host,
        "packages": packages,
        "status": "success" if result.exit_code == 0 else "failed",
        "exit_code": result.exit_code,
    }
    if result.exit_code != 0:
        response["output"] = result.stdout or ""
        response["error"] = result.stderr or ""
    return json.dumps(response)


@mcp.tool()
async def install_harness(
    host: str,
    harness_name: str,
    user: str = "root",
    branch: str = "",
) -> str:
    """Install the benchmark harness on a host. Uses private skill config to determine the install method: 'public_install' downloads and runs the upstream installer with skill-driven flags; 'git_clone' clones from a URL and runs install.sh. Validates and deploys required secrets from the install_contract before running the installer."""
    await _ensure_init()
    private_config = await _skill_provider.get_all_private_config(harness_name)
    provisioning = private_config.get("provisioning", {})
    install_method = provisioning.get("install_method", "git_clone")
    target_path = provisioning.get("install_target_path", f"/opt/{harness_name}")
    constraints = private_config.get("constraints", {})

    # Container-only harnesses (e.g., arcaflow-plugins) don't need
    # installation — just verify the container runtime is available.
    if provisioning.get("skip_install") or install_method == "none":
        verify_cmd = provisioning.get("verify_command", "podman --version")
        verify_result = await _ssh.run(host, verify_cmd, timeout=15)
        if verify_result.exit_code == 0:
            return json.dumps(
                {
                    "host": host,
                    "harness": harness_name,
                    "status": "ready",
                    "message": (
                        f"No installation needed for {harness_name}. "
                        f"Runtime verified: {verify_result.stdout.strip()}"
                    ),
                }
            )
        else:
            return json.dumps(
                {
                    "host": host,
                    "harness": harness_name,
                    "status": "missing_runtime",
                    "message": (
                        f"{harness_name} requires "
                        f"{provisioning.get('prerequisites', ['podman'])} "
                        f"but verification failed: {verify_result.stderr.strip()}"
                    ),
                }
            )

    platform_result = await validate_platform_contract(_ssh, host, private_config)
    if platform_result["status"] == "failed":
        return json.dumps(
            {
                "host": host,
                "harness": harness_name,
                "status": "platform_incompatible",
                "message": platform_result["message"],
                "detected_os": platform_result.get("detected_os"),
            }
        )

    contract_result = await _validate_and_deploy_contract(host, private_config)
    if contract_result["status"] == "failed":
        return json.dumps(
            {
                "host": host,
                "harness": harness_name,
                "status": "contract_failed",
                "message": contract_result["message"],
                "missing": contract_result.get("missing", []),
            }
        )

    if install_method == "public_install":
        installer_url = provisioning.get("installer_url")
        if not installer_url:
            return json.dumps(
                {
                    "host": host,
                    "status": "failed",
                    "message": f"No installer_url in private config for {harness_name}",
                }
            )

        installer_path = "/tmp/harness-install.sh"
        logger.info(f"[provision] Downloading installer from {installer_url}")
        dl_result = await _ssh.run(
            host,
            f"curl --fail --silent --output {installer_path} {installer_url} && chmod +x {installer_path}",
            timeout=60,
        )
        if dl_result.exit_code != 0:
            return json.dumps(
                {
                    "host": host,
                    "status": "failed",
                    "message": f"Failed to download installer: {dl_result.stderr}",
                }
            )

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
        logger.info(f"[provision] Running installer on {host}: {cmd}")
        result = await _ssh.run(host, cmd, timeout=1800)

        if result.exit_code != 0:
            return json.dumps(
                {
                    "host": host,
                    "harness": harness_name,
                    "status": "failed",
                    "exit_code": result.exit_code,
                    "install_path": target_path,
                    "output": result.stdout or "" if result.stdout else "",
                    "error": result.stderr or "" if result.stderr else "",
                    "message": f"Install failed (exit {result.exit_code})",
                }
            )

        for post_cmd in provisioning.get("post_install_commands", []):
            logger.info(f"[provision] Post-install on {host}: {post_cmd}")
            post_result = await _ssh.run(host, post_cmd, timeout=120)
            if post_result.exit_code != 0:
                logger.warning(
                    f"[provision] Post-install command failed: {post_result.stderr}"
                )

        await _ssh.run(host, f"rm -f {installer_path}")

        return json.dumps(
            {
                "host": host,
                "harness": harness_name,
                "status": "success",
                "exit_code": 0,
                "install_path": target_path,
                "constraints": constraints,
                "contract": contract_result.get("deployed_files", []),
                "output": result.stdout or "" if result.stdout else "",
                "message": f"{harness_name} installed via public installer",
            }
        )

    if install_method == "git_clone":
        git_url = provisioning.get("git_url")
        if not git_url:
            return json.dumps(
                {
                    "host": host,
                    "status": "failed",
                    "message": f"No git_url in private config for {harness_name}",
                }
            )

        pre_install_steps = provisioning.get("pre_install_steps", [])
        for step in pre_install_steps:
            logger.info(f"[provision] Pre-install step on {host}: {step}")
            pre_result = await _ssh.run(host, step, timeout=300)
            if pre_result.exit_code != 0:
                logger.warning(
                    f"[provision] Pre-install step failed (continuing): {pre_result.stderr}"
                )

        branch_flag = f"-b {branch}" if branch else ""
        logger.info(f"[provision] Cloning {git_url} to {host}:{target_path}")
        await _ssh.run(host, f"rm -rf {target_path}")
        result = await _ssh.run(
            host,
            f"git clone {branch_flag} {git_url} {target_path}",
            timeout=300,
        )
        if result.exit_code != 0:
            return json.dumps(
                {
                    "host": host,
                    "status": "failed",
                    "message": f"Git clone failed: {result.stderr}",
                }
            )

        install_cmd = provisioning.get("run_install_as_root")
        if not install_cmd:
            install_script = provisioning.get("install_script", "install.sh")
            install_cmd = f"./{install_script}"
        cmd = f"cd {target_path} && {install_cmd}"
        logger.info(f"[provision] Running install on {host}: {cmd}")
        result = await _ssh.run(host, cmd, timeout=900)

        return json.dumps(
            {
                "host": host,
                "harness": harness_name,
                "status": "success" if result.exit_code == 0 else "failed",
                "exit_code": result.exit_code,
                "install_path": target_path,
                "constraints": constraints,
                "contract": contract_result.get("deployed_files", []),
                "output": result.stdout or "" if result.stdout else "",
                "error": result.stderr or "" if result.stderr else "",
                "message": f"{harness_name} installed"
                if result.exit_code == 0
                else f"Install failed (exit {result.exit_code})",
            }
        )

    if install_method == "binary_download":
        install_cmd = provisioning.get("install_command")
        if not install_cmd:
            return json.dumps(
                {
                    "host": host,
                    "status": "failed",
                    "message": f"No install_command in private config for {harness_name}",
                }
            )

        logger.info(f"[provision] Installing {harness_name} binary on {host}")
        result = await _ssh.run(host, install_cmd, timeout=120)
        return json.dumps(
            {
                "host": host,
                "harness": harness_name,
                "status": "success" if result.exit_code == 0 else "failed",
                "exit_code": result.exit_code,
                "install_path": target_path,
                "output": result.stdout or "" if result.stdout else "",
                "error": result.stderr or "" if result.stderr else "",
                "message": (
                    f"{harness_name} binary installed"
                    if result.exit_code == 0
                    else f"Binary install failed (exit {result.exit_code})"
                ),
            }
        )

    if install_method == "container_image":
        image = provisioning.get("container_image")
        if not image:
            return json.dumps(
                {
                    "host": host,
                    "status": "failed",
                    "message": f"No container_image in private config for {harness_name}",
                }
            )

        logger.info(f"[provision] Pulling container image {image} on {host}")
        result = await _ssh.run(host, f"podman pull {image}", timeout=300)
        return json.dumps(
            {
                "host": host,
                "harness": harness_name,
                "status": "success" if result.exit_code == 0 else "failed",
                "exit_code": result.exit_code,
                "install_path": image,
                "output": result.stdout[-1000:] if result.stdout else "",
                "error": result.stderr[-1000:] if result.stderr else "",
                "message": (
                    f"{harness_name} container image pulled"
                    if result.exit_code == 0
                    else f"Image pull failed (exit {result.exit_code})"
                ),
            }
        )

    return json.dumps(
        {
            "host": host,
            "status": "failed",
            "message": f"Unknown install_method '{install_method}' for {harness_name}",
        }
    )


@mcp.tool()
async def verify_harness_install(
    host: str,
    harness_name: str,
    user: str = "root",
    install_path: str = "",
) -> str:
    """Verify that the benchmark harness is correctly installed and functional on a host. Uses private skill config's verify_command."""
    await _ensure_init()
    private_config = await _skill_provider.get_all_private_config(harness_name)
    provisioning = private_config.get("provisioning", {})
    path = install_path or provisioning.get(
        "install_target_path", f"/opt/{harness_name}"
    )
    verify_cmd = provisioning.get("verify_command", f"{path}/bin/{harness_name} help")

    result = await _ssh.run(host, verify_cmd)
    return json.dumps(
        {
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
    )


@mcp.tool()
async def check_existing_install(
    host: str,
    harness_name: str,
    install_path: str = "",
    user: str = "root",
) -> str:
    """Check if the benchmark harness is already installed on a host. Returns whether an installation exists and its version info."""
    await _ensure_init()
    private_config = await _skill_provider.get_all_private_config(harness_name)
    provisioning = private_config.get("provisioning", {})

    # Container-only harnesses have no install path — just check runtime
    if provisioning.get("skip_install") or provisioning.get("install_method") == "none":
        verify_cmd = provisioning.get("verify_command", "podman --version")
        result = await _ssh.run(host, verify_cmd, timeout=15)
        return json.dumps(
            {
                "host": host,
                "harness": harness_name,
                "installed": result.exit_code == 0,
                "install_path": "(container-based, no install path)",
                "output": result.stdout.strip() if result.stdout else "",
                "message": (
                    f"Runtime available: {result.stdout.strip()}"
                    if result.exit_code == 0
                    else "Runtime not found"
                ),
            }
        )

    path = install_path or provisioning.get(
        "install_target_path", f"/opt/{harness_name}"
    )
    verify_cmd = provisioning.get("verify_command", f"{path}/bin/{harness_name} help")

    # NOTE: ssh_debug dict removed — it previously leaked ssh.key_path (security issue)

    result = await _ssh.run(host, f"{verify_cmd} > /dev/null 2>&1")
    if result.exit_code == 0:
        version_result = await _ssh.run(
            host, f"cd {path} && git log --oneline -1 2>/dev/null"
        )
        return json.dumps(
            {
                "host": host,
                "harness": harness_name,
                "installed": True,
                "install_path": path,
                "version": version_result.stdout.strip()
                if version_result.exit_code == 0
                else "unknown",
                "message": f"{harness_name} is already installed at {path}",
            }
        )
    return json.dumps(
        {
            "host": host,
            "harness": harness_name,
            "installed": False,
            "install_path": path,
            "exit_code": result.exit_code,
            "stderr": result.stderr[:500] if result.stderr else "",
            "message": f"No {harness_name} installation found at {path} (exit_code={result.exit_code})",
        }
    )


@mcp.tool()
async def update_install(
    host: str,
    harness_name: str,
    install_path: str = "",
    user: str = "root",
) -> str:
    """Update an existing benchmark harness installation. Runs the harness-specific update command from private config."""
    await _ensure_init()
    private_config = await _skill_provider.get_all_private_config(harness_name)
    provisioning = private_config.get("provisioning", {})
    path = install_path or provisioning.get(
        "install_target_path", f"/opt/{harness_name}"
    )
    update_cmd = provisioning.get("update_command", f"cd {path} && git pull")

    logger.info(f"[provision] Running {harness_name} update on {host}")
    result = await _ssh.run(host, update_cmd, timeout=600)
    return json.dumps(
        {
            "host": host,
            "harness": harness_name,
            "status": "success" if result.exit_code == 0 else "failed",
            "exit_code": result.exit_code,
            "output": result.stdout or "" if result.stdout else "",
            "error": result.stderr or "" if result.stderr else "",
            "message": "Update completed"
            if result.exit_code == 0
            else f"Update failed (exit {result.exit_code})",
        }
    )


@mcp.tool()
async def uninstall_harness(
    host: str,
    harness_name: str,
    user: str = "root",
) -> str:
    """Remove an existing benchmark harness installation from a host. Must be called BEFORE install_harness when reinstalling. Removes the install directory and cleans up any moved/backup copies."""
    await _ensure_init()
    private_config = await _skill_provider.get_all_private_config(harness_name)
    provisioning = private_config.get("provisioning", {})
    result = await cleanup_harness(
        _ssh,
        host,
        harness_name,
        install_path=provisioning.get("install_target_path"),
        pre_uninstall_commands=provisioning.get("pre_uninstall_commands"),
    )
    return json.dumps(result)


@mcp.tool()
async def install_k3s(host: str, user: str = "root") -> str:
    """Install K3s (lightweight Kubernetes) on a host. K3s provides a single-node Kubernetes cluster that crucible uses for kube endpoints. Call this BEFORE install_harness when the ticket's directives include endpoint_type: kube."""
    await _ensure_init()
    logger.info(f"[provision] Installing K3s on {host}")

    selinux_result = await _ssh.run(host, "getenforce 2>/dev/null")
    if selinux_result.exit_code == 0 and selinux_result.stdout.strip() == "Enforcing":
        logger.info(f"[provision] Setting SELinux to permissive on {host}")
        await _ssh.run(host, "setenforce 0")

    result = await _ssh.run(
        host,
        "curl -sfL https://get.k3s.io | sh -",
        timeout=300,
    )
    if result.exit_code != 0:
        return json.dumps(
            {
                "host": host,
                "status": "failed",
                "message": f"K3s install failed: {result.stderr or ''}",
            }
        )

    for attempt in range(12):
        check = await _ssh.run(host, "k3s kubectl cluster-info 2>/dev/null")
        if check.exit_code == 0:
            break
        await _ssh.run(host, "sleep 5")
    else:
        return json.dumps(
            {
                "host": host,
                "status": "failed",
                "message": "K3s API server did not become ready within 60s",
            }
        )

    await _ssh.run(
        host,
        "k3s kubectl wait --for=condition=ready pod -l k8s-app=kube-dns "
        "-n kube-system --timeout=120s",
        timeout=150,
    )

    await _ssh.run(
        host,
        "mkdir -p /root/.kube && ln -sf /etc/rancher/k3s/k3s.yaml /root/.kube/config",
    )

    kubectl_check = await _ssh.run(host, "test -x /usr/local/bin/kubectl")
    if kubectl_check.exit_code != 0:
        await _ssh.run(host, "ln -sf /usr/local/bin/k3s /usr/local/bin/kubectl")

    self_ssh_ok = False
    keygen = await _ssh.run(
        host,
        'test -f /root/.ssh/id_rsa || ssh-keygen -t rsa -b 4096 -f /root/.ssh/id_rsa -C "k3s-self-ssh" -N ""',
    )
    if keygen.exit_code == 0:
        await _ssh.run(
            host,
            "cat /root/.ssh/id_rsa.pub >> /root/.ssh/authorized_keys && "
            "chmod 600 /root/.ssh/authorized_keys && "
            "sort -u /root/.ssh/authorized_keys -o /root/.ssh/authorized_keys",
        )
        verify = await _ssh.run(
            host,
            "ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes localhost hostname",
            timeout=15,
        )
        self_ssh_ok = verify.exit_code == 0

    node_result = await _ssh.run(host, "kubectl get nodes -o wide --no-headers")
    version_result = await _ssh.run(host, "k3s --version 2>/dev/null | head -1")

    return json.dumps(
        {
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
    )


@mcp.tool()
async def configure_host(host: str, config: dict, user: str = "root") -> str:
    """Apply OS-level configuration for optimal benchmark performance. Supports CPU isolation, hugepages, IRQ affinity, tuned profiles."""
    await _ensure_init()
    # Keep simulated for now -- tuning is benchmark-specific
    return json.dumps(
        {
            "host": host,
            "config_applied": config,
            "status": "success",
            "reboot_required": False,
            "message": f"Configuration applied on {host} (simulated)",
        }
    )


@mcp.tool()
async def get_private_config(harness_name: str, key: str) -> str:
    """Fetch private configuration for a benchmark harness. Returns organization-specific data like install method, repo paths, registry URLs, and constraints (supported OS, prerequisites). Use key='constraints' to check OS and platform requirements before attempting installation."""
    await _ensure_init()
    result = await _skill_provider.get_private_config(harness_name, key)
    if result is None:
        return json.dumps(
            {
                "key": key,
                "value": None,
                "message": f"No private config for {harness_name}.{key}",
            }
        )
    return json.dumps({"key": key, "value": result})


if __name__ == "__main__":
    mcp.run()
