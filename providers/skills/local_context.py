"""Explicit, manifest-backed local context for source-aware gateways."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_LOCAL_DOCUMENTS = 256
MAX_LOCAL_DOCUMENT_BYTES = 1024 * 1024
MAX_LOCAL_PACKAGE_BYTES = 16 * MAX_LOCAL_DOCUMENT_BYTES


@dataclass(frozen=True)
class LocalContextSnapshot:
    """Immutable content captured from one manifest-backed local source."""

    revision: str
    documents: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {"revision": self.revision, "documents": list(self.documents)}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> LocalContextSnapshot:
        revision = value.get("revision")
        documents = value.get("documents")
        if (
            not isinstance(revision, str)
            or len(revision) != 64
            or any(char not in "0123456789abcdef" for char in revision)
            or not isinstance(documents, list)
            or len(documents) > MAX_LOCAL_DOCUMENTS
            or any(not isinstance(item, dict) for item in documents)
        ):
            raise ValueError("invalid pinned local context snapshot")
        total = 0
        for item in documents:
            content = item.get("content")
            source_path = item.get("source_path")
            digest = item.get("digest")
            if (
                not isinstance(item.get("entry"), dict)
                or not isinstance(item.get("entry_id"), str)
                or not isinstance(source_path, str)
                or not isinstance(content, str)
                or not isinstance(digest, str)
                or hashlib.sha256(content.encode("utf-8")).hexdigest() != digest
            ):
                raise ValueError("invalid pinned local context document")
            total += len(content.encode("utf-8"))
            if total > MAX_LOCAL_PACKAGE_BYTES:
                raise ValueError("pinned local context exceeds limit")
        return cls(revision, tuple(documents))

    def list_documents(
        self,
        *,
        harness: str,
        benchmark: str | None = None,
        phase: str | None = None,
        agent: str | None = None,
        subject_area: str | list[str] = "all",
    ) -> list[dict[str, Any]]:
        results = []
        for item in self.documents:
            entry = item["entry"]
            if not LocalContextSource._matches_scope(
                entry,
                harness=harness,
                benchmark=benchmark,
                phase=phase,
                agent=agent,
                subject_area=subject_area,
            ):
                continue
            relative = item["source_path"]
            provenance = {
                "source": "local",
                "source_reason": "explicit_manifest_entry",
                "revision": self.revision,
                "entry_id": item["entry_id"],
                "path": relative,
                "harness": harness,
                "benchmark": entry.get("benchmark"),
                "phase": entry.get("phases", entry.get("phase")),
                "agent": entry.get("agents", entry.get("agent")),
                "digest": item["digest"],
            }
            extra_provenance = entry.get("provenance")
            if isinstance(extra_provenance, dict):
                provenance.update(extra_provenance)
            subjects = entry.get(
                "subject_area", entry.get("subjects", entry.get("subject"))
            )
            results.append(
                {
                    "namespace": "local",
                    "harness": harness,
                    "path": f"local/{relative}",
                    "ref": f"local/{relative}",
                    "uri": f"crucible://local/{relative}",
                    "source_path": relative,
                    "source": "local",
                    "authority": "supplemental",
                    "provenance": provenance,
                    "benchmark": entry.get("benchmark"),
                    "entrypoint": bool(entry.get("entrypoint", False)),
                    "subject_area": subjects,
                }
            )
        return sorted(results, key=lambda item: item["path"])

    def read(self, relative: str) -> str | None:
        for item in self.documents:
            if item["source_path"] == relative:
                return item["content"]
        return None


class LocalContextSource:
    """Read explicitly mapped local documents without inferring ownership.

    The manifest is the authority for which local files are context. A file
    merely existing below the local root is not enough to expose it through
    the gateway.
    """

    def __init__(self, manifest_path: str | Path, *, root: str | Path | None = None):
        self.manifest_path = Path(manifest_path).resolve()
        self.root = Path(root).resolve() if root else self.manifest_path.parent

    @property
    def binding_identity(self) -> str:
        """Opaque identity for the configured root and manifest location."""
        return hashlib.sha256(
            f"{self.root}\0{self.manifest_path}".encode("utf-8")
        ).hexdigest()

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

    def revision(self) -> str | None:
        """Return a content revision for the manifest and its mapped files."""
        try:
            return self.capture_snapshot().revision
        except (OSError, ValueError, TypeError):
            return None

    def capture_snapshot(self) -> LocalContextSnapshot:
        """Capture and verify all explicitly mapped content as one revision."""
        manifest_bytes = self.manifest_path.read_bytes()
        if len(manifest_bytes) > MAX_LOCAL_DOCUMENT_BYTES:
            raise ValueError("local context manifest exceeds limit")
        manifest = json.loads(manifest_bytes)
        if not isinstance(manifest, dict):
            raise ValueError("local context manifest must be an object")
        entries = manifest.get("entries", manifest.get("documents", []))
        if isinstance(entries, dict):
            if any(not isinstance(value, dict) for value in entries.values()):
                raise ValueError("local context manifest entry is invalid")
            entries = [dict(value, id=key) for key, value in entries.items()]
        if not isinstance(entries, list) or len(entries) > MAX_LOCAL_DOCUMENTS:
            raise ValueError("local context manifest entries are invalid")
        captured = []
        total = 0
        original_bytes: dict[str, bytes] = {}
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise ValueError("local context manifest entry is invalid")
            safe = self._safe_path(entry.get("path", entry.get("file")))
            if safe is None:
                raise ValueError("local context manifest document is invalid")
            relative, path = safe
            content = path.read_bytes()
            if len(content) > MAX_LOCAL_DOCUMENT_BYTES or b"\x00" in content:
                raise ValueError("local context document is invalid or too large")
            total += len(content)
            if total > MAX_LOCAL_PACKAGE_BYTES:
                raise ValueError("local context package exceeds limit")
            text = content.decode("utf-8")
            original_bytes[relative] = content
            captured.append(
                {
                    "entry": entry,
                    "entry_id": entry.get("id", f"entry-{index}"),
                    "source_path": relative,
                    "content": text,
                    "digest": hashlib.sha256(content).hexdigest(),
                }
            )
        if self.manifest_path.read_bytes() != manifest_bytes:
            raise ValueError("local context manifest changed during capture")
        for relative, content in original_bytes.items():
            safe = self._safe_path(relative)
            if safe is None or safe[1].read_bytes() != content:
                raise ValueError("local context document changed during capture")
        revision_hash = hashlib.sha256()
        revision_hash.update(manifest_bytes)
        for relative, content in sorted(original_bytes.items()):
            revision_hash.update(relative.encode("utf-8"))
            revision_hash.update(b"\0")
            revision_hash.update(content)
            revision_hash.update(b"\0")
        return LocalContextSnapshot(revision_hash.hexdigest(), tuple(captured))

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
        try:
            return self.capture_snapshot().list_documents(
                harness=harness,
                benchmark=benchmark,
                phase=phase,
                agent=agent,
                subject_area=subject_area,
            )
        except (OSError, ValueError, TypeError, UnicodeDecodeError):
            return []

    def read(self, relative: str) -> str | None:
        try:
            return self.capture_snapshot().read(relative)
        except (OSError, ValueError, TypeError, UnicodeDecodeError):
            return None
