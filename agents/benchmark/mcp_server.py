from __future__ import annotations

import json
import logging
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from agents.server_utils import tool_progress
from providers.llm.base import ToolDefinition
from providers.skills.repo_cache import RepoCache
from providers.ssh import SSHExecutor

logger = logging.getLogger(__name__)

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

    return (
        skill_tools
        + doc_tools
        + [
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
                name="execute_benchmark",
                description=(
                    "Execute the benchmark on the controller host. For crucible, sends a "
                    "JSON run-file via SCP and runs 'crucible run'. For zathras, constructs "
                    "a burden command. This may take several minutes."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "controller": {
                            "type": "string",
                            "description": "Controller hostname",
                        },
                        "run_file": {
                            "type": "object",
                            "description": "Complete run-file/config content",
                        },
                        "harness": {
                            "type": "string",
                            "description": "Benchmark harness (e.g., 'crucible', 'zathras')",
                        },
                        "run_command": {
                            "type": "string",
                            "description": "Run command from execution config (e.g., 'crucible run', 'burden')",
                        },
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
                        "controller": {
                            "type": "string",
                            "description": "Controller hostname",
                        },
                        "run_id": {
                            "type": "string",
                            "description": "Run ID or run/results directory path",
                        },
                        "harness": {
                            "type": "string",
                            "description": "Benchmark harness (e.g., 'crucible', 'zathras')",
                        },
                        "results_dir_pattern": {
                            "type": "string",
                            "description": "Pattern for finding results (from execution config)",
                        },
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
                        "question": {
                            "type": "string",
                            "description": "Question to ask",
                        },
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
                        "benchmark_status": {
                            "type": "string",
                            "enum": ["completed", "failed"],
                        },
                        "run_file_used": {"type": "object"},
                        "benchmark_duration": {"type": ["integer", "null"]},
                        "notes": {"type": "string"},
                    },
                    "required": ["run_id", "benchmark_status"],
                },
            ),
        ]
    )


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
        # Arcaflow plugins run as containers via podman — no private
        # execution config or harness installation is needed.
        if harness_name == "arcaflow-plugins":
            return {
                "harness": harness_name,
                "found": True,
                "controller_required": False,
                "run_command": "podman run",
                "endpoint_type": "remotehosts",
                "endpoint_user": "root",
                "run_file_format": "yaml",
                "results_dir_pattern": "",
                "note": (
                    "Arcaflow plugins are self-contained "
                    "containers. Pass the run_file directly "
                    "to execute_benchmark — input is piped "
                    "to the container via stdin. Supports "
                    "local execution when controller is "
                    "localhost/127.0.0.1."
                ),
            }

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
            "default_userenv": execution.get("default_userenv", "discover"),
            "default_osruntime": execution.get("default_osruntime", "podman"),
            "pre_run": execution.get("pre_run", []),
            "run_file_format": execution.get("run_file_format", "json"),
            "results_dir_pattern": execution.get("results_dir_pattern", ""),
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

        async def _benchmark_progress(output_line: str, elapsed: int) -> None:
            minutes = elapsed // 60
            await tool_progress(
                f"[{minutes}m] {output_line}",
                "execute_benchmark",
            )

        if harness_name == "kube-burner":
            try:
                import yaml

                yaml_dump = yaml.dump
            except ImportError:
                yaml_dump = None

            config = run_file.get("config", {})
            templates = run_file.get("templates", {})

            template_dir = f"/tmp/kb-{run_uuid}"
            config_path = f"{template_dir}/config.yml"

            await ssh.run(controller, f"mkdir -p {template_dir}")

            if yaml_dump:
                config_content = yaml_dump(config, default_flow_style=False)
            else:
                config_content = json.dumps(config, indent=2)

            await ssh.run(
                controller,
                f"cat > {config_path} << 'KBEOF'\n{config_content}\nKBEOF",
            )

            for tpl_name, tpl_content in templates.items():
                tpl_path = f"{template_dir}/{tpl_name}"
                await ssh.run(
                    controller,
                    f"cat > {tpl_path} << 'KBEOF'\n{tpl_content}\nKBEOF",
                )

            kb_cmd = run_command or "kube-burner init"
            cmd = (
                f"cd {template_dir} && {kb_cmd} -c {config_path} --uuid {run_uuid} 2>&1"
            )
            logger.info(f"[benchmark] Executing kube-burner: {cmd}")
            result = await ssh.run_with_progress(
                controller,
                cmd,
                progress_callback=_benchmark_progress,
            )

            response = {
                "status": "completed" if result.exit_code == 0 else "failed",
                "exit_code": result.exit_code,
                "run_id": f"kube-burner-{run_uuid}",
                "harness": "kube-burner",
                "message": (
                    "Benchmark completed"
                    if result.exit_code == 0
                    else f"Benchmark failed (exit {result.exit_code})"
                ),
            }
            if result.exit_code == 0:
                metrics_result = await ssh.run(
                    controller,
                    f"cat {template_dir}/collected-metrics/*.json 2>/dev/null | head -c 3000",
                    timeout=30,
                )
                if metrics_result.exit_code == 0 and metrics_result.stdout:
                    try:
                        response["result_summary"] = json.loads(metrics_result.stdout)
                    except json.JSONDecodeError:
                        response["result_summary"] = metrics_result.stdout[:3000]
            if result.exit_code != 0:
                response["output"] = result.stdout[-3000:] if result.stdout else ""
                response["error"] = result.stderr[-1000:] if result.stderr else ""
            return response

        if harness_name == "benchmark-runner":
            env_vars = dict(run_file.get("env_vars", {}))
            container_image = run_file.get(
                "container_image", "quay.io/benchmark-runner/benchmark-runner:latest"
            )
            artifacts_dir = run_file.get(
                "artifacts_dir", "/tmp/benchmark-runner-run-artifacts"
            )
            kubeconfig_path = run_file.get("kubeconfig_path", "/root/.kube/config")

            if "KUBEADMIN_PASSWORD" not in env_vars:
                password_path = run_file.get("kubeadmin_password_path", "")
                if password_path:
                    pw_result = await ssh.run(
                        controller, f"cat {password_path} 2>/dev/null"
                    )
                    if pw_result.exit_code == 0 and pw_result.stdout.strip():
                        env_vars["KUBEADMIN_PASSWORD"] = pw_result.stdout.strip()

            env_flags = " ".join(f'-e {k}="{v}"' for k, v in env_vars.items())

            await ssh.run(controller, f"mkdir -p {artifacts_dir}")

            cmd = (
                f"podman run --rm {env_flags} "
                f"-v {kubeconfig_path}:/root/.kube/config "
                f"-v {artifacts_dir}:{artifacts_dir} "
                f"--privileged "
                f"{container_image} 2>&1"
            )
            logger.info(f"[benchmark] Executing benchmark-runner: {cmd}")
            result = await ssh.run_with_progress(
                controller,
                cmd,
                progress_callback=_benchmark_progress,
            )

            artifacts_cmd = f"ls {artifacts_dir}/ 2>/dev/null | tail -1"
            artifacts_result = await ssh.run(controller, artifacts_cmd)
            run_dir = (
                artifacts_result.stdout.strip()
                if artifacts_result.exit_code == 0
                else ""
            )

            response = {
                "status": "completed" if result.exit_code == 0 else "failed",
                "exit_code": result.exit_code,
                "run_id": f"benchmark-runner-{run_uuid}",
                "run_dir": f"{artifacts_dir}/{run_dir}" if run_dir else "",
                "harness": "benchmark-runner",
                "message": (
                    "Benchmark completed"
                    if result.exit_code == 0
                    else f"Benchmark failed (exit {result.exit_code})"
                ),
            }
            if result.exit_code == 0 and run_dir:
                full_dir = f"{artifacts_dir}/{run_dir}"
                ls_result = await ssh.run(
                    controller,
                    f"ls -la {full_dir}/ 2>/dev/null | head -30",
                    timeout=30,
                )
                if ls_result.exit_code == 0 and ls_result.stdout:
                    response["result_summary"] = ls_result.stdout.strip()
            if result.exit_code != 0:
                response["output"] = result.stdout[-3000:] if result.stdout else ""
                response["error"] = result.stderr[-1000:] if result.stderr else ""
            return response

        if harness_name == "zathras":
            scenario = run_file.get("scenario", {})
            if not scenario and ("global" in run_file or "systems" in run_file):
                scenario = {
                    k: v
                    for k, v in run_file.items()
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
                "no_clean_up",
                "no_packages",
                "no_pip_packages",
                "no_system_packages",
                "no_spot_recover",
                "persistent_log",
                "preflight_check",
                "run_chronicler",
                "run_chronicler_strict",
                "skip_test_version_check",
                "ignore_repo_errors",
                "create_only",
                "force_upload",
                "verbose",
            }
            for section in ("global", "systems"):
                if section not in scenario:
                    continue
                if section == "global":
                    items = scenario["global"]
                    for key in list(items.keys()):
                        if key in ZATHRAS_NO_ARG_FLAGS and items[key] in (
                            True,
                            "true",
                            "True",
                            "yes",
                        ):
                            items[key] = ""
                        if (
                            key == "ssh_key_file"
                            and isinstance(items[key], str)
                            and items[key].startswith("~")
                        ):
                            items[key] = "/root" + items[key][1:]
                else:
                    for sys_name, sys_conf in scenario["systems"].items():
                        if not isinstance(sys_conf, dict):
                            continue
                        if (
                            "ssh_key_file" in sys_conf
                            and isinstance(sys_conf["ssh_key_file"], str)
                            and sys_conf["ssh_key_file"].startswith("~")
                        ):
                            sys_conf["ssh_key_file"] = (
                                "/root" + sys_conf["ssh_key_file"][1:]
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
                        + (preflight.stdout or "")
                        + (preflight.stderr or "")
                    ),
                }

            cmd = f"cd /opt/zathras && {burden_cmd} --scenario {scenario_path}"
            logger.info(f"[benchmark] Executing zathras: {cmd}")
            result = await ssh.run_with_progress(
                controller,
                cmd,
                progress_callback=_benchmark_progress,
            )

            run_dir = ""
            run_dir_re = re.compile(r"Results stored in:\s*(\S+)")
            for line in result.stdout.split("\n"):
                m = run_dir_re.search(line)
                if m:
                    run_dir = m.group(1)
                    break

            response = {
                "status": "completed" if result.exit_code == 0 else "failed",
                "exit_code": result.exit_code,
                "run_dir": run_dir,
                "run_id": run_dir.rstrip("/").split("/")[-1]
                if run_dir
                else f"zathras-{run_uuid}",
                "harness": "zathras",
                "message": "Benchmark completed"
                if result.exit_code == 0
                else f"Benchmark failed (exit {result.exit_code})",
            }
            if result.exit_code == 0 and run_dir:
                ls_result = await ssh.run(
                    controller,
                    f"ls -la {run_dir}/ 2>/dev/null | head -30",
                    timeout=30,
                )
                if ls_result.exit_code == 0 and ls_result.stdout:
                    response["result_summary"] = ls_result.stdout.strip()
            if result.exit_code != 0:
                response["output"] = result.stdout[-3000:] if result.stdout else ""
                response["error"] = result.stderr[-1000:] if result.stderr else ""
            return response

        if harness_name == "ioscale":
            try:
                import yaml

                yaml_dump = yaml.dump
            except ImportError:
                yaml_dump = None

            test_type = run_file.get("test_type", "fio")
            vm_config = run_file.get("vm_config", {})
            test_config = run_file.get("test_config", {})
            kubeconfig = run_file.get("kubeconfig", "/root/.kube/config")

            template_dir = f"/tmp/ioscale-{run_uuid}"
            await ssh.run(controller, f"mkdir -p {template_dir}")

            kc = f"KUBECONFIG={kubeconfig}"

            storage_class = vm_config.get("storage_class", "")
            if not storage_class:
                sc_result = await ssh.run(
                    controller,
                    f"{kc} oc get sc -o jsonpath='{{.items[0].metadata.name}}'",
                )
                storage_class = sc_result.stdout.strip()
                if not storage_class:
                    return {
                        "status": "failed",
                        "harness": "ioscale",
                        "message": "No StorageClass found on cluster",
                    }

            vm_name = f"ioscale-vm-{run_uuid}"
            ns = "default"
            cores = vm_config.get("cores", 4)
            memory = vm_config.get("memory", "8Gi")
            storage_size = vm_config.get("storage_size", "100Gi")
            vm_config.get(
                "image_url",
                "https://dl.fedoraproject.org/pub/fedora/linux/releases/43/"
                "Cloud/x86_64/images/Fedora-Cloud-Base-Generic-43-1.6.x86_64.qcow2",
            )

            await ssh.run(
                controller,
                f"{kc} ssh-keygen -t rsa -f {template_dir}/vm-key -N '' -q 2>/dev/null;"
                f" {kc} oc create secret generic vmkeyroot"
                f" --from-file=key={template_dir}/vm-key.pub"
                f" -n {ns} --dry-run=client -o yaml | {kc} oc apply -f -",
            )

            tpl_file = "geniotest.yml" if test_type == "fio" else "vmdbtest.yml"
            data_dv = f"data-{run_uuid}"
            vm_yaml_cmd = (
                f"sed"
                f" -e 's/ocs-storagecluster-ceph-rbd/{storage_class}/g'"
                f" -e 's/vm-test-io/{vm_name}/g'"
                f" -e 's/vm-test-db/{vm_name}/g'"
                f" -e 's/dataiotest/{data_dv}/g'"
                f" -e 's/datavolumedb/{data_dv}/g'"
                f" -e 's/vm-testvm/{vm_name}/g'"
                f" -e 's/vm-dataiotest/vm-{data_dv}/g'"
                f" -e 's/vm-datavolumedb/vm-{data_dv}/g'"
                f" -e 's/storage: 100Gi/storage: {storage_size}/g'"
                f" -e 's/cores: 4/cores: {cores}/g'"
                f" -e 's/sockets: 2/sockets: 1/g'"
                f" -e 's/memory: 8Gi/memory: {memory}/g'"
                f" /opt/ioscale/templates/{tpl_file}"
                f" > {template_dir}/vm.yaml"
            )
            await ssh.run(controller, vm_yaml_cmd)

            logger.info(f"[benchmark] Creating ioscale VM: {vm_name}")
            await ssh.run(
                controller,
                f"{kc} oc apply -f {template_dir}/vm.yaml -n {ns}",
            )

            for i in range(60):
                check = await ssh.run(
                    controller,
                    f"{kc} oc get vmi {vm_name} -n {ns}"
                    f" -o jsonpath='{{.status.phase}}' 2>/dev/null",
                )
                if check.stdout.strip() == "Running":
                    break
                await ssh.run(controller, "sleep 10")
            else:
                return {
                    "status": "failed",
                    "harness": "ioscale",
                    "message": f"VM {vm_name} did not reach Running in 10 minutes",
                }

            vm_ip_result = await ssh.run(
                controller,
                f"{kc} oc get vmi {vm_name} -n {ns}"
                f" -o jsonpath='{{.status.interfaces[0].ipAddress}}'",
            )
            vm_ip = vm_ip_result.stdout.strip()

            if test_type == "fio":
                fio_cfg = test_config.get("fio", {})
                config_dict = {
                    "vm": {"hosts": vm_name, "namespace": ns},
                    "storage": {
                        "devices": {vm_name: "vdc"},
                        "mount_point": "/root/tests/data",
                        "filesystem": "xfs",
                    },
                    "fio": {
                        "test_size": fio_cfg.get("test_size", "1G"),
                        "runtime": fio_cfg.get("runtime", 300),
                        "block_sizes": fio_cfg.get("block_sizes", "4k"),
                        "io_patterns": fio_cfg.get("io_patterns", "randread"),
                        "numjobs": fio_cfg.get("numjobs", 1),
                        "iodepth": fio_cfg.get("iodepth", 16),
                        "direct_io": fio_cfg.get("direct_io", 1),
                    },
                    "output": {
                        "directory": f"/root/fio-results-{run_uuid}",
                        "format": "json+",
                    },
                    "retry": {"interval": 30, "max_retries": 10},
                    "monitoring": {"task_monitor_interval": 60},
                    "migrate": {"workloads": "", "interval": 0},
                }
                config_path = f"{template_dir}/fio-config.yaml"
                if yaml_dump:
                    content = yaml_dump(config_dict, default_flow_style=False)
                else:
                    content = json.dumps(config_dict, indent=2)
                await ssh.run(
                    controller,
                    f"cat > {config_path} << 'IOEOF'\n{content}\nIOEOF",
                )
                cmd = (
                    f"cd /opt/ioscale/io-generic && "
                    f"{kc} python3 fio-tests.py -c {config_path} --yes-i-mean-it 2>&1"
                )
            else:
                db_cfg = test_config.get("database", {})
                config_dict = {
                    "description": f"ioscale {test_type} benchmark",
                    "storage": {
                        "mount_point": "/perf1",
                        "disk_list": "/dev/vdc",
                        "persistent": False,
                    },
                    "database": {
                        "hosts": vm_name,
                        "namespace": ns,
                        "warehouse_count": db_cfg.get("warehouse_count", 50),
                        "test_duration": db_cfg.get("test_duration", 15),
                    },
                    "test": {
                        "user_count": db_cfg.get("user_count", "1 5 10"),
                        "log_level": "INFO",
                    },
                    "retry": {"interval": 30, "max_retries": 10},
                    "monitoring": {"task_monitor_interval": 60},
                    "migrate": {"user_counts": "", "interval": 0},
                }
                config_path = f"{template_dir}/{test_type}-config.yaml"
                if yaml_dump:
                    content = yaml_dump(config_dict, default_flow_style=False)
                else:
                    content = json.dumps(config_dict, indent=2)
                await ssh.run(
                    controller,
                    f"cat > {config_path} << 'IOEOF'\n{content}\nIOEOF",
                )
                cmd = (
                    f"cd /opt/ioscale/db/{test_type} && "
                    f"{kc} python3 {test_type}.py -c {config_path} 2>&1"
                )

            logger.info(f"[benchmark] Executing ioscale {test_type}: {cmd}")
            result = await ssh.run_with_progress(
                controller,
                cmd,
                progress_callback=_benchmark_progress,
            )

            response = {
                "status": "completed" if result.exit_code == 0 else "failed",
                "exit_code": result.exit_code,
                "run_id": f"ioscale-{run_uuid}",
                "harness": "ioscale",
                "vm_name": vm_name,
                "vm_ip": vm_ip,
                "message": (
                    "Benchmark completed"
                    if result.exit_code == 0
                    else f"Benchmark failed (exit {result.exit_code})"
                ),
            }
            if result.exit_code == 0 and result.stdout:
                try:
                    response["result_summary"] = json.loads(result.stdout)
                except json.JSONDecodeError:
                    response["result_summary"] = result.stdout[:3000]
            if result.exit_code != 0:
                response["output"] = result.stdout[-3000:] if result.stdout else ""
                response["error"] = result.stderr[-1000:] if result.stderr else ""
            return response

        if harness_name == "vstorm":
            cli_args = run_file.get("cli_args", [])
            kubeconfig = run_file.get("kubeconfig", "/root/.kube/config")

            args_str = " ".join(cli_args)
            vs_cmd = run_command or "/opt/vstorm/vstorm"
            cmd = f"KUBECONFIG={kubeconfig} {vs_cmd} {args_str} 2>&1"
            logger.info(f"[benchmark] Executing vstorm: {cmd}")
            result = await ssh.run_with_progress(
                controller,
                cmd,
                progress_callback=_benchmark_progress,
            )

            batch_id = ""
            for line in (result.stdout or "").split("\n"):
                if "batch" in line.lower():
                    import re as _re

                    m = _re.search(r"[0-9a-f]{6}", line)
                    if m:
                        batch_id = m.group(0)
                        break

            response = {
                "status": "completed" if result.exit_code == 0 else "failed",
                "exit_code": result.exit_code,
                "run_id": f"vstorm-{batch_id or run_uuid}",
                "harness": "vstorm",
                "batch_id": batch_id,
                "message": (
                    "Benchmark completed"
                    if result.exit_code == 0
                    else f"Benchmark failed (exit {result.exit_code})"
                ),
            }
            if result.exit_code == 0 and result.stdout:
                try:
                    response["result_summary"] = json.loads(result.stdout)
                except json.JSONDecodeError:
                    response["result_summary"] = result.stdout[:3000]
            if result.exit_code != 0:
                response["output"] = result.stdout[-3000:] if result.stdout else ""
                response["error"] = result.stderr[-1000:] if result.stderr else ""
            return response

        if harness_name == "forge":
            project = run_file.get("project", "rhaiis")
            presets = run_file.get("presets", [])
            cli_args = run_file.get("cli_args", [])
            run_file.get("config_overrides", {})
            artifacts_dir = run_file.get(
                "artifacts_dir", f"/tmp/forge-artifacts-{run_uuid}"
            )
            kubeconfig = run_file.get("kubeconfig", "/root/.kube/config")

            forge_cmd = run_command or "cd /opt/forge && ./bin/run_cli"
            preset_flags = " ".join(f"--preset {p}" for p in presets)
            args_str = " ".join(cli_args)

            env_prefix = f"KUBECONFIG={kubeconfig} ARTIFACT_DIR={artifacts_dir}"

            await ssh.run(controller, f"mkdir -p {artifacts_dir}")

            prep_cmd = f"{env_prefix} {forge_cmd} {project} {preset_flags} prepare 2>&1"
            logger.info(f"[benchmark] Forge prepare: {prep_cmd}")
            prep_result = await ssh.run_with_progress(
                controller,
                prep_cmd,
                progress_callback=_benchmark_progress,
            )

            if prep_result.exit_code != 0:
                return {
                    "status": "failed",
                    "exit_code": prep_result.exit_code,
                    "phase": "prepare",
                    "run_id": f"forge-{run_uuid}",
                    "harness": "forge",
                    "project": project,
                    "output": prep_result.stdout[-3000:] if prep_result.stdout else "",
                    "error": prep_result.stderr[-1000:] if prep_result.stderr else "",
                    "message": f"Forge prepare failed (exit {prep_result.exit_code})",
                }

            test_cmd = (
                f"{env_prefix} {forge_cmd} {project} {preset_flags} test"
                f"{' ' + args_str if args_str else ''} 2>&1"
            )
            logger.info(f"[benchmark] Forge test: {test_cmd}")
            result = await ssh.run_with_progress(
                controller,
                test_cmd,
                progress_callback=_benchmark_progress,
            )

            ai_eval = "{}"
            eval_cmd = (
                f"find {artifacts_dir} -name ai_eval_payload.json"
                f" -exec cat {{}} \\; 2>/dev/null | head -1"
            )
            eval_result = await ssh.run(controller, eval_cmd)
            if eval_result.exit_code == 0 and eval_result.stdout.strip():
                ai_eval = eval_result.stdout.strip()

            response = {
                "status": "completed" if result.exit_code == 0 else "failed",
                "exit_code": result.exit_code,
                "run_id": f"forge-{run_uuid}",
                "harness": "forge",
                "project": project,
                "artifacts_dir": artifacts_dir,
                "ai_eval_payload": ai_eval,
                "message": (
                    "Benchmark completed"
                    if result.exit_code == 0
                    else f"Benchmark failed (exit {result.exit_code})"
                ),
            }
            if result.exit_code != 0:
                response["output"] = result.stdout[-3000:] if result.stdout else ""
                response["error"] = result.stderr[-1000:] if result.stderr else ""
            return response

        if harness_name == "clusterbuster":
            try:
                import yaml

                yaml_dump = yaml.dump
            except ImportError:
                yaml_dump = None

            job_file = run_file.get("job_file", {})
            template_dir = f"/tmp/clusterbuster-{run_uuid}"
            job_path = f"{template_dir}/job.yaml"

            await ssh.run(controller, f"mkdir -p {template_dir}")

            if yaml_dump:
                job_content = yaml_dump(job_file, default_flow_style=False)
            else:
                job_content = json.dumps(job_file, indent=2)

            await ssh.run(
                controller,
                f"cat > {job_path} << 'CBEOF'\n{job_content}\nCBEOF",
            )

            kubeconfig = run_file.get("kubeconfig", "/root/.kube/config")
            cb_cmd = run_command or "clusterbuster"
            cmd = f"KUBECONFIG={kubeconfig} {cb_cmd} -f {job_path} 2>&1"
            logger.info(f"[benchmark] Executing clusterbuster: {cmd}")
            result = await ssh.run_with_progress(
                controller,
                cmd,
                progress_callback=_benchmark_progress,
            )

            response = {
                "status": "completed" if result.exit_code == 0 else "failed",
                "exit_code": result.exit_code,
                "run_id": f"clusterbuster-{run_uuid}",
                "harness": "clusterbuster",
                "message": (
                    "Benchmark completed"
                    if result.exit_code == 0
                    else f"Benchmark failed (exit {result.exit_code})"
                ),
            }
            if result.exit_code == 0 and result.stdout:
                try:
                    response["result_summary"] = json.loads(result.stdout)
                except json.JSONDecodeError:
                    response["result_summary"] = result.stdout[:3000]
            if result.exit_code != 0:
                response["output"] = result.stdout[-3000:] if result.stdout else ""
                response["error"] = result.stderr[-1000:] if result.stderr else ""
            return response

        if harness_name == "k8s-netperf":
            config = run_file.get("config", {})
            cli_flags = run_file.get("cli_flags", [])

            template_dir = f"/tmp/k8s-netperf-{run_uuid}"
            config_path = f"{template_dir}/netperf.yml"

            await ssh.run(controller, f"mkdir -p {template_dir}")

            # v1 flat-dict YAML: k8s-netperf's Go yaml.v3 parser
            # expects {testName: Config} not {tests: [{testName: Config}]}
            tests = config.get("tests", config)
            lines = ["---"]
            if isinstance(tests, list):
                for test in tests:
                    if isinstance(test, dict):
                        for name, params in test.items():
                            lines.append(f"{name}:")
                            if isinstance(params, dict):
                                for k, v in params.items():
                                    lines.append(f"  {k}: {json.dumps(v)}")
            elif isinstance(tests, dict):
                for name, params in tests.items():
                    lines.append(f"{name}:")
                    if isinstance(params, dict):
                        for k, v in params.items():
                            lines.append(f"  {k}: {json.dumps(v)}")
            config_content = "\n".join(lines)

            await ssh.run(
                controller,
                f"cat > {config_path} << 'NPEOF'\n{config_content}\nNPEOF",
            )

            setup_cmds = [
                "kubectl create ns netperf --dry-run=client -o yaml | kubectl apply -f -",
                "kubectl create sa netperf -n netperf --dry-run=client -o yaml | kubectl apply -f -",
                "kubectl label node --all node-role.kubernetes.io/worker= --overwrite",
                "kubectl delete ns netperf --wait=true --ignore-not-found",
                "kubectl create ns netperf",
                "kubectl create sa netperf -n netperf",
            ]
            for setup_cmd in setup_cmds:
                await ssh.run(controller, setup_cmd, timeout=60)

            flags_str = " ".join(cli_flags)
            np_cmd = run_command or "k8s-netperf"
            cmd = f"{np_cmd} --config {config_path} {flags_str} --json 2>&1"
            logger.info(f"[benchmark] Executing k8s-netperf: {cmd}")
            result = await ssh.run_with_progress(
                controller,
                cmd,
                progress_callback=_benchmark_progress,
            )

            response = {
                "status": "completed" if result.exit_code == 0 else "failed",
                "exit_code": result.exit_code,
                "run_id": f"k8s-netperf-{run_uuid}",
                "harness": "k8s-netperf",
                "message": (
                    "Benchmark completed"
                    if result.exit_code == 0
                    else f"Benchmark failed (exit {result.exit_code})"
                ),
            }
            if result.exit_code == 0 and result.stdout:
                try:
                    response["result_summary"] = json.loads(result.stdout)
                except json.JSONDecodeError:
                    response["result_summary"] = result.stdout[:3000]
            if result.exit_code != 0:
                response["output"] = result.stdout[-3000:] if result.stdout else ""
                response["error"] = result.stderr[-1000:] if result.stderr else ""
            return response

        if harness_name == "arcaflow-plugins":
            import asyncio as _asyncio

            plugin_image = run_file.get("plugin_image", "")
            plugin_input = run_file.get("input", {})
            plugin_step = run_file.get("plugin_step", "")

            if not plugin_image:
                return {
                    "status": "failed",
                    "exit_code": -1,
                    "run_id": f"arcaflow-{run_uuid}",
                    "harness": "arcaflow-plugins",
                    "output": "",
                    "error": "No plugin_image specified in run file",
                    "message": "Missing plugin_image",
                }

            # Determine if we can run locally (no SSH needed)
            is_local = controller in (
                "localhost",
                "127.0.0.1",
                "::1",
            )

            # Serialize input as YAML if pyyaml available, else JSON
            try:
                import yaml

                input_content = yaml.dump(plugin_input, default_flow_style=False)
            except ImportError:
                input_content = json.dumps(plugin_input, indent=2)

            if is_local:
                # Run directly via subprocess — no SSH needed
                logger.info(f"[benchmark] Local execution: podman run {plugin_image}")

                # Verify podman is available locally
                podman_path = shutil.which("podman")
                if not podman_path:
                    return {
                        "status": "failed",
                        "exit_code": -1,
                        "run_id": f"arcaflow-{run_uuid}",
                        "harness": "arcaflow-plugins",
                        "output": "",
                        "error": "podman not found locally",
                        "message": ("Arcaflow plugins require podman"),
                    }

                # Build container args: optional -s step,
                # then -f - for stdin input
                container_args = []
                if plugin_step:
                    container_args += ["-s", plugin_step]
                container_args += ["-f", "-"]

                proc = await _asyncio.create_subprocess_exec(
                    podman_path,
                    "run",
                    "-i",
                    "--rm",
                    "--network=host",
                    plugin_image,
                    *container_args,
                    stdin=_asyncio.subprocess.PIPE,
                    stdout=_asyncio.subprocess.PIPE,
                    stderr=_asyncio.subprocess.PIPE,
                )
                stdout_bytes, stderr_bytes = await proc.communicate(
                    input=input_content.encode()
                )
                exit_code = proc.returncode or 0
                stdout_str = stdout_bytes.decode(errors="replace")
                stderr_str = stderr_bytes.decode(errors="replace")
            else:
                # Run via SSH on the remote target
                # Verify podman is available on the target
                podman_check = await ssh.run(controller, "which podman", timeout=10)
                if podman_check.exit_code != 0:
                    return {
                        "status": "failed",
                        "exit_code": -1,
                        "run_id": f"arcaflow-{run_uuid}",
                        "harness": "arcaflow-plugins",
                        "output": "",
                        "error": ("podman not found on target host"),
                        "message": (
                            "Arcaflow plugins require podman on the target host"
                        ),
                    }

                input_path = f"/tmp/arcaflow-input-{run_uuid}.yaml"
                await ssh.run(
                    controller,
                    (f"cat > {input_path} << 'ARCAEOF'\n{input_content}\nARCAEOF"),
                )

                step_flag = f"-s {plugin_step} " if plugin_step else ""
                cmd = (
                    f"cat {input_path} | podman run -i --rm "
                    f"{plugin_image} {step_flag}-f - 2>&1"
                )
                logger.info(f"[benchmark] Executing Arcaflow plugin via SSH: {cmd}")
                result = await ssh.run_with_progress(
                    controller,
                    cmd,
                    progress_callback=_benchmark_progress,
                )
                exit_code = result.exit_code
                stdout_str = result.stdout or ""
                stderr_str = result.stderr or ""

                # Clean up input file
                await ssh.run(
                    controller,
                    f"rm -f {input_path}",
                    timeout=10,
                )

            response = {
                "status": ("completed" if exit_code == 0 else "failed"),
                "exit_code": exit_code,
                "run_id": f"arcaflow-{run_uuid}",
                "harness": "arcaflow-plugins",
                "plugin_image": plugin_image,
                "execution_mode": ("local" if is_local else "ssh"),
                "message": (
                    "Arcaflow plugin completed"
                    if exit_code == 0
                    else (f"Arcaflow plugin failed (exit {exit_code})")
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
            return response

        remote_path = f"/tmp/run-file-{run_uuid}.json"

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
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
            logger.info(
                f"[benchmark] Stopped stale crucible-valkey container on {controller}"
            )

        cmd = f"{run_command or 'crucible run'} {remote_path}"
        logger.info(f"[benchmark] Executing: {cmd}")
        result = await ssh.run_with_progress(
            controller,
            cmd,
            progress_callback=_benchmark_progress,
        )

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

        response = {
            "status": "completed" if result.exit_code == 0 else "failed",
            "exit_code": result.exit_code,
            "run_dir": run_dir,
            "run_id": run_id or f"unknown-{run_uuid}",
            "harness": "crucible",
            "message": "Benchmark completed"
            if result.exit_code == 0
            else f"Benchmark failed (exit {result.exit_code})",
        }
        if result.exit_code == 0 and run_dir:
            summary_result = await ssh.run(
                controller,
                f"cat {run_dir}/run/result-summary.json",
                timeout=30,
            )
            if summary_result.exit_code == 0 and summary_result.stdout:
                try:
                    response["result_summary"] = json.loads(summary_result.stdout)
                except json.JSONDecodeError:
                    pass
        if result.exit_code != 0:
            response["output"] = result.stdout[-3000:] if result.stdout else ""
            response["error"] = result.stderr[-1000:] if result.stderr else ""
        return response

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
            return {
                "status": "not_found",
                "message": f"Run directory not found for {run_id}",
            }

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
            "log_lines": log_result.stdout or "" if log_result.stdout else "",
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
            return {
                "found": False,
                "message": f"No run-file schema for harness '{harness_name}'",
            }
        return {"found": True, "harness": harness_name, "schema": schema}

    async def handle_get_benchmark_params(
        benchmark: str, harness: str | None = None
    ) -> dict:
        harness_name = harness or "crucible"
        if hasattr(skill_provider, "get_provider"):
            provider = skill_provider.get_provider(harness_name)
            params = (
                await provider.get_benchmark_params(benchmark) if provider else None
            )
        else:
            params = await skill_provider.get_benchmark_params(benchmark)
        if params is None:
            return {
                "found": False,
                "message": f"No parameter definitions for '{benchmark}' in '{harness_name}'",
            }
        return {
            "found": True,
            "benchmark": benchmark,
            "harness": harness_name,
            "params": params,
        }

    async def handle_get_example_runfile(
        benchmark: str, harness: str | None = None, endpoint_type: str | None = None
    ) -> dict:
        harness_name = harness or "crucible"
        ep_type = endpoint_type or "remotehosts"
        if hasattr(skill_provider, "get_provider"):
            provider = skill_provider.get_provider(harness_name)
            example = (
                await provider.get_example_runfile(benchmark, endpoint_type=ep_type)
                if provider
                else None
            )
        else:
            example = await skill_provider.get_example_runfile(
                benchmark, endpoint_type=ep_type
            )
        if example is None:
            return {
                "found": False,
                "message": f"No example run-file for '{benchmark}' ({ep_type}) in '{harness_name}'",
            }
        return {
            "found": True,
            "benchmark": benchmark,
            "harness": harness_name,
            "endpoint_type": ep_type,
            "run_file": example,
        }

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
