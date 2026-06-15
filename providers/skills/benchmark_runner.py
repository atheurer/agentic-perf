from __future__ import annotations

from typing import Any

from .base import BenchmarkSuite, RunfileTemplate, SkillProvider

KEYWORD_MAP = {
    "stress": ["stressng_pod"],
    "cpu": ["stressng_pod", "sysbench_pod"],
    "memory": ["stressng_pod"],
    "kernel": ["stressng_pod"],
    "network": ["uperf_pod"],
    "throughput": ["uperf_pod"],
    "latency": ["uperf_pod"],
    "storage": ["fio_pod", "vdbench_pod"],
    "disk": ["fio_pod", "vdbench_pod"],
    "io": ["fio_pod", "vdbench_pod"],
    "fio": ["fio_pod"],
    "database": ["hammerdb_pod_mariadb", "hammerdb_pod_postgresql"],
    "mariadb": ["hammerdb_pod_mariadb"],
    "postgresql": ["hammerdb_pod_postgresql"],
    "postgres": ["hammerdb_pod_postgresql"],
    "sysbench": ["sysbench_pod"],
    "benchmark-runner": ["stressng_pod", "fio_pod", "uperf_pod"],
    "uperf": ["uperf_pod"],
    "vdbench": ["vdbench_pod"],
    "hammerdb": ["hammerdb_pod_mariadb"],
}

_BENCHMARKS: dict[str, dict[str, Any]] = {
    "stressng_pod": {
        "description": (
            "CPU/memory kernel stress test using stress-ng in a Kubernetes pod. "
            "Runs configurable stressors (cpu, memory, matrix, etc.) and reports "
            "throughput metrics (bogo ops/second)."
        ),
        "roles": ["client"],
        "min_hosts": 1,
        "params": {
            "run_type": {
                "type": "string",
                "description": "Test type: func_ci (quick functional) or perf_ci (full performance)",
                "default": "func_ci",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds",
                "default": 600,
            },
            "scale": {
                "type": "integer",
                "description": "Number of parallel pod instances",
                "default": 1,
            },
        },
    },
    "fio_pod": {
        "description": (
            "Storage I/O benchmark using fio in a Kubernetes pod. "
            "Tests read/write throughput, IOPS, and latency on pod-local "
            "or persistent storage."
        ),
        "roles": ["client"],
        "min_hosts": 1,
        "params": {
            "run_type": {
                "type": "string",
                "description": "Test type: func_ci (quick functional) or perf_ci (full performance)",
                "default": "func_ci",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds",
                "default": 600,
            },
            "scale": {
                "type": "integer",
                "description": "Number of parallel pod instances",
                "default": 1,
            },
        },
    },
    "uperf_pod": {
        "description": (
            "Network throughput and latency benchmark using uperf in "
            "Kubernetes pods. Runs client-server pair within the cluster "
            "to measure pod-to-pod network performance."
        ),
        "roles": ["client", "server"],
        "min_hosts": 1,
        "params": {
            "run_type": {
                "type": "string",
                "description": "Test type: func_ci (quick functional) or perf_ci (full performance)",
                "default": "func_ci",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds",
                "default": 600,
            },
            "scale": {
                "type": "integer",
                "description": "Number of parallel pod instances",
                "default": 1,
            },
        },
    },
    "sysbench_pod": {
        "description": (
            "System performance benchmark using sysbench in a Kubernetes pod. "
            "Tests CPU, memory, and mutex performance with configurable "
            "thread counts."
        ),
        "roles": ["client"],
        "min_hosts": 1,
        "params": {
            "run_type": {
                "type": "string",
                "description": "Test type: func_ci (quick functional) or perf_ci (full performance)",
                "default": "func_ci",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds",
                "default": 600,
            },
        },
    },
    "hammerdb_pod_mariadb": {
        "description": (
            "Database performance benchmark using HammerDB with MariaDB "
            "in Kubernetes pods. Measures transactions per second (TPS) "
            "under OLTP workload."
        ),
        "roles": ["client", "server"],
        "min_hosts": 1,
        "params": {
            "run_type": {
                "type": "string",
                "description": "Test type: func_ci or perf_ci",
                "default": "func_ci",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds",
                "default": 900,
            },
        },
    },
    "hammerdb_pod_postgresql": {
        "description": (
            "Database performance benchmark using HammerDB with PostgreSQL "
            "in Kubernetes pods. Measures transactions per second (TPS) "
            "under OLTP workload."
        ),
        "roles": ["client", "server"],
        "min_hosts": 1,
        "params": {
            "run_type": {
                "type": "string",
                "description": "Test type: func_ci or perf_ci",
                "default": "func_ci",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds",
                "default": 900,
            },
        },
    },
    "vdbench_pod": {
        "description": (
            "Storage I/O benchmark using vdbench in a Kubernetes pod. "
            "Tests sequential and random read/write patterns with "
            "configurable block sizes and queue depths."
        ),
        "roles": ["client"],
        "min_hosts": 1,
        "params": {
            "run_type": {
                "type": "string",
                "description": "Test type: func_ci or perf_ci",
                "default": "func_ci",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds",
                "default": 600,
            },
        },
    },
}


class BenchmarkRunnerSkillProvider(SkillProvider):
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
                    harness="benchmark-runner",
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
            harness="benchmark-runner",
        )

    async def resolve_benchmark(self, requirements: dict[str, Any]) -> str | None:
        description = str(requirements.get("description", "")).lower()
        workload_type = str(requirements.get("workload_type", "")).lower()
        search_text = f"{description} {workload_type}"

        scores: dict[str, int] = {}
        for keyword, benchmarks in KEYWORD_MAP.items():
            if keyword in search_text:
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

        defaults = {k: v["default"] for k, v in info.get("params", {}).items()}
        merged = {**defaults, **{k: v for k, v in params.items() if k in defaults}}

        cluster_type = params.get("cluster_type", "openshift")

        env_vars: dict[str, str] = {
            "WORKLOAD": benchmark,
            "CLUSTER": cluster_type,
            "RUN_TYPE": str(merged.get("run_type", "func_ci")),
            "SAVE_ARTIFACTS_LOCAL": "True",
            "log_level": "INFO",
            "DELETE_ALL": "True",
        }

        if merged.get("timeout"):
            env_vars["TIMEOUT"] = str(merged["timeout"])
        if merged.get("scale") and merged["scale"] > 1:
            env_vars["SCALE"] = str(merged["scale"])

        template: dict[str, Any] = {
            "harness": "benchmark-runner",
            "container_image": "quay.io/benchmark-runner/benchmark-runner:latest",
            "env_vars": env_vars,
            "artifacts_dir": "/tmp/benchmark-runner-run-artifacts",
        }

        if params.get("kubeconfig_path"):
            template["kubeconfig_path"] = params["kubeconfig_path"]
        if params.get("kubeadmin_password_path"):
            template["kubeadmin_password_path"] = params["kubeadmin_password_path"]

        return RunfileTemplate(benchmark=benchmark, template=template)

    async def get_benchmark_params(self, benchmark: str) -> dict[str, Any] | None:
        info = _BENCHMARKS.get(benchmark)
        if info is None:
            return None
        return info.get("params", {})

    async def get_runfile_schema(self) -> dict[str, Any] | None:
        return {
            "type": "object",
            "description": "benchmark-runner run-file: container image + env vars",
            "properties": {
                "harness": {"type": "string", "const": "benchmark-runner"},
                "container_image": {
                    "type": "string",
                    "description": "Container image to run (e.g., quay.io/benchmark-runner/benchmark-runner:latest)",
                },
                "env_vars": {
                    "type": "object",
                    "description": "Environment variables passed to the container",
                    "properties": {
                        "WORKLOAD": {"type": "string"},
                        "CLUSTER": {"type": "string", "enum": ["kubernetes", "openshift"]},
                        "RUN_TYPE": {"type": "string", "enum": ["func_ci", "perf_ci"]},
                        "TIMEOUT": {"type": "string"},
                        "SCALE": {"type": "string"},
                        "SAVE_ARTIFACTS_LOCAL": {"type": "string"},
                        "DELETE_ALL": {"type": "string"},
                    },
                    "required": ["WORKLOAD"],
                },
                "artifacts_dir": {
                    "type": "string",
                    "description": "Directory where results are saved inside the container",
                },
            },
            "required": ["harness", "container_image", "env_vars"],
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

        env_vars = run_file.get("env_vars")
        if not isinstance(env_vars, dict):
            return {"valid": False, "errors": ["Missing or invalid 'env_vars' section"]}

        workload = env_vars.get("WORKLOAD")
        if not workload:
            errors.append("Missing WORKLOAD in env_vars")
        elif workload not in _BENCHMARKS:
            errors.append(f"Unknown WORKLOAD '{workload}' — not in benchmark catalog")

        image = run_file.get("container_image")
        if not image:
            errors.append("Missing container_image")

        cluster = env_vars.get("CLUSTER", "kubernetes")
        if cluster not in ("kubernetes", "openshift"):
            errors.append(f"Invalid CLUSTER '{cluster}' — must be 'kubernetes' or 'openshift'")

        run_type = env_vars.get("RUN_TYPE", "func_ci")
        if run_type not in ("func_ci", "perf_ci"):
            errors.append(f"Invalid RUN_TYPE '{run_type}' — must be 'func_ci' or 'perf_ci'")

        return {"valid": len(errors) == 0, "errors": errors}
