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

import asyncio
import json
import logging
import re
import sys
from contextlib import AsyncExitStack
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
from providers.llm.base import ToolDefinition
from providers.ssh import SSHExecutor

logger = logging.getLogger(__name__)

mcp = FastMCP("provisioning-agent")

_SKILLS_DIR = Path(__file__).resolve().parent.parent.parent / "skills"

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
# Shared helpers
# ---------------------------------------------------------------------------


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

    Returns (filtered_hosts, skipped_results).  Skipped hosts get a
    ``status: skipped`` entry so the caller can merge them into the
    final result dict.
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


# ---------------------------------------------------------------------------
# Helper functions — single-host logic
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


async def _check_host_prerequisites_one(host: str) -> dict:
    """Check single host for required software."""
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
    return {
        "host": host,
        "prerequisites": prereqs,
        "all_met": all_met,
        "message": f"All prerequisites met on {host}"
        if all_met
        else f"Missing prerequisites on {host}",
    }


async def _install_packages_one(host: str, packages: list[str]) -> dict:
    """Install packages on a single host."""
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
    return response


BASE_HOST_PACKAGES = ["nmap-ncat"]


async def _ensure_prerequisites_one(
    host: str,
    is_controller: bool,
    extra_packages: list[str],
) -> dict:
    """Check and install missing prerequisites on a single host."""
    already_present = []
    to_install = list(BASE_HOST_PACKAGES)
    for pkg in extra_packages:
        if pkg not in to_install:
            to_install.append(pkg)

    if is_controller:
        prereqs = await _check_host_prerequisites_one(host)
        for pkg, info in prereqs.get("prerequisites", {}).items():
            if info.get("installed"):
                already_present.append(pkg)
            else:
                if pkg not in to_install:
                    to_install.append(pkg)
    else:
        for pkg in list(to_install):
            check = await _ssh.run(
                host,
                f"rpm -q {pkg} 2>/dev/null",
                timeout=10,
            )
            if check.exit_code == 0:
                already_present.append(pkg)
                to_install.remove(pkg)

    newly_installed = []
    failed = []
    if to_install:
        result = await _install_packages_one(host, to_install)
        if result.get("status") == "success":
            newly_installed = to_install
        else:
            failed = to_install

    return {
        "host": host,
        "already_present": already_present,
        "newly_installed": newly_installed,
        "failed": failed,
        "status": "success" if not failed else "failed",
    }


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
        reg = json.loads(result.stdout)
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
    """Remove a harness installation from a host.

    Takes an explicit ssh executor (rather than using the module-global
    _ssh) so it's directly importable and callable by other agents —
    e.g. the resource agent calls this during teardown, outside the
    provisioning MCP server process where _ssh is never initialized.
    """
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


async def _validate_and_deploy_contract(host: str, private_config: dict) -> dict:
    """Validate install contract secrets and deploy them to the host."""
    contract = private_config.get("install_contract")
    if not contract:
        return {"status": "ok", "deployed_files": [], "message": "No install contract"}

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
                    missing.append(f"{secret_key}: no path in secrets config")
                continue

            if _secrets_provider is None:
                if required:
                    missing.append(
                        f"{secret_key}: no secrets provider configured",
                    )
                continue

            local_path = await stack.enter_async_context(
                _secrets_provider.secret_file(secret_path),
            )
            if local_path is None:
                if required:
                    missing.append(
                        f"{description} ({secret_path}): not found in secrets store",
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
            scp_result = await _ssh.copy_to(
                host,
                item["local_path"],
                item["remote_path"],
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
                f"{host}:{item['remote_path']}",
            )

    for cmd in contract.get("pre_install_commands", []):
        logger.info(f"[provision] Contract pre-install on {host}: {cmd}")
        await _ssh.run(host, cmd, timeout=60)

    return {
        "status": "ok",
        "deployed_files": deployed,
        "message": f"Contract satisfied: {len(deployed)} file(s) deployed",
    }


async def _install_harness_one(
    host: str,
    harness_name: str,
    private_config: dict,
    provisioning: dict,
    constraints: dict,
    branch: str = "",
) -> dict:
    """Install harness on a single host."""
    install_method = provisioning.get("install_method", "git_clone")
    target_path = provisioning.get("install_target_path", f"/opt/{harness_name}")

    if provisioning.get("skip_install") or install_method == "none":
        verify_cmd = provisioning.get("verify_command", "podman --version")
        verify_result = await _ssh.run(host, verify_cmd, timeout=15)
        if verify_result.exit_code == 0:
            return {
                "host": host,
                "harness": harness_name,
                "status": "ready",
                "message": (
                    f"No installation needed for {harness_name}. "
                    f"Runtime verified: {verify_result.stdout.strip()}"
                ),
            }
        else:
            return {
                "host": host,
                "harness": harness_name,
                "status": "missing_runtime",
                "message": (
                    f"{harness_name} requires "
                    f"{provisioning.get('prerequisites', ['podman'])} "
                    f"but verification failed: {verify_result.stderr.strip()}"
                ),
            }

    platform_result = await validate_platform_contract(_ssh, host, private_config)
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
                "message": f"No installer_url in private config for {harness_name}",
            }

        installer_path = "/tmp/harness-install.sh"
        logger.info(f"[provision] Downloading installer from {installer_url}")
        dl_result = await _ssh.run(
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
        logger.info(f"[provision] Running installer on {host}: {cmd}")
        result = await _ssh.run(host, cmd, timeout=1800)

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
            logger.info(f"[provision] Post-install on {host}: {post_cmd}")
            post_result = await _ssh.run(host, post_cmd, timeout=120)
            if post_result.exit_code != 0:
                logger.warning(
                    f"[provision] Post-install command failed: {post_result.stderr}"
                )

        await _ssh.run(host, f"rm -f {installer_path}")

        return {
            "host": host,
            "harness": harness_name,
            "status": "success",
            "exit_code": 0,
            "install_path": target_path,
            "constraints": constraints,
            "contract": contract_result.get("deployed_files", []),
            "message": f"{harness_name} installed via public installer",
        }

    if install_method == "git_clone":
        git_url = provisioning.get("git_url")
        if not git_url:
            return {
                "host": host,
                "status": "failed",
                "message": f"No git_url in private config for {harness_name}",
            }

        pre_install_steps = provisioning.get("pre_install_steps", [])
        for step in pre_install_steps:
            logger.info(f"[provision] Pre-install step on {host}: {step}")
            pre_result = await _ssh.run(host, step, timeout=300)
            if pre_result.exit_code != 0:
                logger.warning(
                    f"[provision] Pre-install step failed (continuing): "
                    f"{pre_result.stderr}"
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
        logger.info(f"[provision] Running install on {host}: {cmd}")
        result = await _ssh.run(host, cmd, timeout=900)

        response = {
            "host": host,
            "harness": harness_name,
            "status": "success" if result.exit_code == 0 else "failed",
            "exit_code": result.exit_code,
            "install_path": target_path,
            "message": f"{harness_name} installed"
            if result.exit_code == 0
            else f"Install failed (exit {result.exit_code})",
        }
        if result.exit_code == 0:
            response["constraints"] = constraints
            response["contract"] = contract_result.get("deployed_files", [])
        else:
            response["output"] = result.stdout or ""
            response["error"] = result.stderr or ""
        return response

    if install_method == "binary_download":
        install_cmd = provisioning.get("install_command")
        if not install_cmd:
            return {
                "host": host,
                "status": "failed",
                "message": (f"No install_command in private config for {harness_name}"),
            }

        logger.info(f"[provision] Installing {harness_name} binary on {host}")
        result = await _ssh.run(host, install_cmd, timeout=120)
        response = {
            "host": host,
            "harness": harness_name,
            "status": "success" if result.exit_code == 0 else "failed",
            "exit_code": result.exit_code,
            "install_path": target_path,
            "message": (
                f"{harness_name} binary installed"
                if result.exit_code == 0
                else f"Binary install failed (exit {result.exit_code})"
            ),
        }
        if result.exit_code != 0:
            response["output"] = result.stdout or ""
            response["error"] = result.stderr or ""
        return response

    if install_method == "container_image":
        image = provisioning.get("container_image")
        if not image:
            return {
                "host": host,
                "status": "failed",
                "message": (f"No container_image in private config for {harness_name}"),
            }

        logger.info(f"[provision] Pulling container image {image} on {host}")
        result = await _ssh.run(host, f"podman pull {image}", timeout=300)
        response = {
            "host": host,
            "harness": harness_name,
            "status": "success" if result.exit_code == 0 else "failed",
            "exit_code": result.exit_code,
            "install_path": image,
            "message": (
                f"{harness_name} container image pulled"
                if result.exit_code == 0
                else f"Image pull failed (exit {result.exit_code})"
            ),
        }
        if result.exit_code != 0:
            response["output"] = result.stdout[-1000:] if result.stdout else ""
            response["error"] = result.stderr[-1000:] if result.stderr else ""
        return response

    return {
        "host": host,
        "status": "failed",
        "message": f"Unknown install_method '{install_method}' for {harness_name}",
    }


async def _verify_harness_install_one(
    host: str,
    harness_name: str,
    provisioning: dict,
    install_path: str = "",
) -> dict:
    """Verify harness installation on a single host."""
    path = install_path or provisioning.get(
        "install_target_path", f"/opt/{harness_name}"
    )
    verify_cmd = provisioning.get("verify_command", f"{path}/bin/{harness_name} help")

    result = await _ssh.run(host, verify_cmd)
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
    """Check for existing harness installation on a single host."""
    if provisioning.get("skip_install") or provisioning.get("install_method") == "none":
        verify_cmd = provisioning.get("verify_command", "podman --version")
        result = await _ssh.run(host, verify_cmd, timeout=15)
        return {
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

    path = install_path or provisioning.get(
        "install_target_path", f"/opt/{harness_name}"
    )
    verify_cmd = provisioning.get("verify_command", f"{path}/bin/{harness_name} help")

    result = await _ssh.run(host, f"{verify_cmd} > /dev/null 2>&1")
    if result.exit_code == 0:
        version_result = await _ssh.run(
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
    """Update harness on a single host."""
    path = install_path or provisioning.get(
        "install_target_path", f"/opt/{harness_name}"
    )
    update_cmd = provisioning.get("update_command", f"cd {path} && git pull")

    logger.info(f"[provision] Running {harness_name} update on {host}")
    result = await _ssh.run(host, update_cmd, timeout=600)
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
    """Install K3s on a single host."""
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
        return {
            "host": host,
            "status": "failed",
            "message": f"K3s install failed: {result.stderr or ''}",
        }

    for _attempt in range(12):
        check = await _ssh.run(host, "k3s kubectl cluster-info 2>/dev/null")
        if check.exit_code == 0:
            break
        await _ssh.run(host, "sleep 5")
    else:
        return {
            "host": host,
            "status": "failed",
            "message": "K3s API server did not become ready within 60s",
        }

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
        "test -f /root/.ssh/id_rsa || "
        'ssh-keygen -t rsa -b 4096 -f /root/.ssh/id_rsa -C "k3s-self-ssh" -N ""',
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
            "ssh -o StrictHostKeyChecking=accept-new "
            "-o BatchMode=yes localhost hostname",
            timeout=15,
        )
        self_ssh_ok = verify.exit_code == 0

    node_result = await _ssh.run(host, "kubectl get nodes -o wide --no-headers")
    version_result = await _ssh.run(host, "k3s --version 2>/dev/null | head -1")

    return {
        "host": host,
        "status": "success",
        "k3s_version": version_result.stdout.strip()
        if version_result.exit_code == 0
        else "unknown",
        "node_info": node_result.stdout.strip() if node_result.exit_code == 0 else "",
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
    """Check → install → verify for a single host."""
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


# ---------------------------------------------------------------------------
# MCP Tools — batched
# ---------------------------------------------------------------------------


@mcp.tool()
async def check_platform_contract(
    hosts: list[str], harness_name: str, user: str = "root"
) -> str:
    """Check if hosts meet the platform requirements (OS, repos, packages) for a benchmark harness. Call this before attempting installation to verify compatibility. Returns detected OS, missing repos, and missing packages per host. OS or repo mismatches are hard failures; missing packages are warnings (they can be installed)."""
    await _ensure_init()
    private_config = await _skill_provider.get_all_private_config(harness_name)
    results = await _gather_for_hosts(
        hosts, lambda h, *a: validate_platform_contract(_ssh, h, *a), private_config
    )
    return json.dumps(_summarize(results))


@mcp.tool()
async def check_host_prerequisites(hosts: list[str], user: str = "root") -> str:
    """Check if hosts have the required software installed (podman, git, jq, curl). Returns the status of each prerequisite per host."""
    await _ensure_init()
    results = await _gather_for_hosts(hosts, _check_host_prerequisites_one)
    return json.dumps(_summarize(results))


@mcp.tool()
async def install_packages(targets: list[dict], user: str = "root") -> str:
    """Install required packages on multiple hosts via the system package manager. Each target is {"host": "...", "packages": ["pkg1", "pkg2"]}."""
    await _ensure_init()
    coros = [_install_packages_one(t["host"], t["packages"]) for t in targets]
    raw = await asyncio.gather(*coros, return_exceptions=True)
    results: dict[str, dict] = {}
    for target, result in zip(targets, raw):
        host = target["host"]
        if isinstance(result, Exception):
            results[host] = {"status": "error", "message": str(result)}
        else:
            results[host] = result
    return json.dumps(_summarize(results))


@mcp.tool()
async def ensure_prerequisites(
    hosts: list[str],
    extra_packages: list[str] | None = None,
    controller_host: str = "",
    user: str = "root",
) -> str:
    """Check and install missing prerequisites on all hosts in one call.

    Harness prerequisites (podman, git, jq, curl) are checked and installed
    only on the controller_host. Base host packages (nmap-ncat) are always
    installed on ALL hosts for connectivity testing. Additional extra_packages
    are also installed on all hosts. Use this instead of calling
    check_host_prerequisites then install_packages separately.

    Args:
        hosts: List of host IPs to process
        extra_packages: Additional packages to install on all hosts
            beyond the base host packages
        controller_host: The controller IP — harness prereqs are only
            installed here. If empty, harness prereqs are skipped on
            all hosts (only extra_packages are installed).
    """
    await _ensure_init()
    extras = extra_packages or []

    async def _run_one(host: str) -> dict:
        is_controller = host == controller_host
        return await _ensure_prerequisites_one(host, is_controller, extras)

    results = await _gather_for_hosts(hosts, _run_one)
    return json.dumps(_summarize(results))


@mcp.tool()
async def install_harness(
    hosts: list[str],
    harness_name: str,
    user: str = "root",
    branch: str = "",
    controller_host: str = "",
) -> str:
    """Install the benchmark harness on multiple hosts. Uses private skill config to determine the install method. Validates and deploys required secrets from the install_contract before running the installer."""
    await _ensure_init()
    private_config = await _skill_provider.get_all_private_config(harness_name)
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
    return json.dumps(_summarize(results))


@mcp.tool()
async def verify_harness_install(
    hosts: list[str],
    harness_name: str,
    user: str = "root",
    install_path: str = "",
    controller_host: str = "",
) -> str:
    """Verify that the benchmark harness is correctly installed and functional on multiple hosts."""
    await _ensure_init()
    private_config = await _skill_provider.get_all_private_config(harness_name)
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
    return json.dumps(_summarize(results))


@mcp.tool()
async def check_existing_install(
    hosts: list[str],
    harness_name: str,
    install_path: str = "",
    user: str = "root",
    controller_host: str = "",
) -> str:
    """Check if the benchmark harness is already installed on multiple hosts. Returns whether an installation exists and its version info per host."""
    await _ensure_init()
    private_config = await _skill_provider.get_all_private_config(harness_name)
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
    return json.dumps(_summarize(results))


@mcp.tool()
async def update_install(
    hosts: list[str],
    harness_name: str,
    install_path: str = "",
    user: str = "root",
    controller_host: str = "",
) -> str:
    """Update an existing benchmark harness installation on multiple hosts."""
    await _ensure_init()
    private_config = await _skill_provider.get_all_private_config(harness_name)
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
    return json.dumps(_summarize(results))


@mcp.tool()
async def uninstall_harness(
    hosts: list[str],
    harness_name: str,
    user: str = "root",
    controller_host: str = "",
) -> str:
    """Remove an existing benchmark harness installation from multiple hosts. Must be called BEFORE install_harness when reinstalling."""
    await _ensure_init()
    private_config = await _skill_provider.get_all_private_config(harness_name)
    provisioning = private_config.get("provisioning", {})
    filtered, skipped = _filter_controller_only(
        hosts, controller_host, provisioning, harness_name
    )
    results = await _gather_for_hosts(
        filtered,
        lambda h, *a: cleanup_harness(_ssh, h, *a),
        harness_name,
        provisioning.get("install_target_path"),
        provisioning.get("pre_uninstall_commands"),
    )
    results.update(skipped)
    return json.dumps(_summarize(results))


@mcp.tool()
async def install_k3s(hosts: list[str], user: str = "root") -> str:
    """Install K3s (lightweight Kubernetes) on multiple hosts. K3s provides a single-node Kubernetes cluster that crucible uses for kube endpoints."""
    await _ensure_init()
    results = await _gather_for_hosts(hosts, _install_k3s_one)
    return json.dumps(_summarize(results))


@mcp.tool()
async def list_skill_docs(harness: str) -> str:
    """List available skill documents for a topic. Use 'general' for host-tuning, connectivity, and network-perf guides. Use a harness name (e.g. 'crucible') for harness-specific docs."""
    skill_dir = _SKILLS_DIR / harness
    if not skill_dir.is_dir():
        return json.dumps(
            {"found": False, "message": f"No skill directory for '{harness}'"}
        )
    files = [f.name for f in sorted(skill_dir.iterdir()) if f.suffix == ".md"]
    return json.dumps({"found": True, "harness": harness, "files": files})


def _read_skill_one(harness: str, filename: str) -> dict:
    skill_path = _SKILLS_DIR / harness / filename
    if not skill_path.is_file():
        return {"found": False, "message": f"Skill not found: {harness}/{filename}"}
    resolved = skill_path.resolve()
    if not str(resolved).startswith(str(_SKILLS_DIR.resolve())):
        return {"found": False, "message": "Invalid path"}
    return {"found": True, "filename": filename, "content": skill_path.read_text()}


@mcp.tool()
async def read_skill(harness: str, filename: str) -> str:
    """Read a skill document. Always read 'general/host-tuning.md' before applying any host tuning — it defines the required tool ordering, BBR+fq dependency, and irqbalance strategy."""
    return json.dumps(_read_skill_one(harness, filename))


@mcp.tool()
async def read_skills(docs: list[dict]) -> str:
    """Read multiple skill documents in one call. Use this instead of calling read_skill repeatedly — saves iterations when you need several docs at once (e.g. general/host-tuning.md + general/network-manager.md in one call)."""
    results = []
    for doc in docs:
        harness = doc.get("harness", "")
        filename = doc.get("filename", "")
        result = _read_skill_one(harness, filename)
        result["harness"] = harness
        result["filename"] = filename
        results.append(result)
    return json.dumps(results)


@mcp.tool()
async def disable_firewall(host: str, user: str = "root") -> str:
    """Flush all iptables/ip6tables rules and set default policies to ACCEPT on a host. Use this on dedicated benchmark hosts before running connectivity checks or benchmarks — fresh lab hosts often block benchmark ports (30002/30003 for uperf, etc.) by default. Do NOT call this on shared or production hosts."""
    await _ensure_init()
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
        r = await _ssh.run(host, cmd + " 2>&1")
        if r.exit_code == 0:
            results.append(label)
        else:
            errors.append(f"{label}: {r.stdout.strip()}")

    r_verify = await _ssh.run(host, "iptables -L INPUT -n | head -5")
    return json.dumps(
        {
            "host": host,
            "status": "error" if errors else "ok",
            "applied": results,
            "errors": errors,
            "iptables_input": r_verify.stdout.strip(),
        }
    )


@mcp.tool()
async def open_firewall_port(
    host: str,
    ports: list[int],
    protocol: str = "tcp",
    user: str = "root",
) -> str:
    """Open specific TCP/UDP ports in iptables on a host without flushing all rules. Use this when the host firewall should stay active but benchmark ports need to be explicitly allowed. For dedicated benchmark hosts with no firewall requirements, disable_firewall is simpler."""
    await _ensure_init()
    protos = ["tcp", "udp"] if protocol == "both" else [protocol]
    applied = []
    errors = []
    for port in ports:
        for proto in protos:
            cmd = (
                f"iptables -C INPUT -p {proto} --dport {port} -j ACCEPT 2>/dev/null"
                f" || iptables -I INPUT -p {proto} --dport {port} -j ACCEPT"
            )
            r = await _ssh.run(host, cmd + " 2>&1")
            if r.exit_code == 0:
                applied.append(f"{proto}/{port}")
            else:
                errors.append(f"{proto}/{port}: {r.stdout.strip()}")

    return json.dumps(
        {
            "host": host,
            "status": "error" if errors else "ok",
            "opened": applied,
            "errors": errors,
        }
    )


async def _tune_nic_one(
    host: str,
    interface: str,
    channels: int = 1,
    ring_rx: int | None = None,
    ring_tx: int | None = None,
    offloads: dict | None = None,
) -> dict:
    applied = []
    errors = []

    # Read current channel count
    r = await _ssh.run(host, f"ethtool -l {interface} 2>&1")
    before_channels: int | None = None
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
        r2 = await _ssh.run(host, f"ethtool -L {interface} combined {channels} 2>&1")
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


@mcp.tool()
async def tune_nic(
    host: str,
    interface: str,
    channels: int = 1,
    ring_rx: int | None = None,
    ring_tx: int | None = None,
    offloads: dict | None = None,
    user: str = "root",
    ssh_key_path: str = "",
) -> str:
    """Apply ethtool NIC settings on a host for benchmark preparation. Sets queue/channel count and optionally ring buffer sizes and offloads. MUST be called before pin_irq — changing channel count alters which IRQ numbers the NIC has. Returns before/after state for each setting."""
    await _ensure_init()
    return json.dumps(
        await _tune_nic_one(host, interface, channels, ring_rx, ring_tx, offloads)
    )


async def _tune_tcp_one(
    host: str,
    interface: str | None = None,
    congestion_control: str | None = None,
    qdisc: str | None = None,
    rmem_max: int | None = None,
    wmem_max: int | None = None,
    extra_sysctls: dict | None = None,
) -> dict:
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
        rt = await _ssh.run(host, f"tc qdisc replace dev {interface} root {qdisc} 2>&1")
        if rt.exit_code == 0:
            rv2 = await _ssh.run(host, f"tc qdisc show dev {interface} 2>&1")
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


@mcp.tool()
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
) -> str:
    """Apply TCP/network stack settings on a host. Sets congestion control and qdisc via sysctl AND applies the qdisc directly to existing interfaces via 'tc qdisc replace' — the sysctl alone only affects newly-created interfaces. BBR requires fq (not fq_codel) for per-flow pacing; always set both. Optionally sets socket buffer sizes (rmem_max, wmem_max)."""
    await _ensure_init()
    return json.dumps(
        await _tune_tcp_one(
            host,
            interface,
            congestion_control,
            qdisc,
            rmem_max,
            wmem_max,
            extra_sysctls,
        )
    )


def _device_path(interface: str, pci: str) -> str:
    if interface:
        return f"/sys/class/net/{interface}/device"
    if pci:
        return f"/sys/bus/pci/devices/{pci}"
    return ""


def _parse_cpu_range(text: str) -> list[int]:
    """Parse a Linux cpulist range string (e.g. "0-3,8,32-35") into ints."""
    cpus: list[int] = []
    for part in text.strip().split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            cpus.extend(range(int(lo), int(hi) + 1))
        else:
            cpus.append(int(part))
    return cpus


async def _discover_irqs(
    host: str,
    interface: str = "",
    pci: str = "",
    irqs: list[int] | None = None,
) -> tuple[list[int], str]:
    """Resolve IRQ numbers for a device. Returns (irq_numbers, resolved_pci).

    Primary method: list /sys/.../device/msi_irqs/, which has one entry per
    IRQ for any MSI/MSI-X device regardless of how the driver names its
    /proc/interrupts lines. This is required for drivers like mlx5, which
    name interrupts by PCI address (e.g. "mlx5_comp1@pci:0000:21:00.0") with
    no interface name present at all — a substring match against the
    interface name never finds them.

    Falls back to a /proc/interrupts substring match (interface name and/or
    PCI address) only if msi_irqs is unavailable (legacy INTx devices).
    """
    if irqs:
        return sorted(int(i) for i in irqs), pci

    device_path = _device_path(interface, pci)
    resolved_pci = pci

    if device_path and not resolved_pci:
        rp = await _ssh.run(host, f"basename $(readlink -f {device_path}) 2>&1")
        candidate = rp.stdout.strip()
        if candidate and "/" not in candidate:
            resolved_pci = candidate

    irq_numbers: list[int] = []
    if device_path:
        rm = await _ssh.run(host, f"ls {device_path}/msi_irqs/ 2>&1")
        candidates = sorted(int(tok) for tok in rm.stdout.split() if tok.isdigit())
        if candidates:
            # msi_irqs/ lists ALL allocated MSI-X vectors, including inactive
            # ones the driver has not bound to a queue. Inactive vectors have
            # no /proc/irq/N/ entry and smp_affinity writes fail against them.
            # Filter to only vectors the kernel has created irq dirs for.
            ra = await _ssh.run(host, "ls /proc/irq/ 2>/dev/null")
            active = {int(tok) for tok in ra.stdout.split() if tok.isdigit()}
            irq_numbers = sorted(n for n in candidates if n in active) if active else candidates

    if not irq_numbers and (interface or resolved_pci):
        ri = await _ssh.run(host, "cat /proc/interrupts 2>&1")
        seen = set()
        for line in ri.stdout.splitlines():
            if (interface and interface in line) or (
                resolved_pci and resolved_pci in line
            ):
                try:
                    seen.add(int(line.split(":")[0].strip()))
                except ValueError:
                    pass
        irq_numbers = sorted(seen)

    return irq_numbers, resolved_pci


async def _resolve_target_cpus(
    host: str,
    interface: str,
    pci: str,
    cpus: list[int] | None,
    numa_node: int | None,
) -> tuple[list[int], str, list[str]]:
    """Resolve the CPU list to round-robin IRQs across.

    Precedence: explicit `cpus` > explicit `numa_node` > auto-detected local
    NUMA node of the device. Returns (cpu_list, target_mode, errors).
    """
    if cpus:
        return list(cpus), "cpu_list", []

    if numa_node is not None:
        node = numa_node
        mode = f"numa_node:{node}"
    else:
        device_path = _device_path(interface, pci)
        if not device_path:
            return [], "", ["Cannot auto-detect NUMA node without interface or pci"]
        rn = await _ssh.run(host, f"cat {device_path}/numa_node 2>&1")
        try:
            node = int(rn.stdout.strip())
        except ValueError:
            node = -1
        if node < 0:
            # -1 means "no NUMA affinity" (e.g. single-node or virtualized
            # systems) — node 0 always exists in that case.
            node = 0
        mode = f"numa_node:{node} (auto-detected local node)"

    rc = await _ssh.run(host, f"cat /sys/devices/system/node/node{node}/cpulist 2>&1")
    try:
        cpu_list = _parse_cpu_range(rc.stdout)
    except ValueError:
        cpu_list = []
    if not cpu_list:
        return [], mode, [f"No CPUs found for NUMA node {node}"]
    return cpu_list, mode, []


async def _pin_irq_one(
    host: str,
    interface: str = "",
    pci: str = "",
    irqs: list[int] | None = None,
    cpus: list[int] | None = None,
    numa_node: int | None = None,
    irqbalance_mode: str = "ban_irq",
) -> dict:
    errors: list[str] = []
    applied: list[str] = []

    if not interface and not pci and not irqs:
        return {
            "host": host,
            "status": "error",
            "errors": ["Must provide interface, pci, or irqs to identify the device"],
        }

    irq_numbers, resolved_pci = await _discover_irqs(host, interface, pci, irqs)
    if not irq_numbers:
        ident = interface or pci or "device"
        return {
            "host": host,
            "interface": interface,
            "pci": resolved_pci,
            "status": "error",
            "errors": [
                f"No IRQs found for {ident} (checked msi_irqs and /proc/interrupts)"
            ],
        }

    cpu_list, target_mode, cpu_errors = await _resolve_target_cpus(
        host, interface, pci, cpus, numa_node
    )
    if cpu_errors:
        return {
            "host": host,
            "interface": interface,
            "pci": resolved_pci,
            "irq_numbers": irq_numbers,
            "status": "error",
            "errors": cpu_errors,
        }

    assignments: list[dict] = []
    for i, irq in enumerate(irq_numbers):
        cpu = cpu_list[i % len(cpu_list)]
        # Use smp_affinity_list (plain CPU number) rather than smp_affinity
        # (hex bitmask). The bitmask format requires comma-grouped 32-bit words
        # on systems with >32 CPUs (e.g. CPU 192 needs a 24-group mask), and a
        # raw large hex number is silently rejected. smp_affinity_list accepts
        # a plain integer regardless of CPU count — confirmed live on 768-CPU host.
        r2 = await _ssh.run(
            host, f"echo {cpu} > /proc/irq/{irq}/smp_affinity_list 2>&1"
        )
        if r2.exit_code != 0:
            errors.append(
                f"smp_affinity_list write failed for IRQ {irq}: {r2.stdout.strip()}"
            )
            continue
        # Verify the write actually landed — read back and confirm the CPU
        # appears (managed IRQs on some drivers silently ignore the write).
        rv = await _ssh.run(
            host, f"cat /proc/irq/{irq}/smp_affinity_list 2>&1"
        )
        actual = rv.stdout.strip()
        if str(cpu) not in actual.split(","):
            errors.append(
                f"smp_affinity_list verify failed for IRQ {irq}: "
                f"wrote {cpu}, got '{actual}'"
            )
        else:
            applied.append(f"IRQ {irq} → CPU {cpu}")
            assignments.append({"irq": irq, "cpu": cpu})

    used_cpus = sorted({a["cpu"] for a in assignments})

    ib_result = {"mode": irqbalance_mode}
    if irqbalance_mode == "disable":
        r3 = await _ssh.run(
            host, "systemctl mask irqbalance && systemctl stop irqbalance 2>&1"
        )
        ib_result["status"] = "masked" if r3.exit_code == 0 else "error"
        if r3.exit_code != 0:
            errors.append(f"irqbalance disable failed: {r3.stdout.strip()}")

    elif irqbalance_mode == "ban_irq":
        rb = await _ssh.run(
            host,
            "grep -s IRQBALANCE_BANNED_INTERRUPTS /etc/sysconfig/irqbalance || echo ''",
        )
        existing = ""
        for line in rb.stdout.splitlines():
            if "IRQBALANCE_BANNED_INTERRUPTS" in line:
                existing = line.split("=", 1)[-1].strip().strip('"')
        # Dedupe against whatever's already banned — re-running pin_irq
        # against the same device (e.g. a retry, or a prior run that failed
        # partway through) must not keep appending the same IRQ numbers.
        existing_irqs = {int(tok) for tok in existing.split() if tok.isdigit()}
        merged_irqs = sorted(existing_irqs | set(irq_numbers))
        new_val = " ".join(str(i) for i in merged_irqs)
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
        # Bans the union of all CPUs actually used for this device's IRQs
        # (round-robin pinning can spread IRQs across several CPUs, not
        # just one).
        rb = await _ssh.run(
            host, "grep -s IRQBALANCE_BANNED_CPUS /etc/sysconfig/irqbalance || echo ''"
        )
        existing = ""
        for line in rb.stdout.splitlines():
            if "IRQBALANCE_BANNED_CPUS" in line:
                existing = line.split("=", 1)[-1].strip().strip('"')
        try:
            merged_mask = int(existing, 16) if existing else 0
        except ValueError:
            merged_mask = 0
        for c in used_cpus:
            merged_mask |= 1 << c
        merged = hex(merged_mask)
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
        "pci": resolved_pci,
        "irq_numbers": irq_numbers,
        "cpus": cpu_list,
        "target_mode": target_mode,
        "assignments": assignments,
        "irqbalance": ib_result,
        "status": "error" if errors else "ok",
        "applied": applied,
        "errors": errors,
    }


@mcp.tool()
async def pin_irq(
    host: str,
    interface: str = "",
    pci: str = "",
    irqs: list[int] | None = None,
    cpus: list[int] | None = None,
    numa_node: int | None = None,
    irqbalance_mode: str = "ban_irq",
    user: str = "root",
    ssh_key_path: str = "",
) -> str:
    """Pin NIC IRQ(s) round-robin across CPUs and coordinate irqbalance so the pin is not overridden during a run. Must be called after tune_nic.

    Device selection (provide one): `interface` (e.g. ens1f0np0), `pci` (bus
    address, e.g. 0000:21:00.0), or explicit `irqs` (skips IRQ discovery
    entirely). IRQ discovery uses /sys/.../device/msi_irqs/, which works
    across NIC drivers that don't put the interface name in
    /proc/interrupts (e.g. mlx5/ConnectX).

    CPU targeting (checked in this order):
    - `cpus`: explicit CPU list — IRQs are round-robin assigned across it.
    - `numa_node`: round-robin across that NUMA node's CPUs (no local-node
      auto-detection — use this to intentionally pin to a non-local node).
    - neither: auto-detects the device's own local NUMA node and
      round-robins across its CPUs.

    irqbalance_mode: 'ban_irq' (default) adds the pinned IRQs to
    IRQBALANCE_BANNED_INTERRUPTS so irqbalance keeps running for other IRQs
    but won't touch these; 'ban_cpu' adds all CPUs used by this pin to
    IRQBALANCE_BANNED_CPUS; 'disable' masks and stops irqbalance entirely.
    Use reset_irq_pinning to undo a previous pin."""
    await _ensure_init()
    return json.dumps(
        await _pin_irq_one(host, interface, pci, irqs, cpus, numa_node, irqbalance_mode)
    )


async def _reset_irq_pinning_one(
    host: str,
    interface: str = "",
    pci: str = "",
    irqs: list[int] | None = None,
    cpus: list[int] | None = None,
) -> dict:
    errors: list[str] = []
    applied: list[str] = []

    if not interface and not pci and not irqs:
        return {
            "host": host,
            "status": "error",
            "errors": ["Must provide interface, pci, or irqs to identify the device"],
        }

    irq_numbers, resolved_pci = await _discover_irqs(host, interface, pci, irqs)
    if not irq_numbers:
        ident = interface or pci or "device"
        return {
            "host": host,
            "interface": interface,
            "pci": resolved_pci,
            "status": "error",
            "errors": [
                f"No IRQs found for {ident} (checked msi_irqs and /proc/interrupts)"
            ],
        }

    rd = await _ssh.run(host, "cat /proc/irq/default_smp_affinity 2>&1")
    default_mask = rd.stdout.strip() or "ffffffff"
    for irq in irq_numbers:
        r2 = await _ssh.run(
            host, f"echo {default_mask} > /proc/irq/{irq}/smp_affinity 2>&1"
        )
        if r2.exit_code == 0:
            applied.append(f"IRQ {irq} affinity restored to default ({default_mask})")
        else:
            errors.append(
                f"restore smp_affinity failed for IRQ {irq}: {r2.stdout.strip()}"
            )

    rb = await _ssh.run(
        host,
        "grep -s IRQBALANCE_BANNED_INTERRUPTS /etc/sysconfig/irqbalance || echo ''",
    )
    existing = ""
    for line in rb.stdout.splitlines():
        if "IRQBALANCE_BANNED_INTERRUPTS" in line:
            existing = line.split("=", 1)[-1].strip().strip('"')
    remaining = [
        tok for tok in existing.split() if tok.isdigit() and int(tok) not in irq_numbers
    ]
    new_val = " ".join(remaining)
    r3 = await _ssh.run(
        host,
        "sed -i '/IRQBALANCE_BANNED_INTERRUPTS/d' /etc/sysconfig/irqbalance 2>/dev/null; "
        + (
            f"echo 'IRQBALANCE_BANNED_INTERRUPTS=\"{new_val}\"' >> /etc/sysconfig/irqbalance; "
            if new_val
            else ""
        )
        + "true",
    )
    if r3.exit_code != 0:
        errors.append(
            f"clearing IRQBALANCE_BANNED_INTERRUPTS failed: {r3.stdout.strip()}"
        )
    else:
        applied.append(f"IRQBALANCE_BANNED_INTERRUPTS cleared for IRQs {irq_numbers}")

    if cpus:
        rbc = await _ssh.run(
            host, "grep -s IRQBALANCE_BANNED_CPUS /etc/sysconfig/irqbalance || echo ''"
        )
        existing_cpus_mask = ""
        for line in rbc.stdout.splitlines():
            if "IRQBALANCE_BANNED_CPUS" in line:
                existing_cpus_mask = line.split("=", 1)[-1].strip().strip('"')
        try:
            mask_val = int(existing_cpus_mask, 16) if existing_cpus_mask else 0
        except ValueError:
            mask_val = 0
        for c in cpus:
            mask_val &= ~(1 << c)
        new_cpu_mask = hex(mask_val)
        r4 = await _ssh.run(
            host,
            "sed -i '/IRQBALANCE_BANNED_CPUS/d' /etc/sysconfig/irqbalance 2>/dev/null; "
            + (
                f"echo 'IRQBALANCE_BANNED_CPUS=\"{new_cpu_mask}\"' >> /etc/sysconfig/irqbalance; "
                if mask_val
                else ""
            )
            + "true",
        )
        if r4.exit_code != 0:
            errors.append(
                f"clearing IRQBALANCE_BANNED_CPUS failed: {r4.stdout.strip()}"
            )
        else:
            applied.append(f"IRQBALANCE_BANNED_CPUS cleared for CPUs {cpus}")

    r5 = await _ssh.run(
        host, "systemctl unmask irqbalance 2>&1 && systemctl restart irqbalance 2>&1"
    )
    if r5.exit_code == 0:
        applied.append("irqbalance unmasked and restarted")
    else:
        errors.append(f"irqbalance restart failed: {r5.stdout.strip()}")

    return {
        "host": host,
        "interface": interface,
        "pci": resolved_pci,
        "irq_numbers": irq_numbers,
        "status": "error" if errors else "ok",
        "applied": applied,
        "errors": errors,
    }


@mcp.tool()
async def reset_irq_pinning(
    host: str,
    interface: str = "",
    pci: str = "",
    irqs: list[int] | None = None,
    cpus: list[int] | None = None,
    user: str = "root",
    ssh_key_path: str = "",
) -> str:
    """Undo a previous pin_irq call: restores default smp_affinity for the device's IRQs, removes them from IRQBALANCE_BANNED_INTERRUPTS, and unmasks+restarts irqbalance. Pass `cpus` (the CPU list previously used) to also clear IRQBALANCE_BANNED_CPUS entries for a prior ban_cpu pin. Safe to call unconditionally regardless of which irqbalance_mode was previously used — use this before re-tuning a host that may carry a stale pin from a previous ticket."""
    await _ensure_init()
    return json.dumps(await _reset_irq_pinning_one(host, interface, pci, irqs, cpus))


async def _verify_host_tuning_one(
    host: str,
    interface: str,
    expected: dict | None = None,
) -> dict:
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

    # IRQ affinity — verify every assigned IRQ, not just one. `msi_irqs`
    # discovery (via _discover_irqs) works for drivers that don't put the
    # interface name in /proc/interrupts (e.g. mlx5).
    irq_numbers, _resolved_pci = await _discover_irqs(host, interface=interface)

    irq_check: dict = {"irq_numbers": irq_numbers}
    expected_assignments = exp.get("irq_assignments")
    if irq_numbers:
        affinities: dict[int, str] = {}
        for irq in irq_numbers:
            ra = await _ssh.run(host, f"cat /proc/irq/{irq}/smp_affinity_list 2>&1")
            affinities[irq] = ra.stdout.strip()
        irq_check["cpu_affinity"] = affinities
        if expected_assignments:
            mismatches = []
            for a in expected_assignments:
                irq = a.get("irq")
                exp_cpu = a.get("cpu")
                actual = affinities.get(irq, "")
                if str(exp_cpu) not in actual.split(","):
                    mismatches.append(
                        {"irq": irq, "expected_cpu": exp_cpu, "actual": actual}
                    )
            irq_check["expected_assignments"] = expected_assignments
            irq_check["mismatches"] = mismatches
            irq_ok = not mismatches
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


@mcp.tool()
async def verify_host_tuning(
    host: str,
    interface: str,
    expected: dict | None = None,
    user: str = "root",
    ssh_key_path: str = "",
) -> str:
    """Verify that host tuning settings match expected values. Re-reads sysctl, ethtool channel count, IRQ CPU affinity, and irqbalance status. Returns pass/fail per parameter with actual values. Call after tuning to confirm settings applied, and after benchmarks to detect drift (e.g. irqbalance overriding a pin mid-run).

    For IRQ verification, pass `expected["irq_assignments"]` as the exact
    `assignments` list returned by pin_irq (e.g. [{"irq": 407, "cpu": 2},
    ...]) — every IRQ in the list is checked against its assigned CPU, not
    just the first one."""
    await _ensure_init()
    return json.dumps(await _verify_host_tuning_one(host, interface, expected))


@mcp.tool()
async def tune_hosts(targets: list[dict]) -> str:
    """Apply tuning to multiple hosts in one call. Runs tune_nic → tune_tcp → pin_irq → disable_firewall (if requested) → verify_host_tuning for each host concurrently. Use this instead of calling individual tuning tools one host at a time — saves multiple iterations. Always read general/host-tuning.md before calling this."""
    await _ensure_init()

    async def _tune_one(t: dict) -> dict:
        h = t.get("host", "")
        iface = t.get("interface", "")
        steps = []
        errors = []

        if t.get("channels") is not None:
            r = await _tune_nic_one(
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

        if t.get("congestion_control") or t.get("qdisc"):
            r = await _tune_tcp_one(
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

        pin_irq_result = None
        wants_pin = (
            t.get("pin_irq")
            or t.get("irq_cpus") is not None
            or t.get("irq_numa_node") is not None
        )
        if wants_pin:
            pin_irq_result = await _pin_irq_one(
                host=h,
                interface=iface,
                pci=t.get("irq_pci", ""),
                cpus=t.get("irq_cpus"),
                numa_node=t.get("irq_numa_node"),
                irqbalance_mode=t.get("irqbalance_mode", "ban_irq"),
            )
            steps.append({"pin_irq": pin_irq_result})
            if pin_irq_result.get("status") == "error":
                errors.extend(pin_irq_result.get("errors", []))

        if t.get("mtu") is not None:
            r = await _nm_set_mtu_one(host=h, interface=iface, mtu=t["mtu"])
            steps.append({"nm_set_mtu": r})
            if r.get("status") == "error":
                errors.append(r.get("error", "nm_set_mtu failed"))

        if t.get("disable_firewall"):
            r_json = await disable_firewall(host=h)
            r = json.loads(r_json)
            steps.append({"disable_firewall": r})
            if r.get("status") == "error":
                errors.extend(r.get("errors", []))

        expected = {}
        if t.get("congestion_control"):
            expected["congestion_control"] = t["congestion_control"]
        if t.get("qdisc"):
            expected["qdisc"] = t["qdisc"]
        if t.get("channels") is not None:
            expected["channels"] = t["channels"]
        if pin_irq_result and pin_irq_result.get("assignments"):
            expected["irq_assignments"] = pin_irq_result["assignments"]
        if expected:
            r = await _verify_host_tuning_one(
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

    hosts = [t.get("host", "") for t in targets]
    raw = await asyncio.gather(*[_tune_one(t) for t in targets], return_exceptions=True)
    results: dict[str, dict] = {}
    for host, result in zip(hosts, raw):
        if isinstance(result, Exception):
            results[host] = {"status": "error", "errors": [str(result)]}
        else:
            results[host] = result

    success = sum(1 for r in results.values() if r.get("status") == "ok")
    return json.dumps(
        {
            "results": results,
            "summary": (
                f"{len(results)} host(s): {success} ok, {len(results) - success} failed"
            ),
        }
    )


async def _nm_find_connection(host: str, interface: str) -> str:
    """Return the NM connection name that owns the interface, or the interface name."""
    r = await _ssh.run(
        host,
        f"nmcli -t -f NAME,DEVICE connection show --active 2>/dev/null"
        f" | grep ':{interface}$' | cut -d: -f1",
    )
    name = r.stdout.strip()
    return name if name else interface


async def _nm_set_mtu_one(host: str, interface: str, mtu: int) -> dict:
    conn = await _nm_find_connection(host, interface)
    r_before = await _ssh.run(
        host, f"ip link show {interface} 2>/dev/null | grep -o 'mtu [0-9]*'"
    )
    before_mtu = r_before.stdout.strip()

    r = await _ssh.run(
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

    r_after = await _ssh.run(
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


@mcp.tool()
async def nm_set_mtu(host: str, interface: str, mtu: int, user: str = "root") -> str:
    """Set the MTU on a network interface persistently via NetworkManager. Using 'ip link set mtu' is NOT persistent — NM overrides it on connection events. This tool modifies the NM connection profile and brings it up. Read skills/general/network-manager.md before calling. MTU 9000 requires end-to-end switch support — do not set it unless the user explicitly requests jumbo frames."""
    await _ensure_init()
    return json.dumps(await _nm_set_mtu_one(host, interface, mtu))


@mcp.tool()
async def nm_set_ip(
    host: str,
    interface: str,
    ip_cidr: str,
    gateway: str | None = None,
    dns: str | None = None,
    user: str = "root",
) -> str:
    """Configure a static IP address on an interface via NetworkManager. Used when a ticket requests a private test network. Modifies the NM connection profile and brings it up."""
    await _ensure_init()
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
        r = await _ssh.run(host, cmd + " 2>&1")
        if r.exit_code != 0:
            errors.append(r.stdout.strip())

    r_verify = await _ssh.run(
        host, f"ip addr show {interface} 2>/dev/null | grep 'inet '"
    )
    return json.dumps(
        {
            "host": host,
            "interface": interface,
            "connection": conn,
            "ip_cidr": ip_cidr,
            "gateway": gateway,
            "live_addresses": r_verify.stdout.strip(),
            "status": "error" if errors else "ok",
            "errors": errors,
        }
    )


@mcp.tool()
async def nm_set_dhcp(host: str, interface: str, user: str = "root") -> str:
    """Switch an interface to DHCP via NetworkManager."""
    await _ensure_init()
    conn = await _nm_find_connection(host, interface)
    r = await _ssh.run(
        host,
        f"nmcli connection modify '{conn}' ipv4.method auto ipv4.addresses '' ipv4.gateway '' 2>&1"
        f" && nmcli connection up '{conn}' 2>&1",
    )
    return json.dumps(
        {
            "host": host,
            "interface": interface,
            "connection": conn,
            "status": "ok" if r.exit_code == 0 else "error",
            "output": r.stdout.strip(),
        }
    )


@mcp.tool()
async def nm_show_connection(host: str, interface: str, user: str = "root") -> str:
    """Show the current NetworkManager connection profile for an interface. Returns IP method, addresses, MTU, and connection name. Use this to audit actual interface configuration before a benchmark."""
    await _ensure_init()
    conn = await _nm_find_connection(host, interface)
    r = await _ssh.run(
        host,
        f"nmcli connection show '{conn}' 2>/dev/null"
        f" | grep -E 'ipv4\\.method|ipv4\\.addresses|802-3-ethernet\\.mtu|GENERAL\\.STATE'",
    )
    live = await _ssh.run(
        host,
        f"ip link show {interface} 2>/dev/null | grep -o 'mtu [0-9]*';"
        f" ip addr show {interface} 2>/dev/null | grep 'inet '",
    )
    return json.dumps(
        {
            "host": host,
            "interface": interface,
            "connection": conn,
            "profile": r.stdout.strip(),
            "live": live.stdout.strip(),
        }
    )


@mcp.tool()
async def nm_verify_interface(
    host: str,
    interface: str,
    expected_mtu: int | None = None,
    expected_ip: str | None = None,
    user: str = "root",
) -> str:
    """Verify that a network interface matches expected configuration. Checks live state (ip link) not just the NM profile. Returns pass/fail per parameter: mtu, ip_address, state."""
    await _ensure_init()
    checks: dict[str, dict] = {}
    all_ok = True

    r_link = await _ssh.run(host, f"ip link show {interface} 2>/dev/null")
    link_output = r_link.stdout

    m = re.search(r"mtu (\d+)", link_output)
    actual_mtu = int(m.group(1)) if m else None
    mtu_ok = (expected_mtu is None) or (actual_mtu == expected_mtu)
    all_ok = all_ok and mtu_ok
    checks["mtu"] = {"actual": actual_mtu, "expected": expected_mtu, "ok": mtu_ok}

    r_addr = await _ssh.run(
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

    state_ok = "UP" in link_output and "state UP" in link_output
    checks["state"] = {"up": state_ok, "ok": state_ok}
    all_ok = all_ok and state_ok

    return json.dumps(
        {
            "host": host,
            "interface": interface,
            "all_ok": all_ok,
            "checks": checks,
        }
    )


@mcp.tool()
async def ensure_harness_installed(
    hosts: list[str],
    harness_name: str,
    user: str = "root",
    branch: str = "",
    install_path: str = "",
    controller_host: str = "",
) -> str:
    """Check if harness is installed on each host, install where missing, and verify all installations. Combines check_existing_install + install_harness + verify_harness_install into one batched call. Returns per-host status: already_installed, success, or failure details."""
    await _ensure_init()
    private_config = await _skill_provider.get_all_private_config(harness_name)
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
    return json.dumps(_summarize(results))


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


async def get_registered_tools() -> list[ToolDefinition]:
    """Introspect this server's registered @mcp.tool() functions.

    Returns ToolDefinition objects (name, description, input_schema) for
    every tool this server exposes over MCP. Useful for tests and tooling
    that need to inspect schemas without spawning the actual subprocess.
    """
    tools = await mcp.list_tools()
    return [
        ToolDefinition(
            name=t.name,
            description=t.description or "",
            input_schema=t.parameters,
        )
        for t in tools
    ]


if __name__ == "__main__":
    mcp.run()
