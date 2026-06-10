from __future__ import annotations

import logging
from typing import Any

from providers.llm.base import ToolDefinition
from providers.ssh import SSHExecutor

logger = logging.getLogger(__name__)


def get_provisioning_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="check_host_prerequisites",
            description=(
                "Check if a host has the required software installed "
                "(podman, git, jq, curl). Returns the status of each prerequisite."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "IP or hostname"},
                    "user": {"type": "string", "description": "SSH user (default: root)"},
                },
                "required": ["host"],
            },
        ),
        ToolDefinition(
            name="install_packages",
            description="Install required packages on a host via the system package manager.",
            input_schema={
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "Target host"},
                    "packages": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Package names to install",
                    },
                    "user": {"type": "string", "description": "SSH user (default: root)"},
                },
                "required": ["host", "packages"],
            },
        ),
        ToolDefinition(
            name="install_harness",
            description=(
                "Install the benchmark harness on a host. Uses private skill config to "
                "determine the install method: 'public_install' downloads and runs the "
                "upstream installer with skill-driven flags; 'git_clone' clones from a "
                "URL and runs install.sh. Validates and deploys required secrets from "
                "the install_contract before running the installer."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "Target host"},
                    "harness_name": {"type": "string", "description": "Harness name (e.g., 'crucible', 'zathras')"},
                    "user": {"type": "string", "description": "SSH user (default: root)"},
                    "branch": {"type": "string", "description": "Specific git branch or release tag. Omit to install the default/latest version."},
                },
                "required": ["host", "harness_name"],
            },
        ),
        ToolDefinition(
            name="verify_harness_install",
            description=(
                "Verify that the benchmark harness is correctly installed and functional "
                "on a host. Uses private skill config's verify_command."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "Target host"},
                    "harness_name": {"type": "string", "description": "Harness name (e.g., 'crucible', 'zathras')"},
                    "user": {"type": "string", "description": "SSH user (default: root)"},
                    "install_path": {"type": "string", "description": "Install path override"},
                },
                "required": ["host", "harness_name"],
            },
        ),
        ToolDefinition(
            name="check_existing_install",
            description=(
                "Check if the benchmark harness is already installed on a host. "
                "Returns whether an installation exists and its version info."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "Target host"},
                    "harness_name": {"type": "string", "description": "Harness name (e.g., 'crucible', 'zathras')"},
                    "install_path": {"type": "string", "description": "Path to check (read from private config if omitted)"},
                    "user": {"type": "string", "description": "SSH user (default: root)"},
                },
                "required": ["host", "harness_name"],
            },
        ),
        ToolDefinition(
            name="update_install",
            description=(
                "Update an existing benchmark harness installation. "
                "Runs the harness-specific update command from private config."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "Target host"},
                    "harness_name": {"type": "string", "description": "Harness name (e.g., 'crucible', 'zathras')"},
                    "install_path": {"type": "string", "description": "Install path override"},
                    "user": {"type": "string", "description": "SSH user (default: root)"},
                },
                "required": ["host", "harness_name"],
            },
        ),
        ToolDefinition(
            name="uninstall_harness",
            description=(
                "Remove an existing benchmark harness installation from a host. "
                "Must be called BEFORE install_harness when reinstalling. "
                "Removes the install directory and cleans up any moved/backup copies."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "Target host"},
                    "harness_name": {"type": "string", "description": "Harness name (e.g., 'crucible', 'zathras')"},
                    "user": {"type": "string", "description": "SSH user (default: root)"},
                },
                "required": ["host", "harness_name"],
            },
        ),
        ToolDefinition(
            name="configure_host",
            description=(
                "Apply OS-level configuration for optimal benchmark performance. "
                "Supports CPU isolation, hugepages, IRQ affinity, tuned profiles."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "Target host"},
                    "user": {"type": "string", "description": "SSH user (default: root)"},
                    "config": {
                        "type": "object",
                        "description": "Configuration to apply",
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
                    "harness_name": {"type": "string", "description": "Harness name (e.g., 'crucible', 'zathras')"},
                    "key": {"type": "string", "description": "Config key to fetch (e.g., 'constraints', 'provisioning', 'execution')"},
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
                    "hosts_provisioned": {"type": "array", "items": {"type": "string"}},
                    "harness_version": {"type": "string"},
                    "harness_name": {"type": "string"},
                    "configuration_applied": {"type": "object"},
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

    async def _validate_and_deploy_contract(
        host: str, private_config: dict
    ) -> dict:
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

            if secrets_provider is None:
                if required:
                    missing.append(f"{secret_key}: no secrets provider configured")
                continue

            local_path = await secrets_provider.get_secret_file(secret_path)
            if local_path is None:
                if required:
                    missing.append(f"{description} ({secret_path}): not found in secrets store")
                continue

            resolved.append({
                "secret_key": secret_key,
                "local_path": str(local_path),
                "remote_path": entry["remote_path"],
                "description": description,
            })

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

    async def check_host_prerequisites(host: str, user: str = "root") -> dict:
        prereqs = {}
        for cmd in ["podman", "git", "jq", "curl"]:
            result = await ssh.run(host, f"which {cmd} 2>/dev/null && {cmd} --version 2>/dev/null | head -1")
            if result.exit_code == 0 and result.stdout.strip():
                lines = result.stdout.strip().split("\n")
                prereqs[cmd] = {"installed": True, "version": lines[-1] if len(lines) > 1 else lines[0]}
            else:
                prereqs[cmd] = {"installed": False, "version": None}

        all_met = all(p["installed"] for p in prereqs.values())
        return {
            "host": host,
            "prerequisites": prereqs,
            "all_met": all_met,
            "message": f"All prerequisites met on {host}" if all_met else f"Missing prerequisites on {host}",
        }

    async def install_packages(
        host: str, packages: list[str], user: str = "root"
    ) -> dict:
        pkg_list = " ".join(packages)
        result = await ssh.run(host, f"dnf install -y {pkg_list}", timeout=300)
        return {
            "host": host,
            "packages": packages,
            "status": "success" if result.exit_code == 0 else "failed",
            "exit_code": result.exit_code,
            "output": result.stdout[-500:] if result.stdout else "",
            "error": result.stderr[-500:] if result.stderr else "",
        }

    async def install_harness(
        host: str,
        harness_name: str,
        user: str = "root",
        branch: str = "",
    ) -> dict:
        private_config = await skill_provider.get_all_private_config(harness_name)
        provisioning = private_config.get("provisioning", {})
        install_method = provisioning.get("install_method", "git_clone")
        target_path = provisioning.get("install_target_path", f"/opt/{harness_name}")
        constraints = private_config.get("constraints", {})

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
            dl_result = await ssh.run(
                host,
                f"curl --fail --silent --output {installer_path} {installer_url} && chmod +x {installer_path}",
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
            result = await ssh.run(host, cmd, timeout=1800)

            if result.exit_code != 0:
                return {
                    "host": host,
                    "harness": harness_name,
                    "status": "failed",
                    "exit_code": result.exit_code,
                    "install_path": target_path,
                    "output": result.stdout[-1000:] if result.stdout else "",
                    "error": result.stderr[-1000:] if result.stderr else "",
                    "message": f"Install failed (exit {result.exit_code})",
                }

            for post_cmd in provisioning.get("post_install_commands", []):
                logger.info(f"[provision] Post-install on {host}: {post_cmd}")
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
                "output": result.stdout[-1000:] if result.stdout else "",
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
                pre_result = await ssh.run(host, step, timeout=300)
                if pre_result.exit_code != 0:
                    logger.warning(
                        f"[provision] Pre-install step failed (continuing): {pre_result.stderr}"
                    )

            branch_flag = f"-b {branch}" if branch else ""
            logger.info(f"[provision] Cloning {git_url} to {host}:{target_path}")
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
            logger.info(f"[provision] Running install on {host}: {cmd}")
            result = await ssh.run(host, cmd, timeout=900)

            return {
                "host": host,
                "harness": harness_name,
                "status": "success" if result.exit_code == 0 else "failed",
                "exit_code": result.exit_code,
                "install_path": target_path,
                "constraints": constraints,
                "contract": contract_result.get("deployed_files", []),
                "output": result.stdout[-1000:] if result.stdout else "",
                "error": result.stderr[-1000:] if result.stderr else "",
                "message": f"{harness_name} installed" if result.exit_code == 0 else f"Install failed (exit {result.exit_code})",
            }

        return {
            "host": host,
            "status": "failed",
            "message": f"Unknown install_method '{install_method}' for {harness_name}",
        }

    async def verify_harness_install(
        host: str,
        harness_name: str,
        user: str = "root",
        install_path: str | None = None,
    ) -> dict:
        private_config = await skill_provider.get_all_private_config(harness_name)
        provisioning = private_config.get("provisioning", {})
        path = install_path or provisioning.get("install_target_path", f"/opt/{harness_name}")
        verify_cmd = provisioning.get("verify_command", f"{path}/bin/{harness_name} help")

        result = await ssh.run(host, verify_cmd)
        return {
            "host": host,
            "harness": harness_name,
            "verified": result.exit_code == 0,
            "install_path": path,
            "output": result.stdout[:500] if result.stdout else "",
            "error": result.stderr[:500] if result.stderr else "",
            "message": f"{harness_name} verified" if result.exit_code == 0 else f"Verification failed: {result.stderr[:200]}",
        }

    async def check_existing_install(
        host: str,
        harness_name: str,
        install_path: str | None = None,
        user: str = "root",
    ) -> dict:
        private_config = await skill_provider.get_all_private_config(harness_name)
        provisioning = private_config.get("provisioning", {})
        path = install_path or provisioning.get("install_target_path", f"/opt/{harness_name}")
        verify_cmd = provisioning.get("verify_command", f"{path}/bin/{harness_name} help")

        result = await ssh.run(host, f"set -o pipefail; {verify_cmd} 2>&1 | head -3")
        if result.exit_code == 0:
            version_result = await ssh.run(host, f"cd {path} && git log --oneline -1 2>/dev/null")
            return {
                "host": host,
                "harness": harness_name,
                "installed": True,
                "install_path": path,
                "version": version_result.stdout.strip() if version_result.exit_code == 0 else "unknown",
                "message": f"{harness_name} is already installed at {path}",
            }
        return {
            "host": host,
            "harness": harness_name,
            "installed": False,
            "install_path": path,
            "message": f"No {harness_name} installation found at {path}",
        }

    async def update_install(
        host: str,
        harness_name: str,
        install_path: str | None = None,
        user: str = "root",
    ) -> dict:
        private_config = await skill_provider.get_all_private_config(harness_name)
        provisioning = private_config.get("provisioning", {})
        path = install_path or provisioning.get("install_target_path", f"/opt/{harness_name}")
        update_cmd = provisioning.get("update_command", f"cd {path} && git pull")

        logger.info(f"[provision] Running {harness_name} update on {host}")
        result = await ssh.run(host, update_cmd, timeout=600)
        return {
            "host": host,
            "harness": harness_name,
            "status": "success" if result.exit_code == 0 else "failed",
            "exit_code": result.exit_code,
            "output": result.stdout[-1000:] if result.stdout else "",
            "error": result.stderr[-500:] if result.stderr else "",
            "message": "Update completed" if result.exit_code == 0 else f"Update failed (exit {result.exit_code})",
        }

    async def uninstall_harness(
        host: str,
        harness_name: str,
        user: str = "root",
    ) -> dict:
        private_config = await skill_provider.get_all_private_config(harness_name)
        provisioning = private_config.get("provisioning", {})
        path = provisioning.get("install_target_path", f"/opt/{harness_name}")

        for cmd in provisioning.get("pre_uninstall_commands", []):
            logger.info(f"[provision] Pre-uninstall on {host}: {cmd}")
            await ssh.run(host, cmd, timeout=120)

        logger.info(f"[provision] Uninstalling {harness_name} from {host}:{path}")
        result = await ssh.run(host, f"rm -rf {path}", timeout=120)
        if result.exit_code != 0:
            return {
                "host": host,
                "harness": harness_name,
                "status": "failed",
                "message": f"Failed to remove {path}: {result.stderr}",
            }

        await ssh.run(host, f"rm -rf {path}-moved-on-*", timeout=60)

        return {
            "host": host,
            "harness": harness_name,
            "status": "success",
            "message": f"{harness_name} uninstalled from {path}",
        }

    async def configure_host(
        host: str, config: dict, user: str = "root"
    ) -> dict:
        # Keep simulated for now — tuning is benchmark-specific
        return {
            "host": host,
            "config_applied": config,
            "status": "success",
            "reboot_required": False,
            "message": f"Configuration applied on {host} (simulated)",
        }

    async def get_private_config(harness_name: str, key: str) -> Any:
        result = await skill_provider.get_private_config(harness_name, key)
        if result is None:
            return {"key": key, "value": None, "message": f"No private config for {harness_name}.{key}"}
        return {"key": key, "value": result}

    async def request_clarification(question: str) -> str:
        await request_clarification_fn(question)
        return "Clarification requested. Ticket paused for human input."

    handlers = {
        "check_host_prerequisites": check_host_prerequisites,
        "install_packages": install_packages,
        "check_existing_install": check_existing_install,
        "update_install": update_install,
        "uninstall_harness": uninstall_harness,
        "install_harness": install_harness,
        "verify_harness_install": verify_harness_install,
        "configure_host": configure_host,
        "get_private_config": get_private_config,
        "request_clarification": request_clarification,
    }
    return handlers, ssh
