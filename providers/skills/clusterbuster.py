from __future__ import annotations

from typing import Any

from .base import BenchmarkSuite, RunfileTemplate, SkillProvider

VALID_WORKLOADS = {
    "cpusoaker",
    "fio",
    "uperf",
    "sysbench",
    "memory",
    "files",
    "hammerdb",
    "server",
}

KEYWORD_MAP = {
    "clusterbuster": [
        "cb-cpusoaker", "cb-fio", "cb-uperf", "cb-sysbench",
        "cb-memory", "cb-files", "cb-hammerdb", "cb-server",
    ],
    "cb": [
        "cb-cpusoaker", "cb-fio", "cb-uperf", "cb-sysbench",
        "cb-memory", "cb-files", "cb-hammerdb", "cb-server",
    ],
    "cpusoaker": ["cb-cpusoaker"],
    "cpu soak": ["cb-cpusoaker"],
    "scale": ["cb-cpusoaker", "cb-memory", "cb-files"],
    "pod density": ["cb-cpusoaker"],
    "sysbench": ["cb-sysbench"],
    "hammerdb": ["cb-hammerdb"],
    "tpc-c": ["cb-hammerdb"],
    "tpcc": ["cb-hammerdb"],
    "database": ["cb-hammerdb"],
    "postgresql": ["cb-hammerdb"],
    "mariadb": ["cb-hammerdb"],
    "file stress": ["cb-files"],
    "filesystem": ["cb-files"],
    "memory stress": ["cb-memory"],
    "allocation": ["cb-memory"],
}

_BENCHMARKS: dict[str, dict[str, Any]] = {
    "cb-cpusoaker": {
        "workload": "cpusoaker",
        "description": (
            "CPU stress test — runs Python loops across multiple pods "
            "to measure aggregate CPU throughput at scale. Total processes = "
            "namespaces × deps-per-namespace × processes."
        ),
        "roles": ["client"],
        "min_hosts": 1,
        "params": {
            "workloadruntime": {
                "type": "integer",
                "description": "Duration in seconds",
                "default": 10,
            },
            "namespaces": {
                "type": "integer",
                "description": "Number of namespaces to create",
                "default": 1,
            },
            "deps_per_namespace": {
                "type": "integer",
                "description": "Deployments per namespace",
                "default": 8,
            },
            "processes": {
                "type": "integer",
                "description": "Processes per deployment",
                "default": 3,
            },
        },
    },
    "cb-fio": {
        "workload": "fio",
        "description": (
            "Storage I/O benchmark using fio in Kubernetes pods. "
            "Tests read/write throughput and IOPS on pod storage."
        ),
        "roles": ["client"],
        "min_hosts": 1,
        "params": {
            "workloadruntime": {
                "type": "integer",
                "description": "Duration in seconds",
                "default": 10,
            },
            "replicas": {
                "type": "integer",
                "description": "Number of pod replicas",
                "default": 4,
            },
        },
    },
    "cb-uperf": {
        "workload": "uperf",
        "description": (
            "Network performance test using uperf in Kubernetes pods. "
            "Measures throughput and latency between client/server pods."
        ),
        "roles": ["client", "server"],
        "min_hosts": 1,
        "params": {
            "workloadruntime": {
                "type": "integer",
                "description": "Duration in seconds",
                "default": 30,
            },
            "replicas": {
                "type": "integer",
                "description": "Number of client-server pairs",
                "default": 4,
            },
            "uperf_msg_size": {
                "type": "integer",
                "description": "Message size in bytes",
                "default": 8192,
            },
            "uperf_test_type": {
                "type": "string",
                "description": "Traffic pattern: stream or rr",
                "default": "stream",
            },
            "uperf_proto": {
                "type": "string",
                "description": "Transport protocol: tcp or udp",
                "default": "tcp",
            },
        },
    },
    "cb-sysbench": {
        "workload": "sysbench",
        "description": (
            "Multi-mode system benchmark using sysbench in Kubernetes pods. "
            "Supports cpu, memory, fileio, mutex, and threads workloads."
        ),
        "roles": ["client"],
        "min_hosts": 1,
        "params": {
            "workloadruntime": {
                "type": "integer",
                "description": "Duration in seconds",
                "default": 10,
            },
            "sysbench_workload": {
                "type": "string",
                "description": "Sysbench sub-workload: cpu, memory, fileio, mutex, threads",
                "default": "cpu",
            },
        },
    },
    "cb-memory": {
        "workload": "memory",
        "description": (
            "Memory allocation stress test — allocates, frees, and "
            "optionally uses large chunks of memory across pods."
        ),
        "roles": ["client"],
        "min_hosts": 1,
        "params": {
            "workloadruntime": {
                "type": "integer",
                "description": "Duration in seconds",
                "default": 10,
            },
            "replicas": {
                "type": "integer",
                "description": "Number of pod replicas",
                "default": 8,
            },
            "processes": {
                "type": "integer",
                "description": "Processes per pod",
                "default": 3,
            },
            "memory_size": {
                "type": "string",
                "description": "Memory allocation size per process (e.g., 512Mi)",
                "default": "512Mi",
            },
        },
    },
    "cb-files": {
        "workload": "files",
        "description": (
            "Filesystem metadata stress test — creates, reads, and "
            "deletes large numbers of files to stress filesystem handling."
        ),
        "roles": ["client"],
        "min_hosts": 1,
        "params": {
            "workloadruntime": {
                "type": "integer",
                "description": "Duration in seconds",
                "default": 10,
            },
            "replicas": {
                "type": "integer",
                "description": "Number of pod replicas",
                "default": 4,
            },
        },
    },
    "cb-hammerdb": {
        "workload": "hammerdb",
        "description": (
            "Database benchmark using HammerDB TPC-C. Runs client and "
            "database colocated per pod. Supports PostgreSQL and MariaDB."
        ),
        "roles": ["client"],
        "min_hosts": 1,
        "params": {
            "workloadruntime": {
                "type": "integer",
                "description": "Duration in seconds",
                "default": 180,
            },
            "hammerdb_driver": {
                "type": "string",
                "description": "Database driver: pg (PostgreSQL) or maria (MariaDB)",
                "default": "pg",
            },
            "hammerdb_benchmark": {
                "type": "string",
                "description": "Benchmark type: tpcc or tproc-c",
                "default": "tpcc",
            },
            "hammerdb_virtual_users": {
                "type": "integer",
                "description": "Number of virtual users",
                "default": 4,
            },
            "replicas": {
                "type": "integer",
                "description": "Number of database pod replicas",
                "default": 2,
            },
        },
    },
    "cb-server": {
        "workload": "server",
        "description": (
            "Client-server message exchange benchmark — measures "
            "message passing throughput between pods."
        ),
        "roles": ["client", "server"],
        "min_hosts": 1,
        "params": {
            "workloadruntime": {
                "type": "integer",
                "description": "Duration in seconds",
                "default": 10,
            },
            "replicas": {
                "type": "integer",
                "description": "Number of client-server pairs",
                "default": 4,
            },
        },
    },
}

# Map underscore param names to clusterbuster's dash-separated option names
_PARAM_TO_OPTION = {
    "workloadruntime": "workloadruntime",
    "namespaces": "namespaces",
    "deps_per_namespace": "deps-per-namespace",
    "processes": "processes",
    "replicas": "replicas",
    "memory_size": "memory-size",
    "uperf_msg_size": "uperf-msg-size",
    "uperf_test_type": "uperf-test-type",
    "uperf_proto": "uperf-proto",
    "sysbench_workload": "sysbench-workload",
    "hammerdb_driver": "hammerdb-driver",
    "hammerdb_benchmark": "hammerdb-benchmark",
    "hammerdb_virtual_users": "hammerdb-virtual-users",
}


class ClusterbusterSkillProvider(SkillProvider):
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
                    harness="clusterbuster",
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
            harness="clusterbuster",
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

        defaults = {}
        for k, v in info.get("params", {}).items():
            defaults[k] = v["default"]
        merged = {**defaults, **{k: v for k, v in params.items() if k in defaults}}

        options: dict[str, Any] = {
            "cleanup": True,
            "precleanup": True,
            "workload": info["workload"],
            "exit-at-end": True,
            "report-object-creation": False,
        }

        for param_name, value in merged.items():
            option_name = _PARAM_TO_OPTION.get(param_name, param_name)
            options[option_name] = value

        if info["workload"] == "uperf":
            options["antiaffinity"] = True

        template: dict[str, Any] = {
            "harness": "clusterbuster",
            "job_file": {"options": options},
        }

        return RunfileTemplate(benchmark=benchmark, template=template)

    async def get_default_config(self) -> dict[str, Any]:
        return {
            "provisioning": {
                "install_method": "git_clone",
                "git_url": (
                    "https://github.com/redhat-performance/clusterbuster.git"
                ),
                "install_target_path": "/opt/clusterbuster",
                "run_install_as_root": "pip install -e .",
                "verify_command": "clusterbuster --help",
                "on_existing_install": "skip",
                "pre_install_commands": [
                    "dnf install -y python3-pip python3-devel 2>/dev/null; true",
                ],
            },
            "execution": {
                "controller_required": True,
                "run_command": "clusterbuster",
                "endpoint_type": "kube",
                "endpoint_user": "root",
                "run_file_format": "yaml_job_file",
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
            "description": "clusterbuster run-file bundle",
            "properties": {
                "harness": {"type": "string", "const": "clusterbuster"},
                "job_file": {
                    "type": "object",
                    "properties": {
                        "options": {
                            "type": "object",
                            "properties": {
                                "workload": {
                                    "type": "string",
                                    "enum": sorted(VALID_WORKLOADS),
                                },
                                "workloadruntime": {"type": "integer"},
                                "cleanup": {"type": "boolean"},
                                "precleanup": {"type": "boolean"},
                            },
                            "required": ["workload", "workloadruntime"],
                        },
                    },
                    "required": ["options"],
                },
            },
            "required": ["harness", "job_file"],
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

        job_file = run_file.get("job_file")
        if not isinstance(job_file, dict):
            return {"valid": False, "errors": ["Missing or invalid 'job_file' section"]}

        options = job_file.get("options")
        if not isinstance(options, dict):
            return {"valid": False, "errors": ["Missing or invalid 'options' in job_file"]}

        workload = options.get("workload", "")
        if not workload:
            errors.append("Missing 'workload' in options")
        elif workload not in VALID_WORKLOADS:
            errors.append(
                f"Invalid workload '{workload}', "
                f"expected one of {sorted(VALID_WORKLOADS)}"
            )

        runtime = options.get("workloadruntime", 0)
        if not isinstance(runtime, int) or runtime < 1:
            errors.append("'workloadruntime' must be a positive integer")

        return {"valid": len(errors) == 0, "errors": errors}
