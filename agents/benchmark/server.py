"""FastMCP server for benchmark agent tools.

Exposes benchmark execution tools (skill docs, config, SSH operations)
over stdio.  The SkillProvider, SSHExecutor, and RepoCache are
constructed lazily on first tool call from environment variables and
ticket data, so credentials and provider internals never cross the LLM
boundary.

Run directly:  python agents/benchmark/server.py
Connected via: AgentMCPClient (agents/mcp_client.py)
"""

import json
import logging
import re
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

_project_root = str(Path(__file__).resolve().parents[2])
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from fastmcp import FastMCP

from agents.server_utils import (
    build_repo_cache,
    build_skill_provider,
    build_ssh_from_ticket,
)

logger = logging.getLogger(__name__)

mcp = FastMCP("benchmark-agent")

SKILLS_DIR = Path(__file__).resolve().parent.parent.parent / "skills"

# Module-level globals — lazily initialized by _ensure_init()
_initialized = False
_ssh = None
_skill_provider = None
_repo_cache = None
_ticket: dict[str, Any] = {}


async def _ensure_init():
    """Lazily initialize providers and SSH from env vars on first tool call."""
    global _initialized, _ssh, _skill_provider, _repo_cache, _ticket
    if _initialized:
        return
    _ssh, _ticket = await build_ssh_from_ticket()
    _skill_provider = build_skill_provider()
    try:
        _repo_cache = build_repo_cache()
    except Exception:
        _repo_cache = None
    _initialized = True


# ---------------------------------------------------------------------------
# Skill / Doc tools (no SSH)
# ---------------------------------------------------------------------------


@mcp.tool()
async def read_skill(harness: str, filename: str) -> str:
    """Read a skill document containing critical lessons learned from prior benchmark runs. These are listed in the 'Skills' section of the ticket context. Read ALL skill docs before constructing a run file — they contain pitfalls that will cause failures."""
    await _ensure_init()
    skill_path = SKILLS_DIR / harness / filename
    if not skill_path.is_file():
        return json.dumps(
            {"found": False, "message": f"Skill not found: {harness}/{filename}"}
        )
    resolved = skill_path.resolve()
    if not str(resolved).startswith(str(SKILLS_DIR.resolve())):
        return json.dumps({"found": False, "message": "Invalid path"})
    return json.dumps(
        {"found": True, "filename": filename, "content": skill_path.read_text()}
    )


@mcp.tool()
async def list_harness_docs(harness: str) -> str:
    """List documentation files available for a benchmark harness. Returns file paths and sizes. Use this to discover what reference material is available before constructing a run file."""
    await _ensure_init()
    if not _repo_cache:
        return json.dumps({"docs": [], "message": "No repo cache configured"})
    docs = _repo_cache.list_docs(harness, subdirs=["docs", "config"])
    if not docs:
        return json.dumps(
            {"docs": [], "message": f"No docs found for harness '{harness}'"}
        )
    return json.dumps({"docs": docs, "count": len(docs)})


@mcp.tool()
async def read_harness_doc(harness: str, doc_path: str) -> str:
    """Read a documentation file from a benchmark harness repository. Use this to learn about run-file format, endpoint structure, benchmark parameters, or any other harness-specific details. Call list_harness_docs first to see available files."""
    await _ensure_init()
    if not _repo_cache:
        return json.dumps({"found": False, "message": "No repo cache configured"})
    content = _repo_cache.read_file(harness, doc_path)
    if content is None:
        return json.dumps(
            {"found": False, "message": f"File not found: {harness}/{doc_path}"}
        )
    return json.dumps({"found": True, "path": doc_path, "content": content})


# ---------------------------------------------------------------------------
# Config tools (no SSH)
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_execution_config(harness_name: str) -> str:
    """Get the benchmark harness's execution configuration from private skills. Returns controller requirements, pre-run steps, run command, endpoint type, run file format, and defaults. The harness_name should be the harness that owns the benchmark (e.g., 'crucible' or 'zathras')."""
    await _ensure_init()
    # Arcaflow plugins are self-contained containers — no private
    # execution config or harness installation is needed.
    if harness_name == "arcaflow-plugins":
        return json.dumps(
            {
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
        )

    config = await _skill_provider.get_all_private_config(harness_name)
    execution = config.get("execution", {})
    if not execution:
        return json.dumps(
            {
                "harness": harness_name,
                "found": False,
                "message": f"No execution config found for harness '{harness_name}'",
            }
        )
    return json.dumps(
        {
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
    )


@mcp.tool()
async def get_runfile_schema(harness: str = "crucible") -> str:
    """Get the JSON schema that defines the structure of a valid run-file. Use this to understand what top-level keys, benchmark objects, endpoint structures, and mv-params formats are allowed. The schema enforces additionalProperties: false, so only documented keys are permitted."""
    await _ensure_init()
    harness_name = harness or "crucible"
    if hasattr(_skill_provider, "get_provider"):
        provider = _skill_provider.get_provider(harness_name)
        schema = await provider.get_runfile_schema() if provider else None
    else:
        schema = await _skill_provider.get_runfile_schema()
    if schema is None:
        return json.dumps(
            {
                "found": False,
                "message": f"No run-file schema for harness '{harness_name}'",
            }
        )
    return json.dumps({"found": True, "harness": harness_name, "schema": schema})


@mcp.tool()
async def get_benchmark_params(benchmark: str, harness: str = "crucible") -> str:
    """Get the parameter definitions (multiplex.json) for a specific benchmark. Returns presets (named parameter sets like 'basic', 'default') and validations (regex patterns for allowed values per argument). Use this to understand what mv-params arguments are valid and what values they accept."""
    await _ensure_init()
    harness_name = harness or "crucible"
    if hasattr(_skill_provider, "get_provider"):
        provider = _skill_provider.get_provider(harness_name)
        params = await provider.get_benchmark_params(benchmark) if provider else None
    else:
        params = await _skill_provider.get_benchmark_params(benchmark)
    if params is None:
        return json.dumps(
            {
                "found": False,
                "message": f"No parameter definitions for '{benchmark}' in '{harness_name}'",
            }
        )
    return json.dumps(
        {
            "found": True,
            "benchmark": benchmark,
            "harness": harness_name,
            "params": params,
        }
    )


@mcp.tool()
async def get_example_runfile(
    benchmark: str, harness: str = "crucible", endpoint_type: str = "remotehosts"
) -> str:
    """Get an example run-file for a benchmark. Use this as a structural reference when constructing your own run-file. The example shows the correct format for endpoints, mv-params, and benchmark configuration."""
    await _ensure_init()
    harness_name = harness or "crucible"
    ep_type = endpoint_type or "remotehosts"
    if hasattr(_skill_provider, "get_provider"):
        provider = _skill_provider.get_provider(harness_name)
        example = (
            await provider.get_example_runfile(benchmark, endpoint_type=ep_type)
            if provider
            else None
        )
    else:
        example = await _skill_provider.get_example_runfile(
            benchmark, endpoint_type=ep_type
        )
    if example is None:
        return json.dumps(
            {
                "found": False,
                "message": f"No example run-file for '{benchmark}' ({ep_type}) in '{harness_name}'",
            }
        )
    return json.dumps(
        {
            "found": True,
            "benchmark": benchmark,
            "harness": harness_name,
            "endpoint_type": ep_type,
            "run_file": example,
        }
    )


CONTROLLER_KEY_COMMENT = "agentic-perf-controller-key"


@mcp.tool()
async def setup_passwordless_ssh(
    source: str,
    targets: list[str],
    target_ssh_hosts: list[str] | None = None,
    ssh_user: str = "root",
) -> str:
    """Set up passwordless SSH from a source host to target hosts.

    Generates a key pair on the source if needed and copies the public
    key to each target's authorized_keys. Safe to call multiple times —
    existing keys are deduplicated by comment tag.

    In cloud environments, targets may only be reachable via public IPs
    from the agent machine, but the source host reaches them via private
    IPs. Pass target_ssh_hosts with the SSH-reachable (public) IPs for
    key injection, while targets contains the internal IPs that the
    source uses to connect after setup.
    """
    await _ensure_init()
    user = ssh_user
    ssh_hosts = target_ssh_hosts or targets
    if len(ssh_hosts) != len(targets):
        return json.dumps(
            {
                "status": "failed",
                "message": (
                    f"target_ssh_hosts length ({len(ssh_hosts)}) must match "
                    f"targets length ({len(targets)})"
                ),
            }
        )
    logger.info(f"[benchmark] Setting up SSH keys: {source} -> {targets}")

    pubkey_result = await _ssh.run(source, "cat /root/.ssh/id_rsa.pub 2>/dev/null")
    if pubkey_result.exit_code != 0 or not pubkey_result.stdout.strip():
        keygen_result = await _ssh.run(
            source,
            f'ssh-keygen -t rsa -b 4096 -f /root/.ssh/id_rsa -C "{CONTROLLER_KEY_COMMENT}" -N ""',
        )
        if keygen_result.exit_code != 0:
            return json.dumps(
                {
                    "status": "failed",
                    "message": f"Key generation failed: {keygen_result.stderr}",
                }
            )
        pubkey_result = await _ssh.run(source, "cat /root/.ssh/id_rsa.pub")

    pubkey = pubkey_result.stdout.strip()
    if CONTROLLER_KEY_COMMENT not in pubkey:
        await _ssh.run(
            source,
            "rm -f /root/.ssh/id_rsa /root/.ssh/id_rsa.pub && "
            f'ssh-keygen -t rsa -b 4096 -f /root/.ssh/id_rsa -C "{CONTROLLER_KEY_COMMENT}" -N ""',
        )
        pubkey_result = await _ssh.run(source, "cat /root/.ssh/id_rsa.pub")
        pubkey = pubkey_result.stdout.strip()

    results = {}

    for target, ssh_host in zip(targets, ssh_hosts):
        check = await _ssh.run(
            source,
            f"ssh -o ConnectTimeout=5 -o BatchMode=yes "
            f"-o StrictHostKeyChecking=accept-new "
            f"{user}@{target} hostname",
        )
        if check.exit_code == 0:
            results[target] = {
                "status": "already_accessible",
                "hostname": check.stdout.strip(),
            }
            continue

        inject = await _ssh.run(
            ssh_host,
            f"mkdir -p /root/.ssh && "
            f'sed -i "/{CONTROLLER_KEY_COMMENT}/d" /root/.ssh/authorized_keys 2>/dev/null; '
            f'echo "{pubkey}" >> /root/.ssh/authorized_keys && '
            f"chmod 600 /root/.ssh/authorized_keys",
        )
        if inject.exit_code != 0:
            results[target] = {
                "status": "failed",
                "message": inject.stderr,
            }
            continue

        verify = await _ssh.run(
            source,
            f"ssh -o ConnectTimeout=5 -o BatchMode=yes "
            f"-o StrictHostKeyChecking=accept-new "
            f"{user}@{target} hostname",
        )
        results[target] = {
            "status": "configured" if verify.exit_code == 0 else "failed",
            "hostname": verify.stdout.strip() if verify.exit_code == 0 else "",
            "message": verify.stderr if verify.exit_code != 0 else "",
        }

    all_ok = all(
        r["status"] in ("already_accessible", "configured") for r in results.values()
    )
    return json.dumps(
        {
            "status": "success" if all_ok else "partial_failure",
            "results": results,
            "message": "All targets accessible from source"
            if all_ok
            else "Some targets failed SSH setup",
        }
    )


@mcp.tool()
async def execute_benchmark(
    controller: str,
    run_file: dict,
    harness: str | None = None,
    run_command: str | None = None,
) -> str:
    """Execute the benchmark on the controller host. For crucible, sends a JSON run-file via SCP and runs 'crucible run'. For zathras, constructs a burden command. This may take several minutes."""
    await _ensure_init()

    run_uuid = uuid.uuid4().hex[:8]
    harness_name = harness or "crucible"

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

        await _ssh.run(controller, f"mkdir -p {template_dir}")

        if yaml_dump:
            config_content = yaml_dump(config, default_flow_style=False)
        else:
            config_content = json.dumps(config, indent=2)

        await _ssh.run(
            controller,
            f"cat > {config_path} << 'KBEOF'\n{config_content}\nKBEOF",
        )

        for tpl_name, tpl_content in templates.items():
            tpl_path = f"{template_dir}/{tpl_name}"
            await _ssh.run(
                controller,
                f"cat > {tpl_path} << 'KBEOF'\n{tpl_content}\nKBEOF",
            )

        kb_cmd = run_command or "kube-burner init"
        cmd = f"cd {template_dir} && {kb_cmd} -c {config_path} --uuid {run_uuid} 2>&1"
        logger.info(f"[benchmark] Executing kube-burner: {cmd}")
        result = await _ssh.run(controller, cmd, timeout=0, allocate_pty=True)

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
            metrics_result = await _ssh.run(
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
        return json.dumps(response)

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
                pw_result = await _ssh.run(
                    controller, f"cat {password_path} 2>/dev/null"
                )
                if pw_result.exit_code == 0 and pw_result.stdout.strip():
                    env_vars["KUBEADMIN_PASSWORD"] = pw_result.stdout.strip()

        env_flags = " ".join(f'-e {k}="{v}"' for k, v in env_vars.items())

        await _ssh.run(controller, f"mkdir -p {artifacts_dir}")

        cmd = (
            f"podman run --rm {env_flags} "
            f"-v {kubeconfig_path}:/root/.kube/config "
            f"-v {artifacts_dir}:{artifacts_dir} "
            f"--privileged "
            f"{container_image} 2>&1"
        )
        logger.info(f"[benchmark] Executing benchmark-runner: {cmd}")
        result = await _ssh.run(controller, cmd, timeout=0, allocate_pty=True)

        artifacts_cmd = f"ls {artifacts_dir}/ 2>/dev/null | tail -1"
        artifacts_result = await _ssh.run(controller, artifacts_cmd)
        run_dir = (
            artifacts_result.stdout.strip() if artifacts_result.exit_code == 0 else ""
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
            ls_result = await _ssh.run(
                controller,
                f"ls -la {full_dir}/ 2>/dev/null | head -30",
                timeout=30,
            )
            if ls_result.exit_code == 0 and ls_result.stdout:
                response["result_summary"] = ls_result.stdout.strip()
        if result.exit_code != 0:
            response["output"] = result.stdout[-3000:] if result.stdout else ""
            response["error"] = result.stderr[-1000:] if result.stderr else ""
        return json.dumps(response)

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
            await _ssh.run(
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
        await _ssh.run(
            controller,
            f"cat > {scenario_path} << 'ZEOF'\n{scenario_yaml}\nZEOF",
        )

        burden_cmd = run_command or "/opt/zathras/bin/burden"

        preflight_cmd = f"cd /opt/zathras && {burden_cmd} --preflight_check --scenario {scenario_path}"
        logger.info(f"[benchmark] Running zathras preflight: {preflight_cmd}")
        preflight = await _ssh.run(controller, preflight_cmd, timeout=120)
        if preflight.exit_code != 0:
            return json.dumps(
                {
                    "status": "rejected",
                    "harness": "zathras",
                    "message": (
                        "Scenario failed zathras preflight_check and was NOT executed. "
                        "Fix the scenario and try again.\n"
                        + (preflight.stdout or "")
                        + (preflight.stderr or "")
                    ),
                }
            )

        cmd = f"cd /opt/zathras && {burden_cmd} --scenario {scenario_path}"
        logger.info(f"[benchmark] Executing zathras: {cmd}")
        result = await _ssh.run(controller, cmd, timeout=0, allocate_pty=True)

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
            ls_result = await _ssh.run(
                controller,
                f"ls -la {run_dir}/ 2>/dev/null | head -30",
                timeout=30,
            )
            if ls_result.exit_code == 0 and ls_result.stdout:
                response["result_summary"] = ls_result.stdout.strip()
        if result.exit_code != 0:
            response["output"] = result.stdout[-3000:] if result.stdout else ""
            response["error"] = result.stderr[-1000:] if result.stderr else ""
        return json.dumps(response)

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
        await _ssh.run(controller, f"mkdir -p {template_dir}")

        kc = f"KUBECONFIG={kubeconfig}"

        storage_class = vm_config.get("storage_class", "")
        if not storage_class:
            sc_result = await _ssh.run(
                controller,
                f"{kc} oc get sc -o jsonpath='{{.items[0].metadata.name}}'",
            )
            storage_class = sc_result.stdout.strip()
            if not storage_class:
                return json.dumps(
                    {
                        "status": "failed",
                        "harness": "ioscale",
                        "message": "No StorageClass found on cluster",
                    }
                )

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

        await _ssh.run(
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
        await _ssh.run(controller, vm_yaml_cmd)

        logger.info(f"[benchmark] Creating ioscale VM: {vm_name}")
        await _ssh.run(
            controller,
            f"{kc} oc apply -f {template_dir}/vm.yaml -n {ns}",
        )

        for i in range(60):
            check = await _ssh.run(
                controller,
                f"{kc} oc get vmi {vm_name} -n {ns}"
                f" -o jsonpath='{{.status.phase}}' 2>/dev/null",
            )
            if check.stdout.strip() == "Running":
                break
            await _ssh.run(controller, "sleep 10")
        else:
            return json.dumps(
                {
                    "status": "failed",
                    "harness": "ioscale",
                    "message": f"VM {vm_name} did not reach Running in 10 minutes",
                }
            )

        vm_ip_result = await _ssh.run(
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
            await _ssh.run(
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
            await _ssh.run(
                controller,
                f"cat > {config_path} << 'IOEOF'\n{content}\nIOEOF",
            )
            cmd = (
                f"cd /opt/ioscale/db/{test_type} && "
                f"{kc} python3 {test_type}.py -c {config_path} 2>&1"
            )

        logger.info(f"[benchmark] Executing ioscale {test_type}: {cmd}")
        result = await _ssh.run(controller, cmd, timeout=0, allocate_pty=True)

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
        return json.dumps(response)

    if harness_name == "vstorm":
        cli_args = run_file.get("cli_args", [])
        kubeconfig = run_file.get("kubeconfig", "/root/.kube/config")

        args_str = " ".join(cli_args)
        vs_cmd = run_command or "/opt/vstorm/vstorm"
        cmd = f"KUBECONFIG={kubeconfig} {vs_cmd} {args_str} 2>&1"
        logger.info(f"[benchmark] Executing vstorm: {cmd}")
        result = await _ssh.run(controller, cmd, timeout=0, allocate_pty=True)

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
        return json.dumps(response)

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

        await _ssh.run(controller, f"mkdir -p {artifacts_dir}")

        prep_cmd = f"{env_prefix} {forge_cmd} {project} {preset_flags} prepare 2>&1"
        logger.info(f"[benchmark] Forge prepare: {prep_cmd}")
        prep_result = await _ssh.run(controller, prep_cmd, timeout=0, allocate_pty=True)

        if prep_result.exit_code != 0:
            return json.dumps(
                {
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
            )

        test_cmd = (
            f"{env_prefix} {forge_cmd} {project} {preset_flags} test"
            f"{' ' + args_str if args_str else ''} 2>&1"
        )
        logger.info(f"[benchmark] Forge test: {test_cmd}")
        result = await _ssh.run(controller, test_cmd, timeout=0, allocate_pty=True)

        ai_eval = "{}"
        eval_cmd = (
            f"find {artifacts_dir} -name ai_eval_payload.json"
            f" -exec cat {{}} \\; 2>/dev/null | head -1"
        )
        eval_result = await _ssh.run(controller, eval_cmd)
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
        return json.dumps(response)

    if harness_name == "clusterbuster":
        try:
            import yaml

            yaml_dump = yaml.dump
        except ImportError:
            yaml_dump = None

        job_file = run_file.get("job_file", {})
        template_dir = f"/tmp/clusterbuster-{run_uuid}"
        job_path = f"{template_dir}/job.yaml"

        await _ssh.run(controller, f"mkdir -p {template_dir}")

        if yaml_dump:
            job_content = yaml_dump(job_file, default_flow_style=False)
        else:
            job_content = json.dumps(job_file, indent=2)

        await _ssh.run(
            controller,
            f"cat > {job_path} << 'CBEOF'\n{job_content}\nCBEOF",
        )

        kubeconfig = run_file.get("kubeconfig", "/root/.kube/config")
        cb_cmd = run_command or "clusterbuster"
        cmd = f"KUBECONFIG={kubeconfig} {cb_cmd} -f {job_path} 2>&1"
        logger.info(f"[benchmark] Executing clusterbuster: {cmd}")
        result = await _ssh.run(controller, cmd, timeout=0, allocate_pty=True)

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
        return json.dumps(response)

    if harness_name == "k8s-netperf":
        config = run_file.get("config", {})
        cli_flags = run_file.get("cli_flags", [])

        template_dir = f"/tmp/k8s-netperf-{run_uuid}"
        config_path = f"{template_dir}/netperf.yml"

        await _ssh.run(controller, f"mkdir -p {template_dir}")

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

        await _ssh.run(
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
            await _ssh.run(controller, setup_cmd, timeout=60)

        flags_str = " ".join(cli_flags)
        np_cmd = run_command or "k8s-netperf"
        cmd = f"{np_cmd} --config {config_path} {flags_str} --json 2>&1"
        logger.info(f"[benchmark] Executing k8s-netperf: {cmd}")
        result = await _ssh.run(controller, cmd, timeout=0, allocate_pty=True)

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
        return json.dumps(response)

    if harness_name == "arcaflow-plugins":
        import asyncio as _asyncio
        import shutil

        plugin_image = run_file.get("plugin_image", "")
        plugin_input = run_file.get("input", {})
        plugin_step = run_file.get("plugin_step", "")

        if not plugin_image:
            return json.dumps(
                {
                    "status": "failed",
                    "exit_code": -1,
                    "run_id": f"arcaflow-{run_uuid}",
                    "harness": "arcaflow-plugins",
                    "output": "",
                    "error": "No plugin_image specified in run file",
                    "message": "Missing plugin_image",
                }
            )

        # Determine if we can run locally (no SSH needed)
        is_local = controller in ("localhost", "127.0.0.1", "::1")

        # Serialize input as YAML if pyyaml available, else JSON
        try:
            import yaml

            input_content = yaml.dump(plugin_input, default_flow_style=False)
        except ImportError:
            input_content = json.dumps(plugin_input, indent=2)

        # Build container args: optional -s step, then -f - for stdin
        container_args = []
        if plugin_step:
            container_args += ["-s", plugin_step]
        container_args += ["-f", "-"]

        if is_local:
            logger.info(f"[benchmark] Local execution: podman run {plugin_image}")
            podman_path = shutil.which("podman")
            if not podman_path:
                return json.dumps(
                    {
                        "status": "failed",
                        "exit_code": -1,
                        "run_id": f"arcaflow-{run_uuid}",
                        "harness": "arcaflow-plugins",
                        "output": "",
                        "error": "podman not found locally",
                        "message": "Arcaflow plugins require podman",
                    }
                )

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
            podman_check = await _ssh.run(controller, "which podman", timeout=10)
            if podman_check.exit_code != 0:
                return json.dumps(
                    {
                        "status": "failed",
                        "exit_code": -1,
                        "run_id": f"arcaflow-{run_uuid}",
                        "harness": "arcaflow-plugins",
                        "output": "",
                        "error": "podman not found on target host",
                        "message": "Arcaflow plugins require podman on the target host",
                    }
                )

            input_path = f"/tmp/arcaflow-input-{run_uuid}.yaml"
            await _ssh.run(
                controller,
                f"cat > {input_path} << 'ARCAEOF'\n{input_content}\nARCAEOF",
            )

            step_flag = f"-s {plugin_step} " if plugin_step else ""
            cmd = (
                f"cat {input_path} | podman run -i --rm "
                f"{plugin_image} {step_flag}-f - 2>&1"
            )
            logger.info(f"[benchmark] Executing Arcaflow plugin via SSH: {cmd}")
            result = await _ssh.run(
                controller,
                cmd,
                timeout=0,
                allocate_pty=True,
            )
            exit_code = result.exit_code
            stdout_str = result.stdout or ""
            stderr_str = result.stderr or ""

            await _ssh.run(controller, f"rm -f {input_path}", timeout=10)

        response = {
            "status": "completed" if exit_code == 0 else "failed",
            "exit_code": exit_code,
            "run_id": f"arcaflow-{run_uuid}",
            "harness": "arcaflow-plugins",
            "plugin_image": plugin_image,
            "execution_mode": "local" if is_local else "ssh",
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

    # Default: crucible (and any unknown harness that uses JSON run-files)
    remote_path = f"/tmp/run-file-{run_uuid}.json"

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(run_file, f, indent=2)
        local_path = f.name

    logger.info(f"[benchmark] SCP run-file to {controller}:{remote_path}")
    scp_result = await _ssh.copy_to(controller, local_path, remote_path)
    Path(local_path).unlink(missing_ok=True)

    if scp_result.exit_code != 0:
        return json.dumps(
            {
                "status": "failed",
                "message": f"Failed to copy run-file: {scp_result.stderr}",
            }
        )

    # Stop stale valkey container if no run is active (crucible issue #607)
    valkey_check = await _ssh.run(
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
    result = await _ssh.run(controller, cmd, timeout=0, allocate_pty=True)

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
        summary_result = await _ssh.run(
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
    return json.dumps(response)


@mcp.tool()
async def get_run_logs(
    controller: str,
    run_id: str,
    harness: str | None = None,
    results_dir_pattern: str | None = None,
) -> str:
    """Retrieve logs from a benchmark run on the controller."""
    await _ensure_init()

    if run_id.startswith("/"):
        run_dir = run_id
    elif harness == "zathras":
        search_pattern = results_dir_pattern or "/tmp/results_*"
        result = await _ssh.run(
            controller,
            f"ls -dt {search_pattern} 2>/dev/null | head -1",
        )
        run_dir = result.stdout.strip()
    else:
        result = await _ssh.run(
            controller,
            f"ls -d /var/lib/crucible/run/*{run_id}* 2>/dev/null | head -1",
        )
        run_dir = result.stdout.strip()

    if not run_dir:
        return json.dumps(
            {"status": "not_found", "message": f"Run directory not found for {run_id}"}
        )

    if harness == "zathras":
        log_result = await _ssh.run(
            controller,
            f"find {run_dir} -name '*.log' -o -name '*.out' | head -5 | xargs tail -50 2>/dev/null",
        )
    else:
        log_result = await _ssh.run(
            controller,
            f"test -f {run_dir}/crucible.log.xz && xzcat {run_dir}/crucible.log.xz | tail -100 || cat {run_dir}/crucible.log 2>/dev/null | tail -100",
        )

    return json.dumps(
        {
            "run_dir": run_dir,
            "log_lines": log_result.stdout or "" if log_result.stdout else "",
            "status": "ok" if log_result.exit_code == 0 else "error",
        }
    )


if __name__ == "__main__":
    mcp.run()
