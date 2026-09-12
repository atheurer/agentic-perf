from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from paths import get_ticket_workspace_dir
from providers.execution import AuditedFilesystem, RootedPath

logger = logging.getLogger(__name__)


class WorkspaceSecurityError(ValueError):
    """Raised when a workspace file reference attempts path traversal."""


class WorkspaceManager:
    """Manages per-ticket scratchpad workspace files and query operations."""

    NAMESPACES = ("context", "runfiles", "results", "logs", "metadata", "scratch")
    FILE_KINDS = frozenset(
        {
            "source_context",
            "runfile",
            "result_summary",
            "raw_artifact",
            "log",
            "metadata",
            "scratch",
        }
    )
    CONTEXT_INDEX = "context/indexes/documents.json"

    def __init__(
        self,
        ticket_id: str | None = None,
        workspace_dir: Path | str | None = None,
        agent_name: str | None = None,
        phase: str | None = None,
        audit_emit: Any | None = None,
    ) -> None:
        self.ticket_id = ticket_id or ""
        self.agent_name = agent_name or "unknown"
        self.phase = phase or self._phase_for_agent(self.agent_name)
        self.audience = self._audience_for_agent(self.agent_name)
        if workspace_dir is not None:
            self.workspace_dir = Path(workspace_dir).resolve()
            self.workspace_dir.mkdir(parents=True, exist_ok=True)
        else:
            self.workspace_dir = get_ticket_workspace_dir(self.ticket_id).resolve()
        self._filesystem = AuditedFilesystem(
            RootedPath(self.workspace_dir, "workspace"),
            ticket_id=self.ticket_id or "workspace-scratch",
            emit=audit_emit,
        )
        for namespace in self.NAMESPACES:
            self._filesystem.mkdir(namespace)

    @staticmethod
    def _audience_for_agent(agent_name: str) -> str:
        value = agent_name.lower().replace("-agent", "")
        return value or "unknown"

    @classmethod
    def _phase_for_agent(cls, agent_name: str) -> str:
        return cls._audience_for_agent(agent_name)

    @property
    def _manifest_path(self) -> Path:
        return self.workspace_dir / "metadata" / "workspace-manifest.json"

    def _load_manifest(self) -> dict[str, Any]:
        try:
            value = json.loads(self._manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"schema_version": 1, "entries": {}}
        return (
            value if isinstance(value, dict) else {"schema_version": 1, "entries": {}}
        )

    def _stamp(
        self,
        filename: str,
        *,
        source: str = "local",
        authority: str = "unclassified",
    ) -> None:
        manifest = self._load_manifest()
        entries = manifest.setdefault("entries", {})
        entries[filename] = {
            "source": source,
            "authority": authority,
            "phase": self.phase,
            "audience": self.audience,
            "agent": self.agent_name,
            "kind": self.infer_kind(filename),
        }
        self._filesystem.write(
            "metadata/workspace-manifest.json", json.dumps(manifest, indent=2) + "\n"
        )

    def _is_visible(self, file_ref: str, include_alternates: bool = False) -> bool:
        cleaned = file_ref.strip()
        if cleaned.startswith("workspace://"):
            cleaned = cleaned[len("workspace://") :]
        if cleaned == "context/effective-context.json":
            return True
        entry = self._load_manifest().get("entries", {}).get(cleaned)
        if not isinstance(entry, dict):
            return True  # Legacy/unclassified files remain compatible.
        effective = self.read_effective_context() or {}
        if effective.get("phase") == self.phase and cleaned in {
            str(ref).removeprefix("workspace://")
            for ref in effective.get("workspace_refs", [])
        }:
            return True
        if include_alternates:
            return True
        if entry.get("authority") == "alternate":
            return False
        audience = entry.get("audience")
        return audience in (None, "unknown", self.audience, "shared")

    def _check_visible(self, file_ref: str, include_alternates: bool) -> None:
        if not self._is_visible(file_ref, include_alternates):
            raise WorkspaceSecurityError(
                f"Workspace file is not visible to audience '{self.audience}': {file_ref}"
            )

    @classmethod
    def infer_kind(cls, filename: str) -> str:
        """Infer a manifest kind from the backward-compatible namespace."""
        parts = Path(filename).parts
        if not parts:
            return "scratch"
        namespace = parts[0]
        if namespace == "context":
            return "source_context"
        if namespace == "runfiles":
            return "runfile"
        if namespace == "results":
            return "raw_artifact" if "raw" in parts[1:] else "result_summary"
        if namespace == "logs":
            return "log"
        if namespace == "metadata":
            return "metadata"
        return "scratch"

    def resolve_path(self, file_ref: str) -> Path:
        """Resolve a workspace:// URI or relative filename to an absolute path.

        Prevents path traversal outside of the workspace directory.
        """
        cleaned = file_ref.strip()
        if cleaned.startswith("workspace://"):
            cleaned = cleaned[len("workspace://") :]

        target_path = (self.workspace_dir / cleaned).resolve()
        try:
            target_path.relative_to(self.workspace_dir)
        except ValueError as e:
            raise WorkspaceSecurityError(
                f"Access denied: file reference '{file_ref}' resolves outside workspace"
            ) from e

        return target_path

    def save_file(
        self,
        filename: str,
        content: str | bytes,
        overwrite: bool = True,
    ) -> tuple[str, Path]:
        """Save text or binary content into the workspace.

        Returns (file_ref, resolved_path).
        """
        path = self.resolve_path(filename)
        if not overwrite and path.exists():
            base_stem = path.stem
            suffix = path.suffix
            counter = 1
            while path.exists():
                path = path.parent / f"{base_stem}_{counter}{suffix}"
                counter += 1

        rel_name = str(path.relative_to(self.workspace_dir))
        path = self._filesystem.write(rel_name, content)
        self._stamp(rel_name)
        return f"workspace://{rel_name}", path

    def save_artifact_reference(
        self,
        filename: str,
        artifact_ref: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[str, Path]:
        """Store a small workspace reference to a large artifact-store object."""
        payload: dict[str, Any] = {"artifact_ref": artifact_ref}
        if metadata:
            payload["metadata"] = metadata
        raw_filename = filename.lstrip("/")
        if not raw_filename.startswith("results/raw/"):
            raw_filename = f"results/raw/{raw_filename}"
        return self.save_file(
            raw_filename,
            json.dumps(payload, indent=2) + "\n",
        )

    def save_source_snapshot(
        self,
        source: str,
        provenance: dict[str, Any],
        files: dict[str, str],
        benchmark: str | None = None,
    ) -> dict[str, Any]:
        """Persist an alternate source snapshot without selecting its authority."""
        if source not in {"github", "controller", "local"}:
            raise ValueError(f"unsupported context source: {source}")
        root = f"context/sources/{source}"
        if benchmark:
            root += f"/benchmarks/{benchmark.replace('/', '_')}"
        refs: dict[str, str] = {}
        for filename, content in files.items():
            ref, _ = self.save_file(f"{root}/{filename.lstrip('/')}", content)
            refs[filename] = ref
            self._stamp(
                str(self.resolve_path(ref).relative_to(self.workspace_dir)),
                source=source,
                authority="alternate",
            )
        metadata_ref, _ = self.save_file(
            f"{root}/source.json",
            json.dumps(
                {
                    "source": source,
                    "provenance": provenance,
                    "phase": self.phase,
                    "audience": self.audience,
                    "agent": self.agent_name,
                    "files": refs,
                },
                indent=2,
            )
            + "\n",
        )
        self._stamp(
            str(self.resolve_path(metadata_ref).relative_to(self.workspace_dir)),
            source=source,
            authority="alternate",
        )
        return {"source": source, "files": refs, "metadata": metadata_ref}

    def save_effective_context(self, manifest: dict[str, Any]) -> str:
        """Record the phase-specific logical context as jq-queryable JSON.

        Source selection and workspace snapshot paths are implementation
        details.  They remain in the private context index and audit stamps,
        but must not be copied into the model-facing effective manifest.
        """
        manifest = dict(manifest)
        for key in (
            "source",
            "sources",
            "effective_source",
            "source_reason",
            "source_assumption",
            "provenance",
            "workspace_ref",
            "workspace_refs",
            "alternate_refs",
        ):
            manifest.pop(key, None)
        manifest.update(
            {
                "schema_version": manifest.get("schema_version", 1),
                "phase": self.phase,
                "audience": self.audience,
                "agent": self.agent_name,
            }
        )
        ref, _ = self.save_file(
            "context/effective-context.json", json.dumps(manifest, indent=2) + "\n"
        )
        self._stamp(
            "context/effective-context.json",
            source="workspace",
            authority="effective",
        )
        return ref

    def load_source_snapshot(
        self, source: str, benchmark: str | None = None
    ) -> dict[str, Any] | None:
        """Load one source snapshot without selecting its authority."""
        root_name = f"context/sources/{source}"
        if benchmark:
            root_name += f"/benchmarks/{benchmark.replace('/', '_')}"
        root = self.resolve_path(root_name)
        metadata_path = root / "source.json"
        if not metadata_path.is_file():
            return None
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        files: dict[str, str] = {}
        for path in root.rglob("*"):
            if path.name == "source.json" or not path.is_file():
                continue
            try:
                relative = str(path.relative_to(root))
                if benchmark is None and relative.startswith("benchmarks/"):
                    continue
                files[relative] = path.read_text(encoding="utf-8")
            except OSError:
                continue
        return {
            "source": source,
            "provenance": metadata.get("provenance", {}),
            "files": files,
            "metadata": metadata,
        }

    def read_effective_context(self) -> dict[str, Any] | None:
        path = self.resolve_path("context/effective-context.json")
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _load_context_index(self) -> dict[str, Any]:
        path = self.resolve_path(self.CONTEXT_INDEX)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"schema_version": 1, "documents": []}
        if not isinstance(value, dict) or not isinstance(value.get("documents"), list):
            return {"schema_version": 1, "documents": []}
        return value

    def index_context_documents(self, documents: list[dict[str, Any]]) -> str:
        """Upsert logical context documents backed by workspace snapshots."""
        index = self._load_context_index()
        existing = {
            (item.get("ref"), item.get("source")): item
            for item in index.get("documents", [])
            if isinstance(item, dict)
        }
        for document in documents:
            ref = document.get("ref") or document.get("path")
            workspace_ref = document.get("workspace_ref")
            if not ref or not workspace_ref:
                continue
            item = {key: value for key, value in document.items() if key != "content"}
            item["ref"] = ref
            item["workspace_ref"] = workspace_ref
            item["phase"] = self.phase
            item["audience"] = self.audience
            if item.get("authority") == "effective":
                try:
                    relative = str(
                        self.resolve_path(workspace_ref).relative_to(self.workspace_dir)
                    )
                    self._stamp(relative, source="workspace", authority="effective")
                except (OSError, ValueError, WorkspaceSecurityError):
                    continue
            existing[(ref, item.get("source"))] = item
        payload = {
            "schema_version": 1,
            "phase": self.phase,
            "audience": self.audience,
            "documents": sorted(
                existing.values(),
                key=lambda item: (item.get("ref", ""), item.get("source", "")),
            ),
        }
        ref, _ = self.save_file(
            self.CONTEXT_INDEX, json.dumps(payload, indent=2) + "\n"
        )
        self._stamp(self.CONTEXT_INDEX, source="workspace", authority="effective")
        return ref

    @staticmethod
    def _normalize_document_ref(ref: str) -> str:
        value = ref.strip()
        for prefix in ("context://crucible/", "crucible://"):
            if value.startswith(prefix):
                return value[len(prefix) :]
        return value

    def _context_document(
        self, ref: str, *, include_alternates: bool = False
    ) -> dict[str, Any] | None:
        requested = self._normalize_document_ref(ref)
        candidates = []
        for item in self._load_context_index().get("documents", []):
            if not isinstance(item, dict):
                continue
            aliases = {
                self._normalize_document_ref(str(item.get(key, "")))
                for key in ("ref", "path", "uri", "workspace_ref")
            }
            if requested not in aliases:
                continue
            workspace_ref = item.get("workspace_ref")
            if not workspace_ref:
                continue
            try:
                self._check_visible(workspace_ref, include_alternates)
            except WorkspaceSecurityError:
                continue
            candidates.append(item)
        if not candidates:
            return None
        scoped_effective = [
            item
            for item in candidates
            if item.get("phase") == self.phase
            and item.get("audience") in {self.audience, "shared"}
            and item.get("authority") == "effective"
        ]
        if scoped_effective:
            return scoped_effective[0]
        effective = [
            item for item in candidates if item.get("authority") == "effective"
        ]
        if effective:
            return effective[0]
        scoped = [
            item
            for item in candidates
            if item.get("phase") == self.phase
            and item.get("audience") in {self.audience, "shared"}
        ]
        return (scoped or candidates)[0]

    def context_manifest(self, namespace: str = "") -> dict[str, Any]:
        """Return a source-neutral view of the current phase's context index."""
        documents = []
        for item in self._load_context_index().get("documents", []):
            if not isinstance(item, dict):
                continue
            if item.get("phase") != self.phase or item.get("audience") not in {
                self.audience,
                "shared",
            }:
                continue
            ref = item.get("ref") or item.get("path")
            if not ref or (
                namespace
                and namespace != "all"
                and not str(item.get("namespace", "")).startswith(namespace)
            ):
                continue
            documents.append(
                {
                    "ref": ref,
                    "namespace": item.get("namespace"),
                    "uri": item.get("uri"),
                    "entrypoint": item.get("entrypoint", False),
                    "subject_areas": item.get("subject_areas", []),
                }
            )
        documents.sort(key=lambda item: item["ref"])
        return {
            "schema_version": 1,
            "phase": self.phase,
            "audience": self.audience,
            "namespace": namespace or "all",
            "document_count": len(documents),
            "documents": documents,
        }

    def context_scope_indexed(self, namespace: str = "") -> bool:
        """Return whether the current workspace index covers a logical scope."""
        documents = self._load_context_index().get("documents", [])
        if not namespace or namespace == "all":
            return bool(documents)
        for item in documents:
            if not isinstance(item, dict):
                continue
            if item.get("phase") != self.phase or item.get("audience") not in {
                self.audience,
                "shared",
            }:
                continue
            if (
                namespace
                and namespace != "all"
                and not str(item.get("namespace", "")).startswith(namespace)
            ):
                continue
            workspace_ref = item.get("workspace_ref")
            if not workspace_ref:
                continue
            try:
                self._check_visible(workspace_ref, include_alternates=False)
            except WorkspaceSecurityError:
                continue
            return True
        return False

    def read_document(
        self,
        ref: str,
        *,
        include_alternates: bool = False,
        max_bytes: int = 262144,
    ) -> dict[str, Any]:
        """Read an indexed logical document from the ticket workspace."""
        item = self._context_document(ref, include_alternates=include_alternates)
        if item is None:
            try:
                direct_path = self.resolve_path(ref)
                direct_ref = (
                    f"workspace://{direct_path.relative_to(self.workspace_dir)}"
                )
                self._check_visible(direct_ref, include_alternates)
            except (OSError, ValueError, WorkspaceSecurityError):
                return {"status": "error", "error": "document_not_found", "ref": ref}
            if not direct_path.is_file():
                return {"status": "error", "error": "document_not_found", "ref": ref}
            manifest_entry = (
                self._load_manifest()
                .get("entries", {})
                .get(str(direct_path.relative_to(self.workspace_dir)), {})
            )
            item = {
                "ref": ref,
                "workspace_ref": direct_ref,
                "source": manifest_entry.get("source", "workspace"),
                "authority": manifest_entry.get("authority", "effective"),
                "provenance": manifest_entry,
            }
        workspace_ref = item["workspace_ref"]
        path = self.resolve_path(workspace_ref)
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return {"status": "error", "error": str(exc), "ref": ref}
        encoded = content.encode("utf-8")
        truncated = len(encoded) > max_bytes
        if truncated:
            content = encoded[:max_bytes].decode("utf-8", errors="replace")
        return {
            "status": "ok",
            "ref": item.get("ref"),
            "uri": item.get("uri"),
            "workspace_ref": workspace_ref,
            "source": item.get("source"),
            "authority": item.get("authority"),
            "provenance": item.get("provenance", {}),
            "content": content,
            "size_bytes": len(encoded),
            "truncated": truncated,
        }

    def search_documents(
        self,
        query: str,
        *,
        namespace: str = "",
        include_alternates: bool = False,
        case_insensitive: bool = True,
        max_results: int = 50,
    ) -> dict[str, Any]:
        """Search indexed document paths and contents in the workspace."""
        flags = re.IGNORECASE if case_insensitive else 0
        try:
            pattern = re.compile(query, flags)
        except re.error as exc:
            return {"status": "error", "error": f"invalid_regex: {exc}"}
        results: list[dict[str, Any]] = []
        total_matches = 0
        for item in self._load_context_index().get("documents", []):
            if not isinstance(item, dict):
                continue
            if namespace and not str(item.get("namespace", "")).startswith(namespace):
                continue
            workspace_ref = item.get("workspace_ref")
            if not workspace_ref:
                continue
            try:
                self._check_visible(workspace_ref, include_alternates)
                content = self.resolve_path(workspace_ref).read_text(
                    encoding="utf-8", errors="replace"
                )
            except (OSError, WorkspaceSecurityError):
                continue
            path_matched = bool(pattern.search(str(item.get("ref", ""))))
            line_matches = [
                {"line_number": number, "content": line.rstrip("\r\n")}
                for number, line in enumerate(content.splitlines(), 1)
                if pattern.search(line)
            ]
            if not path_matched and not line_matches:
                continue
            total_matches += 1
            if len(results) >= max_results:
                continue
            results.append(
                {
                    "ref": item.get("ref"),
                    "uri": item.get("uri"),
                    "workspace_ref": workspace_ref,
                    "source": item.get("source"),
                    "authority": item.get("authority"),
                    "provenance": item.get("provenance", {}),
                    "path_match": path_matched,
                    "matches": line_matches[:10],
                    "match_count": len(line_matches),
                }
            )
        return {
            "status": "ok",
            "query": query,
            "namespace": namespace or "all",
            "results": results,
            "total_documents_matched": total_matches,
            "truncated": total_matches > len(results),
        }

    def list_effective_files(self) -> list[dict[str, Any]]:
        """List files while hiding non-effective source alternates from prompts."""
        entries = self.list_files()
        manifest = self.read_effective_context()
        if not manifest:
            return entries
        selected = set(manifest.get("workspace_refs", []))
        return [
            entry
            for entry in entries
            if entry["namespace"] != "context"
            or entry["file_ref"] in selected
            or entry["file_ref"] == "workspace://context/effective-context.json"
        ]

    def list_files(self, include_alternates: bool = False) -> list[dict[str, Any]]:
        """List all files in the ticket workspace with metadata."""
        if not self.workspace_dir.exists():
            return []

        results = []
        for p in sorted(self.workspace_dir.rglob("*")):
            if p.is_file():
                rel_path = str(p.relative_to(self.workspace_dir))
                if rel_path == "metadata/workspace-manifest.json":
                    continue
                if not self._is_visible(f"workspace://{rel_path}", include_alternates):
                    continue
                size_bytes = p.stat().st_size
                ext = p.suffix.lstrip(".").lower()
                results.append(
                    {
                        "filename": rel_path,
                        "file_ref": f"workspace://{rel_path}",
                        "size_bytes": size_bytes,
                        "format": ext or "text",
                        "kind": self.infer_kind(rel_path),
                        "namespace": Path(rel_path).parts[0]
                        if Path(rel_path).parts
                        else "scratch",
                        "source": self._load_manifest()
                        .get("entries", {})
                        .get(rel_path, {})
                        .get("source", "unknown"),
                        "authority": self._load_manifest()
                        .get("entries", {})
                        .get(rel_path, {})
                        .get("authority", "unclassified"),
                        "phase": self._load_manifest()
                        .get("entries", {})
                        .get(rel_path, {})
                        .get("phase"),
                        "audience": self._load_manifest()
                        .get("entries", {})
                        .get(rel_path, {})
                        .get("audience"),
                        "mtime": p.stat().st_mtime,
                    }
                )
        return results

    def jq_query(
        self,
        file_ref: str,
        query: str,
        limit: int = 50,
        max_bytes: int = 16384,
        include_alternates: bool = False,
    ) -> dict[str, Any]:
        """Execute a jq filter against a JSON file in the workspace.

        Args:
            file_ref: workspace:// file reference or relative path
            query: jq filter string (e.g. '.uperf_100 | keys')
            limit: maximum items if result is a list
            max_bytes: maximum byte length of formatted result before truncating
        """
        self._check_visible(file_ref, include_alternates)
        path = self.resolve_path(file_ref)
        if not path.is_file():
            return {
                "status": "error",
                "error": f"File not found: {file_ref}",
            }

        jq_bin = shutil.which("jq")
        if jq_bin:
            try:
                proc = AuditedSubprocessRunner().run_sync(
                    [jq_bin, query, str(path)],
                    timeout=10,
                )
                if proc.returncode != 0:
                    return {
                        "status": "error",
                        "error": f"jq error (exit {proc.returncode}): {proc.stderr.strip()}",
                    }
                raw_out = proc.stdout.decode(errors="replace").strip()
            except subprocess.TimeoutExpired:
                return {
                    "status": "error",
                    "error": "jq query timed out after 10s",
                }
            except Exception as e:
                return {
                    "status": "error",
                    "error": f"Failed to execute jq: {e}",
                }
        else:
            # Fallback: simple python json query if jq CLI is missing
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if query in (".", ""):
                    raw_out = json.dumps(data)
                elif query.startswith("."):
                    key = query.lstrip(".")
                    raw_out = json.dumps(data.get(key, None))
                else:
                    return {
                        "status": "error",
                        "error": "jq executable not found on system and fallback only supports top-level keys",
                    }
            except Exception as e:
                return {
                    "status": "error",
                    "error": f"JSON parsing failed: {e}",
                }

        try:
            parsed = json.loads(raw_out)
            truncated = False
            total_count = None

            if isinstance(parsed, list):
                total_count = len(parsed)
                if len(parsed) > limit:
                    parsed = parsed[:limit]
                    truncated = True
            elif isinstance(parsed, dict) and len(raw_out) > max_bytes:
                truncated = True

            return {
                "status": "ok",
                "file_ref": file_ref,
                "query": query,
                "result": parsed,
                "truncated": truncated,
                "total_items": total_count,
            }
        except json.JSONDecodeError:
            # Could be stream of objects or scalar values
            lines = raw_out.splitlines()
            if len(lines) > limit:
                return {
                    "status": "ok",
                    "file_ref": file_ref,
                    "query": query,
                    "result": "\n".join(lines[:limit]),
                    "truncated": True,
                    "total_items": len(lines),
                }
            return {
                "status": "ok",
                "file_ref": file_ref,
                "query": query,
                "result": raw_out,
                "truncated": False,
                "total_items": len(lines),
            }

    def grep_file(
        self,
        file_ref: str,
        pattern: str,
        max_lines: int = 50,
        context_lines: int = 0,
        case_insensitive: bool = True,
        include_alternates: bool = False,
    ) -> dict[str, Any]:
        """Search a workspace text file for regex or string matches."""
        self._check_visible(file_ref, include_alternates)
        path = self.resolve_path(file_ref)
        if not path.is_file():
            return {
                "status": "error",
                "error": f"File not found: {file_ref}",
            }

        flags = re.IGNORECASE if case_insensitive else 0
        try:
            compiled = re.compile(pattern, flags)
        except re.error as e:
            return {
                "status": "error",
                "error": f"Invalid regex pattern '{pattern}': {e}",
            }

        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError as e:
            return {
                "status": "error",
                "error": f"Failed reading file: {e}",
            }

        matches: list[dict[str, Any]] = []
        matching_indices = [i for i, line in enumerate(lines) if compiled.search(line)]
        total_matches = len(matching_indices)

        emitted_indices = set()
        for idx in matching_indices[:max_lines]:
            start = max(0, idx - context_lines)
            end = min(len(lines), idx + context_lines + 1)
            for i in range(start, end):
                if i not in emitted_indices:
                    emitted_indices.add(i)
                    matches.append(
                        {
                            "line_number": i + 1,
                            "content": lines[i].rstrip("\r\n"),
                            "is_match": i == idx,
                        }
                    )

        matches.sort(key=lambda x: x["line_number"])

        return {
            "status": "ok",
            "file_ref": file_ref,
            "pattern": pattern,
            "total_matches": total_matches,
            "matches_returned": len([m for m in matches if m["is_match"]]),
            "lines": matches,
            "truncated": total_matches > max_lines,
        }

    def read_file_slice(
        self,
        file_ref: str,
        offset_bytes: int = 0,
        max_bytes: int = 4096,
        start_line: int = 1,
        max_lines: int | None = None,
        include_alternates: bool = False,
    ) -> dict[str, Any]:
        """Read a slice of a workspace file by byte offset or line range."""
        self._check_visible(file_ref, include_alternates)
        path = self.resolve_path(file_ref)
        if not path.is_file():
            return {
                "status": "error",
                "error": f"File not found: {file_ref}",
            }

        total_bytes = path.stat().st_size

        if max_lines is not None:
            # Line-oriented reading
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            total_lines = len(lines)
            start_idx = max(0, start_line - 1)
            end_idx = min(total_lines, start_idx + max_lines)
            slice_lines = lines[start_idx:end_idx]
            content = "".join(slice_lines)
            return {
                "status": "ok",
                "file_ref": file_ref,
                "content": content,
                "start_line": start_line,
                "lines_returned": len(slice_lines),
                "total_lines": total_lines,
                "eof": end_idx >= total_lines,
                "next_start_line": end_idx + 1 if end_idx < total_lines else None,
            }

        # Byte-oriented reading
        with open(path, "rb") as f:
            f.seek(offset_bytes)
            data = f.read(max_bytes)

        text = data.decode("utf-8", errors="replace")
        next_offset = offset_bytes + len(data)
        return {
            "status": "ok",
            "file_ref": file_ref,
            "content": text,
            "offset_bytes": offset_bytes,
            "bytes_returned": len(data),
            "total_bytes": total_bytes,
            "eof": next_offset >= total_bytes,
            "next_offset_bytes": next_offset if next_offset < total_bytes else None,
        }

    @staticmethod
    def generate_preview(filename: str, content: str | bytes) -> dict[str, Any]:
        """Generate a compact schema/head preview for spilled tool output (<100 tokens)."""
        raw_bytes = content.encode("utf-8") if isinstance(content, str) else content
        size_bytes = len(raw_bytes)
        ext = Path(filename).suffix.lstrip(".").lower()

        if ext == "json" or (
            isinstance(content, str) and content.strip().startswith(("{", "["))
        ):
            try:
                parsed = json.loads(
                    content if isinstance(content, str) else raw_bytes.decode("utf-8")
                )
                if isinstance(parsed, dict):
                    keys = list(parsed.keys())
                    preview_info: dict[str, Any] = {
                        "format": "json",
                        "type": "object",
                        "size_bytes": size_bytes,
                        "keys": keys[:15],
                        "total_keys": len(keys),
                    }
                    # If single key with nested dict/list, summarize subkeys
                    if len(keys) == 1:
                        sub = parsed[keys[0]]
                        if isinstance(sub, dict):
                            preview_info["subkeys"] = list(sub.keys())[:15]
                        elif isinstance(sub, list):
                            preview_info["array_length"] = len(sub)
                    return preview_info
                elif isinstance(parsed, list):
                    return {
                        "format": "json",
                        "type": "array",
                        "size_bytes": size_bytes,
                        "length": len(parsed),
                        "sample_item_keys": list(parsed[0].keys())[:10]
                        if (parsed and isinstance(parsed[0], dict))
                        else None,
                    }
            except Exception:
                pass

        # Text fallback preview
        text = (
            content
            if isinstance(content, str)
            else raw_bytes[:1024].decode("utf-8", errors="replace")
        )
        lines = text.splitlines()
        return {
            "format": ext or "text",
            "type": "text",
            "size_bytes": size_bytes,
            "total_lines_approx": len(raw_bytes.split(b"\n")),
            "head_preview": lines[:3],
        }

    def generate_chart(
        self,
        file_ref: str,
        title: str = "Performance Metric Chart",
        chart_type: str = "bar",
        harness: str | None = None,
        output_name: str | None = None,
        x_field: str | None = None,
        y_field: str | None = None,
        group_by: str | None = None,
        metric: str | None = None,
        metrics: list[str] | None = None,
        breakout: str | None = None,
        unit: str | None = None,
        max_points: int = 60,
        jq_filter: str | None = None,
    ) -> dict[str, Any]:
        """Generate a declarative Chart.js/Recharts specification from a workspace file and save it to workspace://charts/.

        Returns a dictionary containing the chart spec, file_ref, and preview metadata.
        """
        path = self.resolve_path(file_ref)
        if not path.exists():
            return {
                "status": "error",
                "error": f"File '{file_ref}' does not exist in workspace",
            }

        raw_text = path.read_text(encoding="utf-8", errors="replace")
        data: Any = None
        if path.suffix.lower() == ".json":
            try:
                if jq_filter and shutil.which("jq"):
                    proc = AuditedSubprocessRunner().run_sync(
                        ["jq", "-c", jq_filter],
                        stdin=raw_text.encode("utf-8"),
                        timeout=5,
                    )
                    if proc.returncode == 0:
                        data = json.loads(proc.stdout.decode("utf-8"))
                    else:
                        data = json.loads(raw_text)
                else:
                    data = json.loads(raw_text)
            except Exception as e:
                logger.warning(
                    f"Failed to parse JSON for chart generation from {file_ref}: {e}"
                )
                data = raw_text
        elif path.suffix.lower() == ".csv":
            data = raw_text
        else:
            try:
                data = json.loads(raw_text)
            except Exception:
                data = raw_text

        from providers.workspace.charts import get_chart_registry

        registry = get_chart_registry()
        spec = registry.generate_chart_spec(
            data,
            harness=harness,
            title=title,
            chart_type=chart_type,
            x_field=x_field,
            y_field=y_field,
            group_by=group_by,
            metric=metric,
            metrics=metrics,
            breakout=breakout,
            unit=unit,
            max_points=max_points,
            source_file=file_ref,
        )

        if not output_name:
            safe_title = re.sub(r"[^a-zA-Z0-9_]+", "_", title.lower()).strip("_")
            output_name = f"charts/{safe_title or 'chart'}.json"
        elif not output_name.endswith(".json"):
            output_name = f"{output_name}.json"
        if not output_name.startswith("charts/"):
            output_name = f"charts/{output_name}"

        spec_dict = spec.to_dict()
        chart_ref, _ = self.save_file(output_name, json.dumps(spec_dict, indent=2))

        return {
            "status": "ok",
            "chart_ref": chart_ref,
            "chart_data": spec_dict,
            "summary": f"Generated {spec.type} chart '{spec.title}' with {len(spec.labels)} labels and {len(spec.datasets)} datasets.",
        }


from providers.execution import AuditedSubprocessRunner
