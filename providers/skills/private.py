from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from paths import PRIVATE_SKILLS_DIR as DEFAULT_PRIVATE_SKILLS_DIR

from .base import BenchmarkSuite, RunfileTemplate, SkillProvider
from .gateway import OrganizationSkillResolver, SkillGatewayError


def _validate_suite_name(suite_name: str) -> None:
    """Harness identities are identifiers, never caller-provided file paths."""
    if (
        not isinstance(suite_name, str)
        or len(suite_name) > 64
        or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", suite_name)
    ):
        raise SkillGatewayError("invalid_harness", "Invalid harness identifier")


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

    def __init__(
        self,
        skills_dir: str | Path | None = None,
        *,
        resolver: OrganizationSkillResolver | None = None,
    ) -> None:
        self._dir = Path(skills_dir) if skills_dir else DEFAULT_PRIVATE_SKILLS_DIR
        self._cache: dict[str, dict[str, Any]] = {}
        self._resolver = resolver or OrganizationSkillResolver.from_instance_config()

    def bind_attempt(self, ticket_id: str, attempt_id: str, phase: str) -> None:
        """Bind deterministic tool configuration to the same pinned documents."""
        self._resolver = self._resolver.for_attempt(ticket_id, attempt_id, phase)
        self._cache.clear()

    @property
    def organization_resolver(self) -> OrganizationSkillResolver:
        return self._resolver

    def uses_organization_config(self, suite_name: str) -> bool:
        _validate_suite_name(suite_name)
        return self._resolver.uses_organization_config(f"harness/{suite_name}")

    def _load_config(self, suite_name: str) -> dict[str, Any]:
        _validate_suite_name(suite_name)
        if self.uses_organization_config(suite_name):
            # Configured sources are canonical. Never silently fall back after
            # a required source error, or overlay the old instance JSON.
            config = self._resolver.get_runtime_config(f"harness/{suite_name}")
            assert config is not None
            return config
        subject = f"harness/{suite_name}"
        if self._resolver.uses_legacy_config(subject):
            self._resolver.get_runtime_config(f"harness/{suite_name}")
        elif self._resolver.has_subject(subject):
            # A docs-only organization package is not an implicit opt-in to
            # local private JSON. Provider defaults remain available through
            # the aggregate; legacy JSON requires legacy_config: true.
            return {}
        if suite_name in self._cache:
            return self._cache[suite_name]

        try:
            root = self._dir.resolve()
            config_file = root / f"{suite_name}.json"
            if config_file.is_symlink() or not config_file.resolve().is_relative_to(
                root
            ):
                raise SkillGatewayError(
                    "private_config_denied",
                    "Legacy configuration must stay in its root",
                )
            if not config_file.exists():
                self._cache[suite_name] = {}
                return {}
            protected = [
                binding.service_config
                for group in self._resolver.bindings.values()
                for binding in group
                if binding.service_config is not None
            ]
            # The Crucible projection also applies to aliases of its old file.
            if suite_name != "crucible":
                protected.append(root / "crucible.json")
            read_fd = os.open(config_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                handle = os.fdopen(read_fd, "r", encoding="utf-8")
            except Exception:
                os.close(read_fd)
                raise
            with handle:
                opened = os.fstat(handle.fileno())
                if not stat.S_ISREG(opened.st_mode) or opened.st_size > 1024 * 1024:
                    raise SkillGatewayError(
                        "private_config_denied",
                        "Legacy configuration is not a bounded file",
                    )
                for target in protected:
                    try:
                        target_stat = target.stat()
                    except FileNotFoundError:
                        continue
                    if (opened.st_dev, opened.st_ino) == (
                        target_stat.st_dev,
                        target_stat.st_ino,
                    ):
                        raise SkillGatewayError(
                            "private_config_denied",
                            "Service configuration is not a legacy export",
                        )
                content = handle.read(1024 * 1024 + 1)
                if len(content) > 1024 * 1024:
                    raise SkillGatewayError(
                        "private_config_denied", "Legacy configuration exceeds limit"
                    )
                data = json.loads(content)
            data = data if isinstance(data, dict) else {}
            self._cache[suite_name] = data
            return data
        except (OSError, RuntimeError):
            raise SkillGatewayError(
                "private_config_denied", "Cannot validate legacy configuration origin"
            ) from None
        except (json.JSONDecodeError, UnicodeError):
            self._cache[suite_name] = {}
            return {}

    def list_suites_with_private_config(self) -> list[str]:
        legacy = (
            [
                f.stem
                for f in sorted(self._dir.iterdir())
                if (
                    f.suffix == ".json"
                    and f.is_file()
                    and not f.is_symlink()
                    and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", f.stem)
                    and len(f.stem) <= 64
                )
            ]
            if self._dir.exists()
            else []
        )
        configured = [
            subject.removeprefix("harness/")
            for subject in self._resolver.configured_subjects()
            if subject.startswith("harness/")
        ]
        return sorted(set(legacy + configured))

    async def get_private_config(self, suite_name: str, key: str) -> Any | None:
        config = self._load_config(suite_name)
        return config.get(key)

    async def get_all_private_config(self, suite_name: str) -> dict[str, Any]:
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
