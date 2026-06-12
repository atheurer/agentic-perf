from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import BenchmarkSuite, RunfileTemplate, SkillProvider

KEYWORD_MAP = {
    "memory": ["streams"],
    "stream": ["streams"],
    "bandwidth": ["streams", "uperf"],
    "cpu": ["coremark", "coremark_pro", "linpack", "passmark", "speccpu2017"],
    "compute": ["coremark", "coremark_pro", "linpack"],
    "hpc": ["linpack", "auto_hpl"],
    "linpack": ["linpack", "auto_hpl"],
    "storage": ["fio", "iozone"],
    "disk": ["fio", "iozone"],
    "io": ["fio", "iozone"],
    "network": ["uperf"],
    "throughput": ["uperf"],
    "latency": ["uperf"],
    "database": ["hammerdb", "phoronix_cassandra", "phoronix_cockroach", "phoronix_sqlite"],
    "java": ["specjbb"],
    "python": ["pyperf"],
    "scheduler": ["pig"],
    "boot": ["reboot_measure"],
    "reboot": ["reboot_measure"],
    "php": ["phoronix_phpbench"],
    "crypto": ["phoronix_openssl"],
    "ssl": ["phoronix_openssl"],
    "web": ["phoronix_nginx"],
    "cache": ["phoronix_redis"],
    "redis": ["phoronix_redis"],
    "stress": ["phoronix_stress-ng"],
    "cassandra": ["phoronix_cassandra"],
    "cockroach": ["phoronix_cockroach"],
    "sqlite": ["phoronix_sqlite"],
    "nginx": ["phoronix_nginx"],
    "openssl": ["phoronix_openssl"],
    "spec": ["speccpu2017", "specjbb"],
}


class ZathrasSkillProvider(SkillProvider):
    def __init__(
        self,
        zathras_home: str | Path | None = None,
        fallback_tests: dict[str, Any] | None = None,
    ) -> None:
        self._home = Path(zathras_home) if zathras_home else None
        self._test_defs_path = (
            self._home / "config" / "test_defs.yml" if self._home else None
        )
        self._fallback_tests = fallback_tests

    def _parse_test_defs(self) -> list[dict[str, Any]]:
        if self._test_defs_path and self._test_defs_path.exists():
            return self._parse_test_defs_yaml()
        if self._fallback_tests:
            return self._parse_fallback_tests()
        return []

    def _parse_test_defs_yaml(self) -> list[dict[str, Any]]:
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

    def _parse_fallback_tests(self) -> list[dict[str, Any]]:
        tests = []
        for name, info in sorted(self._fallback_tests.items()):
            if not isinstance(info, dict):
                continue
            reqs = info.get("requirements", {})
            entry = {
                "test_name": name,
                "test_description": info.get("description", f"Zathras test: {name}"),
            }
            if reqs.get("network"):
                entry["network_required"] = "yes"
            if reqs.get("storage"):
                entry["storage_required"] = "yes"
            if reqs.get("java"):
                entry["java_required"] = "yes"
            if info.get("parameters"):
                entry["test_specific"] = info["parameters"]
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

        scenario_global: dict[str, Any] = {
            "results_prefix": f"{benchmark}_test",
            "system_type": "local",
            "test_iter": params.get("test_iter", 1),
        }
        if params.get("os_vendor"):
            scenario_global["os_vendor"] = params["os_vendor"]
        if params.get("ssh_key_file"):
            scenario_global["ssh_key_file"] = params["ssh_key_file"]
        if params.get("tuned_profiles"):
            scenario_global["tuned_profiles"] = params["tuned_profiles"]

        system_config: dict[str, Any] = {
            "tests": benchmark,
            "host_config": host,
        }
        if params.get("test_user"):
            system_config["test_user"] = params["test_user"]

        scenario: dict[str, Any] = {
            "global": scenario_global,
            "systems": {
                "system1": system_config,
            },
        }

        local_config: dict[str, str] = {}
        if len(endpoints) > 1:
            server_ips = []
            client_ips = []
            for ep in endpoints:
                ep_roles = ep.get("roles", ["client"])
                if "server" in ep_roles:
                    server_ips.append(ep["host"])
                else:
                    client_ips.append(ep["host"])
            if server_ips:
                local_config["server_ips"] = ",".join(server_ips)
            if client_ips:
                local_config["client_ips"] = ",".join(client_ips)
        if params.get("storage"):
            local_config["storage"] = params["storage"]

        template: dict[str, Any] = {
            "harness": "zathras",
            "scenario": scenario,
            "local_config": local_config or None,
            "host_config_name": host,
        }

        if params.get("tags"):
            template["tags"] = params["tags"]

        return RunfileTemplate(benchmark=benchmark, template=template)

    VALID_SYSTEM_TYPES = {"local", "aws", "azure", "gcp", "ibm"}

    async def validate_runfile(
        self, run_file: dict[str, Any], harness: str | None = None
    ) -> dict[str, Any]:
        errors: list[str] = []

        scenario = run_file.get("scenario")
        if not isinstance(scenario, dict):
            return {"valid": False, "errors": ["Missing or invalid 'scenario' section"]}

        global_section = scenario.get("global")
        if not isinstance(global_section, dict):
            errors.append("Missing 'global' section in scenario")
        else:
            system_type = global_section.get("system_type")
            if not system_type:
                errors.append("Missing 'system_type' in scenario global")
            elif system_type not in self.VALID_SYSTEM_TYPES:
                errors.append(
                    f"Invalid system_type '{system_type}' — "
                    f"must be one of: {', '.join(sorted(self.VALID_SYSTEM_TYPES))}"
                )

        systems = scenario.get("systems")
        if not isinstance(systems, dict) or not systems:
            errors.append("Missing or empty 'systems' section in scenario")
        else:
            available_tests = {e["test_name"] for e in self._parse_test_defs()}
            test_meta = {e["test_name"]: e for e in self._parse_test_defs()}

            for sys_name, sys_config in systems.items():
                if not isinstance(sys_config, dict):
                    errors.append(f"System '{sys_name}' is not a dict")
                    continue

                tests = sys_config.get("tests")
                if not tests:
                    errors.append(f"System '{sys_name}': missing 'tests' field")
                else:
                    for test_name in str(tests).split(","):
                        test_name = test_name.strip()
                        if available_tests and test_name not in available_tests:
                            errors.append(
                                f"System '{sys_name}': unknown test '{test_name}' — "
                                f"not found in test_defs.yml"
                            )

                host_config = sys_config.get("host_config")
                if not host_config:
                    errors.append(f"System '{sys_name}': missing 'host_config' field")

                if tests and available_tests:
                    for test_name in str(tests).split(","):
                        test_name = test_name.strip()
                        meta = test_meta.get(test_name, {})
                        if meta.get("network_required") == "yes":
                            local_config = run_file.get("local_config")
                            if not local_config or not local_config.get("server_ips"):
                                errors.append(
                                    f"Test '{test_name}' requires network — "
                                    f"local_config must have 'server_ips' and 'client_ips'"
                                )
                        if meta.get("storage_required") == "yes":
                            local_config = run_file.get("local_config")
                            if not local_config or not local_config.get("storage"):
                                errors.append(
                                    f"Test '{test_name}' requires storage — "
                                    f"local_config must have 'storage' field"
                                )

        return {"valid": len(errors) == 0, "errors": errors}
