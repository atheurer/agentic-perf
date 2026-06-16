from __future__ import annotations

from typing import Any

from .base import BenchmarkSuite, RunfileTemplate, SkillProvider

VALID_TEST_TYPES = {"fio", "mariadb", "postgresql"}

FEDORA_IMAGE_URL = (
    "https://dl.fedoraproject.org/pub/fedora/linux/releases/43/"
    "Cloud/x86_64/images/Fedora-Cloud-Base-Generic-43-1.6.x86_64.qcow2"
)

KEYWORD_MAP = {
    "ioscale": ["ioscale-fio", "ioscale-mariadb", "ioscale-postgresql"],
    "fio vm": ["ioscale-fio"],
    "storage vm": ["ioscale-fio"],
    "vm io": ["ioscale-fio"],
    "vm storage": ["ioscale-fio"],
    "vm fio": ["ioscale-fio"],
    "hammerdb vm": ["ioscale-mariadb", "ioscale-postgresql"],
    "database vm": ["ioscale-mariadb", "ioscale-postgresql"],
    "mariadb vm": ["ioscale-mariadb"],
    "postgresql vm": ["ioscale-postgresql"],
    "postgres vm": ["ioscale-postgresql"],
    "vm database": ["ioscale-mariadb", "ioscale-postgresql"],
    "tpcc vm": ["ioscale-mariadb", "ioscale-postgresql"],
}

_COMMON_VM_PARAMS: dict[str, dict[str, Any]] = {
    "vm_cores": {
        "type": "integer",
        "description": "vCPU cores for the test VM",
        "default": 4,
    },
    "vm_memory": {
        "type": "string",
        "description": "Memory for the test VM (e.g., 8Gi)",
        "default": "8Gi",
    },
    "storage_size": {
        "type": "string",
        "description": "Data disk size for the test VM (e.g., 100Gi)",
        "default": "100Gi",
    },
    "storage_class": {
        "type": "string",
        "description": (
            "StorageClass name (auto-detected from cluster if empty). "
            "Templates default to ODF — override for LVMS or other backends."
        ),
        "default": "",
    },
}

_BENCHMARKS: dict[str, dict[str, Any]] = {
    "ioscale-fio": {
        "test_type": "fio",
        "description": (
            "Storage I/O benchmark using fio inside an OpenShift Virtualization "
            "VM. Tests read/write throughput, IOPS, and latency on PVC-backed "
            "block storage. Supports multiple block sizes and I/O patterns."
        ),
        "roles": ["client"],
        "min_hosts": 1,
        "params": {
            **_COMMON_VM_PARAMS,
            "test_size": {
                "type": "string",
                "description": "FIO test file size (e.g., 1G, 10G)",
                "default": "1G",
            },
            "runtime": {
                "type": "integer",
                "description": "FIO runtime in seconds per pattern",
                "default": 300,
            },
            "block_sizes": {
                "type": "string",
                "description": "Space-separated block sizes (e.g., '4k 128k')",
                "default": "4k 128k",
            },
            "io_patterns": {
                "type": "string",
                "description": (
                    "Space-separated I/O patterns "
                    "(e.g., 'read write randread randwrite')"
                ),
                "default": "read write randread randwrite",
            },
            "numjobs": {
                "type": "integer",
                "description": "Number of parallel FIO jobs",
                "default": 1,
            },
            "iodepth": {
                "type": "integer",
                "description": "I/O queue depth",
                "default": 16,
            },
        },
    },
    "ioscale-mariadb": {
        "test_type": "mariadb",
        "description": (
            "MariaDB database benchmark using HammerDB TPC-C inside an "
            "OpenShift Virtualization VM. Measures transactions per minute "
            "(TPM) under varying concurrent user loads."
        ),
        "roles": ["client"],
        "min_hosts": 1,
        "params": {
            **_COMMON_VM_PARAMS,
            "warehouse_count": {
                "type": "integer",
                "description": "TPC-C warehouse count (scale factor)",
                "default": 50,
            },
            "test_duration": {
                "type": "integer",
                "description": "Test duration in minutes",
                "default": 15,
            },
            "user_count": {
                "type": "string",
                "description": (
                    "Space-separated virtual user counts to test "
                    "(e.g., '1 5 10')"
                ),
                "default": "1 5 10",
            },
        },
    },
    "ioscale-postgresql": {
        "test_type": "postgresql",
        "description": (
            "PostgreSQL database benchmark using HammerDB TPC-C inside an "
            "OpenShift Virtualization VM. Measures transactions per minute "
            "(TPM) under varying concurrent user loads."
        ),
        "roles": ["client"],
        "min_hosts": 1,
        "params": {
            **_COMMON_VM_PARAMS,
            "warehouse_count": {
                "type": "integer",
                "description": "TPC-C warehouse count (scale factor)",
                "default": 50,
            },
            "test_duration": {
                "type": "integer",
                "description": "Test duration in minutes",
                "default": 15,
            },
            "user_count": {
                "type": "string",
                "description": (
                    "Space-separated virtual user counts to test "
                    "(e.g., '1 5 10')"
                ),
                "default": "1 5 10",
            },
        },
    },
}


class IoscaleSkillProvider(SkillProvider):
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
                    harness="ioscale",
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
            harness="ioscale",
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

        test_type = info["test_type"]

        vm_config: dict[str, Any] = {
            "cores": merged["vm_cores"],
            "memory": merged["vm_memory"],
            "storage_size": merged["storage_size"],
            "storage_class": merged["storage_class"],
            "image_url": FEDORA_IMAGE_URL,
        }

        test_config: dict[str, Any] = {}

        if test_type == "fio":
            test_config["fio"] = {
                "test_size": merged["test_size"],
                "runtime": merged["runtime"],
                "block_sizes": merged["block_sizes"],
                "io_patterns": merged["io_patterns"],
                "numjobs": merged["numjobs"],
                "iodepth": merged["iodepth"],
                "direct_io": 1,
            }
        elif test_type in ("mariadb", "postgresql"):
            test_config["database"] = {
                "warehouse_count": merged["warehouse_count"],
                "test_duration": merged["test_duration"],
                "user_count": merged["user_count"],
            }

        template: dict[str, Any] = {
            "harness": "ioscale",
            "test_type": test_type,
            "vm_config": vm_config,
            "test_config": test_config,
        }

        return RunfileTemplate(benchmark=benchmark, template=template)

    async def get_default_config(self) -> dict[str, Any]:
        return {
            "provisioning": {
                "install_method": "git_clone",
                "git_url": "https://github.com/ekuric/ioscale.git",
                "install_target_path": "/opt/ioscale",
                "run_install_as_root": "pip install pyyaml paramiko",
                "verify_command": (
                    "python3 /opt/ioscale/io-generic/fio-tests.py --help"
                ),
                "on_existing_install": "skip",
                "pre_install_commands": [
                    "dnf install -y python3-pip python3-devel 2>/dev/null; true",
                    (
                        "which virtctl 2>/dev/null || ("
                        " POD=$(oc get pods -n openshift-cnv"
                        " -l app.kubernetes.io/component=hyperconverged-cluster-cli-download"
                        " -o jsonpath='{.items[0].metadata.name}') &&"
                        " oc cp openshift-cnv/$POD:/home/server/src/amd64/linux/virtctl.tar.gz"
                        " /tmp/virtctl.tar.gz &&"
                        " tar xzf /tmp/virtctl.tar.gz -C /usr/local/bin/ &&"
                        " chmod +x /usr/local/bin/virtctl"
                        ")"
                    ),
                ],
            },
            "execution": {
                "controller_required": True,
                "endpoint_type": "kube",
                "endpoint_user": "root",
                "run_file_format": "yaml_config",
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
            "description": "ioscale run-file bundle",
            "properties": {
                "harness": {"type": "string", "const": "ioscale"},
                "test_type": {
                    "type": "string",
                    "enum": sorted(VALID_TEST_TYPES),
                },
                "vm_config": {
                    "type": "object",
                    "properties": {
                        "cores": {"type": "integer"},
                        "memory": {"type": "string"},
                        "storage_size": {"type": "string"},
                        "storage_class": {"type": "string"},
                        "image_url": {"type": "string"},
                    },
                },
                "test_config": {"type": "object"},
            },
            "required": ["harness", "test_type", "vm_config", "test_config"],
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

        test_type = run_file.get("test_type", "")
        if not test_type:
            errors.append("Missing 'test_type' field")
        elif test_type not in VALID_TEST_TYPES:
            errors.append(
                f"Invalid test_type '{test_type}', "
                f"expected one of {sorted(VALID_TEST_TYPES)}"
            )

        vm_config = run_file.get("vm_config")
        if not isinstance(vm_config, dict):
            errors.append("Missing or invalid 'vm_config' section")

        test_config = run_file.get("test_config")
        if not isinstance(test_config, dict):
            errors.append("Missing or invalid 'test_config' section")
        elif test_type == "fio" and "fio" not in test_config:
            errors.append("test_type is 'fio' but test_config missing 'fio' section")
        elif test_type in ("mariadb", "postgresql") and "database" not in test_config:
            errors.append(
                f"test_type is '{test_type}' but test_config missing "
                f"'database' section"
            )

        return {"valid": len(errors) == 0, "errors": errors}
