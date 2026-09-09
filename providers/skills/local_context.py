"""Explicit, manifest-backed local context for source-aware gateways."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class LocalContextSource:
    """Read explicitly mapped local documents without inferring ownership.

    The manifest is the authority for which local files are context. A file
    merely existing below the local root is not enough to expose it through
    the gateway.
    """

    def __init__(self, manifest_path: str | Path, *, root: str | Path | None = None):
        self.manifest_path = Path(manifest_path).resolve()
        self.root = Path(root).resolve() if root else self.manifest_path.parent

    @staticmethod
    def _values(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        if isinstance(value, (list, tuple, set)):
            return [str(part).strip() for part in value if str(part).strip()]
        return []

    def _entries(self) -> list[dict[str, Any]]:
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(manifest, dict):
            return []
        entries = manifest.get("entries", manifest.get("documents", []))
        if isinstance(entries, dict):
            entries = [dict(value, id=key) for key, value in entries.items()]
        if not isinstance(entries, list):
            return []
        return [entry for entry in entries if isinstance(entry, dict)]

    def _safe_path(self, relative: Any) -> tuple[str, Path] | None:
        if (
            not isinstance(relative, str)
            or not relative
            or Path(relative).is_absolute()
        ):
            return None
        candidate = (self.root / relative).resolve()
        try:
            if not candidate.is_relative_to(self.root):
                return None
        except (AttributeError, ValueError):
            return None
        if not candidate.is_file():
            return None
        return candidate.relative_to(self.root).as_posix(), candidate

    @classmethod
    def _matches_subject(
        cls, entry: dict[str, Any], subject_area: str | list[str]
    ) -> bool:
        requested = {item.lower() for item in cls._values(subject_area)}
        if not requested or "all" in requested or "general" in requested:
            return True
        subjects = {
            item.lower()
            for item in cls._values(
                entry.get("subject_area", entry.get("subjects", entry.get("subject")))
            )
        }
        return not subjects or bool(requested & subjects)

    @classmethod
    def _matches_scope(
        cls,
        entry: dict[str, Any],
        *,
        harness: str,
        benchmark: str | None,
        phase: str | None,
        agent: str | None,
        subject_area: str | list[str],
    ) -> bool:
        if entry.get("harness") != harness:
            return False

        entry_benchmark = entry.get("benchmark")
        if benchmark:
            if entry_benchmark and entry_benchmark != benchmark:
                return False
        elif entry_benchmark:
            return False

        entry_phases = cls._values(entry.get("phases", entry.get("phase")))
        if entry_phases and (not phase or phase not in entry_phases):
            return False

        entry_agents = cls._values(entry.get("agents", entry.get("agent")))
        if entry_agents and (not agent or agent not in entry_agents):
            return False

        return cls._matches_subject(entry, subject_area)

    def list_documents(
        self,
        *,
        harness: str,
        benchmark: str | None = None,
        phase: str | None = None,
        agent: str | None = None,
        subject_area: str | list[str] = "all",
    ) -> list[dict[str, Any]]:
        """Return mapped documents matching the requested gateway scope."""
        documents: list[dict[str, Any]] = []
        for index, entry in enumerate(self._entries()):
            if not self._matches_scope(
                entry,
                harness=harness,
                benchmark=benchmark,
                phase=phase,
                agent=agent,
                subject_area=subject_area,
            ):
                continue
            safe = self._safe_path(entry.get("path", entry.get("file")))
            if safe is None:
                continue
            relative, _ = safe
            entry_id = entry.get("id", f"entry-{index}")
            provenance = {
                "source": "local",
                "source_reason": "explicit_manifest_entry",
                "manifest": str(self.manifest_path),
                "entry_id": entry_id,
                "path": relative,
                "harness": harness,
                "benchmark": entry.get("benchmark"),
                "phase": entry.get("phases", entry.get("phase")),
                "agent": entry.get("agents", entry.get("agent")),
            }
            extra_provenance = entry.get("provenance")
            if isinstance(extra_provenance, dict):
                provenance.update(extra_provenance)
            documents.append(
                {
                    "namespace": "local",
                    "path": f"local/{relative}",
                    "ref": f"local/{relative}",
                    "uri": f"crucible://local/{relative}",
                    "source_path": relative,
                    "source": "local",
                    "authority": "supplemental",
                    "provenance": provenance,
                    "benchmark": entry.get("benchmark"),
                    "subject_area": entry.get(
                        "subject_area", entry.get("subjects", entry.get("subject"))
                    ),
                }
            )
        return sorted(documents, key=lambda item: item["path"])

    def read(self, relative: str) -> str | None:
        safe = self._safe_path(relative)
        if safe is None:
            return None
        try:
            return safe[1].read_text(encoding="utf-8")
        except OSError:
            return None
