"""Shared utilities for agent MCP servers.

All agent MCP servers run as subprocesses and need to set up the Python
path, construct providers, and resolve SSH credentials from tickets.
This module centralizes that setup to avoid duplication.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import posixpath
import sys
import weakref
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx  # noqa: F401 - retained as a stable test patch seam

from providers.execution import AuditedAsyncHTTPClient

logger = logging.getLogger(__name__)

DEFAULT_CRUCIBLE_SOURCE_REPO_URL = "https://github.com/perftool-incubator/crucible.git"


def _state_store_token() -> str:
    """Return this instance's state-store token for ticket-owned MCP tools."""
    token = os.environ.get("AGENTIC_PERF_API_TOKEN", "")
    if token:
        return token
    from state_store.auth import read_token_from_file

    token = read_token_from_file()
    if token:
        # Audited subprocess and HTTP adapters resolve their recorder lazily
        # from the environment, so make the instance credential available for
        # the rest of this ticket-owned MCP process.
        os.environ["AGENTIC_PERF_API_TOKEN"] = token
    return token


def ticket_state_headers() -> dict[str, str]:
    """Return state-store authentication and active ticket fence headers."""
    headers: dict[str, str] = {}
    api_token = _state_store_token()
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"

    from agents.fencing import current_fence_context

    fence = current_fence_context()
    session_id = (
        fence.session_id
        if fence
        else os.environ.get("AGENTIC_PERF_ORCHESTRATOR_SESSION_ID", "")
    )
    epoch = (
        str(fence.epoch)
        if fence
        else os.environ.get("AGENTIC_PERF_ORCHESTRATOR_EPOCH", "")
    )
    claim_id = fence.claim_id if fence else os.environ.get("AGENTIC_PERF_CLAIM_ID", "")
    if session_id and epoch:
        headers.update(
            {
                "X-Agentic-Perf-Orchestrator-Session": session_id,
                "X-Agentic-Perf-Orchestrator-Epoch": epoch,
            }
        )
    if claim_id:
        headers["X-Agentic-Perf-Claim-Id"] = claim_id
    return headers


def setup_project_path() -> str:
    """Add the project root to sys.path. Returns the project root path."""
    root = str(Path(__file__).resolve().parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)
    return root


def _read_instance_config() -> dict[str, Any] | None:
    from paths import CONFIG_PATH

    if not CONFIG_PATH.exists():
        return None
    try:
        raw_config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid agentic-perf configuration: {CONFIG_PATH}") from exc

    if not isinstance(raw_config, dict):
        raise ValueError(f"Invalid agentic-perf configuration: {CONFIG_PATH}")
    return raw_config


def _validated_crucible_source_repo(source_repo: str) -> str:
    repository = Path(source_repo).expanduser()
    catalog_path = repository / "config" / "repos.json"
    if not repository.is_dir():
        raise ValueError(
            f"Configured Crucible source repository is not a directory: {source_repo}"
        )
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"Configured Crucible source repository has no valid config/repos.json: {source_repo}"
        ) from exc
    if not isinstance(catalog, dict):
        raise ValueError(
            f"Configured Crucible source repository has an invalid catalog: {source_repo}"
        )
    for group in ("official", "unofficial"):
        entries = catalog.get(group, [])
        if not isinstance(entries, list):
            raise ValueError(
                f"Configured Crucible source repository has an invalid catalog: {source_repo}"
            )
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("name"), str)
                or not entry["name"]
                or not isinstance(entry.get("type"), str)
                or not entry["type"]
            ):
                raise ValueError(
                    f"Configured Crucible source repository has an invalid catalog: {source_repo}"
                )
    return str(repository)


def _configured_crucible_source_repo() -> str | None:
    """Resolve the effective local Crucible source repository."""
    env_repo = os.environ.get("CRUCIBLE_SOURCE_REPO")
    if env_repo:
        return _validated_crucible_source_repo(env_repo)

    raw_config = _read_instance_config()
    if raw_config is None:
        return None
    configured = raw_config.get("crucible_source_repo")
    if configured is None:
        return None
    if not isinstance(configured, str) or not configured:
        raise ValueError("Configured crucible_source_repo must be a non-empty string")
    return _validated_crucible_source_repo(configured)


def _configured_crucible_source_repo_url() -> str | None:
    """Resolve the Crucible repository URL from the effective configuration."""
    env_repos = os.environ.get("HARNESS_REPOS")
    if env_repos:
        try:
            configured_repos = json.loads(env_repos)
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid HARNESS_REPOS environment variable") from exc
        if not isinstance(configured_repos, dict):
            raise ValueError("Invalid HARNESS_REPOS environment variable")
        configured = configured_repos.get("crucible")
        if configured is not None:
            if not isinstance(configured, str) or not configured:
                raise ValueError("Configured crucible repository URL must be non-empty")
            return configured

    env_url = os.environ.get("CRUCIBLE_SOURCE_REPO_URL")
    if env_url:
        return env_url

    raw_config = _read_instance_config()
    if raw_config is None:
        return DEFAULT_CRUCIBLE_SOURCE_REPO_URL
    harness_repos = raw_config.get("harness_repos")
    if isinstance(harness_repos, dict):
        configured = harness_repos.get("crucible")
        if configured is not None:
            if not isinstance(configured, str) or not configured:
                raise ValueError("Configured crucible repository URL must be non-empty")
            return configured

    configured = raw_config.get("crucible_source_repo_url")
    if configured is not None and (not isinstance(configured, str) or not configured):
        raise ValueError(
            "Configured crucible_source_repo_url must be a non-empty string"
        )
    return configured or DEFAULT_CRUCIBLE_SOURCE_REPO_URL


def build_skill_provider(
    source_repo: str | None = None,
    *,
    crucible_home: str | None = None,
    repo_cache=None,
    source_url: str | None = None,
    zathras_home: str | None = None,
    resolve_source: bool = True,
    catalog_only: bool = False,
    arcaflow_mcp_client: Any | None = None,
    skill_phase: str = "",
    organization_resolver: Any | None = None,
    ticket_id: str | None = None,
):
    """Construct a MultiHarnessSkillProvider from instance configuration.

    Reads CRUCIBLE_HOME and ZATHRAS_HOME from environment variables. When no
    explicit source repository is supplied, resolves ``crucible_source_repo``
    from the environment or effective instance configuration.
    """
    from providers.skills.arcaflow_plugins import ArcaflowPluginSkillProvider
    from providers.skills.benchmark_runner import BenchmarkRunnerSkillProvider
    from providers.skills.clusterbuster import ClusterbusterSkillProvider
    from providers.skills.crucible import CrucibleCatalogSkillProvider
    from providers.skills.forge import ForgeSkillProvider
    from providers.skills.ioscale import IoscaleSkillProvider
    from providers.skills.k8s_netperf import K8sNetperfSkillProvider
    from providers.skills.kube_burner import KubeBurnerSkillProvider
    from providers.skills.local_context import LocalContextSource
    from providers.skills.multi import MultiHarnessSkillProvider
    from providers.skills.private import PrivateSkillProvider
    from providers.skills.source_epoch import SourceEpoch
    from providers.skills.vstorm import VstormSkillProvider
    from providers.skills.zathras import ZathrasSkillProvider

    zathras_home = (
        zathras_home if zathras_home is not None else os.environ.get("ZATHRAS_HOME", "")
    )
    if source_repo is None:
        source_repo = _configured_crucible_source_repo()
    elif source_repo:
        source_repo = _validated_crucible_source_repo(source_repo)
    else:
        raise ValueError("Configured crucible_source_repo must be a non-empty string")
    if source_url is None:
        source_url = _configured_crucible_source_repo_url()

    harnesses: dict[str, Any] = {
        "kube-burner": KubeBurnerSkillProvider(),
        "k8s-netperf": K8sNetperfSkillProvider(),
        "benchmark-runner": BenchmarkRunnerSkillProvider(),
        "clusterbuster": ClusterbusterSkillProvider(),
        "vstorm": VstormSkillProvider(),
        "ioscale": IoscaleSkillProvider(),
        "forge": ForgeSkillProvider(),
        "arcaflow-plugins": ArcaflowPluginSkillProvider(
            mcp_client=arcaflow_mcp_client,
        ),
    }
    # arcaflow-workflows uses the same catalog/discovery as
    # arcaflow-plugins but routes through the Arcaflow MCP
    # workflow engine instead of direct podman execution.
    harnesses["arcaflow-workflows"] = harnesses["arcaflow-plugins"]

    trusted_ticket_id = ticket_id or os.environ.get("TICKET_ID", "")
    source_epoch = None
    if trusted_ticket_id:
        from paths import SKILL_SOURCE_EPOCH_DIR

        source_epoch = SourceEpoch(
            SKILL_SOURCE_EPOCH_DIR,
            ticket_id=trusted_ticket_id,
        )

    if catalog_only:
        harnesses["crucible"] = CrucibleCatalogSkillProvider(
            build_crucible_context_gateway(
                catalog_only=True,
                source_repo=source_repo,
                source_url=source_url,
                source_epoch=source_epoch if source_repo is None else None,
            )
        )

    private = PrivateSkillProvider(resolver=organization_resolver)
    phase = skill_phase or os.environ.get("AGENT_NAME", "").removesuffix("-agent")
    private.bind_current_attempt(phase)

    if zathras_home:
        harnesses["zathras"] = ZathrasSkillProvider(zathras_home)
    else:
        zathras_tests = private._load_config("zathras").get("tests")
        if zathras_tests:
            harnesses["zathras"] = ZathrasSkillProvider(fallback_tests=zathras_tests)

    project_root = Path(__file__).resolve().parents[1]
    manifest = project_root / "skills" / "context-manifest.json"
    project_context_source = (
        LocalContextSource(manifest, root=project_root) if manifest.is_file() else None
    )
    return MultiHarnessSkillProvider(
        harnesses,
        private,
        default_harness="crucible",
        project_context_source=project_context_source,
        source_epoch=source_epoch,
    )


async def build_organization_skill_resolver_async(
    *, secrets_provider: Any | None = None, ticket_id: str | None = None
):
    """Resolve only organization skill sources, loading Git auth if required.

    This narrow path is suitable for context-only callers such as the resource
    gateway. Its secrets provider is used solely by configured organization
    Git sources; it does not construct resource providers or load their service
    credentials (for example ``aws/config.json``).
    """
    from paths import CONFIG_PATH
    from providers.skills.gateway import (
        OrganizationSkillResolver,
        organization_source_descriptors,
    )

    ticket_id = ticket_id or os.environ.get("TICKET_ID") or None
    raw_config = None
    try:
        if CONFIG_PATH.exists():
            raw_config = json.loads(CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        pass
    gateway = raw_config.get("skill_gateway") if isinstance(raw_config, dict) else None
    organization = gateway.get("organization") if isinstance(gateway, dict) else None
    try:
        sources = organization_source_descriptors(organization or {})
    except (TypeError, ValueError):
        sources = []
    needs_secret_provider = any(
        item["source"].get("kind") == "git"
        and isinstance(item["source"].get("auth", {}), dict)
        and item["source"].get("auth", {}).get("kind", "default")
        in {"https-token", "ssh-key-secret"}
        for item in sources
    )
    if secrets_provider is None and needs_secret_provider:
        secrets_provider = build_secrets_provider()
    resolver = await OrganizationSkillResolver.from_instance_config_async(
        raw_config=raw_config,
        secrets_provider=secrets_provider,
        ticket_id=ticket_id,
    )
    return resolver


async def build_skill_provider_async(
    *,
    secrets_provider: Any | None = None,
    ticket_id: str | None = None,
    **kwargs: Any,
):
    """Build skills after resolving authenticated organization Git sources."""
    trusted_ticket_id = ticket_id or os.environ.get("TICKET_ID") or None
    resolver = await build_organization_skill_resolver_async(
        secrets_provider=secrets_provider,
        ticket_id=trusted_ticket_id,
    )
    return build_skill_provider(
        organization_resolver=resolver,
        ticket_id=trusted_ticket_id,
        **kwargs,
    )


def build_crucible_context_gateway(
    *,
    crucible_home: str | None = None,
    repo_cache=None,
    source_repo: str | None = None,
    source_url: str | None = None,
    resolve_source: bool = False,
    catalog_only: bool = False,
    source_epoch: Any | None = None,
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
    trusted_ticket_id = os.environ.get("TICKET_ID", "")
    if (
        source_epoch is None
        and trusted_ticket_id
        and (not catalog_only or source_repo is None)
    ):
        from paths import SKILL_SOURCE_EPOCH_DIR
        from providers.skills.source_epoch import SourceEpoch

        source_epoch = SourceEpoch(
            SKILL_SOURCE_EPOCH_DIR,
            ticket_id=trusted_ticket_id,
        )
    core_repository = source_url or (
        cache.get_origin("crucible") if hasattr(cache, "get_origin") else None
    )
    core_repository = core_repository or DEFAULT_CRUCIBLE_SOURCE_REPO_URL
    if resolve_source:
        logger.warning(
            "resolve_source is ignored; Crucible repositories are not cloned"
        )
    return CrucibleContextGateway(
        home,
        source_repo=source_repo,
        repo_cache=cache,
        catalog_only=catalog_only,
        source_epoch=source_epoch,
        core_repository=core_repository,
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
    content = document.get("content")
    if isinstance(content, str) and "size_bytes" not in public:
        public["size_bytes"] = len(content.encode("utf-8"))
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
        if public.get("operation") in {"bootstrap", "read"}:
            for document in public["documents"]:
                document.pop("content", None)
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
        tool_name="get_skill_context",
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
    ssh_addresses = fields.get("ssh_hardware_ips")
    if isinstance(ssh_addresses, dict):
        value = ssh_addresses.get("controller")
        if isinstance(value, str) and value.strip():
            return value.strip()
    assigned = fields.get("assigned_hardware_ips")
    if isinstance(assigned, dict):
        value = assigned.get("controller")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


FORBIDDEN_REBOOT_HOSTS = frozenset(
    {
        "localhost",
        "127.0.0.1",
        "::1",
        "0.0.0.0",
    }
)


def normalize_address(address: str) -> str:
    """Use one identity for IPv4 and IPv4-mapped IPv6 literals."""
    import ipaddress

    parsed = ipaddress.ip_address(address)
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return str(parsed.ipv4_mapped)
    return str(parsed)


def resolve_addrs(host: str) -> frozenset[str]:
    """Resolve a hostname to a set of IP address strings."""
    import socket

    addrs: set[str] = set()
    try:
        for info in socket.getaddrinfo(host, None):
            addrs.add(normalize_address(info[4][0]))
    except (socket.gaierror, OSError):
        pass
    cleaned = host.strip().lower()
    try:
        import ipaddress

        ipaddress.ip_address(cleaned)
        addrs.add(normalize_address(cleaned))
    except ValueError:
        pass
    return frozenset(addrs)


def _self_addrs() -> frozenset[str]:
    """Compute the set of addresses that identify this host."""
    import socket

    addrs: set[str] = {"127.0.0.1", "::1", "0.0.0.0"}
    try:
        addrs.update(resolve_addrs(socket.gethostname()))
    except Exception:
        pass
    try:
        addrs.update(resolve_addrs(socket.getfqdn()))
    except Exception:
        pass
    return frozenset(addrs)


def _is_local_bind_address(address: str) -> bool:
    """Check local ownership without sending packets or relying on hostname DNS.

    A successful ephemeral UDP bind proves local ownership. Nonlocal-bind
    sysctls may cause conservative refusals; unexpected OS errors also refuse.
    """
    import errno
    import socket

    try:
        normalized = normalize_address(address)
        family = socket.AF_INET6 if ":" in normalized else socket.AF_INET
        literal, _, zone = normalized.partition("%")
        if family == socket.AF_INET6:
            scope = (
                int(zone)
                if zone.isdigit()
                else socket.if_nametoindex(zone)
                if zone
                else 0
            )
            target = (literal, 0, 0, scope)
        else:
            target = (literal, 0)
        with socket.socket(family, socket.SOCK_DGRAM) as probe:
            probe.bind(target)
        return True
    except OSError as exc:
        return exc.errno != errno.EADDRNOTAVAIL
    except ValueError:
        return True


def is_self_host(host: str) -> bool:
    """Return True if *host* resolves to the orchestrator itself.

    Uses address resolution rather than string comparison to catch
    aliases, IPs, and alternate DNS names. Fails closed: an
    unresolvable host is treated as potentially self.
    """
    import socket

    if host.strip().lower() in FORBIDDEN_REBOOT_HOSTS:
        return True
    try:
        own = socket.gethostname()
        if host.strip().lower() == own.lower():
            return True
        fqdn = socket.getfqdn()
        if host.strip().lower() == fqdn.lower():
            return True
    except Exception:
        pass
    host_addrs = resolve_addrs(host)
    if not host_addrs:
        return True
    if any(_is_local_address(addr) for addr in host_addrs):
        return True
    return bool(host_addrs & _self_addrs()) or any(
        _is_local_bind_address(address) for address in host_addrs
    )


def _is_local_address(addr: str) -> bool:
    """True for loopback or unspecified addresses (127.0.0.2, ::, ...).

    A literal allowlist misses the rest of 127.0.0.0/8 and IPv6 loopback
    spellings; address properties do not.
    """
    import ipaddress

    try:
        parsed = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    return parsed.is_loopback or parsed.is_unspecified


def _controller_relative_path(path: str) -> str | None:
    """Normalize a path learned from controller documentation.

    This intentionally does not classify or infer Crucible repositories.  It
    only keeps reads below the installed controller root and excludes obvious
    credential or VCS internals.
    """
    value = str(path or "").strip().removeprefix("crucible://")
    value = value.removeprefix("/opt/crucible/")
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or any(character in value for character in ("\x00", "\t", "\n", "\r"))
    ):
        return None
    candidate = Path(value)
    if any(
        part in {"", ".", ".."}
        or part.lower()
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
        for part in candidate.parts
    ):
        return None
    return candidate.as_posix()


def _controller_relative_pointer(base: str, pointer: str) -> str | None:
    """Resolve an in-root relative pointer before applying the path validator."""
    if (
        not pointer
        or pointer.startswith(("/", "\\"))
        or "\\" in pointer
        or any(character in pointer for character in ("\x00", "\t", "\n", "\r"))
    ):
        return None
    normalized = posixpath.normpath(posixpath.join(base, pointer))
    if normalized in {"", "."}:
        return ""
    return _controller_relative_path(normalized)


async def _search_controller_source_mcp(
    adapter: Any,
    query: str,
    scope_path: str = "",
    max_bytes: int = 131072,
) -> dict[str, Any]:
    raw = await adapter.call_tool(
        "search_documentation",
        {
            "query": query,
            "limit": 20,
        },
    )
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {
            "found": False,
            "operation": "search",
            "reason": "controller_transport_error",
            "message": "The controller search request failed.",
            "next_action": "Retry after controller connectivity is restored.",
        }
    if isinstance(payload, dict) and payload.get("error"):
        return {
            "found": False,
            "operation": "search",
            "reason": "controller_transport_error",
            "message": str(payload["error"]),
            "next_action": "Retry after controller connectivity is restored.",
        }
    raw_results = (
        payload.get("resources", payload.get("results", payload.get("documents", [])))
        if isinstance(payload, dict)
        else payload
        if isinstance(payload, list)
        else []
    )
    if not isinstance(raw_results, list):
        raw_results = []
    results: list[dict[str, Any]] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        uri = item.get("uri") or item.get("url")
        ref = item.get("ref") or item.get("path") or _mcp_resource_ref(item)
        if not isinstance(ref, str) or not ref:
            continue
        content = item.get("content") or item.get("text") or item.get("snippet") or ""
        if isinstance(content, str) and len(content.encode("utf-8")) > max_bytes:
            content = content.encode("utf-8")[:max_bytes].decode(
                "utf-8", errors="ignore"
            )
        results.append(
            {
                "ref": ref,
                "uri": uri if isinstance(uri, str) and uri else f"crucible://{ref}",
                "path": ref,
                "type": "file",
                "content": content if isinstance(content, str) else "",
                "size_bytes": len(content.encode("utf-8"))
                if isinstance(content, str)
                else 0,
            }
        )
    if scope_path:
        scope_prefix = f"{scope_path.rstrip('/')}/"
        results = [item for item in results if item["path"].startswith(scope_prefix)]
    truncated = (
        payload.get("truncated") is True
        if isinstance(payload, dict)
        else len(results) >= 20
    )
    complete = (
        payload.get("complete")
        if isinstance(payload, dict) and payload.get("complete") is not None
        else not truncated
    )
    return {
        "found": bool(results),
        "operation": "search",
        "query": query,
        "namespace": "controller",
        "results": results,
        "total_files": len(results),
        "total_matches": len(results),
        "truncated": truncated,
        "complete": complete is True and not truncated,
        "scan_scope": scope_path or ".",
        "scope_semantics": "recursive subtree; file origins use their parent directory",
        "execution_transport": "crucible_mcp",
        "mcp_contract_version": "3",
    }


def _mcp_resource_ref(resource: dict[str, Any]) -> str:
    uri = resource.get("uri") or resource.get("url") or ""
    for prefix in ("crucible://", "context://crucible/"):
        if isinstance(uri, str) and uri.startswith(prefix):
            return uri[len(prefix) :]
    value = resource.get("path") or resource.get("name") or ""
    return value if isinstance(value, str) else ""


async def _read_controller_document_mcp(
    adapter: Any,
    relative: str,
) -> SimpleNamespace:
    resource_uri = f"crucible://{relative}"
    try:
        raw = await adapter.read_resource(resource_uri)
    except Exception:
        resources = await adapter.list_resources()
        refs = {
            ref
            for resource in resources
            if isinstance(resource, dict)
            and isinstance((ref := _mcp_resource_ref(resource)), str)
            and ref
        }
        if any(ref.startswith(f"{relative.rstrip('/')}/") for ref in refs):
            return SimpleNamespace(stdout="", stderr="", exit_code=23)
        raise
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return SimpleNamespace(stdout="", stderr="", exit_code=26)
    contents = payload.get("contents", []) if isinstance(payload, dict) else []
    if not isinstance(contents, list):
        contents = []
    texts: list[str] = []
    resolved_ref = relative
    for content in contents:
        if not isinstance(content, dict):
            continue
        candidate = _mcp_resource_ref(content)
        if candidate:
            resolved_ref = candidate
        text = content.get("text")
        if isinstance(text, str):
            texts.append(text)
    if not texts:
        resources = await adapter.list_resources()
        refs = {
            ref
            for resource in resources
            if isinstance(resource, dict)
            and isinstance((ref := _mcp_resource_ref(resource)), str)
            and ref
        }
        if any(ref.startswith(f"{relative.rstrip('/')}/") for ref in refs):
            return SimpleNamespace(stdout="", stderr="", exit_code=23)
        return SimpleNamespace(
            stdout="",
            stderr=f"resolved_ref={resolved_ref}",
            exit_code=26,
        )
    return SimpleNamespace(
        stdout="\n".join(texts),
        stderr=f"resolved_ref={resolved_ref}",
        exit_code=0,
    )


async def _list_controller_directory_mcp(
    adapter: Any,
    relative: str,
) -> dict[str, Any]:
    resources = await adapter.list_resources()
    entries: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    prefix = f"{relative.rstrip('/')}/" if relative else ""
    for resource in resources:
        if not isinstance(resource, dict):
            continue
        ref = _mcp_resource_ref(resource)
        if not ref or (prefix and not ref.startswith(prefix)):
            continue
        remainder = ref[len(prefix) :] if prefix else ref
        if not remainder:
            continue
        first_segment = remainder.split("/", 1)[0]
        child_path = (
            f"{relative.rstrip('/')}/{first_segment}" if relative else first_segment
        )
        if child_path in seen_paths:
            continue
        seen_paths.add(child_path)
        size = resource.get("size_bytes", resource.get("size"))
        entries.append(
            {
                "path": child_path,
                "type": "directory" if "/" in remainder else "file",
                "size_bytes": size if isinstance(size, int) and size >= 0 else None,
            }
        )
    return {
        "entries": entries,
        "complete": True,
        "resolved_path": relative,
    }


async def _inspect_controller_path_mcp(
    adapter: Any,
    relative: str,
) -> SimpleNamespace:
    resources = await adapter.list_resources()
    refs = {
        ref
        for resource in resources
        if isinstance(resource, dict)
        and isinstance((ref := _mcp_resource_ref(resource)), str)
        and ref
    }
    if relative in refs:
        return SimpleNamespace(stdout="file", stderr="", exit_code=0)
    directory_prefix = f"{relative.rstrip('/')}/"
    if any(ref.startswith(directory_prefix) for ref in refs):
        return SimpleNamespace(stdout="directory", stderr="", exit_code=0)
    return SimpleNamespace(stdout="", stderr="", exit_code=24)


def _controller_metadata_target(relative: str) -> tuple[str, dict[str, Any]] | None:
    """Map reserved controller refs to structured Crucible MCP metadata tools."""
    if relative == "endpoints":
        return "list_endpoints", {}
    if relative == "tools":
        return "list_tools", {}
    if relative.startswith("tools/"):
        name = relative.removeprefix("tools/")
        if not name or "/" in name:
            return None
        return "list_tools", {"name": name}
    if relative.startswith("benchmark/"):
        name = relative.removeprefix("benchmark/")
        if not name or "/" in name:
            return None
        return "describe_benchmark", {"name": name}
    return None


async def _read_controller_metadata_mcp(
    adapter: Any,
    relative: str,
) -> dict[str, Any]:
    """Read one structured metadata response with stable provenance fields."""
    target = _controller_metadata_target(relative)
    if target is None:
        raise ValueError("not a metadata selector")
    tool_name, arguments = target
    raw = await adapter.call_tool(tool_name, arguments)
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Crucible MCP returned invalid metadata JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("Crucible MCP returned invalid metadata JSON")
    content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    revision = payload.get("revision")
    if not isinstance(revision, str) or not revision:
        revision = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return {
        "tool_name": tool_name,
        "payload": payload,
        "content": content,
        "revision": revision,
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }


async def controller_context_gateway(
    *,
    ssh: Any,
    controller_host: str | None,
    mcp_adapter: Any | None = None,
    ticket_id: str,
    agent_name: str,
    phase: str,
    benchmark: str = "",
    operation: str = "read",
    path: str = "",
    from_ref: str = "",
    scope_path: str = "",
    query: str = "",
    include_alternates: bool = False,
    max_bytes: int = 16 * 1024,
    offset_bytes: int = 0,
) -> str:
    """Read, list, or search controller context under its installed root."""
    from providers.workspace.manager import WorkspaceManager

    manager = WorkspaceManager(ticket_id=ticket_id, agent_name=agent_name, phase=phase)

    def finish(payload: dict[str, Any]) -> str:
        reason = payload.get("reason")
        emit_private_tool_audit_event(
            ticket_id,
            agent_name=agent_name,
            tool_name="get_skill_context",
            event_type="context_gateway",
            data={
                "phase": phase,
                "operation": operation,
                "outcome": "rejected" if reason else "success",
                "reason": reason,
                "requested_ref": payload.get("requested_ref"),
                "resolved_ref": payload.get("resolved_ref"),
                "path_type": payload.get("path_type"),
                "result_count": len(payload.get("results", []))
                if isinstance(payload.get("results"), list)
                else None,
            },
        )
        return json.dumps(
            _public_context_result(
                payload,
                phase=phase,
                audience=manager.audience,
                benchmark=benchmark,
                namespace="controller",
                subject_area="all",
            )
        )

    def failure(
        reason: str,
        message: str,
        *,
        requested_ref: str = "",
        resolved_ref: str = "",
        path_type: str = "",
        next_action: str = "",
    ) -> str:
        payload: dict[str, Any] = {
            "found": False,
            "operation": operation,
            "reason": reason,
            "message": message,
        }
        if requested_ref:
            payload["requested_ref"] = requested_ref
            payload["path"] = requested_ref
        if resolved_ref:
            payload["resolved_ref"] = resolved_ref
        if path_type:
            payload["path_type"] = path_type
        if next_action:
            payload["next_action"] = next_action
        return finish(payload)

    if operation == "bootstrap":
        path = "docs/agentic-perf-workflow"
    if operation == "read" and not from_ref:
        cached = manager.read_document(
            path,
            include_alternates=include_alternates,
            max_bytes=max_bytes,
            offset_bytes=offset_bytes,
        )
        if cached.get("status") == "ok":
            return finish(
                {
                    "found": True,
                    "operation": operation,
                    "document": cached,
                    "documents": [cached],
                    "requested_ref": path,
                    "resolved_ref": cached.get("ref"),
                }
            )
    if operation == "manifest":
        documents = manager.context_manifest("controller").get("documents", [])
        return finish(
            {
                "found": bool(documents),
                "operation": operation,
                "documents": documents,
            }
        )
    if not controller_host:
        return failure(
            "controller_not_identified",
            "The ticket has no usable controller connection.",
            requested_ref=path,
            next_action="Use the configured controller-backed context source.",
        )
    if mcp_adapter is None:
        return failure(
            "unsupported_controller",
            "The assigned Crucible controller does not support MCP contract v3.",
            requested_ref=path,
            next_action=(
                "Install or update Crucible to a controller with MCP contract v3."
            ),
        )

    relative: str | None
    if from_ref:
        origin = _controller_relative_path(from_ref)
        if not origin:
            return failure(
                "invalid_origin",
                "The origin must be a returned controller reference.",
                next_action="Use a controller ref from bootstrap, list, or search.",
            )
        try:
            origin_result = await _inspect_controller_path_mcp(mcp_adapter, origin)
        except Exception:
            return failure(
                "controller_transport_error",
                "The controller could not be reached while checking the origin.",
                requested_ref=origin,
                next_action="Retry the context request after controller connectivity is restored.",
            )
        if origin_result.exit_code not in {0, 20, 21, 22, 24, 25, 27}:
            return failure(
                "controller_transport_error",
                "The controller could not confirm the origin's path type.",
                requested_ref=origin,
                next_action=(
                    "Retry the context request after controller connectivity is "
                    "restored."
                ),
            )
        if origin_result.exit_code != 0:
            return failure(
                {
                    20: "controller_root_unavailable",
                    21: "controller_origin_not_found",
                    22: "controller_path_outside_root",
                    24: "controller_origin_invalid",
                    25: "controller_path_inaccessible",
                    27: "controller_path_excluded",
                }[origin_result.exit_code],
                "The origin could not be resolved safely on the controller.",
                requested_ref=origin,
                path_type={
                    21: "missing",
                    22: "outside_root",
                    24: "unsupported",
                    25: "inaccessible",
                    27: "excluded",
                }.get(origin_result.exit_code, "unknown"),
                next_action="Use a current ref returned by this gateway.",
            )
        origin_is_directory = origin_result.stdout.strip() == "directory"
        if operation == "read" and path:
            base = origin if origin_is_directory else posixpath.dirname(origin)
            relative = _controller_relative_pointer(base, path)
        elif operation == "list" and path:
            base = origin if origin_is_directory else posixpath.dirname(origin)
            relative = _controller_relative_pointer(base, path)
        elif operation == "list":
            relative = origin if origin_is_directory else posixpath.dirname(origin)
        elif operation == "search" and path:
            base = origin if origin_is_directory else posixpath.dirname(origin)
            relative = _controller_relative_pointer(base, path)
        elif operation == "search":
            base = origin if origin_is_directory else posixpath.dirname(origin)
            relative = _controller_relative_path(base) if base else ""
        else:
            relative = origin
    elif operation == "list" and not path:
        relative = ""
    else:
        relative = _controller_relative_path(path)

    if operation in {"bootstrap", "read"}:
        if not relative:
            return failure(
                "invalid_controller_path",
                "The controller path must be relative and remain inside the installed root.",
                requested_ref=origin if from_ref else "",
                path_type="invalid_pointer" if from_ref and path else "invalid_path",
                next_action="Use a returned controller ref or a documented relative path.",
            )
        metadata_target = _controller_metadata_target(relative)
        if operation == "read" and metadata_target is not None:
            try:
                metadata = await _read_controller_metadata_mcp(mcp_adapter, relative)
            except Exception:
                return failure(
                    "controller_metadata_unavailable",
                    "The controller could not return the requested Crucible metadata.",
                    requested_ref=relative,
                    path_type="metadata",
                    next_action="Retry after controller connectivity is restored.",
                )
            provenance = {
                "effective_source": "controller",
                "controller": controller_host,
                "path": relative,
                "mcp_tool": metadata["tool_name"],
                "mcp_contract_version": "3",
                "revision": metadata["revision"],
                "captured_at": metadata["captured_at"],
            }
            saved = manager.save_source_snapshot(
                "controller", provenance, {relative: metadata["content"]}
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
                "entrypoint": False,
                "type": "metadata",
                "role": "software-metadata",
                "revision": metadata["revision"],
                "captured_at": metadata["captured_at"],
                "content": metadata["content"],
                "size_bytes": len(metadata["content"].encode("utf-8")),
                "workspace_ref": saved.get("files", {}).get(relative),
            }
            if relative.startswith("benchmark/"):
                document["benchmark"] = relative.removeprefix("benchmark/")
            manager.index_context_documents([document])
            page = manager.read_document(
                relative,
                include_alternates=include_alternates,
                max_bytes=max_bytes,
                offset_bytes=offset_bytes,
            )
            if page.get("status") != "ok":
                return failure(
                    "controller_snapshot_unavailable",
                    "The controller metadata was read but could not be paged from its ticket snapshot.",
                    requested_ref=relative,
                    path_type="metadata",
                    next_action="Retry the metadata read to refresh its ticket snapshot.",
                )
            document_result = {
                "found": True,
                "operation": operation,
                "document": {**document, **page},
                "documents": [{**document, **page}],
                "requested_ref": relative,
                "resolved_ref": relative,
                "path_type": "metadata",
            }
            return finish(document_result)
        try:
            result = await _read_controller_document_mcp(
                mcp_adapter,
                relative,
            )
        except Exception:
            return failure(
                "controller_transport_error",
                "The controller could not be reached while reading the document.",
                requested_ref=relative,
                next_action="Retry after controller connectivity is restored.",
            )
        if result.exit_code != 0:
            reason, message, next_action = {
                20: (
                    "controller_root_unavailable",
                    "The installed controller root could not be resolved.",
                    "Check the Crucible installation on the controller.",
                ),
                21: (
                    "controller_document_not_found",
                    "The requested controller ref does not exist.",
                    "Check the exact ref or list its parent directory.",
                ),
                22: (
                    "controller_path_outside_root",
                    "The resolved ref leaves the installed controller root.",
                    "Use a ref whose resolved target stays inside the installation.",
                ),
                23: (
                    "controller_path_is_directory",
                    "The requested ref names a directory, not a document.",
                    "Call get_skill_context with operation='list' on this ref.",
                ),
                24: (
                    "controller_path_not_file",
                    "The requested ref is not a regular file.",
                    "Call operation='list' on its parent to choose a document ref.",
                ),
                25: (
                    "controller_path_inaccessible",
                    "The requested controller ref cannot be resolved or read.",
                    "Check controller permissions and retry with an accessible ref.",
                ),
                27: (
                    "controller_path_excluded",
                    "The resolved ref is inside a credential or VCS path.",
                    "Use a returned software reference outside excluded paths.",
                ),
                26: (
                    "controller_read_failed",
                    "The controller could not finish reading this document.",
                    "Retry the read; the gateway does not expose controller stderr.",
                ),
            }.get(
                result.exit_code,
                (
                    "controller_transport_error",
                    "The controller request failed.",
                    "Retry after controller connectivity is restored.",
                ),
            )
            return failure(
                reason,
                message,
                requested_ref=relative,
                path_type={
                    21: "missing",
                    22: "outside_root",
                    23: "directory",
                    24: "unsupported",
                    25: "inaccessible",
                    26: "file",
                    27: "excluded",
                }.get(result.exit_code, "unknown"),
                next_action=next_action,
            )
        resolved_relative = relative
        raw_stderr = getattr(result, "stderr", "")
        if isinstance(raw_stderr, str):
            for line in raw_stderr.splitlines():
                if line.startswith("resolved_ref="):
                    candidate = _controller_relative_path(
                        line.removeprefix("resolved_ref=")
                    )
                    if candidate:
                        resolved_relative = candidate
                    break
        content = result.stdout
        provenance = {
            "effective_source": "controller",
            "controller": controller_host,
            "path": relative,
            "resolved_path": resolved_relative,
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
            "entrypoint": relative == "docs/agentic-perf-workflow",
            "type": "file",
            "content": content,
            "size_bytes": len(content.encode("utf-8")),
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
        document_result = {
            "found": True,
            "operation": operation,
            "document": document,
            "documents": [document],
        }
        metadata_documents = [
            {
                "namespace": "controller",
                "path": selector,
                "ref": selector,
                "uri": f"crucible://{selector}",
                "source_path": selector,
                "source": "controller",
                "authority": "effective",
                "entrypoint": False,
                "type": "metadata",
                "role": "software-metadata",
                "size_bytes": None,
            }
            for selector in ("endpoints", "tools")
        ]
        document_result["documents"] = [document, *metadata_documents]
        document_result["metadata_selectors"] = [
            {
                "ref": "endpoints",
                "description": "Endpoint schemas and capabilities",
                "mcp_tool": "list_endpoints",
            },
            {
                "ref": "tools",
                "description": "All Crucible tool metadata",
                "mcp_tool": "list_tools",
            },
            {
                "ref": "tools/<name>",
                "description": "One Crucible tool's metadata",
                "mcp_tool": "list_tools",
            },
            {
                "ref": "benchmark/<name>",
                "description": "Benchmark metadata and validation rules",
                "mcp_tool": "describe_benchmark",
            },
        ]
        page = manager.read_document(
            relative,
            include_alternates=include_alternates,
            max_bytes=max_bytes,
            offset_bytes=offset_bytes,
        )
        if page.get("status") != "ok":
            return failure(
                "controller_snapshot_unavailable",
                "The controller document was read but could not be paged from its ticket snapshot.",
                requested_ref=relative,
                next_action="Retry the document read to refresh its ticket snapshot.",
            )
        document_result["document"] = {**document, **page}
        document_result["requested_ref"] = relative
        document_result["resolved_ref"] = resolved_relative
        document_result["path_type"] = "file"
        return finish(document_result)
    if operation == "list":
        if relative is None:
            return failure(
                "invalid_controller_path",
                "The directory path must stay inside the installed controller root.",
                requested_ref=origin if from_ref else "",
                path_type="invalid_pointer" if from_ref and path else "invalid_path",
                next_action="Use a returned controller directory ref.",
            )
        try:
            listing = await _list_controller_directory_mcp(
                mcp_adapter,
                relative,
            )
        except Exception:
            listing = {"error": "controller_transport_error"}
        if listing.get("error"):
            reason = {
                "root_unavailable": "controller_root_unavailable",
                "outside_root": "controller_path_outside_root",
                "excluded": "controller_path_excluded",
                "missing": "controller_document_not_found",
                "not_directory": "controller_path_not_directory",
                "inaccessible": "controller_path_inaccessible",
            }.get(listing["error"], "controller_transport_error")
            next_action = (
                "Use a returned software reference outside excluded paths."
                if reason == "controller_path_excluded"
                else "Retry after controller connectivity is restored."
                if reason == "controller_transport_error"
                else "Use operation='list' only with a directory ref."
                if reason == "controller_path_not_directory"
                else "Use a current in-root directory ref and retry."
            )
            return failure(
                reason,
                "The requested controller directory could not be listed safely.",
                requested_ref=relative,
                path_type={
                    "missing": "missing",
                    "outside_root": "outside_root",
                    "not_directory": "file_or_special",
                    "inaccessible": "inaccessible",
                    "excluded": "excluded",
                }.get(listing["error"], "unknown"),
                next_action=next_action,
            )
        entries = []
        for item in listing.get("entries", []):
            child_path = item.get("path")
            if not isinstance(child_path, str):
                continue
            child_ref = _controller_relative_path(child_path)
            if (
                not child_ref
                or posixpath.dirname(child_ref) != relative
                or item.get("type") not in {"file", "directory"}
            ):
                continue
            size = item.get("size_bytes")
            if type(size) is not int or size < 0:
                size = None
            entries.append(
                {
                    "ref": child_ref,
                    "uri": f"crucible://{child_ref}",
                    "path": child_ref,
                    "type": item["type"],
                    "size_bytes": size,
                }
            )
        resolved_relative = (
            _controller_relative_path(str(listing.get("resolved_path", relative)))
            or relative
        )
        return finish(
            {
                "found": bool(entries),
                "operation": operation,
                "requested_ref": relative,
                "resolved_ref": resolved_relative,
                "path_type": "directory",
                "results": entries,
                "total_entries": len(entries),
                "complete": listing.get("complete") is True,
                "scope": "immediate_children",
                "recursive": False,
            }
        )
    elif operation == "search":
        if path and not from_ref:
            return failure(
                "invalid_origin",
                "A relative search path requires a returned from_ref.",
                path_type="invalid_pointer",
                next_action="Use from_ref with a relative path or search an explicit scope.",
            )
        invalid_scope = (from_ref and relative is None) or (
            scope_path and _controller_relative_path(scope_path) is None
        )
        requested_scope = relative if from_ref else scope_path
        checked_scope = (
            _controller_relative_path(requested_scope) if requested_scope else ""
        )
        if invalid_scope:
            return failure(
                "invalid_origin",
                "The search scope must be a returned controller ref.",
                requested_ref=origin if from_ref else "",
                path_type="invalid_pointer" if from_ref and path else "invalid_scope",
                next_action="Use a current ref returned by bootstrap, list, or search.",
            )
        try:
            result = await _search_controller_source_mcp(
                mcp_adapter,
                query,
                scope_path=checked_scope or "",
                max_bytes=max_bytes,
            )
        except Exception:
            return failure(
                "controller_transport_error",
                "The controller search request failed.",
                requested_ref=checked_scope or ".",
                next_action="Retry after controller connectivity is restored.",
            )
        if result.get("reason"):
            return finish(result)
        result["requested_ref"] = checked_scope or "."
        result["scope"] = "recursive_subtree" if checked_scope else "installation"
    else:
        return failure(
            "unsupported_operation",
            "Use bootstrap, read, list, search, or the internal manifest operation.",
            next_action="Use one of the supported context operations.",
        )
    return finish(result)


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
    max_bytes: int = 16 * 1024,
    offset_bytes: int = 0,
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
                "core/AGENTS.md",
                include_alternates=include_alternates,
                max_bytes=max_bytes,
                offset_bytes=offset_bytes,
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
            cached = manager.read_document(
                path,
                include_alternates=include_alternates,
                max_bytes=max_bytes,
                offset_bytes=offset_bytes,
            )
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
                path,
                include_alternates=include_alternates,
                max_bytes=max_bytes,
                offset_bytes=offset_bytes,
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
                    **workspace_document,
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
    from providers.redaction import get_shared_redactor
    from providers.secrets.factory import create_secrets_provider
    from providers.secrets.git_reference import wrap_git_secret_references
    from providers.secrets.recording import RecordingSecretsProvider

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
            provider = CascadingSecretsProvider(
                [
                    ("shared", local),
                    ("vault:shared", vault),
                ]
            )
        except ImportError:
            logger.info(
                "bitwarden-sdk not installed; using local secrets only",
            )
            provider = local
    else:
        provider = local

    provider = wrap_git_secret_references(provider)
    ticket_id = os.environ.get("TICKET_ID")
    return (
        RecordingSecretsProvider(provider, get_shared_redactor(), ticket_id)
        if ticket_id
        else provider
    )


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


async def build_repo_cache():
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
            # The no-ticket cache skips Crucible; ticket-bound captures use SourceEpoch.
            continue
        try:
            await cache.ensure_repo(name, url)
        except Exception:
            logger.warning("Failed to cache repo %s from %s", name, url, exc_info=True)

    return cache


def make_traced_ssh(
    user: str = "root",
    key_path: str | None = None,
    strict_host_key: str = "accept-new",
) -> Any:
    """Create an SSH executor that can persist ticket-scoped audit events."""
    from providers.ssh import SSHExecutor
    from providers.tracing import current_trace_context
    from providers.tracing.client import TraceClient

    token = os.environ.get("AGENTIC_PERF_API_TOKEN", "")
    url = os.environ.get("STATE_STORE_URL", "")
    recorder = TraceClient(url, token) if url and token else None
    ssh = SSHExecutor(
        user=user,
        key_path=key_path,
        strict_host_key=strict_host_key,
        trace_context=current_trace_context(),
        trace_recorder=recorder,
    )
    if recorder is not None:
        # Cleanup SSH executors are short-lived. Tie the recorder's spool lock
        # to the executor so each temporary traced client is released even if
        # a cleanup operation raises before its normal completion path.
        weakref.finalize(ssh, recorder.close)
    return ssh


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
    ticket_id = ticket_id or os.environ.get("TICKET_ID", "")
    state_store_url = state_store_url or os.environ.get(
        "STATE_STORE_URL", "http://localhost:8090"
    )

    if not ticket_id:
        return {}

    headers = ticket_state_headers()
    from agents.fencing import current_fence_context

    fence = current_fence_context()
    session_id = (
        fence.session_id
        if fence
        else os.environ.get("AGENTIC_PERF_ORCHESTRATOR_SESSION_ID", "")
    )
    epoch = (
        str(fence.epoch)
        if fence
        else os.environ.get("AGENTIC_PERF_ORCHESTRATOR_EPOCH", "")
    )
    claim_id = fence.claim_id if fence else os.environ.get("AGENTIC_PERF_CLAIM_ID", "")
    async with AuditedAsyncHTTPClient(timeout=15.0, headers=headers) as client:
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

    claim = cf.get("claim")
    requires_fence = bool(expected_status or status == "executing_benchmark")
    if requires_fence and not isinstance(claim, dict):
        return {
            "status": "rejected",
            "reason": "claim_missing",
            "ticket_status": status,
        }
    if requires_fence and isinstance(claim, dict):
        try:
            claim_expires = datetime.fromisoformat(str(claim["expires"]))
            if claim_expires.tzinfo is None:
                claim_expires = claim_expires.replace(tzinfo=timezone.utc)
            if claim_expires <= datetime.now(timezone.utc):
                return {
                    "status": "rejected",
                    "reason": "claim_expired",
                    "ticket_status": status,
                }
            if (
                not all(
                    isinstance(claim.get(key), str) and claim.get(key)
                    for key in ("session_id", "claim_id")
                )
                or not isinstance(claim.get("epoch"), int)
                or claim["epoch"] <= 0
            ):
                raise ValueError("malformed claim identity")
        except (KeyError, TypeError, ValueError):
            return {
                "status": "rejected",
                "reason": "claim_malformed",
                "ticket_status": status,
            }
    if isinstance(claim, dict) and claim.get("session_id"):
        if (
            claim.get("session_id") != session_id
            or str(claim.get("epoch")) != epoch
            or claim.get("claim_id") != claim_id
        ):
            return {
                "status": "rejected",
                "reason": "stale_epoch",
                "ticket_status": status,
            }

        async with AuditedAsyncHTTPClient(timeout=15.0, headers=headers) as client:
            lease_response = await client.get(
                f"{state_store_url}/api/v1/control/orchestrator-lease"
            )
            lease_response.raise_for_status()
            active_lease = lease_response.json().get("lease")
        try:
            lease_expires = datetime.fromisoformat(str(active_lease["expires_at"]))
            if lease_expires.tzinfo is None:
                lease_expires = lease_expires.replace(tzinfo=timezone.utc)
            lease_valid = lease_expires > datetime.now(timezone.utc)
        except (KeyError, TypeError, ValueError):
            lease_valid = False
        if (
            not active_lease
            or not lease_valid
            or active_lease.get("session_id") != session_id
        ):
            return {
                "status": "rejected",
                "reason": "not_leader",
                "ticket_status": status,
            }
        if int(active_lease.get("epoch", 0)) != int(epoch):
            return {
                "status": "rejected",
                "reason": "stale_epoch",
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
    from providers.ssh import SSHExecutor
    from providers.tracing import (
        bind_trace_context,
        new_trace_context,
        trace_context_from_environment,
    )
    from providers.tracing.client import TraceClient

    ticket_id = ticket_id or os.environ.get("TICKET_ID", "")
    state_store_url = state_store_url or os.environ.get(
        "STATE_STORE_URL", "http://localhost:8090"
    )

    if not ticket_id:
        return SSHExecutor(user="root"), {}

    headers = {}
    api_token = _state_store_token()
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"

    async with AuditedAsyncHTTPClient(timeout=15.0, headers=headers) as client:
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
    trace_context = trace_context_from_environment(
        ticket_id=ticket_id, agent_id=os.environ.get("AGENT_NAME")
    ) or new_trace_context(
        ticket_id=ticket_id,
        agent_id=os.environ.get("AGENT_NAME"),
    )
    # A local MCP process is ticket-owned. Bind its inherited context before
    # callers initialize caches or providers that may perform audited actions.
    bind_trace_context(trace_context)
    return SSHExecutor(
        user=ssh_user,
        key_path=resolved_key,
        strict_host_key=strict,
        trace_context=trace_context,
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
        async with AuditedAsyncHTTPClient(timeout=10.0, headers=headers) as client:
            await client.post(
                f"{state_store_url}/api/v1/tickets/{ticket_id}/comments",
                json={"author": author, "body": message},
            )
    except Exception:
        logger.debug("Failed to post progress comment for %s", ticket_id, exc_info=True)

    _emit_tool_progress_event(ticket_id, author, message)


_progress_redactor = None


def _get_progress_redactor():
    """Return the process-wide registry used by secret providers."""
    global _progress_redactor
    if _progress_redactor is None:
        from providers.redaction import get_shared_redactor

        _progress_redactor = get_shared_redactor()
    return _progress_redactor


def _emit_tool_progress_event(
    ticket_id: str,
    author: str,
    message: str,
) -> None:
    """Record a tool_progress event through the canonical trace store.

    Applies the child process's shared value and pattern registry.
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
        if resolved.is_relative_to((skills_dir / "crucible").resolve()):
            return {
                "found": False,
                "harness": "crucible",
                "message": (
                    "Use get_skill_context(subject='harness/crucible') for "
                    "organization guidance and authoritative software references."
                ),
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


def extract_ticket_references(text: str) -> list[str]:
    """Extract PERF-XXXXXXXX ticket IDs from text.

    Returns deduplicated list preserving first-seen order.
    """
    import re

    ids: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"PERF-[A-F0-9]{8}", text):
        tid = match.group()
        if tid not in seen:
            seen.add(tid)
            ids.append(tid)
    return ids


# Hard cap on how many bytes read_artifact_file_impl will return, so a
# single oversized line (or a file with no newlines) cannot blow up the
# agent process or produce a multi-gigabyte tool response.
_ARTIFACT_READ_MAX_BYTES = 256 * 1024
# Chunk size used while advancing to ``offset``; keeps the skip loop from
# reading an entire oversized line into memory just to count it.
_ARTIFACT_SKIP_CHUNK = 8 * 1024


def read_artifact_file_impl(
    path: str,
    offset: int = 0,
    limit: int = 200,
    ticket_id: str = "",
) -> str:
    """Read lines from a local artifact file with bounds checking.

    Shared implementation for the read_artifact_file MCP tool
    exposed by benchmark and platform servers.

    Args:
        path: Absolute path or path relative to the ticket's
            artifact directory.
        offset: 0-based line number to start reading from.
        limit: Maximum number of lines to return (max 500).
        ticket_id: Required ticket ID for scoping all artifact reads.

    Returns:
        JSON string with file content, metadata, and pagination info.
    """
    import json
    from pathlib import Path

    from paths import ARTIFACT_DIR

    if not ticket_id:
        return json.dumps({"error": f"File not found: {path}"})

    limit = min(limit, 500)

    resolved = Path(path)
    if not resolved.is_absolute():
        resolved = ARTIFACT_DIR / ticket_id / path
    resolved = resolved.resolve()

    # Containment check BEFORE existence check to avoid leaking
    # filesystem information (an out-of-scope existing path must
    # return the same error as a missing one).
    try:
        scoped_dir = (ARTIFACT_DIR / ticket_id).resolve()
        # Use is_relative_to for proper path-component containment.
        # String prefix matching would allow sibling directories
        # like "artifacts-archive/" to pass.
        if not resolved.is_relative_to(scoped_dir):
            return json.dumps({"error": f"File not found: {path}"})
    except (OSError, ValueError):
        return json.dumps({"error": f"File not found: {path}"})

    if not resolved.exists():
        return json.dumps({"error": f"File not found: {path}"})

    try:
        limit = max(0, min(limit, 500))
        offset = max(0, offset)
        size = resolved.stat().st_size
        with open(resolved, encoding="utf-8", errors="replace") as f:
            # Skip to offset, consuming long lines in bounded chunks so a
            # single oversized line cannot be materialized in memory.
            skipped = 0
            while skipped < offset:
                chunk = f.readline(_ARTIFACT_SKIP_CHUNK)
                if chunk == "":
                    break
                if chunk.endswith("\n"):
                    skipped += 1
            # Read up to ``limit`` lines while enforcing a byte budget
            # so a single oversized line (or a file with no newlines)
            # cannot exhaust the agent process or return a huge payload.
            selected: list[str] = []
            used_bytes = 0
            truncated = False
            for _ in range(limit):
                remaining = _ARTIFACT_READ_MAX_BYTES - used_bytes
                # Bound the read itself: a plain readline() would
                # materialize a multi-gigabyte line in memory before the
                # byte budget below could reject it.
                line = f.readline(remaining + 1)
                if line == "":
                    break
                encoded = line.encode("utf-8", "replace")
                if len(encoded) > remaining:
                    if remaining > 0:
                        selected.append(encoded[:remaining].decode("utf-8", "replace"))
                    truncated = True
                    break
                selected.append(line)
                used_bytes += len(encoded)
            has_more = truncated or f.read(1) != ""
        return json.dumps(
            {
                "file": str(resolved),
                "total_bytes": size,
                "offset": offset,
                "limit": limit,
                "lines_returned": len(selected),
                "content": "".join(selected),
                "has_more": has_more,
                "truncated": truncated,
            }
        )
    except Exception as exc:
        return json.dumps({"error": f"Failed to read file: {exc}"})
