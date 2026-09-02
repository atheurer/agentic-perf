from __future__ import annotations

import json
import logging
import re
import subprocess
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
    def __init__(
        self,
        crucible_home: str | Path,
        source_repo: str | Path | None = None,
    ) -> None:
        self._home = Path(crucible_home)
        self._source_repo = Path(source_repo) if source_repo else None
        self._benchmarks_dir = self._home / "subprojects" / "benchmarks"
        self._tools_dir = self._home / "subprojects" / "tools"
        self._examples_dir = (
            self._home / "subprojects" / "docs" / "examples" / "runfile"
        )

    def _discover_benchmarks(self) -> list[str]:
        if not self._benchmarks_dir.exists():
            return []
        return [
            d.name
            for d in sorted(self._benchmarks_dir.iterdir())
            if d.is_dir() or d.is_symlink()
        ]

    def _source_repo_config(self) -> dict[str, Any] | None:
        """Read Crucible's ecosystem index, independent of an installation."""
        if self._source_repo is None:
            return None
        path = self._source_repo / "config" / "repos.json"
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _source_benchmark_entries(self) -> dict[str, dict[str, Any]]:
        config = self._source_repo_config()
        if not config:
            return {}
        entries: dict[str, dict[str, Any]] = {}
        for group in ("official", "unofficial"):
            for entry in config.get(group, []):
                if isinstance(entry, dict) and entry.get("type") == "benchmark":
                    name = entry.get("name")
                    if isinstance(name, str) and name:
                        entries[name] = entry
        return entries

    def _source_benchmark_path(
        self, name: str, entry: dict[str, Any] | None = None
    ) -> Path | None:
        """Find a benchmark checkout when the source ecosystem is cached locally.

        The core repo is the catalog source; benchmark repos may be checked out
        beside it or activated as subproject symlinks.  No controller is needed
        for this lookup.
        """
        candidates = [
            self._source_repo / "subprojects" / "benchmarks" / name
            if self._source_repo
            else None,
            self._source_repo / f"bench-{name}"
            if self._source_repo
            else None,
        ]
        if self._source_repo:
            candidates.append(self._source_repo.parent / f"bench-{name}")
        for candidate in candidates:
            if candidate and candidate.exists():
                return candidate
        return None

    def _benchmark_source(
        self, name: str, entry: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        entry = entry or {}
        checkout = entry.get("checkout") if isinstance(entry, dict) else {}
        source: dict[str, Any] = {
            "repository": entry.get("repository"),
            "ref": (checkout or {}).get("target"),
            "mode": (checkout or {}).get("mode"),
            "catalog": "crucible/config/repos.json",
            "metadata_files": ["multiplex.json", "rickshaw.json"],
        }
        repo_path = self._source_benchmark_path(name, entry)
        if repo_path:
            try:
                revision = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=repo_path,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                if revision.returncode == 0:
                    source["commit"] = revision.stdout.strip()
            except (OSError, subprocess.SubprocessError):
                pass
        return source

    def _discover_tools(self) -> list[str]:
        if not self._tools_dir.exists():
            return []
        return [
            d.name
            for d in sorted(self._tools_dir.iterdir())
            if d.is_dir() or d.is_symlink()
        ]

    def _load_tool_meta(self, name: str) -> dict[str, Any]:
        meta: dict[str, Any] = {"name": name}
        if not name or not self._tools_dir.exists():
            return meta
        tool_dir = (self._tools_dir / name).resolve()
        try:
            if not tool_dir.is_relative_to(self._tools_dir.resolve()):
                return meta
        except (ValueError, AttributeError):
            return meta

        multiplex = tool_dir / "multiplex.json"
        if multiplex.exists():
            try:
                meta["multiplex"] = json.loads(multiplex.read_text())
            except (json.JSONDecodeError, OSError):
                pass

        tool_meta = tool_dir / "tool-metadata.json"
        if tool_meta.exists():
            try:
                meta["metadata"] = json.loads(tool_meta.read_text())
            except (json.JSONDecodeError, OSError):
                pass

        rickshaw = tool_dir / "rickshaw.json"
        if rickshaw.exists():
            try:
                meta["rickshaw"] = json.loads(rickshaw.read_text())
            except (json.JSONDecodeError, OSError):
                pass

        return meta

    def _load_benchmark_meta(self, name: str) -> dict[str, Any]:
        meta: dict[str, Any] = {"name": name}
        if not name or (
            not self._benchmarks_dir.exists() and not self._source_repo_config()
        ):
            return meta
        entry = self._source_benchmark_entries().get(name)
        source_dir = self._source_benchmark_path(name, entry)
        bench_dir = (source_dir or (self._benchmarks_dir / name)).resolve()
        allowed_roots = [self._benchmarks_dir.resolve()]
        if self._source_repo:
            allowed_roots.extend(
                [
                    self._source_repo.resolve(),
                    self._source_repo.parent.resolve(),
                ]
            )
        try:
            if not any(bench_dir.is_relative_to(root) for root in allowed_roots):
                return meta
        except (ValueError, AttributeError):
            return meta

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
        source_entries = self._source_benchmark_entries()
        names = set(self._discover_benchmarks()) | set(source_entries)
        for name in sorted(names):
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
                    source=self._benchmark_source(name, source_entries.get(name)),
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

        source_entries = self._source_benchmark_entries()
        available = set(self._discover_benchmarks()) | set(source_entries)

        # An explicit benchmark/repository name must win over generic workload
        # keywords.  In particular, RDMA requests must not silently become
        # uperf requests merely because both are network throughput tests.
        for name in sorted(available, key=len, reverse=True):
            if re.search(rf"\b{re.escape(name.lower())}\b", search_text):
                return name

        if re.search(r"\b(perftest|rdma|infiniband|ib_(?:write|read|send|atomic))\b", search_text):
            return None

        scores: dict[str, int] = {}
        for keyword, benchmarks in KEYWORD_MAP.items():
            if re.search(rf"\b{re.escape(keyword)}\b", search_text):
                for bench in benchmarks:
                    scores[bench] = scores.get(bench, 0) + 1

        scored = {k: v for k, v in scores.items() if k in available}

        if not scored:
            return None

        return max(scored, key=scored.get)

    def _load_example_runfile(
        self, benchmark: str, endpoint_type: str = "remotehosts"
    ) -> dict[str, Any] | None:
        patterns = [
            f"{benchmark}.json",
            f"{benchmark}-remotehost-runfile.json",
            f"{benchmark}-remotehosts-runfile.json",
        ]
        if endpoint_type == "kube":
            patterns = [
                f"{benchmark}.kube.json",
                f"{benchmark}-k8s-runfile.json",
                f"{benchmark}-kube-runfile.json",
            ] + patterns
        for pattern in patterns:
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

    _GENERATE_INTERNAL_KEYS = frozenset(
        {
            "name",
            "endpoints",
            "tags",
            "userenv",
            "osruntime",
            "harness",
            "endpoint_type",
            "endpoint_user",
            "controller",
            "controller_ip",
            "kube_host",
        }
    )

    async def generate_runfile(
        self, benchmark: str, params: dict[str, Any]
    ) -> RunfileTemplate:
        endpoint_type = params.get("endpoint_type", "remotehosts")
        example = self._load_example_runfile(benchmark, endpoint_type)
        bench_params = {
            k: v for k, v in params.items() if k not in self._GENERATE_INTERNAL_KEYS
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
            if endpoint_type == "kube":
                self._build_kube_endpoints(template, params, endpoints, benchmark)
            else:
                self._build_remotehosts_endpoints(template, params, endpoints)

        if params.get("tags"):
            template["tags"] = params["tags"]

        if "tool-params" not in template:
            template["tool-params"] = [
                {"tool": "sysstat"},
                {"tool": "procstat"},
            ]

        return RunfileTemplate(benchmark=benchmark, template=template)

    def _build_remotehosts_endpoints(
        self,
        template: dict[str, Any],
        params: dict[str, Any],
        endpoints: list[dict[str, Any]],
    ) -> None:
        userenv = params.get("userenv", "default")
        osruntime = params.get("osruntime", "podman")
        ep_user = params.get("endpoint_user", "root")
        if ep_user != "root":
            logger.warning(
                "Crucible requires root SSH access — overriding "
                "endpoint_user=%r to 'root'",
                ep_user,
            )
            ep_user = "root"
        controller = params.get("controller")
        controller_ip = params.get("controller_ip")

        remotes = []
        for ep in endpoints:
            roles = ep.get("roles", ["client"])
            engines = [{"role": r, "ids": [1]} for r in roles]
            settings: dict[str, Any] = {"osruntime": osruntime}
            if controller_ip and controller and ep["host"] == controller:
                settings["controller-ip-address"] = controller_ip
            remotes.append(
                {
                    "engines": engines,
                    "config": {
                        "host": ep["host"],
                        "settings": settings,
                    },
                }
            )

        template["endpoints"] = [
            {
                "type": "remotehosts",
                "settings": {"user": ep_user, "userenv": userenv},
                "remotes": remotes,
            }
        ]

    def _build_kube_endpoints(
        self,
        template: dict[str, Any],
        params: dict[str, Any],
        endpoints: list[dict[str, Any]],
        benchmark: str,
    ) -> None:
        ep_user = params.get("endpoint_user", "root")
        if ep_user != "root":
            logger.warning(
                "Crucible requires root SSH access — overriding "
                "endpoint_user=%r to 'root'",
                ep_user,
            )
            ep_user = "root"
        userenv = params.get("userenv", "default")
        controller_ip = params.get("controller_ip", "")
        kube_host = params.get("kube_host", "")

        all_roles: list[str] = []
        for ep in endpoints:
            all_roles.extend(ep.get("roles", ["client"]))
        seen: set[str] = set()
        unique_roles = [r for r in all_roles if r not in seen and not seen.add(r)]

        engines: dict[str, str] = {}
        for role in unique_roles:
            engines[role] = "1"

        kube_ep: dict[str, Any] = {
            "type": "kube",
            "controller-ip-address": controller_ip or kube_host,
            "host": kube_host or controller_ip,
            "user": ep_user,
            "engines": engines,
        }

        if userenv and userenv != "default":
            kube_ep["config"] = [
                {
                    "targets": "default",
                    "settings": {"userenv": userenv},
                }
            ]

        template["endpoints"] = [kube_ep]

    def _load_schema(self) -> dict[str, Any] | None:
        schema_path = (
            self._home
            / "subprojects"
            / "core"
            / "rickshaw"
            / "schema"
            / "run-file.json"
        )
        if not schema_path.exists():
            return None
        try:
            return json.loads(schema_path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    async def get_runfile_schema(self) -> dict[str, Any] | None:
        return self._load_schema()

    async def get_benchmark_params(self, benchmark: str) -> dict[str, Any] | None:
        meta = self._load_benchmark_meta(benchmark)
        return meta.get("multiplex")

    async def list_tools(self) -> list[str]:
        return self._discover_tools()

    async def get_tool_params(self, tool: str) -> dict[str, Any] | None:
        meta = self._load_tool_meta(tool)
        return meta.get("multiplex")

    async def get_tool_metadata(self, tool: str) -> dict[str, Any] | None:
        meta = self._load_tool_meta(tool)
        return meta.get("metadata")

    async def get_example_runfile(
        self, benchmark: str, endpoint_type: str = "remotehosts"
    ) -> dict[str, Any] | None:
        return self._load_example_runfile(benchmark, endpoint_type)

    async def validate_runfile(
        self, run_file: dict[str, Any], harness: str | None = None
    ) -> dict[str, Any]:
        schema = self._load_schema()
        if schema is None:
            return {
                "valid": True,
                "errors": [],
                "warning": "Schema not found, skipping validation",
            }

        try:
            from jsonschema import ValidationError, validate
        except ImportError:
            return {
                "valid": True,
                "errors": [],
                "warning": "jsonschema not installed, skipping validation",
            }

        errors = []
        try:
            validate(instance=run_file, schema=schema)
        except ValidationError as e:
            errors.append(e.message)

        return {"valid": len(errors) == 0, "errors": errors}
