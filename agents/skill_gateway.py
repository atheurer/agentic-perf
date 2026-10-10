"""Model-facing skill retrieval over server-owned subject and source bindings."""

from __future__ import annotations

import json
import posixpath
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from providers.skills.gateway import (
    OrganizationSkillResolver,
    SkillGatewayError,
    is_organization_ref,
)

_SOFTWARE_PREFIX = "skill://software/controller/harness/crucible/"
_LOCAL_PREFIX = "skill://local/"
_UPSTREAM_PREFIX = "skill://upstream/"
_CONFIG_PREFIX = "skill://configuration/"
_LOCAL_SUBJECT_DIRS = {
    "harness/kube-burner": "kube-burner",
    "resource/aws": "resource/aws",
}
_UPSTREAM_REPOS = {
    "harness/kube-burner": (
        "kube-burner",
        "https://github.com/kube-burner/kube-burner.git",
    ),
}
_MAX_SOURCE_DOCUMENT_BYTES = 1024 * 1024
_MAX_SOURCE_SEARCH_BYTES = 4 * 1024 * 1024
SKILL_GATEWAY_TOOL_DESCRIPTION = """Retrieve subject guidance and software references.

Bootstrap returns applicable organization entrypoints, phase-compatible software
entrypoints, and approved configuration-view refs. Read a returned ref; follow a
relative documentation pointer with from_ref plus path. Organization search uses a POSIX extended regular
expression (no backreferences); from_ref optionally restricts the source. Reads are bounded to
16384 bytes; continue with next_offset_bytes. Subject, source availability and
provenance remain visible, while source paths, credentials, identity and phase
are server-owned. Organization practices cannot alter installed software facts
or code-enforced requirements. Service-only configuration is never a document.

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


def _software_path(ref: str, *, from_ref: str = "", path: str = "") -> str:
    if path:
        if not from_ref.startswith(_SOFTWARE_PREFIX) or ref:
            raise SkillGatewayError(
                "origin_required", "Pointer requires software origin"
            )
        if path.startswith(("/", "\\")) or "://" in path or "\\" in path:
            raise SkillGatewayError("invalid_document", "Invalid relative pointer")
        origin = unquote(from_ref.removeprefix(_SOFTWARE_PREFIX))
        relative = posixpath.normpath(posixpath.join(posixpath.dirname(origin), path))
    elif ref.startswith(_SOFTWARE_PREFIX):
        relative = unquote(ref.removeprefix(_SOFTWARE_PREFIX))
    else:
        raise SkillGatewayError("invalid_ref", "Use a returned software document ref")
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


def _config_ref(revision: str, subject: str, view: str) -> str:
    return f"{_CONFIG_PREFIX}{revision}/{subject}/{view}.json"


def _source_ref(kind: str, subject: str, path: str) -> str:
    prefix = _LOCAL_PREFIX if kind == "local" else _UPSTREAM_PREFIX
    return prefix + quote(subject, safe="/") + "/" + quote(path, safe="/")


def _parse_source_ref(ref: str, kind: str) -> tuple[str, str]:
    prefix = _LOCAL_PREFIX if kind == "local" else _UPSTREAM_PREFIX
    parsed = urlsplit(ref)
    if (
        not ref.startswith(prefix)
        or parsed.scheme != "skill"
        or parsed.netloc != kind
        or parsed.query
        or parsed.fragment
    ):
        raise SkillGatewayError("invalid_ref", "Use a returned source document ref")
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
    return subject, normalized


def _local_base(local_skills_dir: Path | None, subject: str) -> Path | None:
    if local_skills_dir is None or subject not in _LOCAL_SUBJECT_DIRS:
        return None
    root = Path(local_skills_dir).resolve()
    base = (root / _LOCAL_SUBJECT_DIRS[subject]).resolve()
    if not base.is_relative_to(root) or not base.is_dir():
        return None
    return base


def _list_local_documents(
    local_skills_dir: Path | None, subject: str
) -> list[dict[str, Any]]:
    base = _local_base(local_skills_dir, subject)
    if base is None:
        return []
    results: list[dict[str, Any]] = []
    for target in sorted(base.rglob("*.md")):
        try:
            resolved = target.resolve(strict=True)
            if target.is_symlink() or not resolved.is_relative_to(base):
                continue
            if not resolved.is_file():
                continue
            relative = resolved.relative_to(base).as_posix()
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


def _read_local_document(
    local_skills_dir: Path | None,
    subject: str,
    relative: str,
    *,
    offset_bytes: int,
    max_bytes: int,
    full_content: bool = False,
) -> dict[str, Any]:
    base = _local_base(local_skills_dir, subject)
    if base is None:
        raise SkillGatewayError("source_unavailable", "Local subject is unavailable")
    candidate = base / relative
    if any(
        part.is_symlink() for part in [candidate, *candidate.parents] if part != base
    ):
        raise SkillGatewayError("invalid_document", "Invalid local document")
    target = candidate.resolve(strict=True)
    if not target.is_relative_to(base) or not target.is_file():
        raise SkillGatewayError("invalid_document", "Invalid local document")
    if target.suffix.lower() != ".md":
        raise SkillGatewayError("invalid_document", "Only Markdown guidance is exposed")
    if target.stat().st_size > _MAX_SOURCE_DOCUMENT_BYTES:
        raise SkillGatewayError("source_too_large", "Local document exceeds the limit")
    content = target.read_text(encoding="utf-8")
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


def _list_upstream_documents(repo_cache: Any, subject: str) -> list[dict[str, Any]]:
    binding = _UPSTREAM_REPOS.get(subject)
    if not binding or repo_cache is None:
        return []
    repo_name, repo_url = binding
    if repo_cache.get_path(repo_name) is None:
        return []
    results = []
    for item in repo_cache.list_docs(repo_name, subdirs=["docs", "config"]):
        path = item.get("path")
        if not isinstance(path, str):
            continue
        ref = _source_ref("upstream", subject, path)
        results.append(
            {
                "ref": ref,
                "uri": ref,
                "path": path,
                "source": repo_url,
                "scope": "upstream",
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
) -> dict[str, Any]:
    binding = _UPSTREAM_REPOS.get(subject)
    if not binding or repo_cache is None:
        raise SkillGatewayError("source_unavailable", "Upstream cache is unavailable")
    repo_name, repo_url = binding
    root = repo_cache.get_path(repo_name)
    if root is None:
        raise SkillGatewayError("source_unavailable", "Upstream cache is unavailable")
    if not any(relative.startswith(f"{subdir}/") for subdir in ("docs", "config")):
        raise SkillGatewayError("invalid_document", "Invalid upstream document")
    root = Path(root).resolve()
    candidate = root / relative
    if any(
        part.is_symlink() for part in [candidate, *candidate.parents] if part != root
    ):
        raise SkillGatewayError("invalid_document", "Invalid upstream document")
    target = candidate.resolve(strict=True)
    if (
        not target.is_relative_to(root)
        or target.is_symlink()
        or not target.is_file()
        or target.suffix.lower() not in {".md", ".yml", ".yaml"}
    ):
        raise SkillGatewayError("invalid_document", "Invalid upstream document")
    if target.stat().st_size > _MAX_SOURCE_DOCUMENT_BYTES:
        raise SkillGatewayError(
            "source_too_large", "Upstream document exceeds the limit"
        )
    content = target.read_text(encoding="utf-8")
    page = (
        {"content": content, "offset": offset_bytes, "next_offset": None}
        if full_content
        else OrganizationSkillResolver._page(content, offset_bytes, max_bytes)
    )
    ref = _source_ref("upstream", subject, relative)
    return {
        "ref": ref,
        "uri": ref,
        "path": relative,
        "source": repo_url,
        "scope": "upstream",
        "role": "software-reference",
        **page,
        "offset_bytes": page["offset"],
        "next_offset_bytes": page["next_offset"],
    }


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
        if operation not in {"bootstrap", "read", "search"}:
            raise SkillGatewayError(
                "unsupported_operation", "Use bootstrap, read or search"
            )
        if type(max_bytes) is not int or not 4 <= max_bytes <= 16384:
            raise SkillGatewayError("invalid_page", "max_bytes must be 4 through 16384")
        if type(offset_bytes) is not int or offset_bytes < 0:
            raise SkillGatewayError("invalid_page", "offset_bytes must be nonnegative")
        resolver = provider.organization_resolver
        organization = resolver.bootstrap(subject)
        organization.setdefault("scope", "organization")
        if organization.get("status") == "unavailable" and organization.get("required"):
            return json.dumps(
                {
                    "found": False,
                    "subject": subject,
                    "operation": operation,
                    "reason": organization.get("error", "organization_unavailable"),
                    "sources": [organization],
                }
            )
        revision = organization.get("revision", "legacy")
        org_available = organization.get("status") == "available"
        software_allowed = subject == "harness/crucible" and phase in {
            "benchmark",
            "review",
        }
        from agents.server_utils import controller_context_gateway

        if operation == "bootstrap":
            sources = [organization]
            documents = [
                {**item, "scope": "organization", "role": "operational-guidance"}
                for item in organization.get("documents", [])
            ]
            entrypoints = list(organization.get("entrypoints", []))
            if subject in _LOCAL_SUBJECT_DIRS:
                local_documents = _list_local_documents(local_skills_dir, subject)
                sources.append(
                    {
                        "source": "agentic-perf",
                        "scope": "local",
                        "status": "available" if local_documents else "unavailable",
                        "document_count": len(local_documents),
                    }
                )
                documents.extend(local_documents)
                entrypoints.extend(item["ref"] for item in local_documents)
                upstream_documents = _list_upstream_documents(repo_cache, subject)
                upstream = _UPSTREAM_REPOS.get(subject)
                if upstream:
                    sources.append(
                        {
                            "source": upstream[1],
                            "scope": "upstream",
                            "status": "available"
                            if upstream_documents
                            else "unavailable",
                            "document_count": len(upstream_documents),
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
                    "configuration_views": views,
                    "context_conflicts": {
                        "potential_document_conflicts": [
                            item
                            for item in organization.get("overlaps", [])
                            if not item.get("same_content", False)
                        ],
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
            if ref.startswith(_LOCAL_PREFIX) or from_ref.startswith(_LOCAL_PREFIX):
                if path and (ref or not from_ref):
                    raise SkillGatewayError(
                        "invalid_document", "Use origin and relative pointer"
                    )
                source_subject, relative = _parse_source_ref(
                    from_ref if path else ref, "local"
                )
                if source_subject != subject:
                    raise SkillGatewayError("invalid_ref", "Subject does not match ref")
                if path:
                    if path.startswith(("/", "\\")) or "\\" in path:
                        raise SkillGatewayError("invalid_document", "Invalid pointer")
                    relative = posixpath.normpath(
                        posixpath.join(posixpath.dirname(relative), path)
                    )
                    if relative == ".." or relative.startswith("../"):
                        raise SkillGatewayError("invalid_document", "Invalid pointer")
                document = _read_local_document(
                    local_skills_dir,
                    subject,
                    relative,
                    offset_bytes=offset_bytes,
                    max_bytes=max_bytes,
                )
                return json.dumps({"found": True, "document": document})
            if ref.startswith(_UPSTREAM_PREFIX) or from_ref.startswith(
                _UPSTREAM_PREFIX
            ):
                if path and (ref or not from_ref):
                    raise SkillGatewayError(
                        "invalid_document", "Use origin and relative pointer"
                    )
                source_subject, relative = _parse_source_ref(
                    from_ref if path else ref, "upstream"
                )
                if source_subject != subject:
                    raise SkillGatewayError("invalid_ref", "Subject does not match ref")
                if path:
                    if path.startswith(("/", "\\")) or "\\" in path:
                        raise SkillGatewayError("invalid_document", "Invalid pointer")
                    relative = posixpath.normpath(
                        posixpath.join(posixpath.dirname(relative), path)
                    )
                    if relative == ".." or relative.startswith("../"):
                        raise SkillGatewayError("invalid_document", "Invalid pointer")
                document = _read_upstream_document(
                    repo_cache,
                    subject,
                    relative,
                    offset_bytes=offset_bytes,
                    max_bytes=max_bytes,
                )
                return json.dumps({"found": True, "document": document})
            if not software_allowed:
                raise SkillGatewayError("invalid_ref", "Source unavailable in phase")
            relative = _software_path(ref, from_ref=from_ref, path=path)
            result = json.loads(
                await controller_context_gateway(
                    ssh=ssh,
                    controller_host=controller_host,
                    ticket_id=ticket_id,
                    agent_name=agent_name,
                    phase=phase,
                    operation="read",
                    path=relative,
                    max_bytes=max_bytes,
                    offset_bytes=offset_bytes,
                )
            )
            if isinstance(result.get("document"), dict):
                result["document"] = _software_document(result["document"])
                result["documents"] = [result["document"]]
            return json.dumps(result)
        if ref or path:
            raise SkillGatewayError("invalid_search", "Scope search with from_ref")
        if offset_bytes and (not from_ref or not is_organization_ref(from_ref)):
            raise SkillGatewayError(
                "invalid_page", "Search cursor requires organization origin"
            )
        if from_ref and not (
            is_organization_ref(from_ref)
            or from_ref.startswith(_SOFTWARE_PREFIX)
            or from_ref.startswith(_LOCAL_PREFIX)
            or from_ref.startswith(_UPSTREAM_PREFIX)
        ):
            raise SkillGatewayError("invalid_origin", "Use a returned source origin")
        result = {
            "found": False,
            "subject": subject,
            "operation": "search",
            "sources": [],
        }
        if org_available and (not from_ref or is_organization_ref(from_ref)):
            if from_ref:
                resolver.read(subject, from_ref, max_bytes=16384)
            org_result = resolver.search(
                subject,
                None,
                query,
                from_ref=from_ref or None,
                offset=offset_bytes,
                max_bytes=max_bytes,
            )
            org_result["next_offset_bytes"] = org_result.get(
                "next_offset_bytes", org_result.get("next_offset")
            )
            result["sources"].append(org_result)
            result["found"] = bool(org_result.get("matches_count"))
        if software_allowed and (not from_ref or from_ref.startswith(_SOFTWARE_PREFIX)):
            if from_ref:
                _software_path(from_ref)
            software = json.loads(
                await controller_context_gateway(
                    ssh=ssh,
                    controller_host=controller_host,
                    ticket_id=ticket_id,
                    agent_name=agent_name,
                    phase=phase,
                    operation="search",
                    query=query,
                    max_bytes=max_bytes,
                )
            )
            software.update(
                {
                    "source": "controller",
                    "scope": "software",
                    "pagination": "unsupported",
                }
            )
            software["results"] = [
                _software_document(item) for item in software.get("results", [])
            ]
            result["sources"].append(software)
            result["found"] = result["found"] or software.get("found", False)
        if subject in _LOCAL_SUBJECT_DIRS and (
            not from_ref
            or from_ref.startswith(_LOCAL_PREFIX)
            or from_ref.startswith(_UPSTREAM_PREFIX)
        ):
            source_kind = (
                "local"
                if not from_ref or from_ref.startswith(_LOCAL_PREFIX)
                else "upstream"
            )
            source_documents = (
                _list_local_documents(local_skills_dir, subject)
                if source_kind == "local"
                else _list_upstream_documents(repo_cache, subject)
            )
            if from_ref:
                origin_subject, _origin_path = _parse_source_ref(from_ref, source_kind)
                if origin_subject != subject:
                    raise SkillGatewayError(
                        "invalid_origin", "Subject does not match source ref"
                    )
                source_documents = [
                    item for item in source_documents if item["ref"] == from_ref
                ]
            alternatives = [
                term.casefold().strip() for term in query.split("|") if term.strip()
            ]
            matches = []
            scanned_bytes = 0
            for item in source_documents:
                size_bytes = item.get("size_bytes", 0)
                if not isinstance(size_bytes, int) or size_bytes < 0:
                    continue
                if scanned_bytes + size_bytes > _MAX_SOURCE_SEARCH_BYTES:
                    break
                scanned_bytes += size_bytes
                try:
                    document_result = (
                        _read_local_document(
                            local_skills_dir,
                            subject,
                            item["path"],
                            offset_bytes=0,
                            max_bytes=16384,
                            full_content=True,
                        )
                        if source_kind == "local"
                        else _read_upstream_document(
                            repo_cache,
                            subject,
                            item["path"],
                            offset_bytes=0,
                            max_bytes=16384,
                            full_content=True,
                        )
                    )
                    content = document_result.get("content", "")
                    if not alternatives or any(
                        alternative in content.casefold()
                        for alternative in alternatives
                    ):
                        matches.append(
                            {key: value for key, value in item.items() if key != "uri"}
                        )
                except (OSError, ValueError, TypeError):
                    continue
                if len(matches) >= 100:
                    break
            result["sources"].append(
                {
                    "source": "agentic-perf"
                    if source_kind == "local"
                    else _UPSTREAM_REPOS[subject][1],
                    "scope": source_kind,
                    "found": bool(matches),
                    "matches_count": len(matches),
                    "results": matches,
                    "pagination": "bounded",
                    "query_mode": "literal-alternatives",
                    "scanned_bytes": scanned_bytes,
                }
            )
            result["found"] = result["found"] or bool(matches)
        return json.dumps(result)
    except SkillGatewayError as exc:
        return json.dumps(
            {
                "found": False,
                "subject": subject,
                "operation": operation,
                "reason": exc.code,
            }
        )
    except (OSError, ValueError, TypeError):
        return json.dumps(
            {
                "found": False,
                "subject": subject,
                "operation": operation,
                "reason": "context_unavailable",
            }
        )
