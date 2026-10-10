"""Model-facing skill retrieval over server-owned subject and source bindings."""

from __future__ import annotations

import json
import posixpath
from typing import Any
from urllib.parse import quote, unquote

from providers.skills.gateway import (
    OrganizationSkillResolver,
    SkillGatewayError,
    is_organization_ref,
)

_SOFTWARE_PREFIX = "skill://software/controller/harness/crucible/"
_CONFIG_PREFIX = "skill://configuration/"
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
duplicates are identified separately. For contextual claims and preferences,
use locality as a default trust signal: upstream context is a baseline,
organization context normally has more weight for environment-specific
practices, and authenticated user context (when available) normally has more
weight for that user's preferences. Apply this only when the source scope fits
the claim. It cannot override mandatory organization policy or verified
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
    """Identify subjects whose settings must remain behind approved views."""
    return provider.organization_resolver.has_subject(f"harness/{harness}")


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
            is_organization_ref(from_ref) or from_ref.startswith(_SOFTWARE_PREFIX)
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
