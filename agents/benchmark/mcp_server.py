from __future__ import annotations

import json
import logging
import tempfile
import uuid
from pathlib import Path
from typing import Any

from providers.llm.base import ToolDefinition
from providers.skills.repo_cache import RepoCache
from providers.ssh import SSHExecutor

logger = logging.getLogger(__name__)

CONTROLLER_KEY_COMMENT = "agentic-perf-controller-key"


async def cleanup_controller_ssh_keys(
    ssh: SSHExecutor,
    controller: str,
    endpoints: list[str],
) -> dict:
    """Remove agentic-perf SSH keys from endpoints and the controller."""
    logger.info(f"[benchmark] Cleaning up SSH keys: {controller} -> {endpoints}")
    results = {}

    for endpoint in endpoints:
        result = await ssh.run(
            endpoint,
            f"sed -i '/{CONTROLLER_KEY_COMMENT}/d' /root/.ssh/authorized_keys",
        )
        results[endpoint] = "cleaned" if result.exit_code == 0 else f"failed: {result.stderr}"

    check = await ssh.run(
        controller,
        f"grep -q '{CONTROLLER_KEY_COMMENT}' /root/.ssh/id_rsa.pub 2>/dev/null && "
        f"rm -f /root/.ssh/id_rsa /root/.ssh/id_rsa.pub && echo REMOVED || echo SKIPPED",
    )
    controller_key = check.stdout.strip()
    results[f"{controller} (key pair)"] = "removed" if controller_key == "REMOVED" else "skipped (not ours)"

    return {
        "status": "success",
        "results": results,
    }


SKILLS_DIR = Path(__file__).resolve().parent.parent.parent / "skills"


def get_benchmark_tools(
    repo_cache: RepoCache | None = None,
) -> list[ToolDefinition]:
    skill_tools = [
        ToolDefinition(
            name="read_skill",
            description=(
                "Read a skill document containing critical lessons learned from "
                "prior benchmark runs. These are listed in the 'Skills' section "
                "of the ticket context. Read ALL skill docs before constructing "
                "a run file — they contain pitfalls that will cause failures."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "harness": {
                        "type": "string",
                        "description": "Harness name (e.g., 'crucible')",
                    },
                    "filename": {
                        "type": "string",
                        "description": "Skill filename (e.g., 'run-file-pitfalls.md')",
                    },
                },
                "required": ["harness", "filename"],
            },
        ),
    ]

    doc_tools = []
    if repo_cache:
        doc_tools = [
            ToolDefinition(
                name="list_harness_docs",
                description=(
                    "List documentation files available for a benchmark harness. "
                    "Returns file paths and sizes. Use this to discover what "
                    "reference material is available before constructing a run file."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "harness": {
                            "type": "string",
                            "description": "Harness name (e.g., 'crucible', 'zathras')",
                        },
                    },
                    "required": ["harness"],
                },
            ),
            ToolDefinition(
                name="read_harness_doc",
                description=(
                    "Read a documentation file from a benchmark harness repository. "
                    "Use this to learn about run-file format, endpoint structure, "
                    "benchmark parameters, or any other harness-specific details. "
                    "Call list_harness_docs first to see available files."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "harness": {
                            "type": "string",
                            "description": "Harness name (e.g., 'crucible')",
                        },
                        "path": {
                            "type": "string",
                            "description": "Relative path to the doc file (e.g., 'docs/how-run-files-work.md')",
                        },
                    },
                    "required": ["harness", "path"],
                },
            ),
        ]

    return skill_tools + doc_tools + [
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
            name="execute_benchmark",
            description=(
                "Execute the benchmark on the controller host. For crucible, sends a "
                "JSON run-file via SCP and runs 'crucible run'. For zathras, constructs "
                "a burden command. This may take several minutes."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "controller": {"type": "string", "description": "Controller hostname"},
                    "run_file": {"type": "object", "description": "Complete run-file/config content"},
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
            name="get_runfile_schema",
            description=(
                "Get the JSON schema that defines the structure of a valid run-file. "
                "Use this to understand what top-level keys, benchmark objects, endpoint "
                "structures, and mv-params formats are allowed. The schema enforces "
                "additionalProperties: false, so only documented keys are permitted."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "harness": {
                        "type": "string",
                        "description": "Benchmark harness (default: 'crucible')",
                    },
                },
                "required": [],
            },
        ),
        ToolDefinition(
            name="get_benchmark_params",
            description=(
                "Get the parameter definitions (multiplex.json) for a specific benchmark. "
                "Returns presets (named parameter sets like 'basic', 'default') and "
                "validations (regex patterns for allowed values per argument). Use this "
                "to understand what mv-params arguments are valid and what values they accept."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "benchmark": {
                        "type": "string",
                        "description": "Benchmark name (e.g., 'uperf', 'fio', 'trafficgen')",
                    },
                    "harness": {
                        "type": "string",
                        "description": "Benchmark harness (default: 'crucible')",
                    },
                },
                "required": ["benchmark"],
            },
        ),
        ToolDefinition(
            name="get_example_runfile",
            description=(
                "Get an example run-file for a benchmark. Use this as a structural "
                "reference when constructing your own run-file. The example shows "
                "the correct format for endpoints, mv-params, and benchmark configuration."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "benchmark": {
                        "type": "string",
                        "description": "Benchmark name (e.g., 'uperf', 'fio', 'trafficgen')",
                    },
                    "harness": {
                        "type": "string",
                        "description": "Benchmark harness (default: 'crucible')",
                    },
                    "endpoint_type": {
                        "type": "string",
                        "enum": ["remotehosts", "kube"],
                        "description": "Endpoint type to get an example for (default: 'remotehosts')",
                    },
                },
                "required": ["benchmark"],
            },
        ),
        ToolDefinition(
            name="present_runfile_for_approval",
            description=(
                "Present the constructed run-file to the user for review and approval. "
                "The user can approve, request changes, or reject. Returns a status string."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "run_file": {
                        "type": "object",
                        "description": "The complete run-file to present",
                    },
                    "benchmark": {
                        "type": "string",
                        "description": "Benchmark name for context",
                    },
                    "summary": {
                        "type": "string",
                        "description": "Brief summary of what this run-file will do",
                    },
                },
                "required": ["run_file"],
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
    repo_cache: RepoCache | None = None,
) -> tuple[dict[str, Any], SSHExecutor]:

    ssh = SSHExecutor(user="root")

    async def list_harness_docs(harness: str) -> dict:
        if not repo_cache:
            return {"docs": [], "message": "No repo cache configured"}
        docs = repo_cache.list_docs(harness, subdirs=["docs", "config"])
        if not docs:
            return {"docs": [], "message": f"No docs found for harness '{harness}'"}
        return {"docs": docs, "count": len(docs)}

    async def read_harness_doc(harness: str, path: str) -> dict:
        if not repo_cache:
            return {"found": False, "message": "No repo cache configured"}
        content = repo_cache.read_file(harness, path)
        if content is None:
            return {"found": False, "message": f"File not found: {harness}/{path}"}
        return {"found": True, "path": path, "content": content}

    async def read_skill(harness: str, filename: str) -> dict:
        skill_path = SKILLS_DIR / harness / filename
        if not skill_path.is_file():
            return {"found": False, "message": f"Skill not found: {harness}/{filename}"}
        resolved = skill_path.resolve()
        if not str(resolved).startswith(str(SKILLS_DIR.resolve())):
            return {"found": False, "message": "Invalid path"}
        return {"found": True, "filename": filename, "content": skill_path.read_text()}

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

        pubkey_result = await ssh.run(controller, "cat /root/.ssh/id_rsa.pub 2>/dev/null")
        if pubkey_result.exit_code != 0 or not pubkey_result.stdout.strip():
            keygen_result = await ssh.run(
                controller,
                f'ssh-keygen -t rsa -b 4096 -f /root/.ssh/id_rsa -C "{CONTROLLER_KEY_COMMENT}" -N ""',
            )
            if keygen_result.exit_code != 0:
                return {"status": "failed", "message": f"Key generation failed: {keygen_result.stderr}"}
            pubkey_result = await ssh.run(controller, "cat /root/.ssh/id_rsa.pub")

        pubkey = pubkey_result.stdout.strip()
        if CONTROLLER_KEY_COMMENT not in pubkey:
            await ssh.run(
                controller,
                f'rm -f /root/.ssh/id_rsa /root/.ssh/id_rsa.pub && ssh-keygen -t rsa -b 4096 -f /root/.ssh/id_rsa -C "{CONTROLLER_KEY_COMMENT}" -N ""',
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
                f'mkdir -p /root/.ssh && sed -i "/{CONTROLLER_KEY_COMMENT}/d" /root/.ssh/authorized_keys 2>/dev/null; echo "{pubkey}" >> /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys',
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

    async def execute_benchmark(
        controller: str,
        run_file: dict,
        harness: str | None = None,
        run_command: str | None = None,
    ) -> dict:
        import re

        run_uuid = uuid.uuid4().hex[:8]
        harness_name = harness or "crucible"

        if harness_name == "zathras":
            scenario = run_file.get("scenario", {})
            if not scenario and ("global" in run_file or "systems" in run_file):
                scenario = {
                    k: v for k, v in run_file.items()
                    if k not in ("harness", "local_config", "host_config_name", "tags")
                }
            local_config = run_file.get("local_config")
            host_config_name = run_file.get("host_config_name", "")

            if local_config and host_config_name:
                config_content = "\n".join(f"{k}: {v}" for k, v in local_config.items())
                await ssh.run(
                    controller,
                    f"mkdir -p /opt/zathras/local_configs && cat > /opt/zathras/local_configs/{host_config_name}.config << 'ZEOF'\n{config_content}\nZEOF",
                )

            ZATHRAS_NO_ARG_FLAGS = {
                "no_clean_up", "no_packages", "no_pip_packages",
                "no_system_packages", "no_spot_recover", "persistent_log",
                "preflight_check", "run_chronicler", "run_chronicler_strict",
                "skip_test_version_check", "ignore_repo_errors",
                "create_only", "force_upload", "verbose",
            }
            for section in ("global", "systems"):
                if section not in scenario:
                    continue
                if section == "global":
                    items = scenario["global"]
                    for key in list(items.keys()):
                        if key in ZATHRAS_NO_ARG_FLAGS and items[key] in (True, "true", "True", "yes"):
                            items[key] = ""
                        if key == "ssh_key_file" and isinstance(items[key], str) and items[key].startswith("~"):
                            items[key] = "/root" + items[key][1:]
                else:
                    for sys_name, sys_conf in scenario["systems"].items():
                        if not isinstance(sys_conf, dict):
                            continue
                        if "ssh_key_file" in sys_conf and isinstance(sys_conf["ssh_key_file"], str) and sys_conf["ssh_key_file"].startswith("~"):
                            sys_conf["ssh_key_file"] = "/root" + sys_conf["ssh_key_file"][1:]

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

            preflight_cmd = f"cd /opt/zathras && {burden_cmd} --preflight_check --scenario {scenario_path}"
            logger.info(f"[benchmark] Running zathras preflight: {preflight_cmd}")
            preflight = await ssh.run(controller, preflight_cmd, timeout=120)
            if preflight.exit_code != 0:
                return {
                    "status": "rejected",
                    "harness": "zathras",
                    "message": (
                        "Scenario failed zathras preflight_check and was NOT executed. "
                        "Fix the scenario and try again.\n"
                        + (preflight.stdout[-2000:] if preflight.stdout else "")
                        + (preflight.stderr[-1000:] if preflight.stderr else "")
                    ),
                }

            cmd = f"cd /opt/zathras && {burden_cmd} --scenario {scenario_path}"
            logger.info(f"[benchmark] Executing zathras: {cmd}")
            result = await ssh.run(controller, cmd, timeout=0, allocate_pty=True)

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

        # Stop stale valkey container if no run is active (crucible issue #607)
        valkey_check = await ssh.run(
            controller,
            "podman ps --format '{{.Names}}' 2>/dev/null | grep -q crucible-valkey"
            " && ! podman ps --format '{{.Names}}' 2>/dev/null | grep -q crucible-rickshaw-run"
            " && podman stop crucible-valkey 2>/dev/null && echo STOPPED || echo OK",
        )
        if "STOPPED" in (valkey_check.stdout or ""):
            logger.info(f"[benchmark] Stopped stale crucible-valkey container on {controller}")

        cmd = f"{run_command or 'crucible run'} {remote_path}"
        logger.info(f"[benchmark] Executing: {cmd}")
        result = await ssh.run(controller, cmd, timeout=0, allocate_pty=True)

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

    async def handle_get_runfile_schema(harness: str | None = None) -> dict:
        harness_name = harness or "crucible"
        if hasattr(skill_provider, "get_provider"):
            provider = skill_provider.get_provider(harness_name)
            schema = await provider.get_runfile_schema() if provider else None
        else:
            schema = await skill_provider.get_runfile_schema()
        if schema is None:
            return {"found": False, "message": f"No run-file schema for harness '{harness_name}'"}
        return {"found": True, "harness": harness_name, "schema": schema}

    async def handle_get_benchmark_params(
        benchmark: str, harness: str | None = None
    ) -> dict:
        harness_name = harness or "crucible"
        if hasattr(skill_provider, "get_provider"):
            provider = skill_provider.get_provider(harness_name)
            params = await provider.get_benchmark_params(benchmark) if provider else None
        else:
            params = await skill_provider.get_benchmark_params(benchmark)
        if params is None:
            return {"found": False, "message": f"No parameter definitions for '{benchmark}' in '{harness_name}'"}
        return {"found": True, "benchmark": benchmark, "harness": harness_name, "params": params}

    async def handle_get_example_runfile(
        benchmark: str, harness: str | None = None, endpoint_type: str | None = None
    ) -> dict:
        harness_name = harness or "crucible"
        ep_type = endpoint_type or "remotehosts"
        if hasattr(skill_provider, "get_provider"):
            provider = skill_provider.get_provider(harness_name)
            example = await provider.get_example_runfile(benchmark, endpoint_type=ep_type) if provider else None
        else:
            example = await skill_provider.get_example_runfile(benchmark, endpoint_type=ep_type)
        if example is None:
            return {"found": False, "message": f"No example run-file for '{benchmark}' ({ep_type}) in '{harness_name}'"}
        return {"found": True, "benchmark": benchmark, "harness": harness_name, "endpoint_type": ep_type, "run_file": example}

    async def handle_present_runfile_for_approval(
        run_file: dict,
        benchmark: str | None = None,
        summary: str | None = None,
    ) -> str:
        bench_label = f" for {benchmark}" if benchmark else ""
        summary_line = f"\n\n{summary}" if summary else ""
        question = (
            f"Please review this run-file{bench_label}{summary_line}\n\n"
            f"```json\n{json.dumps(run_file, indent=2)}\n```\n\n"
            "Do you approve this configuration? (approve / request changes / reject)"
        )
        await request_clarification_fn(question)
        return "Clarification requested. Ticket paused for user approval of run-file."

    async def request_clarification(question: str) -> str:
        await request_clarification_fn(question)
        return "Clarification requested. Ticket paused for human input."

    handlers = {
        "read_skill": read_skill,
        "get_execution_config": get_execution_config,
        "setup_controller_ssh_keys": setup_controller_ssh_keys,
        "execute_benchmark": execute_benchmark,
        "get_run_logs": get_run_logs,
        "request_clarification": request_clarification,
        "get_runfile_schema": handle_get_runfile_schema,
        "get_benchmark_params": handle_get_benchmark_params,
        "get_example_runfile": handle_get_example_runfile,
        "present_runfile_for_approval": handle_present_runfile_for_approval,
    }
    if repo_cache:
        handlers["list_harness_docs"] = list_harness_docs
        handlers["read_harness_doc"] = read_harness_doc
    return handlers, ssh
