from __future__ import annotations

import json
import logging
import tempfile
import uuid
from pathlib import Path
from typing import Any

from providers.llm.base import ToolDefinition
from providers.ssh import SSHExecutor

logger = logging.getLogger(__name__)


def get_benchmark_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="get_execution_config",
            description=(
                "Get the benchmark harness's execution configuration from private skills. "
                "Returns controller requirements, pre-run steps, run command, endpoint type, "
                "run file format, and defaults. The harness_name should be the harness that "
                "owns the benchmark (e.g., 'crucible' or 'zathras')."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "harness_name": {
                        "type": "string",
                        "description": "Benchmark harness name (e.g., 'crucible', 'zathras')",
                    },
                },
                "required": ["harness_name"],
            },
        ),
        ToolDefinition(
            name="setup_controller_ssh_keys",
            description=(
                "Set up passwordless SSH from the controller host to endpoint hosts. "
                "Generates a key pair on the controller if needed and copies the public "
                "key to each endpoint."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "controller": {"type": "string", "description": "Controller hostname"},
                    "endpoints": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Endpoint hostnames that the controller needs SSH access to",
                    },
                    "user": {"type": "string", "description": "SSH user (default: root)"},
                },
                "required": ["controller", "endpoints"],
            },
        ),
        ToolDefinition(
            name="generate_run_file",
            description=(
                "Generate a run-file or execution configuration for the benchmark. Uses the "
                "harness's skill provider to create a properly formatted config with benchmark "
                "parameters and endpoint definitions. The harness determines the output format "
                "(JSON runfile for crucible, CLI args for zathras, etc.)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "benchmark": {"type": "string", "description": "Benchmark name (e.g., 'fio', 'uperf', 'streams')"},
                    "harness": {"type": "string", "description": "Benchmark harness name (e.g., 'crucible', 'zathras')"},
                    "endpoints": {
                        "type": "array",
                        "description": "Host endpoints for the benchmark",
                        "items": {
                            "type": "object",
                            "properties": {
                                "host": {"type": "string"},
                                "user": {"type": "string"},
                                "roles": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["host", "roles"],
                        },
                    },
                    "controller": {"type": "string", "description": "Controller hostname or IP. Required so the run-file can set controller-ip-address when controller is also an endpoint."},
                    "tags": {"type": "object", "description": "Run tags", "additionalProperties": {"type": "string"}},
                    "userenv": {"type": "string", "description": "User environment / container image (crucible)"},
                    "osruntime": {"type": "string", "description": "OS runtime (crucible: 'podman', 'chroot')"},
                    "os_vendor": {"type": "string", "description": "OS vendor (zathras: 'rhel', 'ubuntu', etc.)"},
                },
                "required": ["benchmark", "endpoints"],
            },
        ),
        ToolDefinition(
            name="execute_benchmark",
            description=(
                "Execute the benchmark on the controller host. For crucible, sends a JSON "
                "run-file via SCP and runs 'crucible run'. For zathras, constructs a burden "
                "command from the run config. This may take several minutes. Returns the "
                "run ID and status."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "controller": {"type": "string", "description": "Controller hostname"},
                    "run_file": {"type": "object", "description": "Complete run-file/config content from generate_run_file"},
                    "harness": {"type": "string", "description": "Benchmark harness (e.g., 'crucible', 'zathras')"},
                    "run_command": {"type": "string", "description": "Run command from execution config (e.g., 'crucible run', 'burden')"},
                },
                "required": ["controller", "run_file"],
            },
        ),
        ToolDefinition(
            name="get_run_logs",
            description="Retrieve logs from a benchmark run on the controller.",
            input_schema={
                "type": "object",
                "properties": {
                    "controller": {"type": "string", "description": "Controller hostname"},
                    "run_id": {"type": "string", "description": "Run ID or run/results directory path"},
                    "harness": {"type": "string", "description": "Benchmark harness (e.g., 'crucible', 'zathras')"},
                    "results_dir_pattern": {"type": "string", "description": "Pattern for finding results (from execution config)"},
                },
                "required": ["controller", "run_id"],
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
            name="submit_benchmark_result",
            description="Submit the benchmark execution result when the run completes or fails.",
            input_schema={
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "benchmark_status": {"type": "string", "enum": ["completed", "failed"]},
                    "run_file_used": {"type": "object"},
                    "benchmark_duration": {"type": ["integer", "null"]},
                    "notes": {"type": "string"},
                },
                "required": ["run_id", "benchmark_status"],
            },
        ),
    ]


def create_benchmark_tool_handlers(
    skill_provider,
    request_clarification_fn=None,
) -> tuple[dict[str, Any], SSHExecutor]:

    ssh = SSHExecutor(user="root")

    async def get_execution_config(harness_name: str) -> dict:
        config = await skill_provider.get_all_private_config(harness_name)
        execution = config.get("execution", {})
        if not execution:
            return {
                "harness": harness_name,
                "found": False,
                "message": f"No execution config found for harness '{harness_name}'",
            }
        return {
            "harness": harness_name,
            "found": True,
            "controller_required": execution.get("controller_required", False),
            "run_command": execution.get("run_command", ""),
            "endpoint_type": execution.get("endpoint_type", "remotehosts"),
            "endpoint_user": execution.get("endpoint_user", "root"),
            "default_userenv": execution.get("default_userenv", "default"),
            "default_osruntime": execution.get("default_osruntime", "podman"),
            "pre_run": execution.get("pre_run", []),
            "run_file_format": execution.get("run_file_format", "json"),
            "results_dir_pattern": execution.get("results_dir_pattern", ""),
        }

    async def setup_controller_ssh_keys(
        controller: str,
        endpoints: list[str],
        user: str = "root",
    ) -> dict:
        logger.info(f"[benchmark] Setting up SSH keys: {controller} -> {endpoints}")

        KEY_COMMENT = "agentic-perf-controller-key"

        pubkey_result = await ssh.run(controller, "cat /root/.ssh/id_rsa.pub 2>/dev/null")
        if pubkey_result.exit_code != 0 or not pubkey_result.stdout.strip():
            keygen_result = await ssh.run(
                controller,
                f'ssh-keygen -t rsa -b 4096 -f /root/.ssh/id_rsa -C "{KEY_COMMENT}" -N ""',
            )
            if keygen_result.exit_code != 0:
                return {"status": "failed", "message": f"Key generation failed: {keygen_result.stderr}"}
            pubkey_result = await ssh.run(controller, "cat /root/.ssh/id_rsa.pub")

        pubkey = pubkey_result.stdout.strip()
        if KEY_COMMENT not in pubkey:
            await ssh.run(
                controller,
                f'rm -f /root/.ssh/id_rsa /root/.ssh/id_rsa.pub && ssh-keygen -t rsa -b 4096 -f /root/.ssh/id_rsa -C "{KEY_COMMENT}" -N ""',
            )
            pubkey_result = await ssh.run(controller, "cat /root/.ssh/id_rsa.pub")
            pubkey = pubkey_result.stdout.strip()

        results = {}

        for endpoint in endpoints:
            check = await ssh.run(
                controller,
                f'ssh -o ConnectTimeout=5 -o BatchMode=yes -o StrictHostKeyChecking=accept-new {user}@{endpoint} hostname',
            )
            if check.exit_code == 0:
                results[endpoint] = {"status": "already_accessible", "hostname": check.stdout.strip()}
                continue

            inject = await ssh.run(
                endpoint,
                f'mkdir -p /root/.ssh && grep -qF "{KEY_COMMENT}" /root/.ssh/authorized_keys 2>/dev/null || echo "{pubkey}" >> /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys',
            )
            if inject.exit_code != 0:
                results[endpoint] = {"status": "failed", "message": inject.stderr}
                continue

            verify = await ssh.run(
                controller,
                f'ssh -o ConnectTimeout=5 -o BatchMode=yes -o StrictHostKeyChecking=accept-new {user}@{endpoint} hostname',
            )
            results[endpoint] = {
                "status": "configured" if verify.exit_code == 0 else "failed",
                "hostname": verify.stdout.strip() if verify.exit_code == 0 else "",
                "message": verify.stderr if verify.exit_code != 0 else "",
            }

        all_ok = all(r["status"] in ("already_accessible", "configured") for r in results.values())
        return {
            "status": "success" if all_ok else "partial_failure",
            "results": results,
            "message": "All endpoints accessible" if all_ok else "Some endpoints failed SSH setup",
        }

    async def generate_run_file(
        benchmark: str,
        endpoints: list[dict],
        harness: str | None = None,
        controller: str | None = None,
        tags: dict | None = None,
        userenv: str | None = None,
        osruntime: str | None = None,
        os_vendor: str | None = None,
    ) -> dict:
        harness_name = harness or "crucible"
        exec_config = await skill_provider.get_all_private_config(harness_name)
        execution = exec_config.get("execution", {})

        resolved_endpoints = []
        resolve_host = controller or endpoints[0]["host"]
        for ep in endpoints:
            host = ep["host"]
            result = await ssh.run(
                resolve_host,
                f"python3 -c \"import socket; print(socket.gethostbyname('{host}'))\"",
            )
            ip = result.stdout.strip() if result.exit_code == 0 and result.stdout.strip() else host
            if ip != host:
                logger.info(f"[benchmark] Resolved {host} -> {ip}")
            resolved_endpoints.append({**ep, "host": ip})

        params: dict[str, Any] = {
            "endpoints": resolved_endpoints,
            "harness": harness_name,
            "endpoint_user": execution.get("endpoint_user", "root"),
        }

        if controller:
            ctrl_result = await ssh.run(
                controller,
                f"python3 -c \"import socket; print(socket.gethostbyname('{controller}'))\"",
            )
            controller_ip = ctrl_result.stdout.strip() if ctrl_result.exit_code == 0 else controller
            params["controller"] = controller_ip
            ep_ips = {ep["host"] for ep in resolved_endpoints}
            if controller_ip in ep_ips:
                params["controller_ip"] = controller_ip
                logger.info(
                    f"[benchmark] Controller is also an endpoint — "
                    f"setting controller-ip-address={controller_ip}"
                )

        if tags:
            params["tags"] = tags
        if userenv:
            params["userenv"] = userenv
        elif execution.get("default_userenv"):
            params["userenv"] = execution["default_userenv"]
        if osruntime:
            params["osruntime"] = osruntime
        elif execution.get("default_osruntime"):
            params["osruntime"] = execution["default_osruntime"]
        if os_vendor:
            params["os_vendor"] = os_vendor

        runfile_template = await skill_provider.generate_runfile(benchmark, params)
        return {"run_file": runfile_template.template, "status": "generated", "harness": harness_name}

    async def execute_benchmark(
        controller: str,
        run_file: dict,
        harness: str | None = None,
        run_command: str | None = None,
    ) -> dict:
        import re

        run_uuid = uuid.uuid4().hex[:8]
        harness_name = harness or "crucible"

        validation = await skill_provider.validate_runfile(run_file, harness_name)
        if not validation.get("valid", True):
            return {
                "status": "rejected",
                "message": (
                    "Run-file failed schema validation and was NOT sent to the controller. "
                    "Fix the run-file and try again. Errors:\n"
                    + "\n".join(f"  - {e}" for e in validation["errors"])
                ),
                "errors": validation["errors"],
            }

        if harness_name == "zathras":
            scenario = run_file.get("scenario", {})
            local_config = run_file.get("local_config")
            host_config_name = run_file.get("host_config_name", "")

            if local_config and host_config_name:
                config_content = "\n".join(f"{k}: {v}" for k, v in local_config.items())
                await ssh.run(
                    controller,
                    f"mkdir -p /opt/zathras/local_configs && cat > /opt/zathras/local_configs/{host_config_name}.config << 'ZEOF'\n{config_content}\nZEOF",
                )

            try:
                import yaml
                scenario_yaml = yaml.dump(scenario, default_flow_style=False)
            except ImportError:
                scenario_yaml = json.dumps(scenario, indent=2)

            scenario_path = f"/tmp/scenario-{run_uuid}.yml"
            await ssh.run(
                controller,
                f"cat > {scenario_path} << 'ZEOF'\n{scenario_yaml}\nZEOF",
            )

            burden_cmd = run_command or "/opt/zathras/bin/burden"
            cmd = f"cd /opt/zathras && {burden_cmd} --scenario {scenario_path}"
            logger.info(f"[benchmark] Executing zathras: {cmd}")
            result = await ssh.run(controller, cmd, timeout=3600)

            run_dir = ""
            run_dir_re = re.compile(r"Results stored in:\s*(\S+)")
            for line in result.stdout.split("\n"):
                m = run_dir_re.search(line)
                if m:
                    run_dir = m.group(1)
                    break

            return {
                "status": "completed" if result.exit_code == 0 else "failed",
                "exit_code": result.exit_code,
                "run_dir": run_dir,
                "run_id": run_dir.rstrip("/").split("/")[-1] if run_dir else f"zathras-{run_uuid}",
                "harness": "zathras",
                "output": result.stdout[-2000:] if result.stdout else "",
                "error": result.stderr[-1000:] if result.stderr else "",
                "message": "Benchmark completed" if result.exit_code == 0 else f"Benchmark failed (exit {result.exit_code})",
            }

        remote_path = f"/tmp/run-file-{run_uuid}.json"

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(run_file, f, indent=2)
            local_path = f.name

        logger.info(f"[benchmark] SCP run-file to {controller}:{remote_path}")
        scp_result = await ssh.copy_to(controller, local_path, remote_path)
        Path(local_path).unlink(missing_ok=True)

        if scp_result.exit_code != 0:
            return {
                "status": "failed",
                "message": f"Failed to copy run-file: {scp_result.stderr}",
            }

        cmd = f"{run_command or 'crucible run'} {remote_path}"
        logger.info(f"[benchmark] Executing: {cmd}")
        result = await ssh.run(controller, cmd, timeout=1800)

        run_dir = ""
        run_dir_re = re.compile(r"(/var/lib/crucible/run/[^/\s]+)")
        for line in result.stdout.split("\n"):
            m = run_dir_re.search(line)
            if m:
                run_dir = m.group(1)
                break

        run_id = ""
        if run_dir:
            dirname = run_dir.rstrip("/").split("/")[-1]
            uuid_match = re.search(r"--([0-9a-f-]{36})$", dirname)
            run_id = uuid_match.group(1) if uuid_match else dirname

        return {
            "status": "completed" if result.exit_code == 0 else "failed",
            "exit_code": result.exit_code,
            "run_dir": run_dir,
            "run_id": run_id or f"unknown-{run_uuid}",
            "harness": "crucible",
            "output": result.stdout[-2000:] if result.stdout else "",
            "error": result.stderr[-1000:] if result.stderr else "",
            "message": "Benchmark completed" if result.exit_code == 0 else f"Benchmark failed (exit {result.exit_code})",
        }

    async def get_run_logs(
        controller: str,
        run_id: str,
        harness: str | None = None,
        results_dir_pattern: str | None = None,
    ) -> dict:
        if run_id.startswith("/"):
            run_dir = run_id
        elif harness == "zathras":
            search_pattern = results_dir_pattern or "/tmp/results_*"
            result = await ssh.run(
                controller,
                f"ls -dt {search_pattern} 2>/dev/null | head -1",
            )
            run_dir = result.stdout.strip()
        else:
            result = await ssh.run(
                controller,
                f"ls -d /var/lib/crucible/run/*{run_id}* 2>/dev/null | head -1",
            )
            run_dir = result.stdout.strip()

        if not run_dir:
            return {"status": "not_found", "message": f"Run directory not found for {run_id}"}

        if harness == "zathras":
            log_result = await ssh.run(
                controller,
                f"find {run_dir} -name '*.log' -o -name '*.out' | head -5 | xargs tail -50 2>/dev/null",
            )
        else:
            log_result = await ssh.run(
                controller,
                f"test -f {run_dir}/crucible.log.xz && xzcat {run_dir}/crucible.log.xz | tail -100 || cat {run_dir}/crucible.log 2>/dev/null | tail -100",
            )

        return {
            "run_dir": run_dir,
            "log_lines": log_result.stdout[-3000:] if log_result.stdout else "",
            "status": "ok" if log_result.exit_code == 0 else "error",
        }

    async def request_clarification(question: str) -> str:
        await request_clarification_fn(question)
        return "Clarification requested. Ticket paused for human input."

    return {
        "get_execution_config": get_execution_config,
        "setup_controller_ssh_keys": setup_controller_ssh_keys,
        "generate_run_file": generate_run_file,
        "execute_benchmark": execute_benchmark,
        "get_run_logs": get_run_logs,
        "request_clarification": request_clarification,
    }, ssh
