from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
from ast import literal_eval
from pathlib import Path
from typing import Any

import httpx

from .base import BenchmarkSuite, RunfileTemplate, SkillProvider

logger = logging.getLogger(__name__)

QUAY_ORGS = ["arcalot", "redhat-performance"]
QUAY_PRIMARY_ORG = "arcalot"
QUAY_API = "https://quay.io/api/v1"
QUAY_IMAGE_PREFIX = f"quay.io/{QUAY_PRIMARY_ORG}"

# Default directory for caching plugin schemas
_DEFAULT_SCHEMA_CACHE_DIR = Path.home() / ".agentic-perf" / "plugin-schema-cache"


class PluginSchemaCache:
    """Cache for plugin schemas discovered by running containers.

    Schemas are persisted as JSON files keyed by image name and
    version. A cached schema is reused if the version matches;
    otherwise it is re-discovered by running the container.
    """

    def __init__(self, cache_dir: Path = _DEFAULT_SCHEMA_CACHE_DIR) -> None:
        self._cache_dir = cache_dir
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._memory: dict[str, dict[str, Any]] = {}

    def _cache_path(self, repo_name: str, version: str) -> Path:
        safe_name = repo_name.replace("/", "_")
        return self._cache_dir / f"{safe_name}_{version}.json"

    def get(self, repo_name: str, version: str) -> dict[str, Any] | None:
        """Return cached schema or None."""
        key = f"{repo_name}:{version}"
        if key in self._memory:
            return self._memory[key]
        path = self._cache_path(repo_name, version)
        if path.exists():
            try:
                data = json.loads(path.read_text())
                self._memory[key] = data
                return data
            except (json.JSONDecodeError, OSError):
                pass
        return None

    def put(
        self,
        repo_name: str,
        version: str,
        schema_data: dict[str, Any],
    ) -> None:
        """Cache a discovered schema."""
        key = f"{repo_name}:{version}"
        self._memory[key] = schema_data
        path = self._cache_path(repo_name, version)
        try:
            path.write_text(json.dumps(schema_data, indent=2))
        except OSError:
            logger.debug(f"[arcaflow-plugins] Failed to write schema cache for {key}")


async def discover_plugin_schema(
    image_ref: str,
) -> dict[str, Any]:
    """Discover a plugin's steps and input schemas by running it.

    Returns a dict with:
        steps: list of step names
        schemas: {step_name: json_schema_dict}
        description: str (from schema title/description if available)
    """
    podman = shutil.which("podman")
    if not podman:
        logger.debug("[arcaflow-plugins] podman not found, cannot discover schemas")
        return {"steps": [], "schemas": {}}

    # Try without -s first (works for single-step plugins)
    proc = await asyncio.create_subprocess_exec(
        podman,
        "run",
        "--rm",
        "--network=host",
        image_ref,
        "--json-schema",
        "input",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    combined = stdout.decode(errors="replace") + stderr.decode(errors="replace")

    if proc.returncode == 0 and stdout.strip():
        # Single-step plugin
        try:
            schema = json.loads(stdout)
            step_name = schema.get("$id", "workload")
            desc = schema.get("description", schema.get("title", ""))
            return {
                "steps": [step_name],
                "schemas": {step_name: schema},
                "description": desc,
            }
        except json.JSONDecodeError:
            pass

    # Multi-step plugin — parse step names from error output
    match = re.search(r"Steps:\s*(\[.*?\])", combined)
    if not match:
        logger.debug(
            f"[arcaflow-plugins] Could not parse steps from "
            f"{image_ref}: {combined[:200]}"
        )
        return {"steps": [], "schemas": {}}

    try:
        steps = literal_eval(match.group(1))
    except (ValueError, SyntaxError):
        return {"steps": [], "schemas": {}}

    # Fetch schema for each step
    schemas: dict[str, Any] = {}
    for step in steps:
        proc = await asyncio.create_subprocess_exec(
            podman,
            "run",
            "--rm",
            "--network=host",
            image_ref,
            "-s",
            step,
            "--json-schema",
            "input",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0 and stdout.strip():
            try:
                schemas[step] = json.loads(stdout)
            except json.JSONDecodeError:
                pass

    return {"steps": steps, "schemas": schemas}


# Repos in the arcalot Quay org that are not runnable plugins.
_EXCLUDED_REPOS = {
    "arcaflow-plugin-baseimage-python-buildbase",
    "arcaflow-plugin-baseimage-python-osbase",
    "arcaflow-plugin-template-python",
    "arcaflow-plugin-test-impl-go",
}

# Metadata for known plugins. Plugins discovered from Quay that are
# not in this map still appear in the catalog with basic info derived
# from their repo name. Adding an entry here provides richer keyword
# matching, parameter schemas, and example inputs.
PLUGIN_METADATA: dict[str, dict[str, Any]] = {
    "arcaflow-plugin-stressng": {
        "description": (
            "CPU, memory, I/O, and mixed stress testing using stress-ng. "
            "Supports cpu, vm, mmap, matrix, mq, hdd, iomix, and sock "
            "stressors with configurable workers and parameters. "
            "Returns structured bogo ops/second metrics."
        ),
        "step": "workload",
        "keywords": [
            "stress",
            "stress-ng",
            "stressng",
            "cpu",
            "memory",
            "cpu stress",
            "memory stress",
            "io stress",
            "matrix",
            "mixed workload",
            "bogo ops",
        ],
        "params": {
            "timeout": {
                "type": "integer",
                "description": "Seconds after which to stop the test",
                "default": 60,
            },
            "stressors": {
                "type": "array",
                "description": (
                    "List of stressor configs. Each needs 'stressor' "
                    "(cpu/vm/mmap/matrix/mq/hdd/iomix/sock) and "
                    "'workers' (int). Additional stressor-specific "
                    "params vary (e.g., vm-bytes, cpu-method, "
                    "hdd-write-size)."
                ),
            },
        },
        "example_input": {
            "timeout": 60,
            "stressors": [
                {
                    "stressor": "cpu",
                    "workers": 4,
                    "cpu-method": "all",
                },
            ],
        },
    },
    "arcaflow-plugin-fio": {
        "description": (
            "Storage I/O benchmark using fio. Executes fio with a "
            "given input configuration and returns structured results "
            "including IOPS, bandwidth, and latency metrics."
        ),
        "step": "workload",
        "keywords": [
            "fio",
            "storage",
            "iops",
            "bandwidth",
            "latency",
            "disk",
            "io",
            "block",
            "random read",
            "random write",
            "sequential",
        ],
        "params": {
            "jobs": {
                "type": "array",
                "description": (
                    "List of fio job definitions. Each job specifies "
                    "parameters like rw, bs, iodepth, size, runtime, "
                    "numjobs, etc."
                ),
            },
        },
        "example_input": {
            "jobs": [
                {
                    "name": "random-read-4k",
                    "rw": "randread",
                    "bs": "4k",
                    "iodepth": 16,
                    "size": "1G",
                    "runtime": 60,
                    "numjobs": 1,
                    "time_based": True,
                },
            ],
        },
    },
    "arcaflow-plugin-sysbench": {
        "description": (
            "System benchmark using sysbench. Supports CPU, memory, "
            "fileio, mutex, and threads sub-benchmarks with "
            "configurable parameters. Use step 'sysbenchcpu', "
            "'sysbenchmemory', or 'sysbenchio'."
        ),
        "step": "sysbenchcpu",
        "keywords": [
            "sysbench",
            "cpu",
            "memory",
            "fileio",
            "mutex",
            "threads",
            "system benchmark",
        ],
        "params": {
            "operation": {
                "type": "string",
                "description": (
                    "Sysbench sub-benchmark: cpu, memory, fileio, mutex, or threads"
                ),
                "default": "cpu",
            },
            "threads": {
                "type": "integer",
                "description": "Number of threads",
                "default": 1,
            },
            "time": {
                "type": "integer",
                "description": "Test duration in seconds",
                "default": 60,
            },
        },
        "example_input": {
            "operation": "cpu",
            "threads": 4,
            "time": 60,
            "cpu-max-prime": 20000,
        },
    },
    "arcaflow-plugin-uperf": {
        "description": (
            "Network throughput and latency benchmark using uperf. "
            "Runs client-server workloads with configurable protocols, "
            "message sizes, and thread counts."
        ),
        "step": "workload",
        "keywords": [
            "uperf",
            "network",
            "throughput",
            "latency",
            "tcp",
            "udp",
            "bandwidth",
            "network performance",
        ],
        "params": {
            "protocol": {
                "type": "string",
                "description": "Network protocol: tcp or udp",
                "default": "tcp",
            },
            "message_size": {
                "type": "integer",
                "description": "Message size in bytes",
                "default": 64,
            },
            "duration": {
                "type": "integer",
                "description": "Test duration in seconds",
                "default": 60,
            },
            "nthreads": {
                "type": "integer",
                "description": "Number of threads",
                "default": 1,
            },
        },
        "example_input": {
            "protocol": "tcp",
            "message_size": 64,
            "duration": 60,
            "nthreads": 1,
        },
    },
    "arcaflow-plugin-coremark-pro": {
        "description": (
            "CoreMark-PRO CPU benchmark suite. Use the 'certify-all' "
            "step to run the full benchmark and return results in a "
            "machine-readable format. Tests integer and floating-point "
            "performance. The 'tune-iterations' step is only used "
            "to calibrate iteration counts for a target runtime and "
            "requires a subsequent certify-all run to produce scores."
        ),
        "step": "certify-all",
        "keywords": [
            "coremark",
            "coremark-pro",
            "cpu",
            "cpu benchmark",
            "integer",
            "floating point",
            "certify",
        ],
        "params": {
            "context_count": {
                "type": "integer",
                "description": ("Number of contexts (parallel workers)"),
                "default": 1,
            },
        },
        "example_input": {
            "context_count": 1,
        },
    },
    "arcaflow-plugin-iperf3": {
        "description": (
            "Network bandwidth measurement using iperf3. Measures "
            "TCP and UDP throughput between client and server."
        ),
        "step": "workload",
        "keywords": [
            "iperf",
            "iperf3",
            "network",
            "bandwidth",
            "throughput",
            "tcp",
            "udp",
        ],
        "params": {
            "host": {
                "type": "string",
                "description": ("Server hostname or IP to connect to"),
            },
            "time": {
                "type": "integer",
                "description": "Test duration in seconds",
                "default": 10,
            },
            "parallel": {
                "type": "integer",
                "description": "Number of parallel streams",
                "default": 1,
            },
        },
        "example_input": {
            "host": "10.0.0.2",
            "time": 30,
            "parallel": 4,
        },
    },
    "arcaflow-plugin-pcp": {
        "description": (
            "Performance Co-Pilot (PCP) telemetry collection. Runs "
            "pmlogger to collect system metrics and produces "
            "structured output."
        ),
        "step": "start_pcp",
        "keywords": [
            "pcp",
            "telemetry",
            "metrics",
            "monitoring",
            "performance co-pilot",
            "pmlogger",
        ],
        "params": {},
        "example_input": {},
    },
    "arcaflow-plugin-rtla": {
        "description": (
            "Real-time latency analysis using rtla timerlat. "
            "Collects CPU latency data for real-time workload "
            "characterization."
        ),
        "step": "workload",
        "keywords": [
            "rtla",
            "timerlat",
            "latency",
            "real-time",
            "rt",
            "cpu latency",
        ],
        "params": {},
        "example_input": {},
    },
    "arcaflow-plugin-metadata": {
        "description": (
            "System metadata collection. Gathers hardware and OS "
            "information from the target host using ansible-facts."
        ),
        "step": "collectMetadata",
        "keywords": [
            "metadata",
            "system info",
            "hardware info",
            "ansible",
            "facts",
            "inventory",
        ],
        "params": {},
        "example_input": {},
    },
    "arcaflow-plugin-smallfile": {
        "description": (
            "Smallfile benchmark for filesystem metadata "
            "performance. Tests create, read, delete, and other "
            "operations on many small files."
        ),
        "step": "workload",
        "keywords": [
            "smallfile",
            "filesystem",
            "metadata",
            "file operations",
            "create",
            "delete",
        ],
        "params": {},
        "example_input": {},
    },
}

# Default cache TTL for Quay discovery (1 hour)
_CACHE_TTL_SECONDS = 3600


def _plugin_name_to_benchmark(repo_name: str) -> str:
    """Convert a Quay repo name to a benchmark suite name.

    arcaflow-plugin-stressng -> arcaflow-stressng
    """
    return repo_name.replace("arcaflow-plugin-", "arcaflow-")


def _benchmark_to_repo_name(benchmark: str) -> str:
    """Convert a benchmark suite name back to a Quay repo name.

    arcaflow-stressng -> arcaflow-plugin-stressng
    """
    return benchmark.replace("arcaflow-", "arcaflow-plugin-")


def _description_from_repo_name(repo_name: str) -> str:
    """Generate a basic description from the repo name."""
    tool = repo_name.replace("arcaflow-plugin-", "")
    return (
        f"Arcaflow plugin for {tool}. Run via: "
        f"podman run -i --rm {QUAY_IMAGE_PREFIX}/{repo_name} -f -"
    )


def _keywords_from_repo_name(repo_name: str) -> list[str]:
    """Generate basic keywords from the repo name."""
    tool = repo_name.replace("arcaflow-plugin-", "")
    return [tool, tool.replace("-", " ")]


class ArcaflowPluginSkillProvider(SkillProvider):
    """Skill provider for Arcaflow plugins run directly as containers.

    Discovers available plugins from the Quay.io arcalot organization
    and enriches them with local metadata (descriptions, keywords,
    parameter schemas) where available. Unknown plugins are included
    with basic info derived from their repository name.

    Plugins are run via:
        cat input.yaml | podman run -i --rm <image> -f -
    """

    def __init__(
        self,
        cache_ttl: int = _CACHE_TTL_SECONDS,
        schema_cache_dir: Path | None = None,
        discover_schemas: bool = True,
    ) -> None:
        self._cache_ttl = cache_ttl
        self._catalog: dict[str, dict[str, Any]] = {}
        self._catalog_timestamp: float = 0
        self._keyword_map: dict[str, list[str]] = {}
        self._schema_cache = PluginSchemaCache(
            schema_cache_dir or _DEFAULT_SCHEMA_CACHE_DIR
        )
        self._discover_schemas = discover_schemas
        self._repo_orgs: dict[str, str] = {}

    def _is_cache_valid(self) -> bool:
        if not self._catalog:
            return False
        return (time.time() - self._catalog_timestamp) < self._cache_ttl

    async def _refresh_catalog(self) -> None:
        """Discover plugins from Quay.io and merge with local metadata."""
        if self._is_cache_valid():
            return

        catalog: dict[str, dict[str, Any]] = {}

        # Discover repos from Quay
        discovered_repos = await self._discover_from_quay()

        for repo_name, version in discovered_repos.items():
            benchmark_name = _plugin_name_to_benchmark(repo_name)
            meta = PLUGIN_METADATA.get(repo_name, {})
            org = self._repo_orgs.get(repo_name, QUAY_PRIMARY_ORG)
            image_prefix = f"quay.io/{org}"
            image_ref = f"{image_prefix}/{repo_name}:{version}"

            # Check schema cache, discover if needed
            discovered = self._schema_cache.get(repo_name, version)
            if discovered is None and self._discover_schemas:
                discovered = await discover_plugin_schema(image_ref)
                if discovered.get("steps"):
                    self._schema_cache.put(repo_name, version, discovered)

            # Merge discovered schema with local metadata
            steps = discovered.get("steps", []) if discovered else []
            discovered_desc = discovered.get("description", "") if discovered else ""

            # Use first step as default, or fall back to metadata
            default_step = steps[0] if steps else meta.get("step", "workload")

            catalog[benchmark_name] = {
                "image": f"{image_prefix}/{repo_name}",
                "version": version,
                "description": meta.get(
                    "description",
                    discovered_desc or _description_from_repo_name(repo_name),
                ),
                "step": default_step,
                "steps": steps,
                "keywords": meta.get(
                    "keywords",
                    _keywords_from_repo_name(repo_name),
                ),
                "params": meta.get("params", {}),
                "example_input": meta.get("example_input", {}),
                "schemas": (discovered.get("schemas", {}) if discovered else {}),
            }

        if catalog:
            self._catalog = catalog
            self._catalog_timestamp = time.time()
            self._rebuild_keyword_map()
            logger.info(
                f"[arcaflow-plugins] Discovered {len(catalog)} plugins from Quay.io"
            )
        elif not self._catalog:
            # Quay unreachable and no cached data — fall back to
            # local metadata only
            logger.warning(
                "[arcaflow-plugins] Quay.io unreachable, using local metadata only"
            )
            self._build_from_local_metadata()

    async def _discover_from_quay(self) -> dict[str, str]:
        """Query Quay.io for arcaflow-plugin-* repos and latest versions.

        Searches QUAY_ORGS in order (arcalot first, then
        redhat-performance). If a plugin exists in both orgs,
        the primary org (arcalot) takes precedence.

        Returns a dict of repo_name -> latest_version_tag.
        """
        repos: dict[str, str] = {}
        # Track which org owns each repo for image path
        repo_orgs: dict[str, str] = {}
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                for org in QUAY_ORGS:
                    try:
                        r = await client.get(
                            f"{QUAY_API}/repository",
                            params={
                                "namespace": org,
                                "public": "true",
                            },
                        )
                        r.raise_for_status()
                        data = r.json()

                        for repo in data.get("repositories", []):
                            name = repo["name"]
                            if not name.startswith("arcaflow-plugin-"):
                                continue
                            if name in _EXCLUDED_REPOS:
                                continue
                            # Primary org takes precedence
                            if name not in repos:
                                repos[name] = "latest"
                                repo_orgs[name] = org
                    except Exception:
                        logger.debug(
                            f"[arcaflow-plugins] Failed to discover from {org}"
                        )

                # Fetch latest version tag for each repo
                for name in list(repos.keys()):
                    org = repo_orgs.get(name, QUAY_PRIMARY_ORG)
                    try:
                        r = await client.get(
                            f"{QUAY_API}/repository/{org}/{name}/tag/",
                            params={
                                "onlyActiveTags": "true",
                                "limit": 10,
                            },
                        )
                        r.raise_for_status()
                        tags = r.json().get("tags", [])
                        # Find the latest semver tag (not
                        # main_latest or latest)
                        for tag in tags:
                            tag_name = tag["name"]
                            if tag_name in (
                                "latest",
                                "main_latest",
                            ):
                                continue
                            if "_" in tag_name:
                                # Skip branch builds like
                                # simplify-schema_be4f338
                                continue
                            # First non-special tag is the latest
                            # release (Quay returns newest first)
                            repos[name] = tag_name
                            break
                    except Exception:
                        logger.debug(
                            f"[arcaflow-plugins] Failed to fetch tags for {org}/{name}"
                        )
        except Exception:
            logger.warning("[arcaflow-plugins] Failed to discover plugins from Quay.io")
        self._repo_orgs = repo_orgs
        return repos

    def _build_from_local_metadata(self) -> None:
        """Build catalog from PLUGIN_METADATA only (offline fallback)."""
        catalog: dict[str, dict[str, Any]] = {}
        for repo_name, meta in PLUGIN_METADATA.items():
            benchmark_name = _plugin_name_to_benchmark(repo_name)
            catalog[benchmark_name] = {
                "image": f"{QUAY_IMAGE_PREFIX}/{repo_name}",
                "version": "latest",
                "description": meta["description"],
                "step": meta.get("step", "workload"),
                "keywords": meta.get("keywords", []),
                "params": meta.get("params", {}),
                "example_input": meta.get("example_input", {}),
            }
        self._catalog = catalog
        self._catalog_timestamp = time.time()
        self._rebuild_keyword_map()
        logger.info(
            f"[arcaflow-plugins] Loaded {len(catalog)} plugins "
            f"from local metadata (offline)"
        )

    def _rebuild_keyword_map(self) -> None:
        """Rebuild the keyword-to-plugin mapping from the catalog."""
        self._keyword_map = {}
        for name, info in self._catalog.items():
            for kw in info.get("keywords", []):
                self._keyword_map.setdefault(kw.lower(), []).append(name)

    def _image_ref(self, plugin: dict[str, Any]) -> str:
        """Build the full container image reference."""
        version = plugin.get("version", "latest")
        return f"{plugin['image']}:{version}"

    async def list_benchmarks(self) -> list[BenchmarkSuite]:
        await self._refresh_catalog()
        results = []
        for name, info in self._catalog.items():
            results.append(
                BenchmarkSuite(
                    name=name,
                    description=info["description"],
                    supported_params=info.get("params", {}),
                    endpoint_types=["remotehosts"],
                    roles=["client"],
                    min_hosts=1,
                    harness="arcaflow-plugins",
                )
            )
        return results

    async def get_benchmark(self, name: str) -> BenchmarkSuite | None:
        await self._refresh_catalog()
        info = self._catalog.get(name)
        if info is None:
            return None
        return BenchmarkSuite(
            name=name,
            description=info["description"],
            supported_params=info.get("params", {}),
            endpoint_types=["remotehosts"],
            roles=["client"],
            min_hosts=1,
            harness="arcaflow-plugins",
        )

    async def resolve_benchmark(self, requirements: dict[str, Any]) -> str | None:
        await self._refresh_catalog()
        description = requirements.get("description", "").lower()
        workload_type = requirements.get("workload_type", "").lower()
        harness = requirements.get("harness", "")

        # Only match if no harness specified or explicitly arcaflow
        if harness and harness not in (
            "arcaflow",
            "arcaflow-plugins",
        ):
            return None

        search_text = f"{description} {workload_type}"
        scores: dict[str, int] = {}
        for keyword, plugins in self._keyword_map.items():
            if re.search(rf"\b{re.escape(keyword)}\b", search_text):
                for plugin in plugins:
                    scores[plugin] = scores.get(plugin, 0) + 1

        if not scores:
            return None

        best = max(scores, key=lambda k: scores[k])
        return best

    async def generate_runfile(
        self, benchmark: str, params: dict[str, Any]
    ) -> RunfileTemplate:
        await self._refresh_catalog()
        info = self._catalog.get(benchmark, {})
        image = self._image_ref(info) if info else benchmark

        # Start from the example input if available,
        # overlay user params
        template: dict[str, Any] = {}
        if info.get("example_input"):
            template = dict(info["example_input"])

        # User params override defaults — filter out
        # harness-level keys that aren't plugin input
        harness_keys = {"harness", "endpoint_type", "hosts"}
        for key, value in params.items():
            if key not in harness_keys:
                template[key] = value

        return RunfileTemplate(
            benchmark=benchmark,
            template={
                "harness": "arcaflow-plugins",
                "plugin_image": image,
                "plugin_step": info.get("step", "workload"),
                "input": template,
            },
        )

    async def get_benchmark_params(self, benchmark: str) -> dict[str, Any] | None:
        await self._refresh_catalog()
        info = self._catalog.get(benchmark)
        if info is None:
            return None

        result: dict[str, Any] = {}

        # Include local metadata params
        if info.get("params"):
            result["params"] = info["params"]

        # Include discovered steps and schemas
        steps = info.get("steps", [])
        if steps:
            result["available_steps"] = steps
            result["default_step"] = info.get("step", steps[0])

        schemas = info.get("schemas", {})
        if schemas:
            result["step_schemas"] = schemas

        return result if result else info.get("params", {})

    async def get_example_runfile(
        self, benchmark: str, endpoint_type: str = "remotehosts"
    ) -> dict[str, Any] | None:
        await self._refresh_catalog()
        info = self._catalog.get(benchmark)
        if info is None:
            return None
        return {
            "harness": "arcaflow-plugins",
            "plugin_image": self._image_ref(info),
            "plugin_step": info.get("step", "workload"),
            "input": info.get("example_input", {}),
        }

    async def get_default_config(self) -> dict[str, Any]:
        """Return provisioning config for arcaflow-plugins.

        Arcaflow plugins are containers — no harness installation
        is needed. Provisioning only needs to verify that podman
        is available on the target host.
        """
        return {
            "provisioning": {
                "install_method": "none",
                "install_target_path": "",
                "verify_command": "podman --version",
                "prerequisites": ["podman"],
                "skip_install": True,
                "note": (
                    "Arcaflow plugins run as containers via "
                    "podman. No harness installation is needed. "
                    "Provisioning should only verify that podman "
                    "is available on the target host."
                ),
            },
            "constraints": {},
            "execution": {
                "controller_required": False,
                "run_command": "podman run",
                "endpoint_type": "remotehosts",
                "run_file_format": "yaml",
            },
        }

    async def validate_runfile(
        self, run_file: dict[str, Any], harness: str | None = None
    ) -> dict[str, Any]:
        errors = []

        if not run_file.get("plugin_image"):
            errors.append("Missing 'plugin_image' — container image required")

        plugin_input = run_file.get("input")
        if plugin_input is None:
            errors.append("Missing 'input' — plugin input data required")
        elif not isinstance(plugin_input, dict):
            errors.append("'input' must be a dictionary")

        return {"valid": len(errors) == 0, "errors": errors}
