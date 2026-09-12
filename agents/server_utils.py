"""Shared utilities for agent MCP servers.

All agent MCP servers run as subprocesses and need to set up the Python
path, construct providers, and resolve SSH credentials from tickets.
This module centralizes that setup to avoid duplication.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import sys
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def setup_project_path() -> str:
    """Add the project root to sys.path. Returns the project root path."""
    root = str(Path(__file__).resolve().parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)
    return root


def build_skill_provider(
    source_repo: str | None = None,
    *,
    crucible_home: str | None = None,
    repo_cache=None,
    source_url: str | None = None,
    zathras_home: str | None = None,
    resolve_source: bool = True,
    catalog_only: bool = False,
):
    """Construct a MultiHarnessSkillProvider from environment variables.

    Reads CRUCIBLE_HOME and ZATHRAS_HOME from env vars.
    """
    from providers.skills.arcaflow_plugins import ArcaflowPluginSkillProvider
    from providers.skills.benchmark_runner import BenchmarkRunnerSkillProvider
    from providers.skills.clusterbuster import ClusterbusterSkillProvider
    from providers.skills.crucible import CrucibleCatalogSkillProvider
    from providers.skills.forge import ForgeSkillProvider
    from providers.skills.ioscale import IoscaleSkillProvider
    from providers.skills.k8s_netperf import K8sNetperfSkillProvider
    from providers.skills.kube_burner import KubeBurnerSkillProvider
    from providers.skills.multi import MultiHarnessSkillProvider
    from providers.skills.private import PrivateSkillProvider
    from providers.skills.vstorm import VstormSkillProvider
    from providers.skills.zathras import ZathrasSkillProvider

    zathras_home = (
        zathras_home if zathras_home is not None else os.environ.get("ZATHRAS_HOME", "")
    )

    harnesses: dict[str, Any] = {
        "kube-burner": KubeBurnerSkillProvider(),
        "k8s-netperf": K8sNetperfSkillProvider(),
        "benchmark-runner": BenchmarkRunnerSkillProvider(),
        "clusterbuster": ClusterbusterSkillProvider(),
        "vstorm": VstormSkillProvider(),
        "ioscale": IoscaleSkillProvider(),
        "forge": ForgeSkillProvider(),
        "arcaflow-plugins": ArcaflowPluginSkillProvider(),
    }

    if catalog_only:
        harnesses["crucible"] = CrucibleCatalogSkillProvider(
            build_crucible_context_gateway(catalog_only=True)
        )

    if zathras_home:
        harnesses["zathras"] = ZathrasSkillProvider(zathras_home)
    else:
        private = PrivateSkillProvider()
        zathras_tests = private._load_config("zathras").get("tests")
        if zathras_tests:
            harnesses["zathras"] = ZathrasSkillProvider(fallback_tests=zathras_tests)

    return MultiHarnessSkillProvider(
        harnesses, PrivateSkillProvider(), default_harness="crucible"
    )


def build_crucible_context_gateway(
    *,
    crucible_home: str | None = None,
    repo_cache=None,
    source_repo: str | None = None,
    source_url: str | None = None,
    resolve_source: bool = False,
    catalog_only: bool = False,
):
    """Build the internal Crucible context/catalog adapter.

    Crucible is intentionally not registered in the general SkillProvider
    aggregate.  Benchmark and review agents retrieve Crucible metadata through
    the controller-backed context gateway; triage may use this adapter only for
    its minimal catalog discovery.
    """
    from providers.skills.crucible import CrucibleContextGateway
    from providers.skills.repo_cache import RepoCache

    home = crucible_home or os.environ.get("CRUCIBLE_HOME", "/opt/crucible")
    cache = repo_cache or RepoCache()
    if resolve_source:
        logger.warning(
            "resolve_source is ignored; Crucible repositories are not cloned"
        )
    return CrucibleContextGateway(
        home,
        source_repo=source_repo,
        repo_cache=cache,
        catalog_only=catalog_only,
    )


_CONTEXT_PRIVATE_KEYS = {
    "source",
    "sources",
    "effective_source",
    "source_reason",
    "source_assumption",
    "provenance",
    "workspace_ref",
    "workspace_refs",
    "alternate_refs",
}


def _public_context_document(document: dict[str, Any]) -> dict[str, Any]:
    """Remove source and workspace implementation details from a document."""
    public = {
        key: value
        for key, value in document.items()
        if key not in _CONTEXT_PRIVATE_KEYS
    }
    public.pop("source_path", None)
    return public


def _context_manifest(
    documents: list[dict[str, Any]],
    *,
    phase: str,
    audience: str,
    benchmark: str,
    namespace: str,
    subject_area: str | list[str],
) -> dict[str, Any]:
    """Build the model-facing, source-neutral context inventory."""
    inventory = []
    for document in documents:
        public = _public_context_document(document)
        public.pop("content", None)
        inventory.append(public)
    inventory.sort(key=lambda item: item.get("ref", item.get("path", "")))
    return {
        "schema_version": 1,
        "phase": phase,
        "audience": audience,
        "benchmark": benchmark,
        "namespace": namespace,
        "subject_area": subject_area,
        "document_count": len(inventory),
        "documents": inventory,
    }


def _public_context_result(
    result: dict[str, Any],
    *,
    phase: str,
    audience: str,
    benchmark: str,
    namespace: str,
    subject_area: str | list[str],
    manifest_documents: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a context result without source-selection implementation data."""
    public = dict(result)
    public.pop("effective_source", None)
    public.pop("source", None)
    public.pop("sources_considered", None)
    public.pop("provenance", None)
    public.pop("selection", None)
    public.pop("workspace", None)
    if isinstance(public.get("documents"), list):
        public["documents"] = [
            _public_context_document(item)
            for item in public["documents"]
            if isinstance(item, dict)
        ]
    if isinstance(public.get("document"), dict):
        public["document"] = _public_context_document(public["document"])
    if isinstance(public.get("results"), list):
        public["results"] = [
            _public_context_document(item) if isinstance(item, dict) else item
            for item in public["results"]
        ]
    documents = manifest_documents or result.get("documents", [])
    if not documents and isinstance(result.get("context"), dict):
        documents = [
            {
                "ref": f"benchmark/{benchmark}/{path}",
                "path": f"benchmark/{benchmark}/{path}",
                "namespace": f"benchmark/{benchmark}",
            }
            for path in result["context"]
        ]
    public["context_manifest"] = _context_manifest(
        documents,
        phase=phase,
        audience=audience,
        benchmark=benchmark,
        namespace=namespace,
        subject_area=subject_area,
    )
    return public


def emit_private_tool_audit_event(
    ticket_id: str,
    *,
    agent_name: str,
    tool_name: str,
    data: dict[str, Any],
    event_type: str = "tool_audit",
) -> None:
    """Record private tool diagnostics outside the MCP response.

    Local MCP tools can use this for structured diagnostics that must be
    available to operators but must not become model context. The payload is
    redacted and written through the canonical trace store.
    """
    if not ticket_id:
        return
    import json as _json

    from paths import TRACE_DB_PATH
    from providers.event_projection import legacy_to_trace
    from state_store.trace_store import TraceStore, TraceStoreWriteError

    redactor = _get_progress_redactor()
    payload = redactor.redact_string(ticket_id, _json.dumps(data, default=str))
    try:
        with TraceStore(TRACE_DB_PATH) as trace_store:
            trace_store.insert_event(
                legacy_to_trace(
                    ticket_id,
                    agent_name,
                    event_type,
                    {"tool": tool_name, "details": _json.loads(payload)},
                )
            )
    except (OSError, ValueError, TraceStoreWriteError):
        logger.debug(
            "Failed to write context audit event for %s", ticket_id, exc_info=True
        )


def _emit_context_audit_event(
    ticket_id: str,
    *,
    agent_name: str,
    phase: str,
    benchmark: str,
    operation: str,
    namespace: str,
    result: dict[str, Any],
) -> None:
    """Record private Crucible context resolution details."""
    data = {
        "benchmark": benchmark,
        "phase": phase,
        "operation": operation,
        "namespace": namespace,
        "effective_source": result.get("effective_source"),
        "source": result.get("source"),
        "source_reason": result.get("source_reason"),
        "source_assumption": result.get("source_assumption"),
        "selection": result.get("selection"),
        "sources_considered": result.get("sources_considered"),
        "provenance": result.get("provenance"),
        "workspace_policy": result.get("workspace_policy"),
        "inventory": result.get("inventory"),
    }
    emit_private_tool_audit_event(
        ticket_id,
        agent_name=agent_name,
        tool_name="get_crucible_benchmark_context",
        event_type="context_resolution",
        data={key: value for key, value in data.items() if value is not None},
    )


def ticket_controller_host(ticket: dict[str, Any]) -> str | None:
    """Return the explicitly assigned Crucible controller from ticket data."""
    fields = ticket.get("custom_fields", {}) if isinstance(ticket, dict) else {}
    context = fields.get("crucible_controller_context")
    if isinstance(context, dict):
        for key in ("host", "controller"):
            value = context.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    assigned = fields.get("assigned_hardware_ips")
    if isinstance(assigned, dict):
        value = assigned.get("controller")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _controller_relative_path(path: str) -> str | None:
    """Normalize a path learned from controller documentation.

    This intentionally does not classify or infer Crucible repositories.  It
    only keeps reads below the installed controller root and excludes obvious
    credential or VCS internals.
    """
    value = str(path or "").strip().removeprefix("crucible://")
    value = value.removeprefix("/opt/crucible/")
    if not value or value.startswith("/"):
        return None
    candidate = Path(value)
    if any(
        part in {"", ".", ".."} or part.startswith(".git") for part in candidate.parts
    ):
        return None
    lowered = value.lower()
    if any(
        marker in lowered
        for marker in (
            "/secrets/",
            "/.ssh/",
            "authorized_keys",
            ".pem",
            ".key",
            "token",
        )
    ):
        return None
    return candidate.as_posix()


async def _search_controller_source(
    *,
    ssh: Any,
    controller_host: str,
    query: str,
    max_results: int = 100,
    max_bytes: int = 65536,
) -> dict[str, Any]:
    """Find candidate documents in the installed controller tree.

    This is discovery, not document resolution. The agent receives
    controller-relative paths and short matching lines, then decides what to
    read. The scan is bounded and excludes repository and credential internals.
    """
    if not query.strip():
        return {"found": False, "operation": "search", "reason": "empty_query"}
    max_results = max(1, min(int(max_results), 100))
    max_bytes = max(1024, min(int(max_bytes), 131072))
    find_pattern = shlex.quote(f".*({query}).*")
    command = (
        "{ "
        "find /opt/crucible -regextype posix-extended "
        "\\( -path '*/.git' -o -path '*/.ssh' -o -path '*/secrets' \\) -prune -o "
        f"\\( -type f -o -type d \\) -regex {find_pattern} "
        "-printf 'NAME\\t%y\\t%p\\n' 2>/dev/null; "
        "grep -rInE --binary-files=without-match "
        "--exclude-dir=.git --exclude-dir=.ssh --exclude-dir=secrets "
        "--exclude='*.pem' --exclude='*.key' --exclude='authorized_keys' "
        f"-- {shlex.quote(query)} /opt/crucible 2>/dev/null "
        "| sed 's/^/CONTENT\\t/'; "
        f"}} | head -n {max_results}"
    )
    result = await ssh.run(controller_host, command, timeout=30)
    raw_output = result.stdout.encode("utf-8", errors="replace")
    grouped: dict[str, dict[str, Any]] = {}
    total_matches = 0
    for line in raw_output[:max_bytes].decode("utf-8", errors="replace").splitlines():
        fields = line.split("\t", 3)
        if not fields:
            continue
        kind = fields[0]
        if kind == "NAME":
            if len(fields) != 3:
                continue
            file_type, value = fields[1:]
            raw_path = value
            line_number = ""
            content = ""
        elif kind == "CONTENT":
            if len(fields) != 2:
                continue
            file_type = "f"
            value = fields[1]
            content_fields = value.split(":", 2)
            if len(content_fields) != 3:
                continue
            raw_path, line_number, content = content_fields
        else:
            continue
        if not raw_path.startswith("/opt/crucible/"):
            continue
        relative = _controller_relative_path(raw_path)
        if not relative:
            continue
        total_matches += 1
        entry = grouped.setdefault(
            relative,
            {
                "ref": relative,
                "uri": f"crucible://{relative}",
                "type": "directory" if file_type == "d" else "file",
                "match_kinds": [],
                "matches": [],
            },
        )
        match_kind = "name" if kind == "NAME" else "content"
        if match_kind not in entry["match_kinds"]:
            entry["match_kinds"].append(match_kind)
        entry["matches"].append(
            {
                "kind": match_kind,
                "line_number": int(line_number) if line_number.isdigit() else None,
                "content": content[:1000] if content else None,
            }
        )
    files = list(grouped.values())
    for entry in files:
        entry["match_count"] = len(entry["matches"])
    return {
        "found": bool(files),
        "operation": "search",
        "query": query,
        "namespace": "controller",
        "results": files,
        "total_files": len(files),
        "total_matches": total_matches,
        "truncated": total_matches >= max_results or len(raw_output) > max_bytes,
    }


async def _read_controller_document(
    *,
    ssh: Any,
    controller_host: str,
    relative: str,
    max_bytes: int = 262144,
) -> Any:
    """Read a controller document only after resolving symlinks safely.

    The lexical path checks above prevent traversal syntax, but do not stop a
    path inside /opt/crucible from being a symlink to another tree.  Resolve
    both the installation root and the requested target on the controller and
    require the real target to remain below the real installation root.
    """
    remote_path = f"/opt/crucible/{relative}"
    quoted_path = shlex.quote(remote_path)
    command = (
        "root=$(realpath -e -- /opt/crucible) && "
        f"candidate=$(realpath -e -- {quoted_path}) && "
        'case "$candidate" in "$root"/*) '
        f'test -f "$candidate" && head -c {max_bytes} "$candidate";; '
        "*) exit 2;; esac"
    )
    return await ssh.run(controller_host, command, timeout=30)


async def controller_context_gateway(
    *,
    ssh: Any,
    controller_host: str | None,
    ticket_id: str,
    agent_name: str,
    phase: str,
    benchmark: str = "",
    operation: str = "read",
    path: str = "",
    query: str = "",
    include_alternates: bool = False,
) -> str:
    """Read controller context by following paths supplied by AGENTS.md.

    The controller filesystem is the source of truth.  This function does not
    parse repos.json, resolve benchmark repositories, or construct a document
    inventory.  Agents bootstrap with AGENTS.md and then request the paths it
    points to, just as a coding agent would.
    """
    from providers.workspace.manager import WorkspaceManager

    manager = WorkspaceManager(ticket_id=ticket_id, agent_name=agent_name, phase=phase)
    if operation == "bootstrap":
        path = "AGENTS.md"
    if operation == "read":
        cached = manager.read_document(path, include_alternates=include_alternates)
        if cached.get("status") == "ok":
            result = {
                "found": True,
                "operation": operation,
                "document": cached,
                "documents": [cached],
            }
            return json.dumps(
                _public_context_result(
                    result,
                    phase=phase,
                    audience=manager.audience,
                    benchmark=benchmark,
                    namespace="controller",
                    subject_area="all",
                )
            )
    if operation == "list":
        documents = manager.context_manifest("controller").get("documents", [])
        result = {
            "found": bool(documents),
            "operation": operation,
            "documents": documents,
        }
        return json.dumps(
            _public_context_result(
                result,
                phase=phase,
                audience=manager.audience,
                benchmark=benchmark,
                namespace="controller",
                subject_area="all",
                manifest_documents=documents,
            )
        )
    relative = _controller_relative_path(path)
    if not controller_host or ssh is None:
        return json.dumps(
            {
                "found": False,
                "operation": operation,
                "reason": "controller_not_identified",
            }
        )
    if operation in {"bootstrap", "read"}:
        if not relative:
            return json.dumps(
                {
                    "found": False,
                    "operation": operation,
                    "reason": "invalid_controller_path",
                    "path": path,
                }
            )
        result = await _read_controller_document(
            ssh=ssh,
            controller_host=controller_host,
            relative=relative,
        )
        if result.exit_code != 0:
            return json.dumps(
                {
                    "found": False,
                    "operation": operation,
                    "reason": "controller_document_not_found",
                    "path": relative,
                }
            )
        content = result.stdout
        provenance = {
            "effective_source": "controller",
            "controller": controller_host,
            "path": relative,
        }
        saved = manager.save_source_snapshot(
            "controller", provenance, {relative: content}
        )
        document = {
            "namespace": "controller",
            "path": relative,
            "ref": relative,
            "uri": f"crucible://{relative}",
            "source_path": relative,
            "source": "controller",
            "authority": "effective",
            "provenance": provenance,
            "entrypoint": relative == "AGENTS.md",
            "content": content,
            "workspace_ref": saved.get("files", {}).get(relative),
        }
        manager.index_context_documents([document])
        manager.save_effective_context(
            {
                "schema_version": 1,
                "policy": "controller_agent_directed",
                "namespace": "controller",
                "documents": [_public_context_document(document)],
            }
        )
        result = {
            "found": True,
            "operation": operation,
            "document": document,
            "documents": [document],
        }
    elif operation == "search":
        result = await _search_controller_source(
            ssh=ssh,
            controller_host=controller_host,
            query=query,
        )
    else:
        result = {
            "found": False,
            "operation": operation,
            "reason": "unsupported_operation",
        }
    _emit_context_audit_event(
        ticket_id,
        agent_name=agent_name,
        phase=phase,
        benchmark=benchmark,
        operation=operation,
        namespace="controller",
        result=result,
    )
    return json.dumps(
        _public_context_result(
            result,
            phase=phase,
            audience=manager.audience,
            benchmark=benchmark,
            namespace="controller",
            subject_area="all",
        )
    )


async def crucible_context_gateway(
    skill_provider: Any,
    *,
    ticket_id: str = "",
    agent_name: str,
    phase: str,
    benchmark: str = "",
    operation: str = "list",
    namespace: str = "all",
    path: str = "",
    subject_area: str | list[str] = "all",
    include_alternates: bool = False,
    query: str = "",
) -> str:
    """Expose and persist the phase-owned Crucible context gateway.

    Identity is supplied by the server registration, not the LLM.  The
    workspace manager stamps phase/audience and the gateway's source policy
    supplies provenance.
    """
    provider = (
        skill_provider.get_provider("crucible")
        if hasattr(skill_provider, "get_provider")
        else skill_provider
    )
    if provider is None or not hasattr(provider, "get_crucible_context"):
        return json.dumps({"found": False, "reason": "crucible_gateway_unavailable"})
    manager = None
    if ticket_id:
        from providers.workspace.manager import WorkspaceManager

        manager = WorkspaceManager(
            ticket_id=ticket_id, agent_name=agent_name, phase=phase
        )
        if operation == "bootstrap":
            cached = manager.read_document(
                "core/AGENTS.md", include_alternates=include_alternates
            )
            if cached.get("status") == "ok":
                return json.dumps(
                    _public_context_result(
                        {
                            "found": True,
                            "operation": "bootstrap",
                            "document": cached,
                            "documents": [cached],
                        },
                        phase=phase,
                        audience=manager.audience,
                        benchmark=benchmark,
                        namespace="core",
                        subject_area="all",
                    )
                )
            return json.dumps(
                {
                    "found": False,
                    "operation": "bootstrap",
                    "reason": "controller_bootstrap_not_available_in_workspace",
                }
            )
        if operation == "read" and path:
            cached = manager.read_document(path, include_alternates=include_alternates)
            if cached.get("status") == "ok":
                return json.dumps(
                    _public_context_result(
                        {
                            "found": True,
                            "operation": "read",
                            "document": cached,
                            "documents": [cached],
                        },
                        phase=phase,
                        audience=manager.audience,
                        benchmark=benchmark,
                        namespace=namespace,
                        subject_area=subject_area,
                    )
                )
        if operation == "search" and manager.context_scope_indexed(namespace):
            return json.dumps(
                _public_context_result(
                    manager.search_documents(
                        query,
                        namespace=namespace if namespace != "all" else "",
                        include_alternates=include_alternates,
                    ),
                    phase=phase,
                    audience=manager.audience,
                    benchmark=benchmark,
                    namespace=namespace,
                    subject_area=subject_area,
                    manifest_documents=manager.context_manifest(namespace).get(
                        "documents", []
                    ),
                )
            )
    requested_operation = operation
    provider_operation = "list" if operation == "search" else operation
    result = await provider.get_crucible_context(
        benchmark or None,
        operation=provider_operation,
        namespace=namespace,
        path=path,
        subject_area=subject_area,
        include_alternates=include_alternates,
        query=query,
        phase=phase,
        agent=agent_name,
        include_content=True,
    )
    if ticket_id and result.get("found"):
        assert manager is not None
        indexed_documents: list[dict[str, Any]] = []
        grouped_files: dict[tuple[str, str | None], dict[str, str]] = {}
        for document in result.get("documents", []):
            content = document.get("content")
            if content is None:
                read_result = await provider.get_crucible_context(
                    benchmark or None,
                    operation="read",
                    namespace=document["namespace"],
                    path=document.get("ref", document["path"]),
                    subject_area="all",
                    include_alternates=include_alternates,
                    phase=phase,
                    agent=agent_name,
                )
                read_document = read_result.get("document")
                if isinstance(read_document, dict):
                    content = read_document.get("content")
            if content is None:
                continue
            document_source = document.get(
                "source", result.get("effective_source", "github")
            )
            document_benchmark = document.get("benchmark")
            doc_namespace = document.get("namespace", "")
            if doc_namespace.startswith("benchmark/"):
                _, document_benchmark = doc_namespace.split("/", 1)
            group = (document_source, document_benchmark)
            grouped_files.setdefault(group, {})[document["source_path"]] = content

        refs: list[str] = []
        saved_groups: dict[tuple[str, str | None], dict[str, Any]] = {}
        for (source, document_benchmark), files in grouped_files.items():
            provenance = next(
                (
                    item.get("provenance", {})
                    for item in result.get("documents", [])
                    if item.get("source", result.get("effective_source")) == source
                    and (
                        item.get("namespace") == f"benchmark/{document_benchmark}"
                        if document_benchmark
                        else not str(item.get("namespace", "")).startswith("benchmark/")
                    )
                ),
                {},
            )
            saved = manager.save_source_snapshot(
                source, provenance, files, benchmark=document_benchmark
            )
            saved_groups[(source, document_benchmark)] = saved
            refs.extend(saved["files"].values())

        for document in result.get("documents", []):
            source = document.get("source", result.get("effective_source", "github"))
            document_benchmark = document.get("benchmark")
            if str(document.get("namespace", "")).startswith("benchmark/"):
                _, document_benchmark = document["namespace"].split("/", 1)
            saved = saved_groups.get((source, document_benchmark))
            workspace_ref = (saved or {}).get("files", {}).get(document["source_path"])
            if not workspace_ref:
                continue
            indexed = dict(document)
            indexed["workspace_ref"] = workspace_ref
            indexed_documents.append(indexed)
            document["workspace_ref"] = workspace_ref

        index_ref = manager.index_context_documents(indexed_documents)
        previous = manager.read_effective_context() or {}
        if previous.get("phase") == phase and previous.get(
            "effective_source"
        ) == result.get("effective_source"):
            refs = list(dict.fromkeys(previous.get("workspace_refs", []) + refs))
        result["workspace"] = {
            "effective_context": manager.save_effective_context(
                {
                    "schema_version": 1,
                    "policy": "phase_owned_source_with_local_supplements",
                    "namespace": namespace,
                    "subject_area": subject_area,
                    "documents": [
                        _public_context_document(document)
                        for document in result.get("documents", [])
                    ],
                }
            ),
            "effective_source": result.get("effective_source"),
            "document_index": index_ref,
        }
        if requested_operation == "read":
            workspace_document = manager.read_document(
                path, include_alternates=include_alternates
            )
            if workspace_document.get("status") == "ok":
                metadata = next(
                    (
                        item
                        for item in result.get("documents", [])
                        if item.get("workspace_ref")
                        == workspace_document.get("workspace_ref")
                    ),
                    {},
                )
                result["document"] = {
                    **metadata,
                    "content": workspace_document["content"],
                }
        elif requested_operation == "search":
            result = {
                **manager.search_documents(
                    query,
                    namespace=namespace if namespace != "all" else "",
                    include_alternates=include_alternates,
                ),
                "workspace": result["workspace"],
                "effective_source": result.get("effective_source"),
            }
        elif requested_operation == "list":
            for document in result.get("documents", []):
                document.pop("content", None)
    _emit_context_audit_event(
        ticket_id,
        agent_name=agent_name,
        phase=phase,
        benchmark=benchmark,
        operation=requested_operation,
        namespace=namespace,
        result=result,
    )
    return json.dumps(
        _public_context_result(
            result,
            phase=phase,
            audience=manager.audience if manager else agent_name,
            benchmark=benchmark,
            namespace=namespace,
            subject_area=subject_area,
        )
    )


def build_secrets_provider():
    """Construct a SecretsProvider from environment and config.

    Builds a local provider from env vars, then wraps it in a cascade
    with a vault layer when Bitwarden Secrets Manager is configured
    in ``~/.agentic-perf/config.json``.
    """
    from providers.secrets.factory import create_secrets_provider

    backend = os.environ.get("SECRETS_BACKEND", "local")
    config: dict[str, Any] = {}
    secrets_path = os.environ.get("SECRETS_PATH")
    if secrets_path:
        config["path"] = secrets_path
    local = create_secrets_provider(backend, **config)

    vault_config = _load_vault_config()
    bw_config = (vault_config or {}).get("bitwarden", {})
    shared_project_id = bw_config.get("shared_project_id")
    if shared_project_id and bw_config.get("organization_id"):
        try:
            from providers.secrets.cascade import CascadingSecretsProvider
            from providers.secrets.factory import create_bitwarden_provider

            vault = create_bitwarden_provider(
                organization_id=bw_config["organization_id"],
                project_id=shared_project_id,
                server_url=bw_config.get("server_url"),
                cache_ttl_seconds=bw_config.get("cache_ttl_seconds", 60),
            )
            return CascadingSecretsProvider(
                [
                    ("shared", local),
                    ("vault:shared", vault),
                ]
            )
        except ImportError:
            logger.info(
                "bitwarden-sdk not installed; using local secrets only",
            )

    return local


def _load_vault_config() -> dict | None:
    """Load vault config from ``~/.agentic-perf/config.json``."""
    import json

    from paths import CONFIG_PATH

    if not CONFIG_PATH.exists():
        return None
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return cfg.get("secrets")
    except (json.JSONDecodeError, OSError):
        return None


def _resolve_vault_secret_name(
    ticket_fields: dict[str, Any] | None = None,
) -> str | None:
    """Determine the vault secret name for SSH key resolution.

    Precedence:
    1. ``ticket_fields["ssh_key_secret"]`` — per-ticket override
    2. ``SSH_KEY_VAULT_SECRET`` env var — deployment override
    3. ``config.json`` → ``ssh_key_vault_secret`` — global default
    """
    if ticket_fields:
        ticket_val = ticket_fields.get("ssh_key_secret")
        if ticket_val:
            return ticket_val

    env_val = os.environ.get("SSH_KEY_VAULT_SECRET")
    if env_val:
        return env_val

    vault_cfg = _load_config_value("ssh_key_vault_secret")
    if vault_cfg:
        return vault_cfg

    return None


def _load_config_value(key: str) -> Any | None:
    """Read a single top-level key from ``~/.agentic-perf/config.json``."""
    import json

    from paths import CONFIG_PATH

    if not CONFIG_PATH.exists():
        return None
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return cfg.get(key)
    except (json.JSONDecodeError, OSError):
        return None


@asynccontextmanager
async def resolve_ssh_key(
    ssh_key_path: str | None,
    secrets_provider: Any | None,
    vault_secret_name: str | None,
) -> AsyncIterator[str | None]:
    """Resolve an SSH key path, falling back to the secrets cascade.

    Yields a filesystem path to the SSH private key. When the key
    comes from a vault provider, it is materialized as an ephemeral
    file (mode 0600) that is removed when the context exits.

    Rules:
    1. ``ssh_key_path`` exists on disk → yield it (no vault call).
    2. ``vault_secret_name`` + provider → materialize from vault.
    3. Vault configured but secret missing → raise
       ``SSHKeyResolutionError`` (fail closed).
    4. Nothing configured → yield original ``ssh_key_path``.
    """
    from providers.ssh import SSHKeyResolutionError

    if ssh_key_path:
        try:
            expanded = Path(ssh_key_path).expanduser()
        except RuntimeError:
            expanded = Path(ssh_key_path)
        if expanded.is_file():
            yield str(expanded)
            return

    if vault_secret_name and secrets_provider:
        async with secrets_provider.secret_file(vault_secret_name) as path:
            if path is not None:
                logger.info(
                    "SSH key resolved from vault secret '%s'",
                    vault_secret_name,
                )
                yield str(path)
                return
        raise SSHKeyResolutionError(
            f"Vault secret '{vault_secret_name}' configured for SSH key "
            f"but not found in secrets provider",
        )

    yield ssh_key_path


_ssh_key_stack: AsyncExitStack | None = None


def build_repo_cache():
    """Construct a RepoCache with harness repos from environment variables."""
    import json

    from providers.skills.repo_cache import RepoCache

    cache = RepoCache()

    default_repos = {
        "crucible-examples": "https://github.com/perftool-incubator/crucible-examples.git",
        "zathras": "https://github.com/redhat-performance/zathras.git",
        "kube-burner": "https://github.com/kube-burner/kube-burner.git",
        "k8s-netperf": "https://github.com/cloud-bulldozer/k8s-netperf.git",
        "benchmark-runner": "https://github.com/redhat-performance/benchmark-runner.git",
        "clusterbuster": "https://github.com/redhat-performance/clusterbuster.git",
        "vstorm": "https://github.com/gqlo/vstorm.git",
        "ioscale": "https://github.com/ekuric/ioscale.git",
        "forge": "https://github.com/openshift-psap/forge.git",
        "boot-time-analysis-scripts": "https://gitlab.com/redhat/edge/tests/perfscale/boot-time-analysis-scripts.git",
    }

    env_repos = os.environ.get("HARNESS_REPOS")
    if env_repos:
        try:
            default_repos.update(json.loads(env_repos))
        except json.JSONDecodeError:
            pass

    for name, url in default_repos.items():
        if name == "crucible":
            # Crucible is never cloned or refreshed by agentic-perf.
            continue
        try:
            cache.ensure_repo(name, url)
        except Exception:
            logger.warning("Failed to cache repo %s from %s", name, url, exc_info=True)

    return cache


async def assert_ticket_active(
    ticket_id: str | None = None,
    state_store_url: str | None = None,
    expected_status: str | None = None,
) -> dict[str, Any]:
    """Check that the ticket is still in an active, expected status.

    Returns the full ticket dict on success. Returns a rejection dict
    (with ``"status": "rejected"``) if the ticket has been aborted or
    drifted — the caller should return this to the LLM as a tool result
    instead of proceeding with the side-effecting operation.
    """
    import httpx

    ticket_id = ticket_id or os.environ.get("TICKET_ID", "")
    state_store_url = state_store_url or os.environ.get(
        "STATE_STORE_URL", "http://localhost:8090"
    )

    if not ticket_id:
        return {}

    headers = {}
    api_token = os.environ.get("AGENTIC_PERF_API_TOKEN", "")
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"

    async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
        r = await client.get(
            f"{state_store_url}/api/v1/tickets/{ticket_id}",
        )
        r.raise_for_status()
        ticket = r.json()

    cf = ticket.get("custom_fields", {})
    status = ticket.get("status", "")

    if cf.get("abort_requested"):
        return {
            "status": "rejected",
            "reason": "Ticket has been aborted",
            "ticket_status": status,
        }

    if expected_status and status != expected_status:
        return {
            "status": "rejected",
            "reason": (f"Ticket status is {status}, expected {expected_status}"),
            "ticket_status": status,
        }

    return ticket


async def build_ssh_from_ticket(
    ticket_id: str | None = None,
    state_store_url: str | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Fetch a ticket and create an SSHExecutor from its custom_fields.

    Returns (SSHExecutor, ticket_dict). If ticket_id is None, reads from
    TICKET_ID env var. If state_store_url is None, reads from STATE_STORE_URL.
    """
    import httpx

    from providers.ssh import SSHExecutor
    from providers.tracing import new_trace_context
    from providers.tracing.client import TraceClient

    ticket_id = ticket_id or os.environ.get("TICKET_ID", "")
    state_store_url = state_store_url or os.environ.get(
        "STATE_STORE_URL", "http://localhost:8090"
    )

    if not ticket_id:
        return SSHExecutor(user="root"), {}

    headers = {}
    api_token = os.environ.get("AGENTIC_PERF_API_TOKEN", "")
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"

    async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
        r = await client.get(f"{state_store_url}/api/v1/tickets/{ticket_id}")
        r.raise_for_status()
        ticket = r.json()

    fields = ticket.get("custom_fields", {})
    ssh_key = fields.get("ssh_key_path")
    # Always use root — provisioning bootstraps root SSH access.
    # The ticket's ssh_user is the initial cloud login user (e.g., ec2-user),
    # not the runtime user for harness operations.
    ssh_user = "root"

    # Jumpstarter boards get reflashed constantly —
    # host keys change every time. Disable strict
    # checking to avoid stale key errors.
    strict = "no" if fields.get("resource_provider") == "jumpstarter" else "accept-new"

    # This stack owns resources held by the process-global MCP SSH executor.
    # Rebuilding the executor (for a new ticket or server reinitialization)
    # closes its predecessor's key material and TraceClient transport first.
    global _ssh_key_stack
    if _ssh_key_stack is not None:
        await _ssh_key_stack.aclose()
    _ssh_key_stack = AsyncExitStack()

    vault_secret_name = _resolve_vault_secret_name(fields)
    resolved_key = ssh_key
    if vault_secret_name:
        sp = build_secrets_provider()
        resolved_key = await _ssh_key_stack.enter_async_context(
            resolve_ssh_key(ssh_key, sp, vault_secret_name),
        )

    trace_recorder = TraceClient(state_store_url, api_token) if api_token else None
    if trace_recorder is not None:
        _ssh_key_stack.callback(trace_recorder.close)
    return SSHExecutor(
        user=ssh_user,
        key_path=resolved_key,
        strict_host_key=strict,
        trace_context=new_trace_context(
            ticket_id=ticket_id,
            agent_id=os.environ.get("AGENT_NAME"),
        ),
        trace_recorder=trace_recorder,
    ), ticket


async def tool_progress(
    message: str,
    tool_name: str,
    ticket_id: str | None = None,
    state_store_url: str | None = None,
) -> None:
    """Post a progress update to the ticket from within an MCP tool.

    Creates both a comment (via the state store API) and a canonical event so the web UI can
    display progress in real time.

    The event uses type "tool_progress" so the UI can distinguish
    it from regular comments and allow collapsing/minimizing.

    Author is formatted as "agent-name/tool-name" (e.g.,
    "resource-agent/setup_ssh"). The agent name comes from the
    AGENT_NAME env var; tool_name is provided by the caller.

    Reads TICKET_ID and STATE_STORE_URL from env if not provided.
    Silently no-ops if ticket_id is unavailable (e.g., in tests).
    """
    import httpx

    ticket_id = ticket_id or os.environ.get("TICKET_ID", "")
    state_store_url = state_store_url or os.environ.get(
        "STATE_STORE_URL",
        "http://localhost:8090",
    )
    if not ticket_id:
        return

    agent_name = os.environ.get("AGENT_NAME", "system")
    author = f"{agent_name}/{tool_name}"

    try:
        headers = {}
        api_token = os.environ.get("AGENTIC_PERF_API_TOKEN", "")
        if api_token:
            headers["Authorization"] = f"Bearer {api_token}"
        async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
            await client.post(
                f"{state_store_url}/api/v1/tickets/{ticket_id}/comments",
                json={"author": author, "body": message},
            )
    except Exception:
        logger.debug("Failed to post progress comment for %s", ticket_id, exc_info=True)

    _emit_tool_progress_event(ticket_id, author, message)


_progress_redactor = None


def _get_progress_redactor():
    """Lazy-init a pattern-only Redactor for the bypass path."""
    global _progress_redactor
    if _progress_redactor is None:
        from providers.redaction import Redactor

        _progress_redactor = Redactor()
    return _progress_redactor


def _emit_tool_progress_event(
    ticket_id: str,
    author: str,
    message: str,
) -> None:
    """Record a tool_progress event through the canonical trace store.

    Applies pattern-only redaction (no value registry — the MCP
    subprocess has no access to the orchestrator's secret registry).
    """
    from paths import TRACE_DB_PATH
    from providers.event_projection import legacy_to_trace
    from state_store.trace_store import TraceStore, TraceStoreWriteError

    redactor = _get_progress_redactor()
    message = redactor.redact_string(ticket_id, message)

    try:
        with TraceStore(TRACE_DB_PATH) as trace_store:
            trace_store.insert_event(
                legacy_to_trace(ticket_id, author, "tool_progress", {"body": message})
            )
    except (OSError, TraceStoreWriteError):
        logger.debug(
            "Failed to write tool_progress event for %s", ticket_id, exc_info=True
        )


def build_investigation_provider():
    """Construct an InvestigationRecordProvider from config.

    Reads investigation_records.backend from config.json.
    Defaults to file-based storage.
    """
    from providers.investigation.registry import (
        create_record_provider,
    )

    return create_record_provider()


def read_skill_document(skills_dir: Path, harness: str, filename: str) -> dict:
    """Read a skill document from skills_dir, normalizing redundant prefixes in harness and filename.

    Handles:
    - Stripping leading 'skills/' from filename
    - Stripping redundant f'{harness}/' from filename
    - Splitting category/filename when harness is empty and filename contains '/'
    - Fallback to Path(filename).name if not found directly
    - Resolving when filename contains another valid harness category
    """
    orig_harness = harness or ""
    orig_filename = filename or ""

    harness = str(harness or "").strip().strip("/")
    filename = str(filename or "").strip().strip("/")

    if filename.startswith("skills/"):
        filename = filename[len("skills/") :].lstrip("/")

    if harness and filename.startswith(f"{harness}/"):
        filename = filename[len(f"{harness}/") :].lstrip("/")

    if not harness and "/" in filename:
        parts = filename.split("/", 1)
        harness = parts[0]
        filename = parts[1]

    skill_path = skills_dir / harness / filename
    if not skill_path.is_file():
        # Fallback 1: if filename contains directory parts that failed, try base name under harness
        name_only = Path(filename).name
        if name_only and (skills_dir / harness / name_only).is_file():
            skill_path = skills_dir / harness / name_only
            filename = name_only
        elif "/" in filename:
            # Fallback 2: check if filename itself matches cat/file relative to skills_dir
            cat, fn = filename.split("/", 1)
            cat = cat.strip("/")
            fn = fn.strip("/")
            if (skills_dir / cat / fn).is_file():
                harness = cat
                filename = fn
                skill_path = skills_dir / harness / filename
            elif (skills_dir / cat / Path(fn).name).is_file():
                harness = cat
                filename = Path(fn).name
                skill_path = skills_dir / harness / filename

    if not skill_path.is_file():
        display_harness = harness or orig_harness
        display_filename = filename or orig_filename
        msg_path = (
            f"{display_harness}/{display_filename}"
            if display_harness
            else display_filename
        )
        return {
            "found": False,
            "harness": display_harness,
            "filename": display_filename,
            "message": f"Skill not found: {msg_path}",
        }

    try:
        resolved = skill_path.resolve()
        if not resolved.is_relative_to(skills_dir.resolve()):
            return {
                "found": False,
                "harness": harness,
                "filename": filename,
                "message": "Invalid path",
            }
    except (OSError, ValueError):
        return {
            "found": False,
            "harness": harness,
            "filename": filename,
            "message": "Invalid path",
        }

    return {
        "found": True,
        "harness": harness,
        "filename": filename,
        "content": skill_path.read_text(),
    }


def read_skill_documents(skills_dir: Path, docs: list[dict]) -> list[dict]:
    """Read multiple skill documents in one call.

    Each entry in *docs* should have ``harness`` (or ``category`` as
    alias) and ``filename`` (or ``name`` as alias).  Non-empty
    ``harness`` takes precedence over ``category``; non-empty
    ``filename`` takes precedence over ``name``.  An empty primary
    value falls through to the alias.

    Returns a list of ``read_skill_document`` results in the same order
    as *docs*, including failure entries for documents not found.
    """
    results: list[dict] = []
    for doc in docs:
        harness = doc.get("harness") or doc.get("category", "")
        filename = doc.get("filename") or doc.get("name", "")
        results.append(read_skill_document(skills_dir, harness, filename))
    return results


def get_board_selector(ticket: dict) -> str:
    """Get board_selector from directives or top-level custom_fields.

    Triage may place board_selector in either location depending
    on the model. Check directives first (authoritative), then
    fall back to top-level custom_fields for model-agnostic
    behavior.
    """
    cf = ticket.get("custom_fields", {})
    directives = cf.get("directives", {})
    return directives.get("board_selector", "") or cf.get("board_selector", "")
