from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

from .base import BenchmarkSuite, RunfileTemplate, SkillProvider
from .local_context import LocalContextSource

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


def select_crucible_context(
    *,
    phase: str,
    controller: dict[str, Any] | None = None,
    update_policy: str | None = None,
) -> dict[str, Any]:
    """Select phase authority without treating cached alternates as effective."""
    controller = controller or {}
    update_unknown = update_policy not in {"update", "no_update"}
    update_expected = update_policy == "update"
    assumption = "no_update" if update_unknown else None
    if phase == "triage":
        return {
            "phase": phase,
            "effective_source": "github",
            "source_reason": "triage_uses_github_source",
            "source_assumption": False,
            "update_policy": update_policy or "unknown",
        }

    controller_usable = all(
        controller.get(key) is True
        for key in (
            "identified",
            "reachable",
            "crucible_installed",
            "snapshot_available",
        )
    )
    if controller_usable and not update_expected:
        return {
            "phase": phase,
            "effective_source": "controller",
            "source_reason": "controller_usable_and_no_update_expected",
            "source_assumption": update_unknown,
            "update_policy": update_policy or "unknown",
            "assumption": assumption,
        }
    return {
        "phase": phase,
        "effective_source": "github",
        "source_reason": (
            "github_until_controller_refresh"
            if update_expected
            else "controller_unavailable_or_not_installed"
        ),
        "source_assumption": update_unknown,
        "update_policy": update_policy or "unknown",
        "assumption": assumption,
    }


@dataclass(frozen=True)
class CrucibleSourceResolution:
    path: Path | None
    provenance: dict[str, Any]


class CrucibleSourceResolver:
    """Resolve an already-present Crucible checkout without network access.

    Crucible repositories are deliberately never cloned or refreshed by
    agentic-perf.  The designated controller or an explicitly configured
    local checkout is the source of truth; unavailable content remains
    unavailable for the caller to report.
    """

    def __init__(
        self,
        repo_cache: Any,
        source_url: str,
        local_fallback: str | Path | None = None,
    ) -> None:
        self._cache = repo_cache
        self._source_url = source_url
        self._local_fallback = Path(local_fallback) if local_fallback else None
        self._resolution: CrucibleSourceResolution | None = None

    @staticmethod
    def _valid_source(path: Path | None) -> bool:
        return bool(path and (path / "config" / "repos.json").is_file())

    @staticmethod
    def _revision(path: Path | None) -> str | None:
        if path is None:
            return None
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=path,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    def resolve(self) -> CrucibleSourceResolution:
        if self._resolution is not None:
            return self._resolution

        candidates = [self._local_fallback, self._cache.get_path("crucible")]
        path = next(
            (
                Path(candidate)
                for candidate in candidates
                if self._valid_source(candidate)
            ),
            None,
        )
        provenance = {
            "effective_source": "local" if path else None,
            "source_reason": "local_checkout" if path else "source_unavailable",
            "refresh_attempted": False,
        }

        provenance.update(
            {
                "core_catalog_commit": self._revision(path),
            }
        )
        self._resolution = CrucibleSourceResolution(path, provenance)
        return self._resolution


class CrucibleCatalogFetcher:
    """Read a bounded set of Crucible files without creating a checkout."""

    def __init__(
        self,
        core_repository: str = "https://github.com/perftool-incubator/crucible.git",
        *,
        fetch_file=None,
        timeout: float = 10.0,
    ) -> None:
        self._core_repository = core_repository
        self._fetch_file = fetch_file
        self._timeout = timeout
        self._cache: dict[tuple[str, str], str | None] = {}

    @staticmethod
    def _raw_url(repository: str, path: str, ref: str = "master") -> str | None:
        if repository.startswith("https://github.com/"):
            base = repository.removesuffix("/").removesuffix(".git")
            return (
                base.replace(
                    "https://github.com/", "https://raw.githubusercontent.com/"
                )
                + f"/{ref}/{path.lstrip('/')}"
            )
        return None

    def read_file(
        self, path: str, *, repository: str | None = None, ref: str = "master"
    ) -> str | None:
        repository = repository or self._core_repository
        key = (repository, f"{ref}:{path}")
        if key in self._cache:
            return self._cache[key]
        if self._fetch_file is not None:
            content = self._fetch_file(repository, path, ref)
        else:
            url = self._raw_url(repository, path, ref)
            content = None
            if url:
                try:
                    headers = {}
                    token = os.environ.get("GITHUB_TOKEN")
                    if token:
                        headers["Authorization"] = f"Bearer {token}"
                    response = httpx.get(
                        url,
                        headers=headers,
                        timeout=self._timeout,
                        follow_redirects=True,
                    )
                    if response.is_success:
                        content = response.text
                except httpx.HTTPError:
                    logger.warning("Unable to retrieve Crucible file %s", url)
        self._cache[key] = content
        return content

    def read_json(
        self, path: str, *, repository: str | None = None, ref: str = "master"
    ) -> dict[str, Any] | None:
        content = self.read_file(repository=repository, path=path, ref=ref)
        if content is None:
            return None
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None


class CrucibleSkillProvider(SkillProvider):
    def __init__(
        self,
        crucible_home: str | Path,
        source_repo: str | Path | None = None,
        repo_cache: Any | None = None,
        source_provenance: dict[str, Any] | None = None,
        catalog_fetcher: CrucibleCatalogFetcher | None = None,
        catalog_only: bool = False,
        local_context_source: LocalContextSource | None = None,
        local_context_manifest: str | Path | None = None,
        local_context_root: str | Path | None = None,
    ) -> None:
        self._home = Path(crucible_home)
        self._catalog_only = catalog_only
        self._source_repo = Path(source_repo) if source_repo else None
        self._benchmarks_dir = self._home / "subprojects" / "benchmarks"
        self._tools_dir = self._home / "subprojects" / "tools"
        self._examples_dir = (
            self._home / "subprojects" / "docs" / "examples" / "runfile"
        )
        self._repo_cache = repo_cache
        self._source_provenance = source_provenance or {}
        self._catalog_fetcher = catalog_fetcher or CrucibleCatalogFetcher(
            os.environ.get(
                "CRUCIBLE_SOURCE_REPO_URL",
                "https://github.com/perftool-incubator/crucible.git",
            )
        )
        if local_context_source is not None:
            self._local_context = local_context_source
        else:
            project_root = Path(__file__).resolve().parents[2]
            manifest = (
                Path(local_context_manifest)
                if local_context_manifest
                else (project_root / "skills" / "context-manifest.json")
            )
            self._local_context = (
                LocalContextSource(manifest, root=local_context_root or project_root)
                if manifest.is_file()
                else None
            )

    _BENCHMARK_CONTEXT_FILES = (
        "AGENTS.md",
        "CLAUDE.md",
        "README.md",
        "benchmark-metadata.json",
        "multiplex.json",
        "rickshaw.json",
    )
    _REPOSITORY_ENTRYPOINT_FILES = frozenset(
        {
            "AGENTS.md",
            "CLAUDE.md",
            "README.md",
            "benchmark-metadata.json",
            "multiplex.json",
            "rickshaw.json",
            "tool-metadata.json",
        }
    )

    # This is a policy for *where* source documentation may live, not an
    # inventory of files.  The inventory is discovered from the pinned
    # checkout so renamed/new upstream documents do not require prompt or
    # agent changes.
    _CONTEXT_ROOTS = ("docs", "config", "schema", "schemas", "skills")
    _CONTEXT_EXTENSIONS = frozenset(
        {".md", ".markdown", ".rst", ".txt", ".json", ".yaml", ".yml", ".toml"}
    )
    _CONTEXT_SUBJECTS = {
        "run-file": ("run", "runfile", "schema", "example"),
        "endpoints": ("endpoint", "remotehost", "kube", "network"),
        "execution": ("exec", "engine", "runtime", "workshop"),
        "engines": ("engine", "container", "runtime"),
        "tools": ("tool", "profiler", "sysstat", "procstat"),
        "benchmark": (
            "benchmark",
            "workload",
            "parameter",
            "multiplex",
            "rickshaw",
            "semantics",
        ),
        "results": ("result", "metric", "analysis", "output"),
    }

    def _discover_benchmarks(self) -> list[str]:
        if self._catalog_only:
            return []
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

    def _controller_repo_config(self) -> dict[str, Any] | None:
        """Read the installed controller's catalog without refreshing it."""
        if self._catalog_only:
            return None
        candidates = [
            self._home / "config" / "repos.json",
            self._home / "subprojects" / "core" / "config" / "repos.json",
            self._home / "repos" / "config" / "repos.json",
        ]
        if self._home.is_dir():
            candidates.extend(sorted(self._home.rglob("repos.json")))
        for path in dict.fromkeys(candidates):
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict):
                return data
        return None

    def _source_repo_entries(self) -> dict[str, dict[str, Any]]:
        config = self._source_repo_config()
        if not config:
            return {}
        entries: dict[str, dict[str, Any]] = {}
        for group in ("official", "unofficial"):
            for entry in config.get(group, []):
                if isinstance(entry, dict):
                    name = entry.get("name")
                    if isinstance(name, str) and name:
                        entries[name] = entry
        return entries

    def _remote_repo_entries(self) -> dict[str, dict[str, Any]]:
        config = self._catalog_fetcher.read_json("config/repos.json")
        if not config:
            return {}
        entries: dict[str, dict[str, Any]] = {}
        for group in ("official", "unofficial"):
            for entry in config.get(group, []):
                if isinstance(entry, dict):
                    name = entry.get("name")
                    if isinstance(name, str) and name:
                        entries[name] = entry
        return entries

    def _catalog_benchmark_entries(self) -> dict[str, dict[str, Any]]:
        entries = self._source_repo_entries()
        if not entries:
            entries = self._remote_repo_entries()
        return {
            name: entry
            for name, entry in entries.items()
            if entry.get("type") == "benchmark"
        }

    def _source_benchmark_entries(self) -> dict[str, dict[str, Any]]:
        return {
            name: entry
            for name, entry in self._source_repo_entries().items()
            if entry.get("type") == "benchmark"
        }

    @staticmethod
    def _entry_repo_name(entry: dict[str, Any]) -> str | None:
        repository = entry.get("repository")
        if not isinstance(repository, str) or not repository:
            return None
        return repository.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")

    @staticmethod
    def _entry_namespace(entry: dict[str, Any]) -> str:
        repo_type = entry.get("type")
        if repo_type == "benchmark":
            return "benchmark"
        if repo_type == "tool":
            return "tool"
        if repo_type == "doc":
            return "doc"
        return "core"

    @staticmethod
    def _entry_ref(entry: dict[str, Any]) -> str:
        checkout = entry.get("checkout")
        if isinstance(checkout, dict) and isinstance(checkout.get("target"), str):
            return checkout["target"]
        if isinstance(entry.get("primary-branch"), str):
            return entry["primary-branch"]
        return "master"

    def _repository_entries_for_namespace(
        self, namespace: str
    ) -> dict[str, dict[str, Any]]:
        entries = self._source_repo_entries()
        if namespace in {"core", "local"}:
            return {}
        prefix, _, name = namespace.partition("/")
        if prefix not in {"core", "tool", "doc", "benchmark"} or not name:
            return {}
        return {
            repo_name: entry
            for repo_name, entry in entries.items()
            if repo_name == name and self._entry_namespace(entry) == prefix
        }

    def _source_repository_path(self, name: str, entry: dict[str, Any]) -> Path | None:
        """Resolve one catalog entry without assuming a monolithic checkout."""
        repo_type = self._entry_namespace(entry)
        repo_name = self._entry_repo_name(entry)
        roots = []
        if self._source_repo:
            roots.extend(
                [
                    self._source_repo / "subprojects" / f"{repo_type}s",
                    self._source_repo / "subprojects" / repo_type,
                    self._source_repo.parent,
                ]
            )
        if repo_name:
            candidates = (
                [root / repo_name for root in roots]
                + [root / f"{repo_type}-{name}" for root in roots]
                + [root / f"bench-{name}" for root in roots]
            )
            for candidate in candidates:
                if candidate.is_dir():
                    return candidate.resolve()

            if self._repo_cache is not None:
                for cache_name in (
                    f"crucible-{name}",
                    f"crucible-{repo_name}",
                    f"crucible-benchmark-{name}",
                ):
                    cached = self._repo_cache.get_path(cache_name)
                    if cached and cached.is_dir():
                        return cached.resolve()

        # Catalog URLs describe where a repository lives, but agentic-perf
        # never clones them.  The repository must already be present in the
        # controller checkout or in an explicitly supplied local checkout.
        return None

    def _controller_repository_path(self, name: str) -> Path | None:
        """Find an installed controller checkout using catalog metadata."""
        catalog = self._controller_repo_config()
        catalog_entries = {
            entry.get("name"): entry
            for group in ("official", "unofficial")
            for entry in (catalog or {}).get(group, [])
            if isinstance(entry, dict) and isinstance(entry.get("name"), str)
        }
        entry = catalog_entries.get(name)
        if catalog and entry is None:
            return None
        repo_type = self._entry_namespace(entry or {})
        repo_name = self._entry_repo_name(entry or {})
        roots = [
            self._home / "repos",
            self._home / "subprojects",
            self._home / "subprojects" / f"{repo_type}s",
            self._home / "subprojects" / repo_type,
        ]
        candidates = [root / name for root in roots]
        candidates.extend(root / f"{repo_type}-{name}" for root in roots)
        candidates.extend(root / f"bench-{name}" for root in roots)
        if repo_name:
            candidates.extend(root / repo_name for root in roots)
        for root in roots:
            if root.is_dir():
                for pattern in (name, f"{repo_type}-{name}", f"bench-{name}"):
                    candidates.extend(
                        path for path in root.rglob(pattern) if path.is_dir()
                    )
                if repo_name:
                    candidates.extend(
                        path for path in root.rglob(repo_name) if path.is_dir()
                    )
        for candidate in candidates:
            if not candidate.is_dir():
                continue
            try:
                resolved = candidate.resolve()
                if any(resolved.is_relative_to(root.resolve()) for root in roots):
                    return resolved
            except (OSError, ValueError, AttributeError):
                continue
        return None

    def _controller_benchmark_path(self, name: str) -> Path | None:
        """Backward-compatible benchmark-specific controller lookup."""
        entry = self._controller_repo_config()
        if entry:
            return self._controller_repository_path(name)
        return None

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
            self._source_repo / f"bench-{name}" if self._source_repo else None,
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
        source: dict[str, Any] = {
            "repository": entry.get("repository"),
            "ref": self._entry_ref(entry) if entry else None,
            "mode": (entry.get("checkout") or {}).get("mode"),
            "catalog": "crucible/config/repos.json",
            "metadata_files": ["multiplex.json", "rickshaw.json"],
        }
        source.update(self._source_provenance)
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

    def _git_revision(self, repo_path: Path) -> str | None:
        try:
            revision = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return revision.stdout.strip() if revision.returncode == 0 else None

    def _ensure_source_benchmark(self, name: str, entry: dict[str, Any]) -> Path | None:
        existing = self._source_benchmark_path(name, entry)
        if existing:
            return existing

        if self._repo_cache is not None:
            cached = self._repo_cache.get_path(f"crucible-benchmark-{name}")
            if cached and cached.is_dir():
                return cached

        # A catalog repository URL is metadata only.  Do not turn it into a
        # network fetch; callers must use the controller/local checkout.
        return None

    async def get_benchmark_context(self, benchmark: str) -> dict[str, Any]:
        """Retrieve allowlisted context from the catalog-selected benchmark repo.

        This is intentionally a source-only, triage-time operation. Controller
        selection and post-provision refresh are separate phases.
        """
        entry = self._source_benchmark_entries().get(benchmark)
        if entry is None:
            return {
                "found": False,
                "benchmark": benchmark,
                "effective_source": None,
                "reason": "benchmark_not_in_catalog",
            }

        repo_path = self._ensure_source_benchmark(benchmark, entry)
        source = self._benchmark_source(benchmark, entry)
        result: dict[str, Any] = {
            "found": repo_path is not None,
            "benchmark": benchmark,
            "effective_source": "github",
            "source_reason": "catalog_repository",
            "source_assumption": False,
            "repository": source.get("repository"),
            "ref": source.get("ref"),
            "commit": self._git_revision(repo_path) if repo_path else None,
            "files": [],
            "missing_files": list(self._BENCHMARK_CONTEXT_FILES),
            "context": {},
        }
        if repo_path is None:
            result["reason"] = "benchmark_repository_unavailable"
            return result

        # Paths are fixed relative paths, and the resolved checkout is the only
        # permitted root. No caller-provided path is read here.
        root = repo_path.resolve()
        for relative in self._BENCHMARK_CONTEXT_FILES:
            path = root / relative
            if not path.is_file():
                continue
            try:
                resolved = path.resolve()
                if not resolved.is_relative_to(root):
                    continue
                result["context"][relative] = resolved.read_text()
            except OSError:
                continue
            result["files"].append(relative)
            result["missing_files"].remove(relative)
        return result

    @staticmethod
    def _safe_context_path(root: Path, relative: str) -> Path | None:
        """Resolve a document below *root*, rejecting traversal and escapes."""
        if (
            not isinstance(relative, str)
            or not relative
            or Path(relative).is_absolute()
        ):
            return None
        candidate = (root / relative).resolve()
        try:
            if not candidate.is_relative_to(root.resolve()):
                return None
        except (AttributeError, ValueError):
            return None
        return candidate if candidate.is_file() else None

    @classmethod
    def _safe_context_file(
        cls, root: Path, relative: Path, *, benchmark_namespace: bool = False
    ) -> bool:
        if (
            any(part.startswith(".") for part in relative.parts)
            or relative.suffix.lower() not in cls._CONTEXT_EXTENSIONS
        ):
            return False
        if len(relative.parts) == 1:
            if relative.name in cls._REPOSITORY_ENTRYPOINT_FILES:
                return True
            return relative.suffix.lower() in {".md", ".rst", ".txt"}
        return relative.parts[0] in cls._CONTEXT_ROOTS

    @classmethod
    def _discover_context_files(
        cls, root: Path, *, benchmark_namespace: bool = False
    ) -> list[str]:
        """Return deterministic, policy-approved files under a source root."""
        return cls._discover_context_inventory(
            root, benchmark_namespace=benchmark_namespace
        )[0]

    @classmethod
    def _discover_context_inventory(
        cls, root: Path, *, benchmark_namespace: bool = False
    ) -> tuple[list[str], dict[str, int]]:
        """Return safe files and counts for each deterministic exclusion reason."""
        root = root.resolve()
        found: list[str] = []
        excluded: dict[str, int] = {}
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            try:
                relative = path.resolve().relative_to(root)
            except (OSError, ValueError):
                excluded["path_escape_or_unresolvable"] = (
                    excluded.get("path_escape_or_unresolvable", 0) + 1
                )
                continue
            if cls._safe_context_file(
                root, relative, benchmark_namespace=benchmark_namespace
            ):
                found.append(relative.as_posix())
                continue
            if any(part.startswith(".") for part in relative.parts):
                reason = "hidden_or_repository_internal"
            elif relative.suffix.lower() not in cls._CONTEXT_EXTENSIONS:
                reason = "unsupported_extension"
            else:
                reason = "outside_safe_context_roots"
            excluded[reason] = excluded.get(reason, 0) + 1
        return found, excluded

    @classmethod
    def _subject_matches(cls, path: str, subject_area: str | list[str]) -> bool:
        if not subject_area:
            return True
        areas = (
            subject_area if isinstance(subject_area, list) else subject_area.split(",")
        )
        areas = [area.strip().lower() for area in areas if area.strip()]
        if not areas or "all" in areas or "general" in areas:
            return True
        value = path.lower()
        return any(
            term in value
            for area in areas
            for term in cls._CONTEXT_SUBJECTS.get(area, ())
        )

    @classmethod
    def _subject_tags(cls, path: str) -> list[str]:
        """Classify a path without using classification to hide inventory."""
        return [
            area for area in cls._CONTEXT_SUBJECTS if cls._subject_matches(path, area)
        ]

    async def get_crucible_context(
        self,
        benchmark: str | None = None,
        *,
        operation: str = "list",
        namespace: str = "all",
        path: str = "",
        subject_area: str | list[str] = "all",
        include_alternates: bool = False,
        query: str = "",
        phase: str = "triage",
        controller: dict[str, Any] | None = None,
        update_policy: str | None = None,
        agent: str | None = None,
        include_content: bool = False,
    ) -> dict[str, Any]:
        """Unified source gateway for Crucible core and catalog benchmark docs.

        ``include_alternates`` is intentionally metadata-only at this layer;
        source authority is selected by the phase workspace policy.  The
        gateway never accepts an audience and never reads outside a pinned
        source checkout.
        """
        if operation not in {"list", "read", "search"}:
            return {"found": False, "reason": "unsupported_operation"}
        if namespace not in {"all", "core", "local"} and not any(
            namespace.startswith(prefix)
            for prefix in ("core/", "tool/", "doc/", "benchmark/")
        ):
            return {"found": False, "reason": "unsupported_namespace"}

        benchmark_name = benchmark
        if namespace.startswith("benchmark/"):
            benchmark_name = namespace.split("/", 1)[1] or benchmark
        repository_name = benchmark_name
        if namespace.startswith(("core/", "tool/", "doc/")):
            repository_name = namespace.split("/", 1)[1]
        controller_repo = (
            self._controller_repository_path(repository_name)
            if repository_name
            else None
        )
        policy_controller = dict(controller or {})
        policy_controller["snapshot_available"] = bool(
            policy_controller.get("snapshot_available") is True or controller_repo
        )
        selection = select_crucible_context(
            phase=phase, controller=policy_controller, update_policy=update_policy
        )
        selected_source = selection["effective_source"]
        if selected_source == "controller" and not controller_repo:
            selection = {
                **selection,
                "effective_source": "github",
                "source_reason": "controller_selected_but_benchmark_checkout_unavailable",
            }
            selected_source = "github"

        github_repositories: dict[str, tuple[Path, dict[str, Any]]] = {}
        if namespace.startswith(("core/", "tool/", "doc/", "benchmark/")):
            for name, entry in self._repository_entries_for_namespace(
                namespace
            ).items():
                repo = self._source_repository_path(name, entry)
                if repo:
                    github_repositories[name] = (repo, entry)
        elif namespace == "all" and benchmark_name:
            entry = self._source_benchmark_entries().get(benchmark_name)
            if entry:
                repo = self._source_repository_path(benchmark_name, entry)
                if repo:
                    github_repositories[benchmark_name] = (repo, entry)

        sources_considered = [
            {
                "source": "github",
                "available": bool(
                    (self._source_repo and self._source_repo.is_dir())
                    or bool(github_repositories)
                ),
                "reason": "pinned_source_checkout"
                if self._source_repo
                else "source_checkout_unavailable",
            },
            {
                "source": "controller",
                "available": bool(controller_repo),
                "reason": "installed_catalog_checkout"
                if controller_repo
                else "controller_benchmark_checkout_unavailable",
            },
            {
                "source": "local",
                "available": self._local_context is not None,
                "reason": "manifest_backed_supplemental_context",
                "authority": "supplemental",
            },
        ]
        for candidate in sources_considered:
            candidate["selected"] = candidate["source"] == selected_source
            if not candidate["selected"]:
                candidate["skipped_reason"] = (
                    selection["source_reason"]
                    if candidate["available"]
                    else candidate["reason"]
                )
        sources: list[tuple[str, Path, dict[str, Any]]] = []
        if namespace in {"all", "core"}:
            core_repo = (
                self._home if selected_source == "controller" else self._source_repo
            )
            if core_repo and core_repo.is_dir():
                provenance = dict(
                    self._source_provenance
                    if selected_source == "github"
                    else {
                        "effective_source": "controller",
                        "source_reason": selection["source_reason"],
                        "repository": "installed-controller",
                        "commit": self._git_revision(core_repo),
                    }
                )
                sources.append(("core", core_repo, provenance))
        if selected_source == "github":
            for name, (repo, entry) in github_repositories.items():
                namespace_name = f"{self._entry_namespace(entry)}/{name}"
                sources.append(
                    (
                        namespace_name,
                        repo,
                        self._benchmark_source(name, entry)
                        if entry.get("type") == "benchmark"
                        else {
                            **self._source_provenance,
                            "repository": entry.get("repository"),
                            "ref": (entry.get("checkout") or {}).get("target"),
                            "catalog": "crucible/config/repos.json",
                        },
                    )
                )
        elif namespace.startswith(("core/", "tool/", "doc/")) and controller_repo:
            sources.append(
                (
                    namespace,
                    controller_repo,
                    {
                        "effective_source": "controller",
                        "source_reason": selection["source_reason"],
                        "repository": "installed-controller",
                        "commit": self._git_revision(controller_repo),
                    },
                )
            )
        elif namespace.startswith("benchmark/") and controller_repo:
            sources.append(
                (
                    f"benchmark/{benchmark_name}",
                    controller_repo,
                    {
                        "effective_source": "controller",
                        "source_reason": selection["source_reason"],
                        "repository": "installed-controller",
                        "commit": self._git_revision(controller_repo),
                    },
                )
            )

        documents: list[dict[str, Any]] = []
        source_roots: dict[str, Path] = {}
        exclusion_counts: dict[str, int] = {}
        for name, root, provenance in sources:
            source_roots[name] = root
            discovered, source_exclusions = self._discover_context_inventory(
                root, benchmark_namespace=name.startswith("benchmark/")
            )
            for reason, count in source_exclusions.items():
                exclusion_counts[reason] = exclusion_counts.get(reason, 0) + count
            for relative in discovered:
                subject_match = self._subject_matches(relative, subject_area)
                logical_ref = f"{name}/{relative}"
                document = {
                    "namespace": name,
                    "path": logical_ref,
                    "ref": logical_ref,
                    "uri": f"crucible://{logical_ref}",
                    "source_path": relative,
                    "source": selected_source,
                    "authority": "alternate" if include_alternates else "effective",
                    "provenance": provenance,
                    "entrypoint": relative in self._BENCHMARK_CONTEXT_FILES
                    or relative in {"README.md", "AGENTS.md", "CLAUDE.md"},
                    "subject_areas": self._subject_tags(relative),
                    "subject_match": subject_match,
                }
                documents.append(document)

        # Local material is supplemental and can only be exposed through an
        # explicit manifest entry. It never changes GitHub/controller source
        # authority or causes a broad skills-directory scan.
        if self._local_context and (
            namespace in {"all", "core", "local"} or namespace.startswith("benchmark/")
        ):
            # Local manifest scope (harness/benchmark/phase/agent) is an access
            # policy. Subject labels are advisory discovery metadata and must
            # not make an otherwise in-scope document disappear.
            local_documents = self._local_context.list_documents(
                harness="crucible",
                benchmark=benchmark_name if namespace != "core" else None,
                phase=phase,
                agent=agent,
                subject_area="all",
            )
            if namespace == "core":
                local_documents = [
                    item for item in local_documents if item.get("benchmark") is None
                ]
            for item in local_documents:
                labels = LocalContextSource._values(item.get("subject_area"))
                requested = LocalContextSource._values(subject_area)
                item["subject_areas"] = labels
                item["subject_match"] = bool(
                    not requested
                    or "all" in requested
                    or "general" in requested
                    or not labels
                    or set(map(str.lower, requested)) & set(map(str.lower, labels))
                )
                item["entrypoint"] = bool(item.get("entrypoint", False))
                documents.append(item)

        documents.sort(key=lambda item: item["path"])
        matching_count = sum(bool(item.get("subject_match")) for item in documents)
        expected_namespaces = []
        if namespace in {"all", "core"}:
            expected_namespaces.append("core")
        if benchmark_name and (
            namespace == "all" or namespace.startswith("benchmark/")
        ):
            expected_namespaces.append(f"benchmark/{benchmark_name}")
        available_namespaces = {
            str(item.get("namespace"))
            for item in documents
            if item.get("source") != "local"
        }
        if namespace not in {"all", "core", "local"}:
            expected_namespaces = [namespace]
        inventory_complete = all(
            expected in available_namespaces for expected in expected_namespaces
        )
        excluded_count = sum(exclusion_counts.values())
        response: dict[str, Any] = {
            "found": bool(documents),
            "operation": operation,
            "benchmark": benchmark,
            "namespace": namespace,
            "subject_area": subject_area,
            "source": selected_source,
            "effective_source": selected_source,
            "selection": selection,
            "sources_considered": sources_considered,
            "provenance": [item["provenance"] for item in documents],
            "guidance": {
                "subject_area": subject_area,
                "required_subject_areas": list(self._CONTEXT_SUBJECTS),
                "selection": (
                    "complete safe namespace inventory; subject matching is "
                    "advisory metadata and never hides documents"
                ),
            },
            "inventory": {
                "complete": inventory_complete,
                "discovered": len(documents) + excluded_count,
                "returned": len(documents),
                "subject_matches": matching_count,
                "excluded": excluded_count,
                "exclusions": [
                    {"reason": reason, "count": count}
                    for reason, count in sorted(exclusion_counts.items())
                ],
                "missing_namespaces": sorted(
                    set(expected_namespaces) - available_namespaces
                ),
            },
            "workspace_policy": {
                "effective_source": selected_source,
                "supplemental_sources": ["local"] if self._local_context else [],
                "effective_visibility": "default",
                "alternate_visibility": "explicit_comparison_only",
                "audience": "assigned_by_pipeline_workspace",
            },
            "documents": documents,
            "repository_namespaces": sorted(
                {
                    f"{self._entry_namespace(entry)}/{name}"
                    for name, entry in self._source_repo_entries().items()
                    if name != "crucible"
                }
            ),
        }
        if operation in {"read", "search"} or include_content:
            requested = path.strip().removeprefix("workspace://")
            matches = documents
            if operation == "read":
                matches = [
                    item
                    for item in documents
                    if item["path"] == requested
                    or item["ref"] == requested
                    or item["uri"] == path.strip()
                    or item["source_path"] == requested
                    or item["path"].startswith(requested.rstrip("/") + "/")
                ]
            if operation == "search":
                matches = documents
            if not matches:
                response.update(
                    {"found": False, "reason": "document_not_found", "path": path}
                )
                return response
            read_documents: list[dict[str, Any]] = []
            for item in matches:
                if item["namespace"] == "local":
                    content = (
                        self._local_context.read(item["source_path"])
                        if self._local_context
                        else None
                    )
                    if content is None:
                        return {
                            **response,
                            "found": False,
                            "reason": "document_unreadable",
                            "path": path,
                        }
                    item = dict(item)
                    item["content"] = content
                else:
                    logical_path = item["path"]
                    owner = next(
                        (
                            candidate
                            for candidate in sorted(source_roots, key=len, reverse=True)
                            if logical_path == candidate
                            or logical_path.startswith(candidate + "/")
                        ),
                        "core",
                    )
                    root = source_roots.get(owner)
                    resolved = (
                        self._safe_context_path(root, item["source_path"])
                        if root
                        else None
                    )
                    if resolved is None:
                        return {
                            **response,
                            "found": False,
                            "reason": "unsafe_document_path",
                            "path": path,
                        }
                    try:
                        item = dict(item)
                        item["content"] = resolved.read_text(encoding="utf-8")
                    except OSError:
                        return {
                            **response,
                            "found": False,
                            "reason": "document_unreadable",
                            "path": path,
                        }
                read_documents.append(item)
            if operation == "search":
                needle = query.strip().lower()
                if not needle:
                    response.update(
                        {
                            "found": False,
                            "reason": "missing_search_query",
                            "query": query,
                        }
                    )
                    return response
                read_documents = [
                    item
                    for item in read_documents
                    if needle in str(item.get("content", "")).lower()
                    or needle in str(item.get("path", "")).lower()
                ]
                if not read_documents:
                    response.update(
                        {"found": False, "reason": "no_search_matches", "query": query}
                    )
                    return response
            response["documents"] = read_documents
            if operation == "read":
                response["document"] = (
                    read_documents[0] if len(read_documents) == 1 else None
                )
        return response

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
        catalog_entries = self._catalog_benchmark_entries()
        if not name or (
            not self._benchmarks_dir.exists()
            and not catalog_entries
            and not self._controller_repo_config()
        ):
            return meta
        entry = catalog_entries.get(name)
        source_dir = self._source_benchmark_path(name, entry)
        source_dir = source_dir or self._controller_benchmark_path(name)
        if source_dir is None and entry:
            repository = entry.get("repository")
            ref = self._entry_ref(entry)
            if isinstance(repository, str) and isinstance(ref, str):
                for filename, key in (
                    ("multiplex.json", "multiplex"),
                    ("rickshaw.json", "rickshaw"),
                ):
                    data = self._catalog_fetcher.read_json(
                        filename, repository=repository, ref=ref
                    )
                    if data is not None:
                        meta[key] = data
                return meta
        bench_dir = (source_dir or (self._benchmarks_dir / name)).resolve()
        allowed_roots = [
            self._benchmarks_dir.resolve(),
            (self._home / "repos").resolve(),
        ]
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
        source_entries = self._catalog_benchmark_entries()
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

        source_entries = self._catalog_benchmark_entries()
        available = set(self._discover_benchmarks()) | set(source_entries)

        # An explicit benchmark/repository name must win over generic workload
        # keywords.  In particular, RDMA requests must not silently become
        # uperf requests merely because both are network throughput tests.
        for name in sorted(available, key=len, reverse=True):
            if re.search(rf"\b{re.escape(name.lower())}\b", search_text):
                return name

        if re.search(
            r"\b(perftest|rdma|infiniband|ib_(?:write|read|send|atomic))\b", search_text
        ):
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
