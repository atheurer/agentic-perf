"""Model-facing skill retrieval over server-owned subject and source bindings."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import posixpath
import re
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from providers.skills.gateway import (
    OrganizationSkillResolver,
    SkillGatewayError,
    is_organization_ref,
)
from providers.skills.local_context import LocalContextSnapshot, LocalContextSource

_SOFTWARE_PREFIX = "skill://software/controller/harness/crucible/"
_PROJECT_PREFIX = "skill://project/"
_LOCAL_PREFIX = "skill://local/"
_UPSTREAM_PREFIX = "skill://upstream/"
_CONFIG_PREFIX = "skill://configuration/"
_LOCAL_SUBJECT_DIRS = {
    "harness/kube-burner": "kube-burner",
    "harness/k8s-netperf": "k8s-netperf",
    "resource/aws": "resource/aws",
}
_UPSTREAM_REPOS = {
    "harness/kube-burner": "kube-burner",
    "harness/k8s-netperf": "k8s-netperf",
}
_MAX_SOURCE_DOCUMENT_BYTES = 1024 * 1024
_MAX_UPSTREAM_DOCUMENTS = 256
_MAX_UPSTREAM_PACKAGE_BYTES = 32 * 1024 * 1024
_MAX_SOURCE_SEARCH_BYTES = 4 * 1024 * 1024
_MAX_LOCAL_DIRECTORY_ALIASES = 256
_MAX_LOCAL_TREE_ENTRIES = 8192
SKILL_GATEWAY_TOOL_DESCRIPTION = """Retrieve subject guidance and software references.

Bootstrap returns applicable bundled project and organization entrypoints,
phase-compatible software entrypoints, and approved configuration-view refs.
Project entries come from the explicit Crucible manifest and are filtered by
the server-owned phase and agent. Read a returned file ref; use
operation='list' with a directory ref to see its immediate children. List results
include typed, reusable refs and do not read directory contents. Organization
directories are not exposed; listing that source reports an unsupported
capability. Relative reads and lists use from_ref plus path. The server checks
whether from_ref is a file or directory: file origins use their parent directory,
and directory origins use that directory.

Search without from_ref searches every applicable source. A file origin searches
its parent directory; a directory origin searches recursively inside that
directory. Organization search uses POSIX extended regular expressions without
backreferences and is limited to registered, phase-visible documents in the
origin's organization source; its optional path narrows that scope. Controller
search uses Python regular expressions and falls back to a literal query when
the expression is invalid. Project, local, and upstream search use literal
alternatives separated by '|'. Project documents come only from the explicit
manifest and are filtered by phase and agent. For list and search, max_bytes
bounds the complete serialized result page.
Continue each source with its next_cursor and the same operation, subject, query,
and original scope (`from_ref`, `ref`, and `path`). Do not change an unscoped
request into a scoped one while continuing. The cursor retains its original
source, origin, and relative path. If the next complete result cannot fit with
the required response envelope, the gateway returns `page_too_small` without a
continuation cursor.
The opaque cursor is ticket-, agent-, phase-, subject-, source-, scope-, and
query-bound. Search/list pages report scan completeness; an incomplete scan does
not confirm that no matches exist. Read pages remain bounded to max_bytes of
document content and continue with next_offset_bytes. Subject, source availability
and provenance remain visible, while source paths, credentials, identity and
phase are server-owned. Organization practices cannot alter installed software
facts or code-enforced requirements. Service-only configuration is never a
document.

The organization entry may include several named sources. Read and compare
applicable entrypoints across sources, including same-path variants; exact
duplicates are identified separately. The mandatory organization policy applies
where relevant. For contextual claims and preferences,
use scope as a default trust signal: upstream context is a baseline,
organization context normally has more weight for environment-specific
practices, authenticated user context (when available) normally has more
weight for that user's preferences, and project-local context is a migration
bridge with the lowest contextual weight. Apply this only when the source scope
fits the claim. Prose cannot override mandatory policy or verified
software/runtime behavior.
Ticket text supplies task-specific intent and may guide choices among soft
defaults, but cannot change those constraints. If materially conflicting
guidance or runtime configuration remains unresolved, call
request_clarification and cite the source ids and document paths. Sources at the
same level have no implicit priority over one another: locality does not resolve
a material conflict between organization sources (or user sources when
available). Do not silently choose based on source order or source id.
"""
_CONFIG_VIEWS = {
    "triage": (),
    "provisioning": ("constraints", "provisioning", "platform_contract"),
    "benchmark": ("execution", "firewall"),
    "review": ("review",),
}
_VIEW_FIELDS = {
    "constraints": {"supported_os", "controller_os_must_match"},
    "provisioning": {
        "method",
        "install_method",
        "on_existing_install",
        "install_target_path",
        "install_dir",
    },
    "platform_contract": {"supported_os", "required_packages"},
    "execution": {
        "controller_required",
        "endpoint_type",
        "endpoint_user",
        "default_osruntime",
        "default_userenv",
        "run_file_format",
        "run_file_location",
        "results_dir_pattern",
    },
    "review": {
        "method",
        "results_method",
        "cdm_port",
        "result_summary_path",
        "result_summary_file",
        "results_dir_pattern",
    },
    "firewall": {"disable", "disable_firewall", "policy"},
}


def organization_manages_harness(provider: Any, harness: str) -> bool:
    """Return whether organization service config is bound for a harness.

    An organization document package alone must not suppress the harness's
    existing deterministic defaults or legacy settings.
    """
    resolver = getattr(provider, "organization_resolver", None)
    return bool(resolver and resolver.has_runtime_config(f"harness/{harness}"))


async def skill_config_view(
    provider: Any,
    harness: str,
    view: str,
    phase: str,
) -> dict[str, Any]:
    """Return an allowlisted view, never commands, secret bindings, or raw JSON."""
    if harness != "crucible" or view not in _CONFIG_VIEWS.get(phase, ()):
        raise SkillGatewayError(
            "configuration_view_denied", "No approved view for this subject and phase"
        )
    config = await provider.get_all_private_config(harness)
    section = config.get(view)
    if not isinstance(section, dict):
        return {}
    result = {k: v for k, v in section.items() if k in _VIEW_FIELDS[view]}
    if view == "provisioning":
        options = section.get("options_on_existing", [])
        if isinstance(options, list):
            result["options_on_existing"] = [
                {"action": item["action"]}
                for item in options
                if isinstance(item, dict) and isinstance(item.get("action"), str)
            ]
    if view == "execution" and isinstance(section.get("kube"), dict):
        result["kube"] = {
            k: v
            for k, v in section["kube"].items()
            if k
            in {
                "min_root_volume_gb",
                "self_ssh_required",
                "selinux",
                "tool_params_required",
            }
        }
    return result


def _software_ref(path: str) -> str:
    return _SOFTWARE_PREFIX + quote(path, safe="/")


def _software_path(ref: str) -> str:
    if not ref.startswith(_SOFTWARE_PREFIX):
        raise SkillGatewayError("invalid_ref", "Use a returned software document ref")
    relative = unquote(ref.removeprefix(_SOFTWARE_PREFIX))
    from agents.server_utils import _controller_relative_path

    checked = _controller_relative_path(relative)
    if not checked:
        raise SkillGatewayError("invalid_document", "Document escapes software source")
    return checked


def _software_document(document: dict[str, Any]) -> dict[str, Any]:
    path = str(
        document.get("source_path") or document.get("path") or document.get("ref") or ""
    )
    return {
        **document,
        "ref": _software_ref(path),
        "uri": _software_ref(path),
        "source": "controller",
        "scope": "software",
        "role": "software-reference",
    }


def _project_ref(path: str) -> str:
    return _PROJECT_PREFIX + quote(path, safe="/")


def _project_document(document: dict[str, Any]) -> dict[str, Any]:
    """Expose manifest-selected project guidance with a safe provenance view."""
    source_path = str(document.get("source_path", ""))
    provenance = document.get("provenance", {})
    provenance = provenance if isinstance(provenance, dict) else {}
    safe_provenance = {
        "source_id": "agentic-perf",
        "revision": provenance.get("revision"),
        "entry_id": provenance.get("entry_id"),
        "path": source_path,
        "harness": document.get("harness", "crucible"),
        "benchmark": document.get("benchmark"),
        "phase": provenance.get("phase"),
        "agent": provenance.get("agent"),
    }
    if provenance.get("reason"):
        safe_provenance["reason"] = provenance["reason"]
    if isinstance(provenance.get("captured_at"), str):
        safe_provenance["captured_at"] = provenance["captured_at"]
    subjects = document.get("subject_area")
    return {
        "ref": _project_ref(source_path),
        "uri": _project_ref(source_path),
        "path": source_path,
        "source_path": source_path,
        "source": "agentic-perf",
        "source_id": "agentic-perf",
        "scope": "project",
        "role": "project-guidance",
        "authority": "supplemental",
        "revision": provenance.get("revision"),
        "provenance": safe_provenance,
        "benchmark": document.get("benchmark"),
        "subject_areas": LocalContextSource._values(subjects),
        "entrypoint": bool(document.get("entrypoint", True)),
        "size_bytes": document.get("size_bytes"),
    }


def _ticket_source_epoch(provider: Any, ticket_id: str) -> Any:
    epoch = getattr(provider, "source_epoch", None)
    if epoch is None:
        from paths import SKILL_SOURCE_EPOCH_DIR
        from providers.skills.source_epoch import SourceEpoch

        epoch = SourceEpoch(SKILL_SOURCE_EPOCH_DIR, ticket_id=ticket_id)
    if getattr(epoch, "ticket_id", None) != ticket_id:
        raise SkillGatewayError(
            "source_binding_changed", "Ticket source binding does not match"
        )
    return epoch


async def _project_documents(
    provider: Any,
    *,
    ticket_id: str,
    subject: str,
    phase: str,
    agent_name: str,
    benchmark: str | None,
) -> tuple[Any | None, list[dict[str, Any]]]:
    if subject != "harness/crucible":
        return None, []
    source = getattr(provider, "project_context_source", None)
    if source is None:
        return None, []
    epoch = _ticket_source_epoch(provider, ticket_id)
    try:
        snapshot_data = await asyncio.to_thread(
            epoch.get_or_capture,
            "local:agentic-perf-manifest",
            source.binding_identity,
            lambda: source.capture_snapshot().to_dict(),
        )
        source = LocalContextSnapshot.from_dict(snapshot_data)
    except (OSError, ValueError, TypeError) as exc:
        raise SkillGatewayError(
            "source_snapshot_unavailable", "Project context snapshot is unavailable"
        ) from exc
    documents = source.list_documents(
        harness="crucible",
        benchmark=benchmark,
        phase=phase,
        agent=agent_name,
        subject_area="all",
    )
    exposed = [_project_document(item) for item in documents]
    exposed.sort(key=lambda item: item["path"])
    return source, exposed


def _project_read_target(
    documents: list[dict[str, Any]],
    *,
    ref: str,
    path: str,
    from_ref: str,
    allow_directory_origin: bool = False,
    allow_directory_target: bool = False,
) -> dict[str, Any]:
    by_ref = {item["ref"]: item for item in documents}
    origin = None
    if from_ref:
        if ref or from_ref not in by_ref:
            if ref or not allow_directory_origin:
                raise SkillGatewayError(
                    "origin_required", "Origin must be an exported project ref"
                )
            origin = _project_directory_target(documents, from_ref)
        else:
            origin = by_ref[from_ref]
    if path:
        if origin is None:
            raise SkillGatewayError(
                "origin_required", "Pointer requires a returned project document ref"
            )
        if path.startswith(("/", "\\")) or "://" in path or "\\" in path:
            raise SkillGatewayError("invalid_document", "Invalid relative pointer")
        origin_path = origin["source_path"]
        base_path = (
            origin_path
            if origin.get("is_directory")
            else posixpath.dirname(origin_path)
        )
        target = posixpath.normpath(posixpath.join(base_path, path))
        if target == ".." or target.startswith("../"):
            raise SkillGatewayError(
                "invalid_document", "Pointer escapes the project source"
            )
        target = "" if target == "." else target
        candidate = next(
            (item for item in documents if item["source_path"] == target), None
        )
        if candidate is None:
            try:
                candidate = _project_directory_target(
                    documents, _project_ref(target or ".")
                )
            except SkillGatewayError:
                candidate = None
            if candidate is not None and not allow_directory_target:
                raise SkillGatewayError(
                    "source_path_is_directory",
                    "The project pointer is a directory; use operation='list'.",
                )
    else:
        if origin is not None:
            candidate = origin
        else:
            candidate = by_ref.get(ref)
            if candidate is None:
                try:
                    candidate = _project_directory_target(documents, ref)
                except SkillGatewayError:
                    candidate = None
                if candidate is not None and not allow_directory_target:
                    raise SkillGatewayError(
                        "source_path_is_directory",
                        "The project ref is a directory; use operation='list'.",
                    )
        if (
            candidate is not None
            and candidate.get("is_directory")
            and not allow_directory_target
        ):
            raise SkillGatewayError(
                "source_path_is_directory",
                "The project origin is a directory; use operation='list'.",
            )
    if candidate is None:
        raise SkillGatewayError("invalid_ref", "Use a returned project document ref")
    return candidate


def _project_directory_target(
    documents: list[dict[str, Any]], ref: str
) -> dict[str, Any]:
    if not isinstance(ref, str) or not ref.startswith(_PROJECT_PREFIX):
        raise SkillGatewayError("invalid_ref", "Use an exported project directory ref")
    encoded_path = ref[len(_PROJECT_PREFIX) :]
    source_path = unquote(encoded_path)
    if _project_ref(source_path) != ref:
        raise SkillGatewayError("invalid_ref", "Use a canonical project directory ref")
    if source_path in {"", "."}:
        directory = ""
    elif (
        source_path.startswith(("/", "\\"))
        or "\\" in source_path
        or posixpath.normpath(source_path) != source_path
        or source_path.startswith("../")
        or source_path == ".."
    ):
        raise SkillGatewayError("invalid_ref", "Invalid project directory ref")
    else:
        directory = source_path
    prefix = f"{directory.rstrip('/')}/" if directory else ""
    if not any(
        item["source_path"].startswith(prefix) and item["source_path"] != directory
        for item in documents
    ):
        raise SkillGatewayError(
            "invalid_ref", "Project directory has no exported files"
        )
    return {
        "ref": ref,
        "source_path": directory,
        "is_directory": True,
    }


def _project_scope_path(target: dict[str, Any]) -> str:
    if target.get("is_directory"):
        return target["source_path"]
    return posixpath.dirname(target["source_path"])


def _project_list_entries(
    documents: list[dict[str, Any]], scope_path: str
) -> list[dict[str, Any]]:
    """Build immediate project children using only exported manifest documents."""
    prefix = f"{scope_path.rstrip('/')}/" if scope_path else ""
    entries: dict[str, dict[str, Any]] = {}
    for document in documents:
        source_path = document["source_path"]
        if prefix:
            if not source_path.startswith(prefix):
                continue
            remainder = source_path[len(prefix) :]
        else:
            remainder = source_path
        if not remainder:
            continue
        child_name, separator, _tail = remainder.partition("/")
        if separator:
            child_path = (
                posixpath.join(scope_path, child_name) if scope_path else child_name
            )
            if child_path not in entries:
                child_ref = _project_ref(child_path)
                entries[child_path] = {
                    "ref": child_ref,
                    "uri": child_ref,
                    "path": child_path,
                    "source": "agentic-perf",
                    "scope": "project",
                    "type": "directory",
                    "size_bytes": None,
                    "provenance_status": "project-content-revision",
                }
        elif posixpath.dirname(source_path) == scope_path:
            entries[source_path] = {
                "ref": document["ref"],
                "uri": document["uri"],
                "path": source_path,
                "source": "agentic-perf",
                "scope": "project",
                "type": "file",
                "size_bytes": document.get("size_bytes"),
                "provenance_status": "project-content-revision",
            }
    return [entries[path] for path in sorted(entries)]


def _project_search_documents(
    source: Any,
    documents: list[dict[str, Any]],
    query: str,
    *,
    scope_path: str = "",
) -> dict[str, Any]:
    alternatives = [
        part.strip().casefold() for part in query.split("|") if part.strip()
    ]
    if not alternatives or len(query) > 256:
        raise SkillGatewayError(
            "invalid_query", "Search query must contain 1 through 256 characters"
        )
    results = []
    scanned_bytes = 0
    complete = True
    for document in documents:
        relative = document["source_path"]
        if scope_path and not (
            relative == scope_path or relative.startswith(scope_path.rstrip("/") + "/")
        ):
            continue
        content = source.read(relative)
        if content is None:
            complete = False
            continue
        scanned_bytes += len(content.encode("utf-8"))
        if scanned_bytes > _MAX_SOURCE_SEARCH_BYTES:
            complete = False
            break
        folded = content.casefold()
        if alternatives and any(term in folded for term in alternatives):
            line_number = next(
                (
                    number
                    for number, line in enumerate(content.splitlines(), 1)
                    if any(term in line.casefold() for term in alternatives)
                ),
                1,
            )
            line = content.splitlines()[line_number - 1]
            results.append(
                {
                    "ref": document["ref"],
                    "source_id": "agentic-perf",
                    "path": relative,
                    "line": line_number,
                    "snippet": line[:512],
                }
            )
            if len(results) >= 500:
                complete = False
                break
    return {
        "results": results,
        "complete": complete,
        "scanned_bytes": scanned_bytes,
        "query_mode": "literal-alternatives",
        "scope_path": scope_path or ".",
        "scope_semantics": "manifest documents within the selected subtree",
        "source": "agentic-perf",
        "provenance_status": "project-content-revision",
    }


def _config_ref(revision: str, subject: str, view: str) -> str:
    return f"{_CONFIG_PREFIX}{revision}/{subject}/{view}.json"


def _source_ref(
    kind: str, subject: str, path: str, *, revision: str | None = None
) -> str:
    prefix = _LOCAL_PREFIX if kind == "local" else _UPSTREAM_PREFIX
    ref = prefix + quote(subject, safe="/") + "/" + quote(path, safe="/")
    if kind == "upstream":
        if not isinstance(revision, str) or not re.fullmatch(
            r"[0-9a-f]{40,64}", revision
        ):
            raise SkillGatewayError(
                "invalid_ref", "Upstream refs require a pinned revision"
            )
        ref += f"?revision={revision}"
    return ref


def _source_path_excluded(path: str) -> bool:
    return any(
        part.lower()
        in {
            ".ssh",
            ".env",
            "secrets",
            "credential",
            "credentials",
            "authorized_keys",
            "id_rsa",
            "id_ed25519",
            "known_hosts",
            "private_key",
        }
        or part.lower().startswith(".git")
        or part.lower().endswith((".pem", ".key"))
        or "token" in part.lower()
        for part in Path(path).parts
    )


def _is_exposed_upstream_path(relative: str) -> bool:
    normalized = posixpath.normpath(relative)
    return any(
        normalized == directory or normalized.startswith(f"{directory}/")
        for directory in ("docs", "config")
    )


def _source_path_type(root: Path, relative: str, *, kind: str) -> tuple[Path, str]:
    if kind == "upstream" and not _is_exposed_upstream_path(relative):
        raise SkillGatewayError(
            "invalid_document", "Upstream refs must stay inside exposed docs or config"
        )
    if _source_path_excluded(relative):
        raise SkillGatewayError("invalid_document", "Source path is excluded")
    try:
        target = (root / relative).resolve(strict=True)
    except PermissionError:
        raise SkillGatewayError(
            "source_path_inaccessible", "The requested source path cannot be read"
        ) from None
    except FileNotFoundError:
        raise SkillGatewayError(
            "source_path_not_found", "The requested source path does not exist"
        ) from None
    except (OSError, RuntimeError):
        raise SkillGatewayError(
            "source_path_inaccessible", "The requested source path cannot be resolved"
        ) from None
    if not target.is_relative_to(root):
        raise SkillGatewayError(
            "source_path_outside_root", "The resolved source path leaves its source"
        )
    if _source_path_excluded(target.relative_to(root).as_posix()):
        raise SkillGatewayError(
            "invalid_document", "The resolved source path is excluded"
        )
    if kind == "upstream" and not _is_exposed_upstream_path(
        target.relative_to(root).as_posix()
    ):
        raise SkillGatewayError(
            "invalid_document",
            "Resolved upstream refs must stay inside exposed docs or config",
        )
    if target.is_dir():
        return target, "directory"
    if target.is_file():
        return target, "file"
    raise SkillGatewayError(
        "invalid_document", "The source path is not a file or directory"
    )


def _parse_source_ref(ref: str, kind: str) -> tuple[str, str, str | None]:
    prefix = _LOCAL_PREFIX if kind == "local" else _UPSTREAM_PREFIX
    parsed = urlsplit(ref)
    if (
        not ref.startswith(prefix)
        or parsed.scheme != "skill"
        or parsed.netloc != kind
        or parsed.fragment
    ):
        raise SkillGatewayError("invalid_ref", "Use a returned source document ref")
    revision = None
    if kind == "local":
        if parsed.query:
            raise SkillGatewayError("invalid_ref", "Use a returned local document ref")
    else:
        query = parsed.query.split("=")
        if (
            len(query) != 2
            or query[0] != "revision"
            or not re.fullmatch(r"[0-9a-f]{40,64}", query[1])
        ):
            raise SkillGatewayError(
                "invalid_ref", "Upstream refs require a pinned revision"
            )
        revision = query[1]
    parts = unquote(parsed.path).lstrip("/").split("/", 2)
    if len(parts) != 3:
        raise SkillGatewayError("invalid_ref", "Use a returned source document ref")
    subject = "/".join(parts[:2])
    path = parts[2]
    if subject not in _LOCAL_SUBJECT_DIRS or not path:
        raise SkillGatewayError("invalid_ref", "Use a returned source document ref")
    if path.startswith(("/", "\\")) or "\\" in path or ":" in path:
        raise SkillGatewayError("invalid_document", "Invalid document path")
    normalized = posixpath.normpath(path)
    if normalized in {"", ".", ".."} or normalized.startswith("../"):
        raise SkillGatewayError("invalid_document", "Invalid document path")
    return subject, normalized, revision


def _local_base(local_skills_dir: Path | None, subject: str) -> Path | None:
    if local_skills_dir is None or subject not in _LOCAL_SUBJECT_DIRS:
        return None
    root = Path(local_skills_dir).resolve()
    base = (root / _LOCAL_SUBJECT_DIRS[subject]).resolve()
    if not base.is_relative_to(root) or not base.is_dir():
        return None
    return base


class _TicketRepoView:
    """Expose one verified ticket-owned checkout through RepoCache's read API."""

    def __init__(
        self,
        name: str,
        root: Path,
        origin: str,
        revision: str,
        captured_at: str | None = None,
    ) -> None:
        self._name = name
        self._root = root
        self._origin = origin
        self._revision = revision
        self.captured_at = captured_at

    def get_path(self, name: str) -> Path | None:
        return self._root if name == self._name else None

    def get_origin(self, name: str) -> str | None:
        return self._origin if name == self._name else None

    def get_revision(self, name: str) -> str | None:
        return self._revision if name == self._name else None

    def list_docs(
        self,
        name: str,
        subdirs: list[str] | str = "docs",
        extensions: list[str] | None = None,
    ) -> list[dict[str, str | int]]:
        if name != self._name:
            return []
        roots = [subdirs] if isinstance(subdirs, str) else subdirs
        allowed = set(extensions or [".md", ".yml", ".yaml"])
        results = []
        package_bytes = 0
        for subdir in roots:
            directory = (self._root / subdir).resolve()
            try:
                directory.relative_to(self._root.resolve())
            except (OSError, ValueError):
                continue
            if not directory.is_dir():
                continue
            for target in sorted(directory.rglob("*")):
                if not target.is_file() or target.suffix.lower() not in allowed:
                    continue
                try:
                    resolved = target.resolve(strict=True)
                    resolved.relative_to(self._root.resolve())
                    relative = resolved.relative_to(self._root.resolve()).as_posix()
                except (OSError, ValueError, RuntimeError):
                    continue
                if _source_path_excluded(relative):
                    continue
                size_bytes = resolved.stat().st_size
                if size_bytes > _MAX_SOURCE_DOCUMENT_BYTES:
                    raise SkillGatewayError(
                        "source_too_large", "Upstream document exceeds the limit"
                    )
                package_bytes += size_bytes
                if (
                    len(results) >= _MAX_UPSTREAM_DOCUMENTS
                    or package_bytes > _MAX_UPSTREAM_PACKAGE_BYTES
                ):
                    raise SkillGatewayError(
                        "source_too_large", "Upstream context package exceeds its limit"
                    )
                results.append({"path": relative, "size_bytes": size_bytes})
        return sorted(results, key=lambda item: str(item["path"]))


def _capture_local_directory_aliases(base: Path) -> dict[str, str]:
    """Capture bounded, safe directory symlinks without following them."""
    aliases: dict[str, str] = {}
    scanned_entries = 0
    for current, directory_names, file_names in os.walk(
        base, topdown=True, followlinks=False
    ):
        scanned_entries += len(directory_names) + len(file_names)
        if scanned_entries > _MAX_LOCAL_TREE_ENTRIES:
            raise SkillGatewayError(
                "source_too_large", "Local directory inventory exceeds its limit"
            )
        current_path = Path(current)
        for name in tuple(directory_names):
            candidate = current_path / name
            if not candidate.is_symlink():
                continue
            alias = candidate.relative_to(base).as_posix()
            if _source_path_excluded(alias):
                continue
            try:
                link_target = Path(os.readlink(candidate))
                lexical_target = (
                    link_target
                    if link_target.is_absolute()
                    else candidate.parent / link_target
                )
                lexical_target = Path(os.path.abspath(lexical_target))
                lexical_target.relative_to(base)
                resolved = candidate.resolve(strict=True)
                if not resolved.is_dir() or resolved != lexical_target.resolve(
                    strict=True
                ):
                    continue
                canonical = resolved.relative_to(base).as_posix()
                if canonical == ".":
                    canonical = ""
                if _source_path_excluded(canonical):
                    continue
                target_component = base
                has_symlink_chain = False
                for part in Path(canonical).parts:
                    target_component = target_component / part
                    if target_component.is_symlink():
                        has_symlink_chain = True
                        break
                if has_symlink_chain:
                    continue
            except (OSError, ValueError, RuntimeError):
                continue
            aliases[alias] = canonical
            if len(aliases) > _MAX_LOCAL_DIRECTORY_ALIASES:
                raise SkillGatewayError(
                    "source_too_large", "Local directory aliases exceed their limit"
                )
    return dict(sorted(aliases.items()))


def _capture_local_tree(local_skills_dir: Path | None, subject: str) -> dict[str, Any]:
    base = _local_base(local_skills_dir, subject)
    if base is None:
        return {
            "available": False,
            "revision": hashlib.sha256(b"missing").hexdigest(),
            "documents": [],
        }
    directory_aliases = _capture_local_directory_aliases(base)
    listed = _list_local_documents(local_skills_dir, subject)
    documents = []
    total = 0
    captured: dict[str, bytes] = {}
    for item in listed:
        relative = str(item["path"])
        target, target_type = _source_path_type(base, relative, kind="local")
        if target_type != "file":
            continue
        content_bytes = target.read_bytes()
        if len(content_bytes) > _MAX_SOURCE_DOCUMENT_BYTES or b"\x00" in content_bytes:
            raise SkillGatewayError(
                "source_too_large", "Local document exceeds its limit"
            )
        total += len(content_bytes)
        if total > 16 * _MAX_SOURCE_DOCUMENT_BYTES:
            raise SkillGatewayError(
                "source_too_large", "Local context package exceeds its limit"
            )
        try:
            content = content_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SkillGatewayError(
                "source_unavailable", "Local document is not UTF-8"
            ) from exc
        captured[relative] = content_bytes
        documents.append(
            {
                "path": relative,
                "content": content,
                "size_bytes": len(content_bytes),
                "digest": hashlib.sha256(content_bytes).hexdigest(),
            }
        )
    latest = _list_local_documents(local_skills_dir, subject)
    if [item["path"] for item in latest] != [item["path"] for item in listed]:
        raise SkillGatewayError(
            "source_changed_during_capture", "Local source changed during capture"
        )
    if _capture_local_directory_aliases(base) != directory_aliases:
        raise SkillGatewayError(
            "source_changed_during_capture", "Local directory aliases changed"
        )
    for relative, content in captured.items():
        target, target_type = _source_path_type(base, relative, kind="local")
        if target_type != "file" or target.read_bytes() != content:
            raise SkillGatewayError(
                "source_changed_during_capture", "Local source changed during capture"
            )
    digest = hashlib.sha256()
    digest.update(b"local-tree-v2\0documents\0")
    for item in documents:
        digest.update(item["path"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(captured[item["path"]])
        digest.update(b"\0")
    digest.update(b"directory-aliases\0")
    for alias, canonical in directory_aliases.items():
        digest.update(alias.encode("utf-8"))
        digest.update(b"\0")
        digest.update(canonical.encode("utf-8"))
        digest.update(b"\0")
    return {
        "available": True,
        "revision": digest.hexdigest(),
        "documents": documents,
        "directory_aliases": directory_aliases,
    }


async def _ticket_source_snapshot(
    provider: Any,
    *,
    ticket_id: str,
    subject: str,
    kind: str,
    local_skills_dir: Path | None,
    repo_cache: Any,
) -> tuple[dict[str, Any] | None, Any | None]:
    """Load one immutable ticket pin; upstream never falls back to shared cache files."""
    if subject not in _LOCAL_SUBJECT_DIRS or kind not in {"local", "upstream"}:
        return None, None
    epoch = _ticket_source_epoch(provider, ticket_id)

    if kind == "local":
        root = Path(local_skills_dir).resolve() if local_skills_dir else Path()
        identity = f"local-tree:{subject}:{root}"
        try:
            snapshot = await asyncio.to_thread(
                epoch.get_or_capture,
                f"local-tree:{subject}",
                identity,
                lambda: _capture_local_tree(local_skills_dir, subject),
            )
        except (OSError, ValueError, TypeError) as exc:
            raise SkillGatewayError(
                "source_snapshot_unavailable", "Local context snapshot is unavailable"
            ) from exc
        result = (snapshot, None)
    else:
        repo_name = _UPSTREAM_REPOS.get(subject)
        try:
            source_id = f"upstream:{repo_name}"
            snapshot = await asyncio.to_thread(epoch.read_pinned, source_id)
            if snapshot is None:
                get_origin = getattr(repo_cache, "get_origin", None)
                origin = (
                    get_origin(repo_name)
                    if repo_name and callable(get_origin)
                    else None
                )
                if not isinstance(origin, str) or not origin:
                    raise SkillGatewayError(
                        "source_unavailable",
                        "No configured upstream origin is available",
                    )
                ref = "HEAD"
            else:
                origin = snapshot.get("repository")
                ref = snapshot.get("ref")
                if not isinstance(origin, str) or not isinstance(ref, str):
                    raise SkillGatewayError(
                        "invalid_snapshot", "Pinned upstream source metadata is invalid"
                    )
            identity = f"{origin}\0{ref}"
            # Revalidate the durable record and its ticket-owned checkout on
            # every list, read, or search request. In particular, do not let a
            # provider-local cache outlive closed-ticket cleanup.
            snapshot = await asyncio.to_thread(
                epoch.capture_git_repository,
                source_id,
                identity,
                origin,
                ref=ref,
            )
            checkout = (epoch.snapshot_root / snapshot["checkout_path"]).resolve(
                strict=True
            )
        except (OSError, KeyError, ValueError, TypeError) as exc:
            raise SkillGatewayError(
                "source_snapshot_unavailable", "Pinned upstream context is unavailable"
            ) from exc
        result = (
            snapshot,
            _TicketRepoView(
                repo_name,
                checkout,
                origin,
                str(snapshot["revision"]),
                snapshot.get("captured_at"),
            ),
        )
    return result


def _list_local_documents(
    local_skills_dir: Path | None,
    subject: str,
    *,
    snapshot: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if snapshot is not None:
        revision = snapshot.get("revision")
        documents = snapshot.get("documents")
        if not isinstance(revision, str) or not isinstance(documents, list):
            return []
        results = []
        for item in documents:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                continue
            relative = item["path"]
            ref = _source_ref("local", subject, relative)
            results.append(
                {
                    "ref": ref,
                    "uri": ref,
                    "path": relative,
                    "source": "agentic-perf",
                    "scope": "local",
                    "role": "operational-guidance",
                    "revision": revision,
                    "captured_at": snapshot.get("captured_at"),
                    "size_bytes": item.get("size_bytes"),
                }
            )
        return results
    base = _local_base(local_skills_dir, subject)
    if base is None:
        return []
    results: list[dict[str, Any]] = []
    for target in sorted(base.rglob("*.md")):
        try:
            resolved = target.resolve(strict=True)
            relative = target.relative_to(base).as_posix()
            if (
                _source_path_excluded(relative)
                or not resolved.is_relative_to(base)
                or _source_path_excluded(resolved.relative_to(base).as_posix())
            ):
                continue
            if not resolved.is_file():
                continue
            results.append(
                {
                    "ref": _source_ref("local", subject, relative),
                    "uri": _source_ref("local", subject, relative),
                    "path": relative,
                    "source": "agentic-perf",
                    "scope": "local",
                    "role": "operational-guidance",
                    "size_bytes": resolved.stat().st_size,
                }
            )
        except (OSError, ValueError, RuntimeError):
            continue
        if len(results) >= 128:
            break
    return results


def _list_source_directory(
    kind: str,
    root: Path,
    subject: str,
    relative: str,
    *,
    provenance: tuple[str, str] | None = None,
    revision: str | None = None,
    captured_at: str | None = None,
    max_entries: int = 4096,
) -> dict[str, Any]:
    """List registered document types and immediate directories only."""
    target, target_type = _source_path_type(root, relative, kind=kind)
    if target_type != "directory":
        raise SkillGatewayError(
            "source_path_not_directory",
            "The requested source ref is a file; list its containing directory.",
        )
    allowed_suffixes = {".md"} if kind == "local" else {".md", ".yml", ".yaml"}
    results = []
    complete = True
    try:
        entries = sorted(target.iterdir(), key=lambda item: item.name)
    except PermissionError:
        raise SkillGatewayError(
            "source_path_inaccessible", "The source directory cannot be read"
        ) from None
    for entry in entries:
        child_relative = posixpath.join(relative, entry.name)
        if _source_path_excluded(child_relative):
            continue
        try:
            child, child_type = _source_path_type(root, child_relative, kind=kind)
        except SkillGatewayError as exc:
            if exc.code in {"source_path_outside_root", "invalid_document"}:
                continue
            complete = False
            continue
        if child_type == "file" and child.suffix.lower() not in allowed_suffixes:
            continue
        if child_type not in {"directory", "file"}:
            continue
        ref = _source_ref(kind, subject, child_relative, revision=revision)
        source = (
            "agentic-perf"
            if kind == "local"
            else (provenance or ("upstream", "unverified"))[0]
        )
        result = {
            "ref": ref,
            "uri": ref,
            "path": child_relative,
            "source": source,
            "scope": kind,
            "type": child_type,
            "size_bytes": child.stat().st_size if child_type == "file" else None,
        }
        if kind == "upstream":
            _source, provenance_status = provenance or ("upstream", "unverified")
            result["provenance_status"] = provenance_status
            result["revision"] = revision
            result["captured_at"] = captured_at
        results.append(result)
        if len(results) >= max_entries:
            complete = False
            break
    results.sort(key=lambda item: item["path"])
    return {"results": results, "complete": complete}


def _local_snapshot_resolved_path(snapshot: dict[str, Any], relative: str) -> str:
    """Resolve a local directory alias using only the ticket-pinned map."""
    if not isinstance(relative, str) or relative.startswith(("/", "\\")):
        raise SkillGatewayError("invalid_document", "Invalid local source path")
    normalized = posixpath.normpath(relative)
    if normalized in {"..", "."} and relative not in {"", "."}:
        raise SkillGatewayError("invalid_document", "Invalid local source path")
    if normalized.startswith("../") or "\\" in normalized or ":" in normalized:
        raise SkillGatewayError("invalid_document", "Invalid local source path")
    normalized = "" if normalized == "." else normalized
    if _source_path_excluded(normalized):
        raise SkillGatewayError("invalid_document", "Source path is excluded")

    aliases = snapshot.get("directory_aliases", {})
    if not isinstance(aliases, dict) or len(aliases) > _MAX_LOCAL_DIRECTORY_ALIASES:
        raise SkillGatewayError(
            "invalid_snapshot", "Local directory alias map is invalid"
        )
    checked: dict[str, str] = {}
    for alias, canonical in aliases.items():
        if not isinstance(alias, str) or not isinstance(canonical, str):
            raise SkillGatewayError(
                "invalid_snapshot", "Local directory alias map is invalid"
            )
        alias_path = posixpath.normpath(alias)
        canonical_path = posixpath.normpath(canonical)
        alias_path = "" if alias_path == "." else alias_path
        canonical_path = "" if canonical_path == "." else canonical_path
        if (
            not alias_path
            or alias_path == ".."
            or alias_path != alias
            or canonical_path == ".."
            or canonical_path != canonical
            or alias_path.startswith("../")
            or canonical_path.startswith("../")
            or alias_path.startswith("/")
            or canonical_path.startswith("/")
            or "\\" in alias_path
            or "\\" in canonical_path
            or ":" in alias_path
            or ":" in canonical_path
            or _source_path_excluded(alias_path)
            or _source_path_excluded(canonical_path)
            or alias_path == canonical_path
        ):
            raise SkillGatewayError(
                "invalid_snapshot", "Local directory alias map is invalid"
            )
        checked[alias_path] = canonical_path

    # A mapping target must not itself pass through another alias. This also
    # rejects cycles, so request resolution always uses one pinned mapping.
    for alias, canonical in checked.items():
        if any(
            canonical == other or canonical.startswith(f"{other}/") for other in checked
        ):
            raise SkillGatewayError("invalid_snapshot", "Local directory alias chain")

    for alias in sorted(checked, key=lambda item: (-len(item), item)):
        if normalized == alias or normalized.startswith(f"{alias}/"):
            suffix = normalized[len(alias) :].lstrip("/")
            resolved = posixpath.normpath(posixpath.join(checked[alias], suffix))
            resolved = "" if resolved == "." else resolved
            if (
                resolved == ".."
                or resolved.startswith("../")
                or _source_path_excluded(resolved)
            ):
                raise SkillGatewayError("invalid_document", "Invalid local source path")
            if any(
                (resolved == other or resolved.startswith(f"{other}/"))
                and other != alias
                for other in checked
            ):
                raise SkillGatewayError(
                    "invalid_document", "Local directory alias chain"
                )
            return resolved
    return normalized


def _local_snapshot_type(snapshot: dict[str, Any], relative: str) -> str:
    relative = _local_snapshot_resolved_path(snapshot, relative)
    documents = snapshot.get("documents", [])
    paths = {
        item.get("path")
        for item in documents
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    }
    if relative in paths:
        return "file"
    prefix = relative.rstrip("/") + "/" if relative else ""
    if any(path.startswith(prefix) for path in paths):
        return "directory"
    raise SkillGatewayError(
        "source_path_not_found", "The local source path does not exist"
    )


def _list_local_snapshot_directory(
    snapshot: dict[str, Any], subject: str, relative: str
) -> dict[str, Any]:
    if not snapshot.get("available"):
        raise SkillGatewayError("source_unavailable", "Local subject is unavailable")
    relative = _local_snapshot_resolved_path(snapshot, relative)
    if _local_snapshot_type(snapshot, relative) != "directory":
        raise SkillGatewayError(
            "source_path_not_directory",
            "The requested source ref is a file; list its containing directory.",
        )
    prefix = relative.rstrip("/") + "/" if relative else ""
    children: dict[str, str] = {}
    sizes: dict[str, int] = {}
    for item in snapshot.get("documents", []):
        if not isinstance(item, dict):
            continue
        item_path = item.get("path")
        if not isinstance(item_path, str) or not item_path.startswith(prefix):
            continue
        suffix = item_path[len(prefix) :]
        if not suffix:
            continue
        child = suffix.split("/", 1)[0]
        child_path = posixpath.join(relative, child) if relative else child
        children[child_path] = "directory" if "/" in suffix else "file"
        if children[child_path] == "file" and child_path == item_path:
            sizes[child_path] = int(item.get("size_bytes", 0))
    results = []
    for path, kind in sorted(children.items()):
        if kind == "file" and not path.lower().endswith(".md"):
            continue
        ref = _source_ref("local", subject, path)
        results.append(
            {
                "ref": ref,
                "uri": ref,
                "path": path,
                "source": "agentic-perf",
                "scope": "local",
                "type": kind,
                "revision": snapshot.get("revision"),
                "captured_at": snapshot.get("captured_at"),
                "size_bytes": sizes.get(path) if kind == "file" else None,
            }
        )
    return {"results": results, "complete": len(results) < 4096}


def _resolve_local_snapshot_pointer(
    snapshot: dict[str, Any], relative: str, pointer: str
) -> tuple[str, str]:
    if pointer.startswith(("/", "\\")) or "://" in pointer or "\\" in pointer:
        raise SkillGatewayError("invalid_document", "Invalid relative pointer")
    relative = _local_snapshot_resolved_path(snapshot, relative)
    origin_type = _local_snapshot_type(snapshot, relative)
    base = relative if origin_type == "directory" else posixpath.dirname(relative)
    resolved = posixpath.normpath(posixpath.join(base, pointer))
    if resolved in {"", ".", ".."} or resolved.startswith("../"):
        raise SkillGatewayError("invalid_document", "Pointer leaves its source")
    resolved = _local_snapshot_resolved_path(snapshot, resolved)
    return resolved, _local_snapshot_type(snapshot, resolved)


def _read_local_document(
    local_skills_dir: Path | None,
    subject: str,
    relative: str,
    *,
    offset_bytes: int,
    max_bytes: int,
    full_content: bool = False,
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if snapshot is not None:
        relative = _local_snapshot_resolved_path(snapshot, relative)
        matches = [
            item
            for item in snapshot.get("documents", [])
            if isinstance(item, dict) and item.get("path") == relative
        ]
        if not snapshot.get("available") or not matches:
            raise SkillGatewayError(
                "source_path_not_found", "The local document does not exist"
            )
        content = matches[0].get("content")
        if not isinstance(content, str):
            raise SkillGatewayError(
                "source_read_failed", "The local document could not be read"
            )
        page = (
            {"content": content, "offset": offset_bytes, "next_offset": None}
            if full_content
            else OrganizationSkillResolver._page(content, offset_bytes, max_bytes)
        )
        ref = _source_ref("local", subject, relative)
        return {
            "ref": ref,
            "uri": ref,
            "path": relative,
            "source": "agentic-perf",
            "scope": "local",
            "role": "operational-guidance",
            "revision": snapshot.get("revision"),
            "captured_at": snapshot.get("captured_at"),
            **page,
            "offset_bytes": page["offset"],
            "next_offset_bytes": page["next_offset"],
        }
    base = _local_base(local_skills_dir, subject)
    if base is None:
        raise SkillGatewayError("source_unavailable", "Local subject is unavailable")
    candidate = base / relative
    if _source_path_excluded(relative):
        raise SkillGatewayError("invalid_document", "Invalid local document")
    try:
        target = candidate.resolve(strict=True)
    except PermissionError:
        raise SkillGatewayError(
            "source_path_inaccessible", "The local document cannot be read"
        ) from None
    except FileNotFoundError:
        raise SkillGatewayError(
            "source_path_not_found", "The local document does not exist"
        ) from None
    except (OSError, RuntimeError):
        raise SkillGatewayError(
            "source_path_inaccessible", "The local document cannot be resolved"
        ) from None
    if not target.is_relative_to(base) or _source_path_excluded(
        target.relative_to(base).as_posix()
    ):
        raise SkillGatewayError("invalid_document", "Invalid local document")
    if not target.is_file():
        if target.is_dir():
            raise SkillGatewayError(
                "source_path_is_directory",
                "The reference is a directory; use operation='list'.",
            )
        raise SkillGatewayError("invalid_document", "Invalid local document")
    if target.suffix.lower() != ".md":
        raise SkillGatewayError("invalid_document", "Only Markdown guidance is exposed")
    if target.stat().st_size > _MAX_SOURCE_DOCUMENT_BYTES:
        raise SkillGatewayError("source_too_large", "Local document exceeds the limit")
    try:
        content = target.read_text(encoding="utf-8")
    except PermissionError:
        raise SkillGatewayError(
            "source_path_inaccessible", "The local document cannot be read"
        ) from None
    except OSError:
        raise SkillGatewayError(
            "source_read_failed", "The local document could not be read"
        ) from None
    page = (
        {"content": content, "offset": offset_bytes, "next_offset": None}
        if full_content
        else OrganizationSkillResolver._page(content, offset_bytes, max_bytes)
    )
    ref = _source_ref("local", subject, relative)
    return {
        "ref": ref,
        "uri": ref,
        "path": relative,
        "source": "agentic-perf",
        "scope": "local",
        "role": "operational-guidance",
        **page,
        "offset_bytes": page["offset"],
        "next_offset_bytes": page["next_offset"],
    }


def _upstream_provenance(repo_cache: Any, subject: str) -> tuple[str, str]:
    repo_name = _UPSTREAM_REPOS.get(subject)
    if not repo_name:
        return "unknown cached repository", "unverified"
    get_origin = getattr(repo_cache, "get_origin", None)
    origin = get_origin(repo_name) if callable(get_origin) else None
    get_revision = getattr(repo_cache, "get_revision", None)
    revision = get_revision(repo_name) if callable(get_revision) else None
    if isinstance(origin, str) and origin and isinstance(revision, str):
        return origin, "pinned-upstream-commit"
    if isinstance(origin, str) and origin:
        return origin, "configured-cache-origin-unverified"
    return f"cached repository: {repo_name}", "unverified"


def _list_upstream_documents(repo_cache: Any, subject: str) -> list[dict[str, Any]]:
    repo_name = _UPSTREAM_REPOS.get(subject)
    if not repo_name or repo_cache is None:
        return []
    source_path = repo_cache.get_path(repo_name)
    if source_path is None:
        return []
    root = Path(source_path).resolve()
    source, provenance_status = _upstream_provenance(repo_cache, subject)
    get_revision = getattr(repo_cache, "get_revision", None)
    revision = get_revision(repo_name) if callable(get_revision) else None
    if not isinstance(revision, str):
        return []
    results = []
    for item in repo_cache.list_docs(repo_name, subdirs=["docs", "config"]):
        path = item.get("path")
        if not isinstance(path, str):
            continue
        try:
            target, path_type = _source_path_type(root, path, kind="upstream")
        except SkillGatewayError:
            continue
        if path_type != "file" or target.suffix.lower() not in {
            ".md",
            ".yml",
            ".yaml",
        }:
            continue
        ref = _source_ref("upstream", subject, path, revision=revision)
        results.append(
            {
                "ref": ref,
                "uri": ref,
                "path": path,
                "source": source,
                "scope": "upstream",
                "provenance_status": provenance_status,
                "revision": revision,
                "captured_at": getattr(repo_cache, "captured_at", None),
                "role": "software-reference",
                "size_bytes": item.get("size_bytes", 0),
            }
        )
    return results


def _read_upstream_document(
    repo_cache: Any,
    subject: str,
    relative: str,
    *,
    offset_bytes: int,
    max_bytes: int,
    full_content: bool = False,
    expected_revision: str | None = None,
) -> dict[str, Any]:
    repo_name = _UPSTREAM_REPOS.get(subject)
    if not repo_name or repo_cache is None:
        raise SkillGatewayError("source_unavailable", "Upstream cache is unavailable")
    root = repo_cache.get_path(repo_name)
    if root is None:
        raise SkillGatewayError("source_unavailable", "Upstream cache is unavailable")
    root = Path(root).resolve()
    get_revision = getattr(repo_cache, "get_revision", None)
    revision = get_revision(repo_name) if callable(get_revision) else None
    if not isinstance(revision, str) or revision != expected_revision:
        raise SkillGatewayError(
            "source_binding_changed", "Upstream revision does not match the ref"
        )
    target, path_type = _source_path_type(root, relative, kind="upstream")
    if path_type == "directory":
        raise SkillGatewayError(
            "source_path_is_directory",
            "This upstream ref is a directory; use operation='list' to inspect its immediate children.",
        )
    if target.suffix.lower() not in {".md", ".yml", ".yaml"}:
        raise SkillGatewayError("invalid_document", "Invalid upstream document")
    if target.stat().st_size > _MAX_SOURCE_DOCUMENT_BYTES:
        raise SkillGatewayError(
            "source_too_large", "Upstream document exceeds the limit"
        )
    try:
        content = target.read_text(encoding="utf-8")
    except PermissionError:
        raise SkillGatewayError(
            "source_path_inaccessible", "The upstream document cannot be read"
        ) from None
    except OSError:
        raise SkillGatewayError(
            "source_read_failed", "The upstream document could not be read"
        ) from None
    page = (
        {"content": content, "offset": offset_bytes, "next_offset": None}
        if full_content
        else OrganizationSkillResolver._page(content, offset_bytes, max_bytes)
    )
    ref = _source_ref("upstream", subject, relative, revision=revision)
    source, provenance_status = _upstream_provenance(repo_cache, subject)
    return {
        "ref": ref,
        "uri": ref,
        "path": relative,
        "source": source,
        "scope": "upstream",
        "provenance_status": provenance_status,
        "revision": revision,
        "captured_at": getattr(repo_cache, "captured_at", None),
        "role": "software-reference",
        **page,
        "offset_bytes": page["offset"],
        "next_offset_bytes": page["next_offset"],
    }


def _gateway_cursor_path(cursor: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{32}", cursor):
        raise SkillGatewayError("invalid_cursor", "The continuation cursor is invalid")
    return f"context/gateway-cursors/{cursor}.json"


def _load_gateway_cursor(
    manager: Any,
    cursor: str,
    *,
    ticket_id: str,
    agent_name: str,
    phase: str,
    subject: str,
    operation: str,
    query: str,
    from_ref: str,
    ref: str,
    path: str,
) -> dict[str, Any]:
    cursor_path = _gateway_cursor_path(cursor)
    try:
        value = json.loads(
            manager.resolve_path(cursor_path).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError, ValueError):
        raise SkillGatewayError(
            "invalid_cursor", "The cursor is unknown for this ticket"
        ) from None
    if not isinstance(value, dict) or not isinstance(value.get("binding"), dict):
        raise SkillGatewayError("invalid_cursor", "The cursor state is invalid")
    binding = value["binding"]
    expected = {
        "ticket_id": ticket_id,
        "agent_name": agent_name,
        "phase": phase,
        "subject": subject,
        "operation": operation,
        "query": query,
    }
    if any(
        binding.get(key) != expected_value for key, expected_value in expected.items()
    ):
        raise SkillGatewayError(
            "cursor_scope_mismatch",
            "The cursor belongs to another ticket, phase, subject, operation, or query.",
        )
    if from_ref and from_ref != binding.get("origin"):
        raise SkillGatewayError(
            "cursor_scope_mismatch", "The cursor belongs to a different source origin."
        )
    if ref and ref != binding.get("ref"):
        raise SkillGatewayError(
            "cursor_scope_mismatch", "The cursor belongs to a different source ref."
        )
    if path and path != binding.get("path", ""):
        raise SkillGatewayError(
            "cursor_scope_mismatch", "The cursor belongs to a different relative scope."
        )
    return value


def _save_gateway_cursor(
    manager: Any, binding: dict[str, Any], state: dict[str, Any]
) -> str:
    cursor = secrets.token_urlsafe(24)
    relative_path = _gateway_cursor_path(cursor)
    manager.save_file(
        relative_path,
        json.dumps({"binding": binding, "state": state}, separators=(",", ":")),
    )
    manager._stamp(relative_path, source="workspace", authority="alternate")
    return cursor


def _source_root(
    kind: str,
    subject: str,
    local_skills_dir: Path | None,
    repo_cache: Any,
) -> tuple[Path, tuple[str, str]] | None:
    if kind == "local":
        root = _local_base(local_skills_dir, subject)
        return (root, ("agentic-perf", "checked-in-project-context")) if root else None
    repo_name = _UPSTREAM_REPOS.get(subject)
    if not repo_name or repo_cache is None:
        return None
    source_path = repo_cache.get_path(repo_name)
    if source_path is None:
        return None
    root = Path(source_path).resolve()
    return root, _upstream_provenance(repo_cache, subject)


def _resolve_source_pointer(
    kind: str,
    root: Path,
    relative: str,
    pointer: str,
) -> tuple[str, str]:
    if pointer.startswith(("/", "\\")) or "://" in pointer or "\\" in pointer:
        raise SkillGatewayError("invalid_document", "Invalid relative pointer")
    _target, origin_type = _source_path_type(root, relative, kind=kind)
    base = relative if origin_type == "directory" else posixpath.dirname(relative)
    resolved = posixpath.normpath(posixpath.join(base, pointer))
    if resolved in {"", ".", ".."} or resolved.startswith("../"):
        raise SkillGatewayError("invalid_document", "Pointer leaves its source")
    if kind == "upstream" and not _is_exposed_upstream_path(resolved):
        raise SkillGatewayError(
            "invalid_document", "Pointer leaves exposed upstream docs"
        )
    _target, target_type = _source_path_type(root, resolved, kind=kind)
    return resolved, target_type


def _search_source_documents(
    kind: str,
    subject: str,
    root: Path,
    provenance: tuple[str, str],
    query: str,
    *,
    scope_path: str = "",
    local_skills_dir: Path | None = None,
    repo_cache: Any = None,
    local_snapshot: dict[str, Any] | None = None,
    expected_revision: str | None = None,
) -> dict[str, Any]:
    documents = (
        _list_local_documents(local_skills_dir, subject, snapshot=local_snapshot)
        if kind == "local"
        else _list_upstream_documents(repo_cache, subject)
    )
    complete = len(documents) < 128 if kind == "local" else True
    if scope_path:
        if kind == "local":
            scope_path = _local_snapshot_resolved_path(local_snapshot or {}, scope_path)
            scope_type = _local_snapshot_type(local_snapshot or {}, scope_path)
            scope = None
        else:
            scope, scope_type = _source_path_type(root, scope_path, kind=kind)
        if scope_type == "directory":
            # Search inventories use repository-relative paths; normalize a
            # safe directory alias before filtering so its target's documents
            # remain discoverable with reusable canonical refs.
            scope_path = (
                scope_path if kind == "local" else scope.relative_to(root).as_posix()
            )
        if scope_type == "file":
            allowed_ref = _source_ref(
                kind, subject, scope_path, revision=expected_revision
            )
            if not any(item["ref"] == allowed_ref for item in documents):
                raise SkillGatewayError(
                    "invalid_origin", "The file is not an exposed source document"
                )
            scope_path = posixpath.dirname(scope_path)
        if scope_path not in {"", "."}:
            prefix = f"{scope_path.rstrip('/')}/"
            documents = [item for item in documents if item["path"].startswith(prefix)]
    alternatives = [
        term.casefold().strip() for term in query.split("|") if term.strip()
    ]
    results = []
    scanned_bytes = 0
    for item in documents:
        size = item.get("size_bytes", 0)
        if not isinstance(size, int) or size < 0:
            complete = False
            continue
        if scanned_bytes + size > _MAX_SOURCE_SEARCH_BYTES:
            complete = False
            break
        scanned_bytes += size
        try:
            document = (
                _read_local_document(
                    local_skills_dir,
                    subject,
                    item["path"],
                    offset_bytes=0,
                    max_bytes=16384,
                    full_content=True,
                    snapshot=local_snapshot,
                )
                if kind == "local"
                else _read_upstream_document(
                    repo_cache,
                    subject,
                    item["path"],
                    offset_bytes=0,
                    max_bytes=16384,
                    full_content=True,
                    expected_revision=expected_revision,
                )
            )
        except (OSError, ValueError, TypeError, SkillGatewayError):
            complete = False
            continue
        content = document.get("content", "").casefold()
        if not alternatives or any(term in content for term in alternatives):
            results.append(
                {
                    key: value
                    for key, value in item.items()
                    if key not in {"uri", "role"}
                }
            )
            if len(results) >= 500:
                complete = False
                break
    return {
        "results": results,
        "complete": complete,
        "scanned_bytes": scanned_bytes,
        "query_mode": "literal-alternatives",
        "scope_path": scope_path or ".",
        "scope_semantics": "recursive subtree; file origins use their parent directory",
        "source": provenance[0],
        "provenance_status": provenance[1],
    }


def _source_page(
    items: list[dict[str, Any]],
    start: int,
    budget: int,
) -> list[dict[str, Any]]:
    page = []
    used = 0
    for item in items[start:]:
        size = len(json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode())
        if page and used + size > budget:
            break
        if not page and size > budget:
            # Let final envelope accounting decide whether this indivisible
            # result fits the actual response budget.
            page.append(item)
            break
        page.append(item)
        used += size
        if len(page) >= 20:
            break
    return page


def _append_source_page(
    sources: list[dict[str, Any]],
    pages: list[dict[str, Any]],
    *,
    items: list[dict[str, Any]],
    key: str,
    start: int,
    budget: int,
    binding: dict[str, Any],
    metadata: dict[str, Any],
    complete: bool,
    total: int,
    mode: str = "items",
    backend_more: bool = False,
    organization_offset: int = 0,
) -> None:
    page = _source_page(items, start, budget)
    has_more = start + len(page) < len(items) or backend_more
    source = {
        **metadata,
        key: page,
        "found": total > 0,
        "matches_count": total,
        "complete": complete,
        "absence_confirmed": total == 0 and complete,
        "has_more": has_more,
    }
    if has_more:
        source["next_cursor"] = "c" * 32
    sources.append(source)
    pages.append(
        {
            "source": source,
            "items": items,
            "key": key,
            "start": start,
            "binding": binding,
            "mode": mode,
            "backend_more": backend_more,
            "organization_offset": organization_offset,
            "forced_more": False,
        }
    )


def _compact_json_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def _page_has_pending_items(page: dict[str, Any]) -> bool:
    if page["mode"] == "organization_offset":
        return bool(page["items"]) or page["backend_more"]
    return page["start"] < len(page["items"])


def _page_too_small_result(
    result: dict[str, Any], pages: list[dict[str, Any]], budget: int
) -> str:
    blocked_sources = []
    for page in pages:
        if page["source"][page["key"]] or not _page_has_pending_items(page):
            continue
        source_ref = (
            page["source"].get("scope_ref")
            or page["binding"].get("origin")
            or page["binding"].get("ref")
            or (
                page["items"][page["start"]].get("ref")
                if page["start"] < len(page["items"])
                else None
            )
        )
        blocked = {
            "source": page["source"].get(
                "source", page["source"].get("scope", "unknown")
            )
        }
        if source_ref:
            blocked["scope_ref"] = source_ref
        blocked_sources.append(blocked)

    return _page_too_small_response(
        result["subject"], result["operation"], blocked_sources, budget
    )


def _page_too_small_response(
    subject: str,
    operation: str,
    blocked_sources: list[dict[str, Any]],
    budget: int,
) -> str:
    payload = {
        "found": False,
        "subject": subject,
        "operation": operation,
        "reason": "page_too_small",
        "message": "Increase max_bytes and retry; no continuation cursor was created.",
        "sources": blocked_sources,
    }
    while _compact_json_bytes(payload) > budget and len(blocked_sources) > 1:
        blocked_sources.pop()
    if _compact_json_bytes(payload) > budget:
        for blocked in blocked_sources:
            blocked.pop("scope_ref", None)
    if _compact_json_bytes(payload) > budget:
        payload = {
            "found": False,
            "operation": operation,
            "reason": "page_too_small",
        }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _finalize_search_list_result(
    result: dict[str, Any],
    pages: list[dict[str, Any]],
    *,
    manager: Any,
    budget: int,
) -> str:
    while _compact_json_bytes(result) > budget:
        candidates = [page for page in pages if page["source"][page["key"]]]
        if not candidates:
            return _page_too_small_result(result, pages, budget)
        page = max(
            candidates,
            key=lambda item: _compact_json_bytes(item["source"][item["key"]][-1]),
        )
        page["source"][page["key"]].pop()
        page["forced_more"] = True
        page["source"]["has_more"] = True
        page["source"].setdefault("next_cursor", "c" * 32)
    if any(
        not page["source"][page["key"]] and _page_has_pending_items(page)
        for page in pages
    ):
        return _page_too_small_result(result, pages, budget)
    for page in pages:
        source = page["source"]
        if page["mode"] == "organization_offset":
            shown_bytes = sum(
                len((json.dumps(item, ensure_ascii=False) + "\n").encode("utf-8"))
                for item in source[page["key"]]
            )
            next_offset = page["organization_offset"] + shown_bytes
            more = (
                page["backend_more"]
                or page["forced_more"]
                or len(source[page["key"]]) < len(page["items"])
            )
            state = {"mode": "organization_offset", "offset": next_offset}
        else:
            next_index = page["start"] + len(source[page["key"]])
            more = next_index < len(page["items"]) or page["forced_more"]
            state = {
                "mode": "items",
                "items": page["items"],
                "index": next_index,
                "metadata": {
                    key: value
                    for key, value in source.items()
                    if key not in {page["key"], "next_cursor", "has_more"}
                },
                "key": page["key"],
            }
        source["has_more"] = more
        if more:
            source["next_cursor"] = _save_gateway_cursor(
                manager, page["binding"], state
            )
        else:
            source.pop("next_cursor", None)
    result["found"] = any(source.get("found") for source in result["sources"])
    result["complete"] = bool(result["sources"]) and all(
        source.get("complete") is True for source in result["sources"]
    )
    result["absence_confirmed"] = not result["found"] and result["complete"]
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


async def _skill_context_list_search(
    provider: Any,
    *,
    resolver: OrganizationSkillResolver,
    organization: dict[str, Any],
    ticket_id: str,
    agent_name: str,
    phase: str,
    subject: str,
    operation: str,
    ref: str,
    path: str,
    from_ref: str,
    query: str,
    max_bytes: int,
    response_overhead_bytes: int,
    cursor: str,
    ssh: Any,
    controller_host: str | None,
    local_skills_dir: Path | None,
    repo_cache: Any,
) -> str:
    from agents.server_utils import controller_context_gateway
    from providers.workspace.manager import WorkspaceManager

    manager = WorkspaceManager(ticket_id=ticket_id, agent_name=agent_name, phase=phase)
    project_source, project_documents = await _project_documents(
        provider,
        ticket_id=ticket_id,
        subject=subject,
        phase=phase,
        agent_name=agent_name,
        benchmark=None,
    )
    budget = max_bytes - max(0, response_overhead_bytes)
    if budget < 512:
        raise SkillGatewayError(
            "page_too_small", "max_bytes must leave 512 bytes for the complete result."
        )
    if operation == "list" and not cursor and (not ref and not from_ref):
        raise SkillGatewayError(
            "origin_required",
            "List a returned directory ref or a directory relative to from_ref.",
        )
    if operation == "search" and ref:
        raise SkillGatewayError("invalid_search", "Use from_ref to scope a search.")
    if operation == "search" and (not query.strip() or len(query) > 256):
        raise SkillGatewayError(
            "invalid_query", "Search query must contain 1 through 256 characters."
        )
    if cursor:
        saved = _load_gateway_cursor(
            manager,
            cursor,
            ticket_id=ticket_id,
            agent_name=agent_name,
            phase=phase,
            subject=subject,
            operation=operation,
            query=query,
            from_ref=from_ref,
            ref=ref,
            path=path,
        )
        saved_binding = saved["binding"]
        saved_state = saved.get("state", {})
        if not isinstance(saved_state, dict):
            raise SkillGatewayError("invalid_cursor", "The cursor state is invalid")
        selected_sources = [saved_binding.get("source")]
    else:
        saved_binding = saved_state = None
        if operation == "list":
            selected_sources = [
                "controller"
                if (ref or from_ref).startswith(_SOFTWARE_PREFIX)
                else "project"
                if (ref or from_ref).startswith(_PROJECT_PREFIX)
                else "local"
                if (ref or from_ref).startswith(_LOCAL_PREFIX)
                else "upstream"
                if (ref or from_ref).startswith(_UPSTREAM_PREFIX)
                else "organization"
                if is_organization_ref(ref or from_ref)
                else ""
            ]
        elif from_ref:
            selected_sources = [
                "organization"
                if is_organization_ref(from_ref)
                else "controller"
                if from_ref.startswith(_SOFTWARE_PREFIX)
                else "project"
                if from_ref.startswith(_PROJECT_PREFIX)
                else "local"
                if from_ref.startswith(_LOCAL_PREFIX)
                else "upstream"
                if from_ref.startswith(_UPSTREAM_PREFIX)
                else ""
            ]
        else:
            selected_sources = []
            if organization.get("status") in {"available", "unavailable"}:
                selected_sources.append("organization")
            if subject == "harness/crucible" and phase in {"benchmark", "review"}:
                selected_sources.append("controller")
            if subject in _LOCAL_SUBJECT_DIRS:
                selected_sources.extend(["local", "upstream"])
            if project_documents:
                selected_sources.append("project")
    if any(not source for source in selected_sources):
        raise SkillGatewayError("invalid_origin", "Use a returned source ref.")

    effective_from_ref = saved_binding.get("origin", "") if saved_binding else from_ref
    effective_ref = saved_binding.get("ref", "") if saved_binding else ref
    effective_path = saved_binding.get("path", "") if saved_binding else path

    sources: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    # Let each source use the remaining result budget; the final envelope pass
    # removes any excess and keeps continuation cursors for trimmed results.
    per_source_budget = max(64, budget - 256)
    for source_kind in selected_sources:
        origin_ref = effective_from_ref
        bound_ref = effective_ref
        source_scope = saved_binding.get("scope", "") if saved_binding else ""
        binding = {
            "ticket_id": ticket_id,
            "agent_name": agent_name,
            "phase": phase,
            "subject": subject,
            "operation": operation,
            "query": query,
            "source": source_kind,
            "scope": source_scope,
            "origin": origin_ref,
            "ref": bound_ref,
            "path": effective_path,
        }
        if saved_binding:
            binding.update(saved_binding)
        source_snapshot = None
        source_repo_cache = repo_cache
        source_revision = None
        if source_kind in {"local", "upstream"}:
            try:
                source_snapshot, source_repo_cache = await _ticket_source_snapshot(
                    provider,
                    ticket_id=ticket_id,
                    subject=subject,
                    kind=source_kind,
                    local_skills_dir=local_skills_dir,
                    repo_cache=repo_cache,
                )
                if source_kind == "local":
                    source_revision = source_snapshot.get("revision")
                    if not source_snapshot.get("available"):
                        raise SkillGatewayError(
                            "source_unavailable", "Local subject is unavailable"
                        )
                else:
                    source_revision = source_snapshot.get("revision")
                    if source_repo_cache is None or not isinstance(
                        source_revision, str
                    ):
                        raise SkillGatewayError(
                            "source_unavailable",
                            "Pinned upstream context is unavailable",
                        )
            except SkillGatewayError as exc:
                sources.append(
                    {
                        "source": "agentic-perf"
                        if source_kind == "local"
                        else "upstream",
                        "scope": source_kind,
                        "found": False,
                        "complete": False,
                        "absence_confirmed": False,
                        "status": "unavailable",
                        "reason": exc.code,
                    }
                )
                continue
        if cursor and saved_state.get("mode") == "items":
            items = saved_state.get("items")
            start = saved_state.get("index")
            metadata = saved_state.get("metadata")
            key = saved_state.get("key")
            if (
                not isinstance(items, list)
                or type(start) is not int
                or start < 0
                or start > len(items)
                or not isinstance(metadata, dict)
                or key not in {"results", "matches"}
            ):
                raise SkillGatewayError("invalid_cursor", "The saved page is invalid")
            _append_source_page(
                sources,
                pages,
                items=items,
                key=key,
                start=start,
                budget=per_source_budget,
                binding=binding,
                metadata=metadata,
                complete=metadata.get("complete") is True,
                total=metadata.get(
                    "matches_count", metadata.get("total_entries", len(items))
                ),
            )
            continue

        if operation == "list" and source_kind == "organization":
            sources.append(
                {
                    "source": "organization",
                    "scope": "organization",
                    "found": False,
                    "complete": False,
                    "absence_confirmed": False,
                    "capability": "unsupported",
                    "reason": "directory_listing_unsupported",
                    "message": "Organization directories are not exposed; use returned document refs.",
                }
            )
            continue
        if operation == "list" and source_kind == "controller":
            if subject != "harness/crucible" or phase not in {"benchmark", "review"}:
                raise SkillGatewayError(
                    "source_unavailable", "Software source is unavailable in this phase"
                )
            if from_ref:
                controller_origin = _software_path(from_ref)
                software = json.loads(
                    await controller_context_gateway(
                        ssh=ssh,
                        controller_host=controller_host,
                        ticket_id=ticket_id,
                        agent_name=agent_name,
                        phase=phase,
                        operation="list",
                        from_ref=controller_origin,
                        path=path,
                    )
                )
            else:
                controller_path = _software_path(ref)
                software = json.loads(
                    await controller_context_gateway(
                        ssh=ssh,
                        controller_host=controller_host,
                        ticket_id=ticket_id,
                        agent_name=agent_name,
                        phase=phase,
                        operation="list",
                        path=controller_path,
                    )
                )
            if not software.get("found") and software.get("reason"):
                sources.append(
                    {
                        "source": "controller",
                        "scope": "software",
                        "found": False,
                        "complete": False,
                        "absence_confirmed": False,
                        "reason": software["reason"],
                        "message": software.get("message"),
                        "next_action": software.get("next_action"),
                    }
                )
                continue
            items = [_software_document(item) for item in software.get("results", [])]
            source_scope = software.get("resolved_ref", "")
            binding.update({"scope": source_scope})
            _append_source_page(
                sources,
                pages,
                items=items,
                key="results",
                start=0,
                budget=per_source_budget,
                binding=binding,
                metadata={
                    "source": "controller",
                    "scope": "software",
                    "scope_ref": _software_ref(source_scope) if source_scope else "",
                    "scope_kind": "immediate_children",
                    "recursive": False,
                    "total_entries": software.get("total_entries", len(items)),
                    "complete": software.get("complete") is True,
                },
                complete=software.get("complete") is True,
                total=software.get("total_entries", len(items)),
            )
            continue
        if operation == "list" and source_kind == "project":
            if project_source is None:
                raise SkillGatewayError(
                    "source_unavailable", "Project context is unavailable"
                )
            if from_ref:
                origin = _project_read_target(
                    project_documents,
                    ref="",
                    path="",
                    from_ref=from_ref,
                    allow_directory_origin=True,
                    allow_directory_target=True,
                )
                if path:
                    origin = _project_read_target(
                        project_documents,
                        ref="",
                        path=path,
                        from_ref=from_ref,
                        allow_directory_origin=True,
                        allow_directory_target=True,
                    )
            else:
                origin = _project_read_target(
                    project_documents,
                    ref=ref,
                    path="",
                    from_ref="",
                    allow_directory_origin=True,
                    allow_directory_target=True,
                )
            scope_path = _project_scope_path(origin)
            items = _project_list_entries(project_documents, scope_path)
            binding.update({"scope": scope_path})
            _append_source_page(
                sources,
                pages,
                items=items,
                key="results",
                start=0,
                budget=per_source_budget,
                binding=binding,
                metadata={
                    "source": "agentic-perf",
                    "source_id": "agentic-perf",
                    "scope": "project",
                    "scope_ref": origin["ref"],
                    "scope_kind": "manifest-immediate-children",
                    "recursive": False,
                    "total_entries": len(items),
                    "complete": True,
                },
                complete=True,
                total=len(items),
            )
            continue
        if operation == "list":
            kind = source_kind
            if kind == "local":
                if from_ref:
                    origin_subject, origin_path, origin_revision = _parse_source_ref(
                        from_ref, kind
                    )
                    if origin_subject != subject or origin_revision is not None:
                        raise SkillGatewayError(
                            "invalid_origin", "Subject does not match source ref"
                        )
                    if path:
                        relative, _path_kind = _resolve_local_snapshot_pointer(
                            source_snapshot, origin_path, path
                        )
                    else:
                        origin_type = _local_snapshot_type(source_snapshot, origin_path)
                        relative = (
                            origin_path
                            if origin_type == "directory"
                            else posixpath.dirname(origin_path)
                        )
                else:
                    origin_subject, relative, origin_revision = _parse_source_ref(
                        ref, kind
                    )
                    if origin_subject != subject or origin_revision is not None:
                        raise SkillGatewayError(
                            "invalid_ref", "Subject does not match ref"
                        )
                listed = _list_local_snapshot_directory(
                    source_snapshot, subject, relative
                )
                provenance = ("agentic-perf", "ticket-pinned-local-content")
                source_revision = source_snapshot.get("revision")
            else:
                source_root = _source_root(
                    kind, subject, local_skills_dir, source_repo_cache
                )
                if source_root is None:
                    raise SkillGatewayError(
                        "source_unavailable", "Pinned source is not available"
                    )
                root, provenance = source_root
                if from_ref:
                    origin_subject, origin_path, origin_revision = _parse_source_ref(
                        from_ref, kind
                    )
                    if origin_subject != subject or origin_revision != source_revision:
                        raise SkillGatewayError(
                            "source_binding_changed",
                            "Upstream ref revision does not match",
                        )
                    if path:
                        relative, _kind = _resolve_source_pointer(
                            kind, root, origin_path, path
                        )
                    else:
                        _target, origin_type = _source_path_type(
                            root, origin_path, kind=kind
                        )
                        relative = (
                            origin_path
                            if origin_type == "directory"
                            else posixpath.dirname(origin_path)
                        )
                else:
                    origin_subject, relative, origin_revision = _parse_source_ref(
                        ref, kind
                    )
                    if origin_subject != subject or origin_revision != source_revision:
                        raise SkillGatewayError(
                            "source_binding_changed",
                            "Upstream ref revision does not match",
                        )
                listed = _list_source_directory(
                    kind,
                    root,
                    subject,
                    relative,
                    provenance=provenance,
                    revision=source_revision,
                    captured_at=source_snapshot.get("captured_at"),
                )
            binding.update({"scope": relative})
            _append_source_page(
                sources,
                pages,
                items=listed["results"],
                key="results",
                start=0,
                budget=per_source_budget,
                binding=binding,
                metadata={
                    "source": provenance[0],
                    "scope": kind,
                    "scope_ref": _source_ref(
                        kind,
                        subject,
                        relative,
                        revision=source_revision if kind == "upstream" else None,
                    ),
                    "scope_kind": "immediate_children",
                    "recursive": False,
                    "captured_at": source_snapshot.get("captured_at"),
                    "total_entries": len(listed["results"]),
                    "complete": listed["complete"],
                },
                complete=listed["complete"],
                total=len(listed["results"]),
            )
            continue

        if source_kind == "organization":
            if not organization.get("status") == "available":
                sources.append(
                    {
                        "source": "organization",
                        "scope": "organization",
                        "found": False,
                        "complete": False,
                        "absence_confirmed": False,
                        "reason": organization.get("error", "source_unavailable"),
                        "message": organization.get(
                            "message",
                            "Configured organization guidance could not be searched.",
                        ),
                    }
                )
                continue
            if origin_ref and not is_organization_ref(origin_ref):
                raise SkillGatewayError(
                    "invalid_origin", "Origin source does not match cursor"
                )
            org_offset = (
                saved_state.get("offset", 0)
                if cursor and saved_state.get("mode") == "organization_offset"
                else 0
            )
            try:
                org = resolver.search(
                    subject,
                    None,
                    query,
                    from_ref=origin_ref or None,
                    path=effective_path or None,
                    offset=org_offset,
                    max_bytes=max(128, per_source_budget - 128),
                )
            except SkillGatewayError as exc:
                if exc.code != "page_too_small":
                    raise
                safe_ref = origin_ref or next(
                    (
                        document.get("ref")
                        for document in organization.get("documents", [])
                        if isinstance(document.get("ref"), str)
                    ),
                    None,
                )
                blocked = {"source": "organization"}
                if safe_ref:
                    blocked["scope_ref"] = safe_ref
                return _page_too_small_response(subject, operation, [blocked], budget)
            org_matches = org.get("matches", [])
            complete = not org.get("search_limited", False)
            more = org.get("next_offset") is not None
            binding.update(
                {
                    "scope": org.get(
                        "scan_scope", "all applicable organization documents"
                    )
                }
            )
            metadata = {
                "source": "organization",
                "scope": "organization",
                "scope_ref": origin_ref or None,
                "relative_path": effective_path or None,
                "scan_scope": org.get(
                    "scan_scope", "all applicable organization documents"
                ),
                "revision": org.get("revision"),
                "search_limited": org.get("search_limited", False),
                "query_mode": "posix-extended-regex",
            }
            _append_source_page(
                sources,
                pages,
                items=org_matches,
                key="matches",
                start=0,
                budget=per_source_budget,
                binding=binding,
                metadata=metadata,
                complete=complete,
                total=org.get("matches_count", 0),
                mode="organization_offset",
                backend_more=more,
                organization_offset=org_offset,
            )
            continue
        if source_kind == "controller":
            if subject != "harness/crucible" or phase not in {"benchmark", "review"}:
                raise SkillGatewayError(
                    "source_unavailable", "Software source is unavailable in this phase"
                )
            if from_ref:
                controller_origin = _software_path(from_ref)
                software = json.loads(
                    await controller_context_gateway(
                        ssh=ssh,
                        controller_host=controller_host,
                        ticket_id=ticket_id,
                        agent_name=agent_name,
                        phase=phase,
                        operation="search",
                        from_ref=controller_origin,
                        path=path,
                        query=query,
                    )
                )
            else:
                software = json.loads(
                    await controller_context_gateway(
                        ssh=ssh,
                        controller_host=controller_host,
                        ticket_id=ticket_id,
                        agent_name=agent_name,
                        phase=phase,
                        operation="search",
                        query=query,
                    )
                )
            if software.get("reason"):
                sources.append(
                    {
                        "source": "controller",
                        "scope": "software",
                        "found": False,
                        "complete": False,
                        "absence_confirmed": False,
                        "reason": software["reason"],
                        "message": software.get("message"),
                        "next_action": software.get("next_action"),
                    }
                )
                continue
            items = [_software_document(item) for item in software.get("results", [])]
            source_scope = software.get("scan_scope", ".")
            binding.update({"scope": source_scope})
            total = software.get("total_files", len(items))
            _append_source_page(
                sources,
                pages,
                items=items,
                key="results",
                start=0,
                budget=per_source_budget,
                binding=binding,
                metadata={
                    "source": "controller",
                    "scope": "software",
                    "scope_ref": _software_ref(source_scope)
                    if source_scope != "."
                    else None,
                    "total_files": total,
                    "total_matches": software.get("total_matches", total),
                    "truncated": software.get("truncated", False),
                    "scan_scope": software.get("scan_scope", "."),
                    "scope_semantics": software.get("scope_semantics"),
                },
                complete=software.get("complete") is True,
                total=total,
            )
            continue

        if source_kind == "project":
            if project_source is None:
                raise SkillGatewayError(
                    "source_unavailable", "Project context is unavailable"
                )
            scope_path = ""
            if from_ref:
                origin = _project_read_target(
                    project_documents,
                    ref="",
                    path="",
                    from_ref=from_ref,
                    allow_directory_origin=True,
                    allow_directory_target=True,
                )
                if path:
                    target = _project_read_target(
                        project_documents,
                        ref="",
                        path=path,
                        from_ref=from_ref,
                        allow_directory_origin=True,
                        allow_directory_target=True,
                    )
                    scope_path = _project_scope_path(target)
                else:
                    scope_path = _project_scope_path(origin)
            scanned = _project_search_documents(
                project_source,
                project_documents,
                query,
                scope_path=scope_path,
            )
            items = scanned["results"]
            binding.update({"scope": scanned["scope_path"]})
            _append_source_page(
                sources,
                pages,
                items=items,
                key="results",
                start=0,
                budget=per_source_budget,
                binding=binding,
                metadata={
                    "source": "agentic-perf",
                    "source_id": "agentic-perf",
                    "scope": "project",
                    "scope_ref": from_ref or None,
                    "provenance_status": scanned["provenance_status"],
                    "query_mode": scanned["query_mode"],
                    "scanned_bytes": scanned["scanned_bytes"],
                    "scan_scope": scanned["scope_path"],
                    "scope_semantics": scanned["scope_semantics"],
                },
                complete=scanned["complete"],
                total=len(items),
            )
            continue

        local_snapshot = source_snapshot if source_kind == "local" else None
        if source_kind == "local":
            root = Path()
            provenance = ("agentic-perf", "ticket-pinned-local-content")
        else:
            root_data = _source_root(
                source_kind, subject, local_skills_dir, source_repo_cache
            )
            if root_data is None:
                sources.append(
                    {
                        "source": "upstream",
                        "scope": source_kind,
                        "found": False,
                        "complete": False,
                        "absence_confirmed": False,
                        "reason": "source_unavailable",
                    }
                )
                continue
            root, provenance = root_data
        scope_path = ""
        if from_ref:
            origin_subject, origin_path, origin_revision = _parse_source_ref(
                from_ref, source_kind
            )
            if (
                origin_subject != subject
                or (source_kind == "upstream" and origin_revision != source_revision)
                or (source_kind == "local" and origin_revision is not None)
            ):
                raise SkillGatewayError(
                    "source_binding_changed", "Source ref does not match its ticket pin"
                )
            if path:
                if source_kind == "local":
                    scope_path, _scope_type = _resolve_local_snapshot_pointer(
                        local_snapshot, origin_path, path
                    )
                else:
                    scope_path, _scope_type = _resolve_source_pointer(
                        source_kind, root, origin_path, path
                    )
            else:
                scope_path = origin_path
        if cursor and saved_state.get("mode") == "items":
            continue
        scanned = _search_source_documents(
            source_kind,
            subject,
            root,
            provenance,
            query,
            scope_path=scope_path,
            local_skills_dir=local_skills_dir,
            repo_cache=source_repo_cache,
            local_snapshot=local_snapshot,
            expected_revision=source_revision if source_kind == "upstream" else None,
        )
        items = scanned["results"]
        binding.update({"scope": scanned["scope_path"]})
        _append_source_page(
            sources,
            pages,
            items=items,
            key="results",
            start=0,
            budget=per_source_budget,
            binding=binding,
            metadata={
                "source": scanned["source"],
                "scope": source_kind,
                "scope_ref": from_ref or None,
                "revision": source_revision,
                "provenance_status": scanned["provenance_status"],
                "query_mode": scanned["query_mode"],
                "scanned_bytes": scanned["scanned_bytes"],
                "scan_scope": scanned["scope_path"],
                "scope_semantics": scanned["scope_semantics"],
            },
            complete=scanned["complete"],
            total=len(items),
        )

    result = {
        "found": False,
        "complete": False,
        "absence_confirmed": False,
        "subject": subject,
        "operation": operation,
        "query": query if operation == "search" else None,
        "sources": sources,
        "pagination": "ticket-scoped cursor",
    }
    result = {key: value for key, value in result.items() if value is not None}
    # Include the maximum cursor length while fitting so cursor emission itself
    # cannot push the final JSON over the caller's byte budget.
    return _finalize_search_list_result(
        result,
        pages,
        manager=manager,
        budget=budget,
    )


async def skill_context_gateway(
    provider: Any,
    *,
    ticket_id: str,
    agent_name: str,
    phase: str,
    ssh: Any = None,
    controller_host: str | None = None,
    subject: str = "harness/crucible",
    operation: str = "bootstrap",
    ref: str = "",
    path: str = "",
    from_ref: str = "",
    query: str = "",
    max_bytes: int = 16384,
    offset_bytes: int = 0,
    cursor: str = "",
    response_overhead_bytes: int = 0,
    local_skills_dir: Path | None = None,
    repo_cache: Any = None,
) -> str:
    """Combine private organization guidance with phase-compatible software docs.

    Tool registration supplies ticket, actor, phase and controller. The model
    selects only a subject and returned document references. Organization and
    software documents coexist; neither silently substitutes for the other.
    """
    if not ticket_id:
        return json.dumps({"found": False, "reason": "ticket_required"})
    try:
        if operation not in {"bootstrap", "read", "list", "search"}:
            raise SkillGatewayError(
                "unsupported_operation", "Use bootstrap, read, list, or search"
            )
        if type(max_bytes) is not int or not 4 <= max_bytes <= 16384:
            raise SkillGatewayError("invalid_page", "max_bytes must be 4 through 16384")
        if type(offset_bytes) is not int or offset_bytes < 0:
            raise SkillGatewayError("invalid_page", "offset_bytes must be nonnegative")
        if operation in {"list", "search"}:
            if offset_bytes:
                raise SkillGatewayError(
                    "invalid_page", "Use cursor for list/search result pagination"
                )
            if cursor and not isinstance(cursor, str):
                raise SkillGatewayError("invalid_cursor", "Cursor must be a string")
            if path and not from_ref:
                raise SkillGatewayError(
                    "invalid_origin", "Use from_ref with a relative list or search path"
                )
            response_budget = max_bytes - max(0, response_overhead_bytes)
            if response_budget < 512:
                raise SkillGatewayError(
                    "page_too_small", "max_bytes must leave 512 bytes for a result page"
                )
        elif cursor:
            raise SkillGatewayError(
                "invalid_cursor", "Cursor is supported only for list and search"
            )
        resolver = provider.organization_resolver
        organization = resolver.bootstrap(subject)
        organization.setdefault("scope", "organization")
        if organization.get("status") == "unavailable" and organization.get("required"):
            unavailable_source = {
                **organization,
                "complete": False,
                "absence_confirmed": False,
            }
            return json.dumps(
                {
                    "found": False,
                    "complete": False,
                    "absence_confirmed": False,
                    "subject": subject,
                    "operation": operation,
                    "reason": organization.get("error", "organization_unavailable"),
                    "sources": [unavailable_source],
                }
            )
        revision = organization.get("revision", "legacy")
        software_allowed = subject == "harness/crucible" and phase in {
            "benchmark",
            "review",
        }
        project_source, project_documents = await _project_documents(
            provider,
            ticket_id=ticket_id,
            subject=subject,
            phase=phase,
            agent_name=agent_name,
            benchmark=None,
        )
        from agents.server_utils import controller_context_gateway

        if operation == "bootstrap":
            sources = [organization]
            documents = [
                {**item, "scope": "organization", "role": "operational-guidance"}
                for item in organization.get("documents", [])
            ]
            entrypoints = list(organization.get("entrypoints", []))
            if project_source is not None:
                project_revision = getattr(project_source, "revision", None)
                if callable(project_revision):
                    project_revision = project_revision()
                sources.append(
                    {
                        "source": "agentic-perf",
                        "source_id": "agentic-perf",
                        "scope": "project",
                        "status": "available" if project_revision else "unavailable",
                        "reason": "explicit_manifest_source",
                        "revision": project_revision,
                        "captured_at": getattr(project_source, "captured_at", None),
                        "document_count": len(project_documents),
                    }
                )
                documents.extend(project_documents)
                entrypoints.extend(
                    item["ref"] for item in project_documents if item["entrypoint"]
                )
            if subject in _LOCAL_SUBJECT_DIRS:
                try:
                    local_snapshot, _local_view = await _ticket_source_snapshot(
                        provider,
                        ticket_id=ticket_id,
                        subject=subject,
                        kind="local",
                        local_skills_dir=local_skills_dir,
                        repo_cache=repo_cache,
                    )
                except SkillGatewayError as exc:
                    local_snapshot = None
                    sources.append(
                        {
                            "source": "agentic-perf",
                            "scope": "local",
                            "status": "unavailable",
                            "reason": exc.code,
                            "complete": False,
                        }
                    )
                local_documents = (
                    _list_local_documents(
                        local_skills_dir, subject, snapshot=local_snapshot
                    )
                    if local_snapshot and local_snapshot.get("available")
                    else []
                )
                if local_snapshot is not None:
                    sources.append(
                        {
                            "source": "agentic-perf",
                            "scope": "local",
                            "status": "available"
                            if local_snapshot.get("available")
                            else "unavailable",
                            "reason": "ticket_pinned_local_content",
                            "revision": local_snapshot.get("revision"),
                            "captured_at": local_snapshot.get("captured_at"),
                            "document_count": len(local_documents),
                        }
                    )
                documents.extend(local_documents)
                entrypoints.extend(item["ref"] for item in local_documents)
                upstream = _UPSTREAM_REPOS.get(subject)
                if upstream:
                    try:
                        (
                            upstream_snapshot,
                            upstream_cache,
                        ) = await _ticket_source_snapshot(
                            provider,
                            ticket_id=ticket_id,
                            subject=subject,
                            kind="upstream",
                            local_skills_dir=local_skills_dir,
                            repo_cache=repo_cache,
                        )
                        upstream_documents = _list_upstream_documents(
                            upstream_cache, subject
                        )
                        source, provenance_status = _upstream_provenance(
                            upstream_cache, subject
                        )
                        sources.append(
                            {
                                "source": source,
                                "scope": "upstream",
                                "provenance_status": provenance_status,
                                "status": "available",
                                "revision": upstream_snapshot.get("revision"),
                                "captured_at": upstream_snapshot.get("captured_at"),
                                "reason": "ticket_pinned_upstream_commit",
                                "document_count": len(upstream_documents),
                            }
                        )
                    except SkillGatewayError as exc:
                        upstream_documents = []
                        sources.append(
                            {
                                "source": "upstream",
                                "scope": "upstream",
                                "status": "unavailable",
                                "reason": exc.code,
                                "complete": False,
                            }
                        )
                    documents.extend(upstream_documents)
            if software_allowed:
                software = json.loads(
                    await controller_context_gateway(
                        ssh=ssh,
                        controller_host=controller_host,
                        ticket_id=ticket_id,
                        agent_name=agent_name,
                        phase=phase,
                        operation="bootstrap",
                        max_bytes=max_bytes,
                    )
                )
                sources.append(
                    {
                        "source": "controller",
                        "scope": "software",
                        "status": "available"
                        if software.get("found")
                        else "unavailable",
                        "reason": software.get("reason"),
                    }
                )
                if isinstance(software.get("document"), dict):
                    document = _software_document(software["document"])
                    document.pop("content", None)
                    documents.append(document)
                    entrypoints.append(document["ref"])
            elif subject == "harness/crucible" and phase == "triage":
                sources.append(
                    {
                        "source": "github",
                        "scope": "software",
                        "status": "catalog_only",
                        "tools": [
                            "list_benchmarks",
                            "resolve_benchmark",
                            "get_benchmark_details",
                        ],
                    }
                )
            views = []
            if (
                subject == "harness/crucible"
                and organization.get("status") != "unavailable"
                and not organization.get("runtime_config_conflict")
            ):
                for view in _CONFIG_VIEWS.get(phase, ()):
                    if await skill_config_view(provider, "crucible", view, phase):
                        views.append(
                            {"name": view, "ref": _config_ref(revision, subject, view)}
                        )
            return json.dumps(
                {
                    "found": bool(documents or views),
                    "subject": subject,
                    "operation": operation,
                    "phase": phase,
                    "sources": sources,
                    "documents": documents,
                    "entrypoints": entrypoints,
                    "context_manifest": {
                        "schema_version": 1,
                        "subject": subject,
                        "phase": phase,
                        "document_count": len(documents),
                        "documents": [
                            {
                                key: value
                                for key, value in document.items()
                                if key != "content"
                            }
                            for document in documents
                        ],
                    },
                    "configuration_views": views,
                    "context_conflicts": {
                        "potential_document_conflicts": [
                            item
                            for item in organization.get("overlaps", [])
                            if not item.get("same_content", False)
                        ],
                        "cross_source_potential_overlaps": [
                            {
                                "kind": "same_path"
                                if project_document["source_path"]
                                == str(
                                    other.get("source_path") or other.get("path") or ""
                                )
                                else "same_basename",
                                "documents": [
                                    project_document["ref"],
                                    other.get("ref"),
                                ],
                                "scopes": [
                                    "project",
                                    other.get("scope", "organization"),
                                ],
                            }
                            for project_document in project_documents
                            for other in documents
                            if other.get("scope") in {"organization", "software"}
                            and (
                                project_document["source_path"]
                                == str(
                                    other.get("source_path") or other.get("path") or ""
                                )
                                or posixpath.basename(project_document["source_path"])
                                == posixpath.basename(
                                    str(
                                        other.get("source_path")
                                        or other.get("path")
                                        or ""
                                    )
                                )
                            )
                        ],
                        "cross_source_comparison_required": {
                            "project_vs_organization": bool(project_documents)
                            and any(
                                item.get("scope") == "organization"
                                for item in documents
                            ),
                            "project_vs_software": bool(project_documents)
                            and any(
                                item.get("scope") == "software" for item in documents
                            ),
                        },
                        "exact_duplicate_documents": organization.get("duplicates", []),
                        "runtime_configuration_conflict": organization.get(
                            "runtime_config_conflict", False
                        ),
                        "runtime_configuration_sources": organization.get(
                            "runtime_config_sources", []
                        ),
                    },
                }
            )
        if operation == "read":
            if ref.startswith(_CONFIG_PREFIX):
                expected = (
                    {
                        _config_ref(revision, subject, view): view
                        for view in _CONFIG_VIEWS.get(phase, ())
                    }
                    if subject == "harness/crucible"
                    else {}
                )
                if ref not in expected or path or from_ref:
                    raise SkillGatewayError("configuration_view_denied", "Unknown view")
                value = await skill_config_view(
                    provider, "crucible", expected[ref], phase
                )
                page = OrganizationSkillResolver._page(
                    json.dumps(value, indent=2),
                    offset_bytes,
                    max_bytes,
                )
                document = {
                    "ref": ref,
                    "scope": "organization",
                    "role": "configuration-view",
                    "revision": revision,
                    **page,
                    "offset_bytes": page["offset"],
                    "next_offset_bytes": page["next_offset"],
                }
                return json.dumps({"found": True, "document": document})
            if is_organization_ref(ref) or is_organization_ref(from_ref):
                if path and (ref or not from_ref):
                    raise SkillGatewayError(
                        "invalid_document", "Use origin and pointer"
                    )
                document = resolver.read(
                    subject,
                    path or ref,
                    from_ref=from_ref or None,
                    offset=offset_bytes,
                    max_bytes=max_bytes,
                )
                document.update(
                    {
                        "scope": "organization",
                        "role": "operational-guidance",
                        "offset_bytes": document["offset"],
                        "next_offset_bytes": document["next_offset"],
                    }
                )
                return json.dumps({"found": True, "document": document})
            if ref.startswith(_PROJECT_PREFIX) or from_ref.startswith(_PROJECT_PREFIX):
                if project_source is None:
                    raise SkillGatewayError(
                        "source_unavailable", "Project context is unavailable"
                    )
                target = _project_read_target(
                    project_documents,
                    ref=ref,
                    path=path,
                    from_ref=from_ref,
                    allow_directory_origin=True,
                )
                if target.get("is_directory"):
                    raise SkillGatewayError(
                        "source_path_is_directory",
                        "The project ref is a directory; use operation='list'.",
                    )
                content = project_source.read(target["source_path"])
                if content is None:
                    raise SkillGatewayError(
                        "document_unreadable", "Project document is unavailable"
                    )
                page = OrganizationSkillResolver._page(content, offset_bytes, max_bytes)
                document = {
                    **target,
                    "scope": "project",
                    "role": "project-guidance",
                    "authority": "supplemental",
                    **page,
                    "offset_bytes": page["offset"],
                    "next_offset_bytes": page["next_offset"],
                }
                return json.dumps({"found": True, "document": document})
            if ref.startswith(_LOCAL_PREFIX) or from_ref.startswith(_LOCAL_PREFIX):
                if path and (ref or not from_ref):
                    raise SkillGatewayError(
                        "invalid_document", "Use origin and relative pointer"
                    )
                source_subject, relative, source_revision = _parse_source_ref(
                    from_ref if path else ref, "local"
                )
                if source_subject != subject or source_revision is not None:
                    raise SkillGatewayError("invalid_ref", "Subject does not match ref")
                source_snapshot, _source_view = await _ticket_source_snapshot(
                    provider,
                    ticket_id=ticket_id,
                    subject=subject,
                    kind="local",
                    local_skills_dir=local_skills_dir,
                    repo_cache=repo_cache,
                )
                if path:
                    relative, _target_type = _resolve_local_snapshot_pointer(
                        source_snapshot, relative, path
                    )
                document = _read_local_document(
                    local_skills_dir,
                    subject,
                    relative,
                    offset_bytes=offset_bytes,
                    max_bytes=max_bytes,
                    snapshot=source_snapshot,
                )
                return json.dumps({"found": True, "document": document})
            if ref.startswith(_UPSTREAM_PREFIX) or from_ref.startswith(
                _UPSTREAM_PREFIX
            ):
                if path and (ref or not from_ref):
                    raise SkillGatewayError(
                        "invalid_document", "Use origin and relative pointer"
                    )
                source_subject, relative, source_revision = _parse_source_ref(
                    from_ref if path else ref, "upstream"
                )
                if source_subject != subject:
                    raise SkillGatewayError("invalid_ref", "Subject does not match ref")
                source_snapshot, source_repo_cache = await _ticket_source_snapshot(
                    provider,
                    ticket_id=ticket_id,
                    subject=subject,
                    kind="upstream",
                    local_skills_dir=local_skills_dir,
                    repo_cache=repo_cache,
                )
                if source_revision != source_snapshot.get("revision"):
                    raise SkillGatewayError(
                        "source_binding_changed",
                        "Upstream ref revision does not match the ticket pin",
                    )
                if path:
                    source = _source_root(
                        "upstream", subject, local_skills_dir, source_repo_cache
                    )
                    if source is None:
                        raise SkillGatewayError(
                            "source_unavailable", "Upstream cache is unavailable"
                        )
                    relative, _target_type = _resolve_source_pointer(
                        "upstream", source[0], relative, path
                    )
                document = _read_upstream_document(
                    source_repo_cache,
                    subject,
                    relative,
                    offset_bytes=offset_bytes,
                    max_bytes=max_bytes,
                    expected_revision=source_revision,
                )
                return json.dumps({"found": True, "document": document})
            if not software_allowed:
                raise SkillGatewayError("invalid_ref", "Source unavailable in phase")
            if path:
                if ref or not from_ref:
                    raise SkillGatewayError(
                        "invalid_document", "Use a software origin and relative pointer"
                    )
                origin = _software_path(from_ref)
                relative = ""
            else:
                origin = ""
                relative = _software_path(ref)
            result = json.loads(
                await controller_context_gateway(
                    ssh=ssh,
                    controller_host=controller_host,
                    ticket_id=ticket_id,
                    agent_name=agent_name,
                    phase=phase,
                    operation="read",
                    from_ref=origin,
                    path=path if origin else relative,
                    max_bytes=max_bytes,
                    offset_bytes=offset_bytes,
                )
            )
            if isinstance(result.get("document"), dict):
                result["document"] = _software_document(result["document"])
                result["documents"] = [result["document"]]
            return json.dumps(result)
        if operation in {"list", "search"}:
            return await _skill_context_list_search(
                provider,
                resolver=resolver,
                organization=organization,
                ticket_id=ticket_id,
                agent_name=agent_name,
                phase=phase,
                subject=subject,
                operation=operation,
                ref=ref,
                path=path,
                from_ref=from_ref,
                query=query,
                max_bytes=max_bytes,
                response_overhead_bytes=response_overhead_bytes,
                cursor=cursor,
                ssh=ssh,
                controller_host=controller_host,
                local_skills_dir=local_skills_dir,
                repo_cache=repo_cache,
            )
        raise SkillGatewayError(
            "unsupported_operation", "Unsupported context operation"
        )
    except SkillGatewayError as exc:
        payload = {
            "found": False,
            "subject": subject,
            "operation": operation,
            "reason": exc.code,
            "message": str(exc),
        }
        if operation in {"list", "search"}:
            return json.dumps(payload, separators=(",", ":"))
        return json.dumps(payload)
    except (OSError, ValueError, TypeError):
        return json.dumps(
            {
                "found": False,
                "subject": subject,
                "operation": operation,
                "reason": "context_unavailable",
            }
        )
