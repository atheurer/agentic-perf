from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .base import BenchmarkSuite, RunfileTemplate, SkillProvider

DEFAULT_PRIVATE_SKILLS_DIR = Path.home() / ".agentic-perf" / "private-skills"


class PrivateSkillProvider(SkillProvider):
    """Loads organization-specific private skill configs from a local directory.

    Private skills contain knowledge that shouldn't be in public repos:
    container registry URLs, vault paths for auth tokens, custom install flags,
    internal infrastructure details. Secrets themselves stay in vault — this
    provider stores the knowledge of where to find them.

    Directory structure:
        ~/.agentic-perf/private-skills/
        ├── crucible.json    # Private config for crucible suite
        ├── custom-bench.json  # Private config for a custom benchmark
        └── ...

    Each file is JSON with arbitrary keys:
        {
            "container_registry": "quay.io/crucible",
            "auth_vault_path": "secret/perf/registry-tokens",
            "install_flags": "--client-server-registry quay.io/crucible",
            "internal_docs_url": "https://wiki.internal/crucible-setup"
        }
    """

    def __init__(self, skills_dir: str | Path | None = None) -> None:
        self._dir = Path(skills_dir) if skills_dir else DEFAULT_PRIVATE_SKILLS_DIR
        self._cache: dict[str, dict[str, Any]] = {}

    def _load_config(self, suite_name: str) -> dict[str, Any]:
        if suite_name in self._cache:
            return self._cache[suite_name]

        config_file = self._dir / f"{suite_name}.json"
        if not config_file.exists():
            self._cache[suite_name] = {}
            return {}

        try:
            data = json.loads(config_file.read_text())
            self._cache[suite_name] = data
            return data
        except (json.JSONDecodeError, OSError):
            self._cache[suite_name] = {}
            return {}

    def list_suites_with_private_config(self) -> list[str]:
        if not self._dir.exists():
            return []
        return [
            f.stem
            for f in sorted(self._dir.iterdir())
            if f.suffix == ".json" and f.is_file()
        ]

    async def get_private_config(
        self, suite_name: str, key: str
    ) -> Any | None:
        config = self._load_config(suite_name)
        return config.get(key)

    async def get_all_private_config(
        self, suite_name: str
    ) -> dict[str, Any]:
        return dict(self._load_config(suite_name))

    async def list_benchmarks(self) -> list[BenchmarkSuite]:
        return []

    async def get_benchmark(self, name: str) -> BenchmarkSuite | None:
        return None

    async def resolve_benchmark(self, requirements: dict[str, Any]) -> str | None:
        return None

    async def generate_runfile(
        self, benchmark: str, params: dict[str, Any]
    ) -> RunfileTemplate:
        return RunfileTemplate(benchmark=benchmark)
