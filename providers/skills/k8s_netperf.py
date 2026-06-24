from __future__ import annotations

import re
from typing import Any

from .base import BenchmarkSuite, RunfileTemplate, SkillProvider

VALID_PROFILES = {
    "TCP_STREAM",
    "TCP_STREAM_LAT",
    "UDP_STREAM",
    "TCP_RR",
    "UDP_RR",
    "TCP_CRR",
    "UDP_CRR",
    "SCTP_STREAM",
    "SCTP_RR",
    "SCTP_CRR",
}

DRIVER_PROFILES = {
    "netperf": {
        "TCP_STREAM",
        "UDP_STREAM",
        "TCP_RR",
        "UDP_RR",
        "TCP_CRR",
        "UDP_CRR",
        "SCTP_STREAM",
        "SCTP_RR",
        "SCTP_CRR",
    },
    "iperf3": {"TCP_STREAM", "UDP_STREAM"},
    "uperf": {"TCP_STREAM", "TCP_STREAM_LAT", "UDP_STREAM", "TCP_RR", "UDP_RR"},
}

KEYWORD_MAP = {
    "netperf": ["k8s-netperf"],
    "iperf": ["k8s-netperf"],
    "iperf3": ["k8s-netperf"],
    "uperf": ["k8s-netperf"],
    "k8s-netperf": ["k8s-netperf"],
    "network": ["k8s-netperf"],
    "throughput": ["k8s-netperf"],
    "latency": ["k8s-netperf"],
    "tcp": ["k8s-netperf"],
    "udp": ["k8s-netperf"],
    "rr": ["k8s-netperf"],
    "stream": ["k8s-netperf"],
    "pod-to-pod": ["k8s-netperf"],
    "bandwidth": ["k8s-netperf"],
}

_BENCHMARKS: dict[str, dict[str, Any]] = {
    "k8s-netperf": {
        "description": (
            "Kubernetes network performance testing — measures throughput, "
            "latency, and transactions/sec between pods using netperf, "
            "iperf3, or uperf drivers. Supports pod network, host network, "
            "service routing, same-node, and cross-AZ scenarios."
        ),
        "roles": ["client"],
        "min_hosts": 1,
        "params": {
            "driver": {
                "type": "string",
                "description": (
                    "Benchmark driver: netperf (default), iperf3, or uperf"
                ),
                "default": "netperf",
                "enum": ["netperf", "iperf3", "uperf"],
            },
            "profiles": {
                "type": "array",
                "description": (
                    "Test profiles to run: TCP_STREAM, TCP_STREAM_LAT (uperf only), "
                    "UDP_STREAM, TCP_RR, UDP_RR, TCP_CRR, UDP_CRR, "
                    "SCTP_STREAM, SCTP_RR, SCTP_CRR"
                ),
                "default": ["TCP_STREAM"],
            },
            "duration": {
                "type": "integer",
                "description": "Test duration in seconds",
                "default": 30,
            },
            "samples": {
                "type": "integer",
                "description": "Number of test iterations per profile",
                "default": 3,
            },
            "messagesize": {
                "type": "integer",
                "description": "Message/datagram size in bytes",
                "default": 1024,
            },
            "parallelism": {
                "type": "integer",
                "description": "Number of concurrent streams",
                "default": 1,
            },
            "service": {
                "type": "boolean",
                "description": "Route traffic through a Kubernetes Service",
                "default": False,
            },
            "hostNet": {
                "type": "boolean",
                "description": "Use host networking instead of pod network",
                "default": False,
            },
            "local": {
                "type": "boolean",
                "description": "Force client and server on the same node",
                "default": False,
            },
            "across": {
                "type": "boolean",
                "description": "Force client and server in different AZs",
                "default": False,
            },
        },
    },
}


class K8sNetperfSkillProvider(SkillProvider):
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
                    harness="k8s-netperf",
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
            harness="k8s-netperf",
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

        profiles = merged.get("profiles", ["TCP_STREAM"])
        if isinstance(profiles, str):
            profiles = [profiles]

        tests = []
        for profile in profiles:
            test_entry: dict[str, Any] = {
                "parallelism": merged["parallelism"],
                "profile": profile,
                "duration": merged["duration"],
                "samples": merged["samples"],
                "messagesize": merged["messagesize"],
            }
            if merged.get("service"):
                test_entry["service"] = True
            test_name = f"{profile}_{merged['messagesize']}"
            tests.append({test_name: test_entry})

        cli_flags = []
        driver = merged.get("driver", "netperf")
        # k8s-netperf CLI flag is --iperf (not --iperf3)
        driver_flag = "iperf" if driver == "iperf3" else driver
        cli_flags.append(f"--{driver_flag}")
        if merged.get("hostNet"):
            cli_flags.append("--hostNet")
        if merged.get("local"):
            cli_flags.append("--local")
        if merged.get("across"):
            cli_flags.append("--across")

        template: dict[str, Any] = {
            "harness": "k8s-netperf",
            "config": {"tests": tests},
            "cli_flags": cli_flags,
            "driver": driver,
        }

        return RunfileTemplate(benchmark="k8s-netperf", template=template)

    async def get_default_config(self) -> dict[str, Any]:
        return {
            "provisioning": {
                "install_method": "binary_download",
                "install_command": (
                    "curl -Ls https://raw.githubusercontent.com/cloud-bulldozer/"
                    "k8s-netperf/refs/heads/main/hack/install.sh"
                    " | INSTALL_DIR=/usr/local/bin sh"
                ),
                "install_target_path": "/usr/local/bin",
                "verify_command": "k8s-netperf --help",
                "on_existing_install": "skip",
                "pre_install_commands": [
                    "systemctl mask firewalld iptables nftables 2>/dev/null;"
                    " systemctl stop firewalld iptables nftables 2>/dev/null;"
                    " true",
                ],
            },
            "execution": {
                "controller_required": True,
                "run_command": "k8s-netperf",
                "endpoint_type": "kube",
                "endpoint_user": "root",
                "run_file_format": "yaml_config",
                "kube": {
                    "description": (
                        "Kubernetes network performance testing with k8s-netperf"
                    ),
                    "min_root_volume_gb": 50,
                    "self_ssh_required": False,
                },
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
            "description": "k8s-netperf run-file bundle",
            "properties": {
                "harness": {"type": "string", "const": "k8s-netperf"},
                "driver": {
                    "type": "string",
                    "enum": ["netperf", "iperf3", "uperf"],
                },
                "cli_flags": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "config": {
                    "type": "object",
                    "properties": {
                        "tests": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "description": (
                                    "Single-key dict: {test_name: {profile, "
                                    "duration, samples, messagesize, parallelism}}"
                                ),
                            },
                        },
                    },
                    "required": ["tests"],
                },
            },
            "required": ["harness", "config", "driver"],
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

        config = run_file.get("config")
        if not isinstance(config, dict):
            return {"valid": False, "errors": ["Missing or invalid 'config' section"]}

        tests = config.get("tests")
        if not isinstance(tests, list) or not tests:
            errors.append("Missing or empty 'tests' list in config")
        else:
            for i, test in enumerate(tests):
                if not isinstance(test, dict):
                    errors.append(f"Test {i} is not a dict")
                    continue
                if len(test) != 1:
                    errors.append(
                        f"Test {i}: expected single-key dict, got {len(test)} keys"
                    )
                    continue
                test_config = next(iter(test.values()))
                if not isinstance(test_config, dict):
                    errors.append(f"Test {i}: config is not a dict")
                    continue
                profile = test_config.get("profile", "")
                if profile not in VALID_PROFILES:
                    errors.append(
                        f"Test {i}: invalid profile '{profile}', "
                        f"expected one of {sorted(VALID_PROFILES)}"
                    )

        driver = run_file.get("driver", "netperf")
        if driver not in DRIVER_PROFILES:
            errors.append(
                f"Invalid driver '{driver}', expected one of {sorted(DRIVER_PROFILES)}"
            )
        elif isinstance(tests, list):
            supported = DRIVER_PROFILES[driver]
            for i, test in enumerate(tests):
                if not isinstance(test, dict) or len(test) != 1:
                    continue
                test_config = next(iter(test.values()))
                if not isinstance(test_config, dict):
                    continue
                profile = test_config.get("profile", "")
                if profile and profile not in supported:
                    errors.append(
                        f"Test {i}: profile '{profile}' is not supported "
                        f"by driver '{driver}'"
                    )

        return {"valid": len(errors) == 0, "errors": errors}
