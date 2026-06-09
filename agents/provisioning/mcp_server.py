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
                "determine the install method: 'internal_repo' copies a local repo and runs "
                "its install script; 'git_clone' clones from a URL and runs install.sh."
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
    request_clarification_fn,
) -> tuple[dict[str, Any], SSHExecutor]:

    ssh = SSHExecutor(user="root")

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
        install_method = provisioning.get("install_method", "internal_repo")
        target_path = provisioning.get("install_target_path", f"/opt/{harness_name}")

        constraints = private_config.get("constraints", {})

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
                "output": result.stdout[-1000:] if result.stdout else "",
                "error": result.stderr[-1000:] if result.stderr else "",
                "message": f"{harness_name} installed" if result.exit_code == 0 else f"Install failed (exit {result.exit_code})",
            }

        local_repo = private_config.get("internal_repo_local_path")
        if not local_repo:
            return {
                "host": host,
                "status": "failed",
                "message": f"No internal_repo_local_path in private config for {harness_name}",
            }

        logger.info(f"[provision] Copying {local_repo} to {host}:{target_path}")
        await ssh.run(host, f"rm -rf {target_path}")
        scp_result = await ssh.copy_to(host, local_repo, target_path, timeout=120)
        if scp_result.exit_code != 0:
            return {
                "host": host,
                "status": "failed",
                "message": f"Failed to copy {harness_name} repo to {host}: {scp_result.stderr}",
            }

        effective_branch = branch if branch and branch.lower() not in ("latest", "default", "main") else ""
        release_var = private_config.get("release_env_var", "RELEASE")
        env = f"{release_var}={effective_branch}" if effective_branch else ""
        install_script = provisioning.get("install_script", private_config.get("install_script", "install.sh"))
        cmd = f"cd {target_path} && {env} ./{install_script}"
        logger.info(f"[provision] Running install on {host}: {cmd}")
        result = await ssh.run(host, cmd, timeout=900)

        return {
            "host": host,
            "harness": harness_name,
            "status": "success" if result.exit_code == 0 else "failed",
            "exit_code": result.exit_code,
            "install_path": target_path,
            "output": result.stdout[-1000:] if result.stdout else "",
            "error": result.stderr[-1000:] if result.stderr else "",
            "message": f"{harness_name} installed" if result.exit_code == 0 else f"Install failed (exit {result.exit_code})",
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

        result = await ssh.run(host, f"{verify_cmd} 2>&1 | head -3")
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
        "install_harness": install_harness,
        "verify_harness_install": verify_harness_install,
        "configure_host": configure_host,
        "get_private_config": get_private_config,
        "request_clarification": request_clarification,
    }
    return handlers, ssh
