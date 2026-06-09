from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import BenchmarkSuite, RunfileTemplate, SkillProvider

KEYWORD_MAP = {
    "memory": ["streams", "numa_streams"],
    "bandwidth": ["streams", "uperf"],
    "cpu": ["coremark", "coremark_pro", "linpack", "passmark"],
    "compute": ["coremark", "coremark_pro", "linpack"],
    "hpc": ["linpack"],
    "storage": ["fio", "iozone"],
    "disk": ["fio", "iozone"],
    "io": ["fio", "iozone"],
    "network": ["uperf"],
    "throughput": ["uperf"],
    "latency": ["uperf"],
    "database": ["hammerdb"],
    "java": ["specjbb"],
    "python": ["pyperformance"],
    "scheduler": ["pig"],
    "boot": ["reboot_measure"],
    "reboot": ["reboot_measure"],
    "php": ["phpbench"],
    "crypto": ["openssl"],
    "ssl": ["openssl"],
    "web": ["nginx"],
    "cache": ["redis"],
    "stress": ["stress_ng"],
}


class ZathrasSkillProvider(SkillProvider):
    def __init__(self, zathras_home: str | Path) -> None:
        self._home = Path(zathras_home)
        self._test_defs_path = self._home / "config" / "test_defs.yml"

    def _parse_test_defs(self) -> list[dict[str, Any]]:
        if not self._test_defs_path.exists():
            return []

        try:
            import yaml
        except ImportError:
            return []

        try:
            text = self._test_defs_path.read_text()
            data = yaml.safe_load(text)
        except (OSError, yaml.YAMLError):
            return []

        if not isinstance(data, dict):
            return []

        test_defs = data.get("test_defs", {})
        if not isinstance(test_defs, dict):
            return []

        tests = []
        for _key, entry in sorted(test_defs.items()):
            if not isinstance(entry, dict):
                continue
            name = entry.get("test_name")
            if not name:
                continue
            tests.append(entry)

        return tests

    async def list_benchmarks(self) -> list[BenchmarkSuite]:
        results = []
        for entry in self._parse_test_defs():
            name = entry["test_name"]
            description = entry.get("test_description", f"Zathras test: {name}")

            roles = ["client"]
            min_hosts = 1
            if entry.get("network_required") == "yes":
                roles = ["client", "server"]
                min_hosts = 2

            supported_params: dict[str, Any] = {}
            if entry.get("test_specific"):
                supported_params["test_specific"] = entry["test_specific"]
            if entry.get("os_supported"):
                supported_params["os_supported"] = entry["os_supported"]
            if entry.get("storage_required") == "yes":
                supported_params["storage_required"] = True
            if entry.get("java_required") == "yes":
                supported_params["java_required"] = True

            results.append(
                BenchmarkSuite(
                    name=name,
                    description=description,
                    supported_params=supported_params,
                    roles=roles,
                    min_hosts=min_hosts,
                    harness="zathras",
                )
            )
        return results

    async def get_benchmark(self, name: str) -> BenchmarkSuite | None:
        benchmarks = await self.list_benchmarks()
        for b in benchmarks:
            if b.name == name:
                return b
        return None

    async def resolve_benchmark(self, requirements: dict[str, Any]) -> str | None:
        description = str(requirements.get("description", "")).lower()
        workload_type = str(requirements.get("workload_type", "")).lower()
        search_text = f"{description} {workload_type}"

        scores: dict[str, int] = {}
        for keyword, benchmarks in KEYWORD_MAP.items():
            if keyword in search_text:
                for bench in benchmarks:
                    scores[bench] = scores.get(bench, 0) + 1

        available = {e["test_name"] for e in self._parse_test_defs()}
        scored = {k: v for k, v in scores.items() if k in available}

        if not scored:
            return None

        return max(scored, key=scored.get)

    async def generate_runfile(
        self, benchmark: str, params: dict[str, Any]
    ) -> RunfileTemplate:
        endpoints = params.get("endpoints", [])
        host = ""
        if endpoints:
            host = endpoints[0].get("host", "")

        command_args: dict[str, Any] = {
            "tests": benchmark,
            "system_type": "local",
            "host_config": host,
        }

        if params.get("os_vendor"):
            command_args["os_vendor"] = params["os_vendor"]
        if params.get("ssh_key_file"):
            command_args["ssh_key_file"] = params["ssh_key_file"]
        if params.get("test_user"):
            command_args["test_user"] = params["test_user"]
        if params.get("archive"):
            command_args["archive"] = params["archive"]
        if params.get("tuned_profiles"):
            command_args["tuned_profiles"] = params["tuned_profiles"]
        if params.get("scenario"):
            command_args["scenario"] = params["scenario"]

        local_config = None
        if len(endpoints) > 1:
            server_ips = []
            client_ips = []
            for ep in endpoints:
                ep_roles = ep.get("roles", ["client"])
                if "server" in ep_roles:
                    server_ips.append(ep["host"])
                else:
                    client_ips.append(ep["host"])
            local_config = {
                "server_ips": ",".join(server_ips),
                "client_ips": ",".join(client_ips),
            }
            if params.get("storage"):
                local_config["storage"] = params["storage"]

        template: dict[str, Any] = {
            "harness": "zathras",
            "command_args": command_args,
            "local_config": local_config,
        }

        if params.get("tags"):
            template["tags"] = params["tags"]

        return RunfileTemplate(benchmark=benchmark, template=template)
