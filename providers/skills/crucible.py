from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from .base import BenchmarkSuite, RunfileTemplate, SkillProvider

KEYWORD_MAP = {
    "network": ["uperf", "trafficgen", "iperf"],
    "throughput": ["uperf", "trafficgen", "iperf"],
    "latency": ["uperf", "cyclictest", "oslat"],
    "storage": ["fio"],
    "disk": ["fio"],
    "io": ["fio"],
    "realtime": ["cyclictest", "oslat"],
    "jitter": ["cyclictest", "oslat"],
    "cpu": ["uperf", "fio"],
    "dpdk": ["trafficgen"],
    "packet": ["trafficgen"],
    "forwarding": ["trafficgen"],
}

SKIP_RICKSHAW_KEYS = {"rickshaw-benchmark", "benchmark", "controller"}


class CrucibleSkillProvider(SkillProvider):
    def __init__(self, crucible_home: str | Path) -> None:
        self._home = Path(crucible_home)
        self._benchmarks_dir = self._home / "subprojects" / "benchmarks"
        self._examples_dir = self._home / "subprojects" / "docs" / "examples" / "runfile"

    def _discover_benchmarks(self) -> list[str]:
        if not self._benchmarks_dir.exists():
            return []
        return [
            d.name
            for d in sorted(self._benchmarks_dir.iterdir())
            if d.is_dir() or d.is_symlink()
        ]

    def _load_benchmark_meta(self, name: str) -> dict[str, Any]:
        bench_dir = self._benchmarks_dir / name
        meta: dict[str, Any] = {"name": name}

        multiplex = bench_dir / "multiplex.json"
        if multiplex.exists():
            try:
                meta["multiplex"] = json.loads(multiplex.read_text())
            except (json.JSONDecodeError, OSError):
                pass

        rickshaw = bench_dir / "rickshaw.json"
        if rickshaw.exists():
            try:
                meta["rickshaw"] = json.loads(rickshaw.read_text())
            except (json.JSONDecodeError, OSError):
                pass

        return meta

    def _extract_roles(self, rickshaw: dict[str, Any]) -> list[str]:
        return [k for k in rickshaw if k not in SKIP_RICKSHAW_KEYS]

    async def list_benchmarks(self) -> list[BenchmarkSuite]:
        results = []
        for name in self._discover_benchmarks():
            meta = self._load_benchmark_meta(name)
            params = meta.get("multiplex", {})

            roles = []
            if "rickshaw" in meta:
                roles = self._extract_roles(meta["rickshaw"])

            min_hosts = len(set(roles)) if roles else 1

            results.append(
                BenchmarkSuite(
                    name=name,
                    description=f"Crucible benchmark: {name}",
                    supported_params=params,
                    roles=roles,
                    min_hosts=min_hosts,
                    harness="crucible",
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

        available = set(self._discover_benchmarks())
        scored = {k: v for k, v in scores.items() if k in available}

        if not scored:
            return None

        return max(scored, key=scored.get)

    def _load_example_runfile(self, benchmark: str) -> dict[str, Any] | None:
        for pattern in [f"{benchmark}.json", f"{benchmark}-remotehost-runfile.json",
                        f"{benchmark}-remotehosts-runfile.json"]:
            path = self._examples_dir / benchmark / pattern
            if path.exists():
                try:
                    return json.loads(path.read_text())
                except (json.JSONDecodeError, OSError):
                    pass
        bench_dir = self._examples_dir / benchmark
        if bench_dir.exists():
            for f in bench_dir.iterdir():
                if f.suffix == ".json":
                    try:
                        return json.loads(f.read_text())
                    except (json.JSONDecodeError, OSError):
                        pass
        return None

    async def generate_runfile(
        self, benchmark: str, params: dict[str, Any]
    ) -> RunfileTemplate:
        example = self._load_example_runfile(benchmark)
        bench_params = {
            k: v for k, v in params.items()
            if k not in ("name", "endpoints", "tags", "userenv", "osruntime", "harness")
        }
        if example:
            template = dict(example)
            template.pop("endpoints", None)
            if bench_params:
                for bench in template.get("benchmarks", []):
                    if bench.get("name") == benchmark:
                        bench.update(bench_params)
        else:
            template = {
                "benchmarks": [
                    {
                        "name": benchmark,
                        "ids": bench_params.get("ids", "1"),
                    }
                ],
                "run-params": {
                    "num-samples": 1,
                    "max-sample-failures": 3,
                },
            }

        endpoints = params.get("endpoints", [])
        if endpoints:
            userenv = params.get("userenv", "default")
            osruntime = params.get("osruntime", "podman")
            ep_user = params.get("endpoint_user", "root")
            controller = params.get("controller")

            controller = params.get("controller")
            controller_ip = params.get("controller_ip")

            remotes = []
            for ep in endpoints:
                roles = ep.get("roles", ["client"])
                engines = [{"role": r, "ids": [1]} for r in roles]
                settings: dict[str, Any] = {"osruntime": osruntime}
                if controller_ip and controller and ep["host"] == controller:
                    settings["controller-ip-address"] = controller_ip
                remotes.append({
                    "engines": engines,
                    "config": {
                        "host": ep["host"],
                        "settings": settings,
                    },
                })

            template["endpoints"] = [
                {
                    "type": "remotehosts",
                    "settings": {"user": ep_user, "userenv": userenv},
                    "remotes": remotes,
                }
            ]

        if params.get("tags"):
            template["tags"] = params["tags"]

        return RunfileTemplate(benchmark=benchmark, template=template)

    def _load_schema(self) -> dict[str, Any] | None:
        schema_path = (
            self._home / "subprojects" / "core" / "rickshaw" / "schema" / "run-file.json"
        )
        if not schema_path.exists():
            return None
        try:
            return json.loads(schema_path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    async def validate_runfile(
        self, run_file: dict[str, Any]
    ) -> dict[str, Any]:
        schema = self._load_schema()
        if schema is None:
            return {"valid": True, "errors": [], "warning": "Schema not found, skipping validation"}

        try:
            from jsonschema import validate, ValidationError
        except ImportError:
            return {"valid": True, "errors": [], "warning": "jsonschema not installed, skipping validation"}

        errors = []
        try:
            validate(instance=run_file, schema=schema)
        except ValidationError as e:
            errors.append(e.message)

        return {"valid": len(errors) == 0, "errors": errors}
