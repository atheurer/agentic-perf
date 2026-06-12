from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path.home() / ".agentic-perf" / "skill-cache"


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
        repo_path = self._dir / name
        target = repo_path / rel_path
        if not target.is_file():
            return None
        resolved = target.resolve()
        if not str(resolved).startswith(str(repo_path.resolve())):
            return None
        return target.read_text()
