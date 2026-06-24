from __future__ import annotations

import re
from typing import Any

from .base import BenchmarkSuite, RunfileTemplate, SkillProvider

KEYWORD_MAP = {
    "vstorm": [
        "vstorm-containerdisk",
        "vstorm-stress-ng",
        "vstorm-dirty-pages",
    ],
    "vm": ["vstorm-containerdisk", "vstorm-stress-ng"],
    "virtual machine": ["vstorm-containerdisk", "vstorm-stress-ng"],
    "vm scale": ["vstorm-containerdisk"],
    "vm density": ["vstorm-containerdisk"],
    "vm boot": ["vstorm-containerdisk"],
    "containerdisk": ["vstorm-containerdisk"],
    "kubevirt": ["vstorm-containerdisk", "vstorm-stress-ng"],
    "cnv": ["vstorm-containerdisk", "vstorm-stress-ng"],
    "stress-ng vm": ["vstorm-stress-ng"],
    "vm stress": ["vstorm-stress-ng"],
    "dirty pages": ["vstorm-dirty-pages"],
    "dirty memory": ["vstorm-dirty-pages"],
    "live migration": ["vstorm-dirty-pages"],
}

_COMMON_PARAMS: dict[str, dict[str, Any]] = {
    "vms": {
        "type": "integer",
        "description": "Number of VMs to create",
        "default": 4,
    },
    "namespaces": {
        "type": "integer",
        "description": "Number of namespaces to distribute VMs across",
        "default": 1,
    },
    "cores": {
        "type": "integer",
        "description": "vCPU cores per VM",
        "default": 1,
    },
    "memory": {
        "type": "string",
        "description": "Memory per VM (e.g., 1Gi, 512Mi)",
        "default": "1Gi",
    },
    "wait": {
        "type": "boolean",
        "description": "Wait for all VMs to reach Running state",
        "default": True,
    },
}

_BENCHMARKS: dict[str, dict[str, Any]] = {
    "vstorm-containerdisk": {
        "description": (
            "VM boot scale test — spawns VMs from a container disk image "
            "with no storage requirements. Measures VM provisioning speed "
            "and scheduling on OpenShift Virtualization."
        ),
        "roles": ["client"],
        "min_hosts": 1,
        "params": dict(_COMMON_PARAMS),
        "mode": "containerdisk",
    },
    "vstorm-stress-ng": {
        "description": (
            "VM stress test — spawns VMs with stress-ng running inside via "
            "cloud-init. Tests VM lifecycle and CPU/memory stress under load. "
            "Workload types: memory-heavy, cpu-heavy, balanced."
        ),
        "roles": ["client"],
        "min_hosts": 1,
        "params": {
            **_COMMON_PARAMS,
            "workload_type": {
                "type": "string",
                "description": (
                    "Stress-ng workload preset: memory-heavy, cpu-heavy, or balanced"
                ),
                "default": "memory-heavy",
            },
        },
        "mode": "containerdisk",
        "cloudinit": "workload/cloudinit-stress-ng-workload.yaml",
    },
    "vstorm-dirty-pages": {
        "description": (
            "VM dirty memory pages test — spawns VMs that continuously "
            "dirty a fraction of their memory. Tests live migration "
            "readiness and memory write patterns."
        ),
        "roles": ["client"],
        "min_hosts": 1,
        "params": {
            **_COMMON_PARAMS,
            "dirty_rate_fraction": {
                "type": "string",
                "description": ("Fraction of guest RAM to dirty (0.1 to 0.9)"),
                "default": "0.5",
            },
        },
        "mode": "containerdisk",
        "cloudinit": "workload/cloudinit-dirty-mem-pages.yaml",
    },
}


class VstormSkillProvider(SkillProvider):
    async def list_benchmarks(self) -> list[BenchmarkSuite]:
        results = []
        for name, info in _BENCHMARKS.items():
            results.append(
                BenchmarkSuite(
                    name=name,
                    description=info["description"],
                    supported_params=info.get("params", {}),
                    endpoint_types=["kube"],
                    roles=info["roles"],
                    min_hosts=info["min_hosts"],
                    harness="vstorm",
                )
            )
        return results

    async def get_benchmark(self, name: str) -> BenchmarkSuite | None:
        info = _BENCHMARKS.get(name)
        if info is None:
            return None
        return BenchmarkSuite(
            name=name,
            description=info["description"],
            supported_params=info.get("params", {}),
            endpoint_types=["kube"],
            roles=info["roles"],
            min_hosts=info["min_hosts"],
            harness="vstorm",
        )

    async def resolve_benchmark(self, requirements: dict[str, Any]) -> str | None:
        description = str(requirements.get("description", "")).lower()
        workload_type = str(requirements.get("workload_type", "")).lower()
        search_text = f"{description} {workload_type}"

        scores: dict[str, int] = {}
        for keyword, benchmarks in KEYWORD_MAP.items():
            if re.search(rf"\b{re.escape(keyword)}\b", search_text):
                for bench in benchmarks:
                    scores[bench] = scores.get(bench, 0) + 1

        scored = {k: v for k, v in scores.items() if k in _BENCHMARKS}
        if not scored:
            return None
        return max(scored, key=scored.get)

    async def generate_runfile(
        self, benchmark: str, params: dict[str, Any]
    ) -> RunfileTemplate:
        info = _BENCHMARKS.get(benchmark)
        if info is None:
            return RunfileTemplate(benchmark=benchmark, template={})

        defaults = {}
        for k, v in info.get("params", {}).items():
            defaults[k] = v["default"]
        merged = {**defaults, **{k: v for k, v in params.items() if k in defaults}}

        cli_args = []

        if info.get("mode") == "containerdisk":
            cli_args.append("--containerdisk")

        cli_args.append(f"--vms={merged['vms']}")
        cli_args.append(f"--namespaces={merged['namespaces']}")
        cli_args.append(f"--cores={merged['cores']}")
        cli_args.append(f"--memory={merged['memory']}")

        if merged.get("wait", True):
            cli_args.append("--wait")

        cloudinit = info.get("cloudinit")
        if cloudinit:
            install_path = "/opt/vstorm"
            cli_args.append(f"--cloudinit={install_path}/{cloudinit}")

        env_vars: list[str] = []
        workload_type = merged.get("workload_type")
        if workload_type:
            env_vars.append(f"WORKLOAD_TYPE={workload_type}")

        dirty_rate = merged.get("dirty_rate_fraction")
        if dirty_rate and benchmark == "vstorm-dirty-pages":
            env_vars.append(f"DIRTY_RATE_FRACTION={dirty_rate}")

        for env in env_vars:
            cli_args.append(f"--env={env}")

        template: dict[str, Any] = {
            "harness": "vstorm",
            "cli_args": cli_args,
        }

        return RunfileTemplate(benchmark=benchmark, template=template)

    async def get_default_config(self) -> dict[str, Any]:
        return {
            "provisioning": {
                "install_method": "git_clone",
                "git_url": "https://github.com/gqlo/vstorm.git",
                "install_target_path": "/opt/vstorm",
                "run_install_as_root": (
                    "echo 'export PATH=/opt/vstorm:$PATH' >> /etc/profile.d/vstorm.sh"
                ),
                "verify_command": "/opt/vstorm/vstorm -h",
                "on_existing_install": "skip",
                "pre_install_commands": [
                    "dnf install -y vim-common 2>/dev/null; true",
                ],
            },
            "execution": {
                "controller_required": True,
                "run_command": "/opt/vstorm/vstorm",
                "endpoint_type": "kube",
                "endpoint_user": "root",
                "run_file_format": "cli_args",
            },
        }

    async def get_benchmark_params(self, benchmark: str) -> dict[str, Any] | None:
        info = _BENCHMARKS.get(benchmark)
        if info is None:
            return None
        return info.get("params", {})

    async def get_runfile_schema(self) -> dict[str, Any] | None:
        return {
            "type": "object",
            "description": "vstorm run-file bundle",
            "properties": {
                "harness": {"type": "string", "const": "vstorm"},
                "cli_args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "CLI arguments for vstorm command",
                },
            },
            "required": ["harness", "cli_args"],
        }

    async def get_example_runfile(
        self, benchmark: str, endpoint_type: str = "remotehosts"
    ) -> dict[str, Any] | None:
        info = _BENCHMARKS.get(benchmark)
        if info is None:
            return None
        result = await self.generate_runfile(benchmark, {})
        return result.template

    async def validate_runfile(
        self, run_file: dict[str, Any], harness: str | None = None
    ) -> dict[str, Any]:
        errors: list[str] = []

        cli_args = run_file.get("cli_args")
        if not isinstance(cli_args, list) or not cli_args:
            return {"valid": False, "errors": ["Missing or empty 'cli_args' list"]}

        has_vms = any(a.startswith("--vms=") for a in cli_args)
        if not has_vms:
            errors.append("Missing --vms=N in cli_args")

        for arg in cli_args:
            if arg.startswith("--vms="):
                try:
                    n = int(arg.split("=", 1)[1])
                    if n < 1:
                        errors.append("--vms must be >= 1")
                except ValueError:
                    errors.append(f"Invalid --vms value: {arg}")

        return {"valid": len(errors) == 0, "errors": errors}
