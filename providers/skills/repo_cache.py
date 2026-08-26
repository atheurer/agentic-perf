from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from paths import SKILL_CACHE_DIR as DEFAULT_CACHE_DIR

logger = logging.getLogger(__name__)


class RepoCache:
    def __init__(self, cache_dir: str | Path | None = None) -> None:
        self._dir = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR

    def ensure_repo(self, name: str, url: str) -> Path:
        repo_path = self._dir / name
        if repo_path.exists() and (repo_path / ".git").exists():
            logger.info(f"[repo-cache] Updating {name} from {url}")
            subprocess.run(
                ["git", "pull", "--ff-only"],
                cwd=repo_path,
                capture_output=True,
                timeout=60,
            )
        else:
            repo_path.parent.mkdir(parents=True, exist_ok=True)
            logger.info(f"[repo-cache] Cloning {name} from {url}")
            subprocess.run(
                ["git", "clone", "--depth", "1", url, str(repo_path)],
                capture_output=True,
                timeout=120,
            )
        return repo_path

    def get_path(self, name: str) -> Path | None:
        repo_path = self._dir / name
        if repo_path.exists():
            return repo_path
        return None

    def list_docs(
        self,
        name: str,
        subdirs: list[str] | str = "docs",
        extensions: list[str] | None = None,
    ) -> list[dict[str, str | int]]:
        repo_path = self._dir / name
        if isinstance(subdirs, str):
            subdirs = [subdirs]
        if extensions is None:
            extensions = [".md", ".yml", ".yaml"]
        results = []
        seen: set[str] = set()
        for subdir in subdirs:
            docs_path = repo_path / subdir
            if not docs_path.is_dir():
                continue
            for f in sorted(docs_path.rglob("*")):
                if not f.is_file():
                    continue
                if not any(f.name.endswith(ext) for ext in extensions):
                    continue
                rel = str(f.relative_to(repo_path))
                if rel not in seen:
                    seen.add(rel)
                    results.append({"path": rel, "size_bytes": f.stat().st_size})
        return sorted(results, key=lambda d: d["path"])

    def read_file(self, name: str, rel_path: str) -> str | None:
        if not name:
            return None
        repo_path = self._dir / name
        if not repo_path.exists():
            return None

        clean_path = str(rel_path or "").strip().lstrip("/")
        if clean_path.startswith(f"{name}/"):
            clean_path = clean_path[len(f"{name}/") :].lstrip("/")

        candidates = [
            repo_path / clean_path,
            repo_path / "docs" / clean_path,
            repo_path / "config" / clean_path,
        ]

        if clean_path.startswith("docs/"):
            stripped = clean_path[len("docs/") :].lstrip("/")
            candidates.append(repo_path / stripped)
            candidates.append(repo_path / "docs" / stripped)
        if clean_path.startswith("config/"):
            stripped = clean_path[len("config/") :].lstrip("/")
            candidates.append(repo_path / stripped)
            candidates.append(repo_path / "config" / stripped)

        name_only = Path(clean_path).name
        if name_only:
            candidates.append(repo_path / "docs" / name_only)
            candidates.append(repo_path / "config" / name_only)
            candidates.append(repo_path / name_only)

        repo_resolved = repo_path.resolve()
        for target in candidates:
            try:
                resolved = target.resolve()
                if not resolved.is_relative_to(repo_resolved):
                    continue
                if target.is_file():
                    return target.read_text()
            except (OSError, ValueError):
                continue

        if name_only:
            for subdir in ("docs", "config"):
                sub_path = repo_path / subdir
                if sub_path.is_dir():
                    for f in sub_path.rglob(name_only):
                        if f.is_file():
                            try:
                                resolved = f.resolve()
                                if resolved.is_relative_to(repo_resolved):
                                    return f.read_text()
                            except (OSError, ValueError):
                                continue

        return None
