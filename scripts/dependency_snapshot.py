"""Submit the repository's pip-compile locks to GitHub's dependency graph."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

LOCK_FILES = ("requirements.lock", "requirements-dev.lock")
PIN = re.compile(
    r"(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]*)(?:\[[^\]]+\])?"
    r"==(?P<version>[A-Za-z0-9][A-Za-z0-9.!+_-]*)(?:\s*\\)?"
)


def parse_lock(content: str, filename: str) -> dict[str, dict[str, str]]:
    """Extract exact pins and reject formats we cannot represent accurately."""
    resolved: dict[str, dict[str, str]] = {}
    names: set[str] = set()
    for number, raw_line in enumerate(content.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith(("#", "--hash=")):
            continue
        match = PIN.fullmatch(line)
        if match is None:
            raise ValueError(f"Unsupported lock entry in {filename}:{number}: {line}")
        name = re.sub(r"[-_.]+", "-", match["name"]).lower()
        if name in names:
            raise ValueError(f"Duplicate package {name} in {filename}:{number}")
        names.add(name)
        purl = f"pkg:pypi/{name}@{quote(match['version'], safe='')}"
        # The lock files contain resolved transitive as well as direct packages.
        resolved[purl] = {
            "package_url": purl,
            "relationship": "indirect",
            # requirements-dev.lock is also installed into the container.
            "scope": "runtime",
        }
    if not resolved:
        raise ValueError(f"No pinned packages found in {filename}")
    return resolved


def snapshot(root: Path, sha: str, run_id: str) -> dict[str, object]:
    """Build a snapshot tied to the commit whose lock files were checked out."""
    manifests = {}
    for filename in LOCK_FILES:
        content = (root / filename).read_text(encoding="utf-8")
        manifests[filename] = {
            "name": filename,
            "file": {"source_location": filename},
            "resolved": parse_lock(content, filename),
        }
    return {
        "version": 0,
        "sha": sha,
        "ref": "refs/heads/main",
        "job": {"correlator": "agentic-perf-pip-locks", "id": run_id},
        "detector": {
            "name": "agentic-perf-pip-locks",
            "version": "1.0.0",
            "url": "https://github.com/atheurer/agentic-perf",
        },
        "scanned": datetime.now(timezone.utc).isoformat(),
        "manifests": manifests,
    }


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    print(
        json.dumps(
            snapshot(root, os.environ["GITHUB_SHA"], os.environ["GITHUB_RUN_ID"])
        )
    )


if __name__ == "__main__":
    main()
