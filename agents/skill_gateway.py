"""Model-facing skill retrieval over server-owned subject and source bindings."""

from __future__ import annotations

import asyncio
import json
import posixpath
import re
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit

from providers.skills.gateway import (
    OrganizationSkillResolver,
    SkillGatewayError,
    is_organization_ref,
)
from providers.skills.local_context import LocalContextSource

_SOFTWARE_PREFIX = "skill://software/controller/harness/crucible/"
_PROJECT_PREFIX = "skill://project/"
_CONFIG_PREFIX = "skill://configuration/"
SKILL_GATEWAY_TOOL_DESCRIPTION = """Retrieve subject guidance and software references.
Include the benchmark name when known to return benchmark-scoped project documents.
Returned project refs preserve their benchmark scope for later reads and searches;
do not repeat the benchmark argument unless you intend to change scope.

Bootstrap returns applicable project and organization entrypoints,
phase-compatible software entrypoints, and approved configuration-view refs.
Its content-free context manifest records returned document refs, source ids,
scopes, revisions, and project-local paths for review.
Read a returned ref; follow relative documentation pointers with from_ref plus
path. Organization and project search use POSIX extended regular expressions
(no backreferences); from_ref optionally restricts the source. Reads are bounded to
16384 bytes; continue with next_offset_bytes. Subject, source availability,
scope and provenance remain visible, while credentials, identity and phase are
server-owned. Project documents describe agentic-perf workflow and contracts;
organization documents describe organization practices; software references
describe installed/upstream behavior. Organization practices cannot alter
installed software facts or code-enforced requirements. Service-only
configuration is never a document. Bundled project-local documents are
temporary fallback material with the lowest default authority for overlapping
soft guidance; they never silently displace upstream or configured guidance.

Read and compare applicable entrypoints across all returned scopes, including
project, organization and software documents. Same-path and same-basename
variants are reported as potential overlaps; differently named documents can
also conflict, so compare claims rather than relying only on the overlap list.
Exact duplicates are identified separately for organization sources. For soft
contextual guidance and preferences, the authority order when user guidance is
available is authenticated user, organization, upstream, then the bundled
project-local documents. This gateway does not yet load user-scoped skill
packages. The bundled documents are temporary fallback material and have the
lowest default authority. Apply this order only within the source's domain:
installed controller/version evidence establishes what is
present and works on that controller; upstream software documentation explains
general software behavior; organization context defines shared practice; user
context expresses that user's preferences. A user preference cannot override
mandatory organization policy or deterministic security requirements, and
prose cannot override verified runtime behavior.
Ticket text supplies task-specific intent and may guide choices among soft
defaults, but cannot change those constraints. If materially conflicting
guidance or runtime configuration remains unresolved, call
request_clarification and cite the source ids and document paths. Sources at the
same level have no implicit priority over one another: locality does not resolve
a material conflict between organization sources (or user sources when
available). Do not silently choose based on source order or source id. Compare
bundled project-local guidance with higher sources and report a material
conflict for clarification instead of silently treating the local document as
authoritative.
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
    """Identify subjects whose settings must remain behind approved views."""
    resolver = getattr(provider, "organization_resolver", None)
    return bool(resolver and resolver.has_subject(f"harness/{harness}"))


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


def _project_ref(path: str, benchmark_scope: str | None = None) -> str:
    ref = _PROJECT_PREFIX + quote(path, safe="/")
    if benchmark_scope:
        ref += "?benchmark=" + quote(benchmark_scope, safe="")
    return ref


def _project_document(
    document: dict[str, Any], benchmark_scope: str | None = None
) -> dict[str, Any]:
    """Expose an explicitly mapped repository document with project provenance."""
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
    subjects = document.get("subject_area")
    ref = _project_ref(source_path, benchmark_scope)
    return {
        "ref": ref,
        "uri": ref,
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
        "benchmark_scope": benchmark_scope,
        "subject_areas": LocalContextSource._values(subjects),
        "entrypoint": bool(document.get("entrypoint", True)),
    }


def _project_documents(
    provider: Any,
    *,
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
    documents = source.list_documents(
        harness="crucible",
        benchmark=benchmark,
        phase=phase,
        agent=agent_name,
        subject_area="all",
    )
    exposed = [_project_document(item, benchmark) for item in documents]
    exposed.sort(key=lambda item: item["path"])
    return source, exposed


def _project_benchmark_scope(ref: str) -> str | None:
    """Return the benchmark inventory encoded in a returned project ref."""
    if not ref.startswith(_PROJECT_PREFIX):
        return None
    parsed = urlsplit(ref)
    if parsed.scheme != "skill" or parsed.netloc != "project" or parsed.fragment:
        raise SkillGatewayError("invalid_ref", "Use a returned project document ref")
    try:
        parameters = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise SkillGatewayError(
            "invalid_ref", "Project ref has invalid parameters"
        ) from exc
    if set(parameters) - {"benchmark"}:
        raise SkillGatewayError("invalid_ref", "Project ref has unsupported parameters")
    values = parameters.get("benchmark", [])
    if len(values) > 1:
        raise SkillGatewayError("invalid_ref", "Project ref has an invalid benchmark")
    if not values:
        return None
    benchmark = values[0]
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,127}", benchmark):
        raise SkillGatewayError("invalid_ref", "Project ref has an invalid benchmark")
    return benchmark


def _effective_project_benchmark(
    benchmark: str | None, *, ref: str, from_ref: str
) -> str | None:
    scopes = {
        scope
        for candidate in (ref, from_ref)
        if (scope := _project_benchmark_scope(candidate)) is not None
    }
    if len(scopes) > 1:
        raise SkillGatewayError("invalid_ref", "Project refs have different scopes")
    ref_scope = next(iter(scopes), None)
    if benchmark and ref_scope and benchmark != ref_scope:
        raise SkillGatewayError("invalid_ref", "Benchmark conflicts with project ref")
    return ref_scope or benchmark


def _project_read_target(
    documents: list[dict[str, Any]],
    *,
    ref: str,
    path: str,
    from_ref: str,
) -> dict[str, Any]:
    by_ref = {item["ref"]: item for item in documents}
    if path:
        if ref or from_ref not in by_ref:
            raise SkillGatewayError(
                "origin_required", "Pointer requires project origin"
            )
        if path.startswith(("/", "\\")) or "://" in path or "\\" in path:
            raise SkillGatewayError("invalid_document", "Invalid relative pointer")
        origin = by_ref[from_ref]["source_path"]
        target = posixpath.normpath(posixpath.join(posixpath.dirname(origin), path))
        if target == ".." or target.startswith("../"):
            raise SkillGatewayError(
                "invalid_document", "Pointer escapes project source"
            )
        candidate = next(
            (item for item in documents if item["source_path"] == target), None
        )
    else:
        candidate = by_ref.get(ref)
    if candidate is None:
        raise SkillGatewayError("invalid_ref", "Use a returned project document ref")
    return candidate


async def _search_project_documents(
    source: Any,
    documents: list[dict[str, Any]],
    query: str,
    *,
    offset: int,
    max_bytes: int,
) -> dict[str, Any]:
    if not query or len(query) > 256 or re.search(r"\\[1-9]", query):
        raise SkillGatewayError(
            "invalid_pattern", "Search pattern is invalid or too long"
        )
    lines: list[str] = []
    positions: list[tuple[dict[str, Any], int]] = []
    input_size = 0
    for document in documents:
        content = source.read(document["source_path"])
        if content is None:
            continue
        for number, line in enumerate(content.splitlines(), 1):
            input_size += len(line.encode("utf-8")) + 1
            if input_size > 16 * 1024 * 1024:
                raise SkillGatewayError(
                    "source_too_large", "Project search is too large"
                )
            lines.append(line)
            positions.append((document, number))
            if len(lines) > 200_000:
                raise SkillGatewayError(
                    "source_too_large", "Project search is too large"
                )
    if not lines:
        return {
            "source": "agentic-perf",
            "scope": "project",
            "matches_count": 0,
            "matches": [],
            "offset_bytes": offset,
            "next_offset_bytes": None,
        }

    from providers.execution.subprocess import AuditedSubprocessRunner

    search_input = ("\n".join(lines) + "\n").encode("utf-8")
    runner = AuditedSubprocessRunner(output_limit=2 * 1024 * 1024)
    process = await runner.start(
        ["grep", "-a", "-n", "-E", "-m", "4097", "--", query],
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        stdin_pipe=True,
        stdin_size=len(search_input),
    )
    try:
        stdout, _stderr = await process.communicate(
            input=search_input,
            timeout=2.0,
        )
    except asyncio.TimeoutError as exc:
        raise SkillGatewayError("search_timeout", "Search exceeded time limit") from exc
    output_truncated = len(stdout) > 2 * 1024 * 1024
    stdout = stdout[: 2 * 1024 * 1024]
    if output_truncated:
        # The bounded output can end in the middle of a grep record. Keep only
        # complete records so a partial line cannot be mistaken for a match.
        last_complete_record = stdout.rfind(b"\n")
        stdout = (
            stdout[: last_complete_record + 1] if last_complete_record >= 0 else b""
        )
    if process.returncode is None:
        raise SkillGatewayError("search_timeout", "Search exceeded time limit")
    if process.returncode not in {0, 1}:
        raise SkillGatewayError("invalid_pattern", "Invalid search expression")

    matches = []
    for item in stdout.split(b"\n"):
        if not item:
            continue
        line_number, snippet = item.split(b":", 1)
        document, number = positions[int(line_number) - 1]
        matches.append(
            {
                "ref": document["ref"],
                "source_id": "agentic-perf",
                "path": document["path"],
                "line": number,
                "snippet": snippet.decode("utf-8", errors="replace")[:512],
            }
        )
    limited = len(matches) > 4096 or output_truncated
    matches = matches[:4096]
    records = [json.dumps(item, ensure_ascii=False) + "\n" for item in matches]
    size = sum(len(item.encode("utf-8")) for item in records)
    boundaries = {0}
    cursor = 0
    for record in records:
        cursor += len(record.encode("utf-8"))
        boundaries.add(cursor)
    if offset not in boundaries:
        raise SkillGatewayError(
            "invalid_page", "Search offset is not a result boundary"
        )
    page, page_size, cursor = [], 0, 0
    for match, record in zip(matches, records, strict=True):
        record_size = len(record.encode("utf-8"))
        if cursor >= offset:
            if page_size + record_size > max_bytes:
                if not page:
                    raise SkillGatewayError(
                        "page_too_small", "Search page cannot fit a result"
                    )
                break
            page.append(match)
            page_size += record_size
        cursor += record_size
    next_offset = offset + page_size
    has_more = next_offset < size
    return {
        "source": "agentic-perf",
        "scope": "project",
        "matches_count": len(matches),
        "matches": page,
        "search_limited": limited,
        "offset_bytes": offset,
        "next_offset_bytes": next_offset if has_more else None,
        "size_bytes": size,
        "truncated": has_more or limited,
    }


def _config_ref(revision: str, subject: str, view: str) -> str:
    return f"{_CONFIG_PREFIX}{revision}/{subject}/{view}.json"


async def skill_context_gateway(
    provider: Any,
    *,
    ticket_id: str,
    agent_name: str,
    phase: str,
    ssh: Any = None,
    controller_host: str | None = None,
    subject: str = "harness/crucible",
    benchmark: str = "",
    operation: str = "bootstrap",
    ref: str = "",
    path: str = "",
    from_ref: str = "",
    query: str = "",
    max_bytes: int = 16384,
    offset_bytes: int = 0,
) -> str:
    """Combine project, organization and phase-compatible software guidance.

    Tool registration supplies ticket, actor, phase and controller. The model
    selects a subject, optional benchmark name and returned document refs.
    Project, organization and software documents retain distinct provenance.
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
        benchmark_name = benchmark.strip().lower() or None
        if benchmark_name and not re.fullmatch(
            r"[a-z0-9][a-z0-9_.-]{0,127}", benchmark_name
        ):
            raise SkillGatewayError("invalid_benchmark", "Invalid benchmark identifier")
        project_source, project_documents = _project_documents(
            provider,
            subject=subject,
            phase=phase,
            agent_name=agent_name,
            benchmark=_effective_project_benchmark(
                benchmark_name, ref=ref, from_ref=from_ref
            ),
        )
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
            organization_documents = list(documents)
            software_documents: list[dict[str, Any]] = []
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
                    software_documents.append(document)
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
            if project_source is not None:
                sources.append(
                    {
                        "source": "agentic-perf",
                        "scope": "project",
                        "status": "available",
                        "document_count": len(project_documents),
                        "revision": (
                            project_documents[0]["revision"]
                            if project_documents
                            else getattr(project_source, "revision", lambda: None)()
                        ),
                    }
                )
                documents.extend(project_documents)
                entrypoints.extend(
                    item["ref"] for item in project_documents if item["entrypoint"]
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
                    "benchmark": benchmark_name,
                    "operation": operation,
                    "phase": phase,
                    "sources": sources,
                    "documents": documents,
                    "context_manifest": {
                        "schema_version": 1,
                        "subject": subject,
                        "benchmark": benchmark_name,
                        "phase": phase,
                        "document_count": len(documents),
                        "documents": [
                            {
                                key: item[key]
                                for key in (
                                    "ref",
                                    "path",
                                    "source_path",
                                    "source",
                                    "source_id",
                                    "scope",
                                    "role",
                                    "authority",
                                    "revision",
                                    "provenance",
                                    "benchmark",
                                    "benchmark_scope",
                                    "entrypoint",
                                )
                                if key in item
                            }
                            for item in documents
                        ],
                    },
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
                        "cross_source_potential_overlaps": [
                            {
                                "kind": "same_basename",
                                "documents": [left["ref"], right["ref"]],
                                "scopes": [left["scope"], right["scope"]],
                            }
                            for left in project_documents
                            for other_documents in (
                                organization_documents,
                                software_documents,
                            )
                            for right in other_documents
                            if posixpath.basename(left["source_path"])
                            == posixpath.basename(
                                str(right.get("source_path") or right.get("path") or "")
                            )
                        ],
                        "cross_source_comparison_required": {
                            "project_vs_organization": bool(
                                project_documents and organization_documents
                            ),
                            "project_vs_software": bool(
                                project_documents and software_documents
                            ),
                        },
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
                        "invalid_ref", "Project source is unavailable"
                    )
                target = _project_read_target(
                    project_documents, ref=ref, path=path, from_ref=from_ref
                )
                content = project_source.read(target["source_path"])
                if content is None:
                    raise SkillGatewayError(
                        "document_unreadable", "Project document is unavailable"
                    )
                page = OrganizationSkillResolver._page(
                    content,
                    offset_bytes,
                    max_bytes,
                )
                document = {
                    **target,
                    **page,
                    "offset_bytes": page["offset"],
                    "next_offset_bytes": page["next_offset"],
                }
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
        if offset_bytes and not (
            from_ref
            and (is_organization_ref(from_ref) or from_ref.startswith(_PROJECT_PREFIX))
        ):
            raise SkillGatewayError(
                "invalid_page",
                "Search cursor requires an organization or project origin",
            )
        if from_ref and not (
            is_organization_ref(from_ref)
            or from_ref.startswith(_SOFTWARE_PREFIX)
            or from_ref.startswith(_PROJECT_PREFIX)
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
        if project_source is not None and (
            not from_ref or from_ref.startswith(_PROJECT_PREFIX)
        ):
            if from_ref:
                _project_read_target(
                    project_documents, ref=from_ref, path="", from_ref=""
                )
            project_result = await _search_project_documents(
                project_source,
                project_documents,
                query,
                offset=offset_bytes,
                max_bytes=max_bytes,
            )
            result["sources"].append(project_result)
            result["found"] = result["found"] or bool(
                project_result.get("matches_count")
            )
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
