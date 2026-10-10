"""FastMCP server for resource agent tools.

Exposes resource allocation tools (parse hosts, list/check/reserve providers,
validate hosts) over stdio.  The ResourceProviderRegistry and SSHExecutor are
constructed lazily on first tool call from environment variables and ticket
data, so credentials and provider internals never cross the LLM boundary.

Run directly:  python agents/resource/server.py
Connected via: AgentMCPClient (agents/mcp_client.py)
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

_project_root = str(Path(__file__).resolve().parents[2])
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from agents.mcp_audit import create_ticket_mcp
from agents.server_utils import (
    build_secrets_provider,
    build_skill_provider_async,
    build_ssh_from_ticket,
    get_board_selector,
)
from agents.skill_gateway import SKILL_GATEWAY_TOOL_DESCRIPTION, skill_context_gateway
from paths import get_default_ssh_key
from providers.resource.base import has_reservation_metadata, reservation_failed
from providers.tracing import (
    bind_trace_context,
    child_context,
    current_trace_context,
    reset_trace_context,
)

logger = logging.getLogger(__name__)

mcp = create_ticket_mcp("resource-agent")
SKILLS_DIR = Path(_project_root) / "skills"

# Module-level globals -- lazily initialized by _ensure_init()
_initialized = False
_ssh = None
_ticket: dict[str, Any] = {}
_registry = None
_skill_provider = None

# Fleet: first available untested device from check_available.
# Set by check_available_resources, read by reserve_resources.
_fleet_next_device: str | None = None

# Guardrail: set after a successful or uncertain reservation outcome.
# Prevents discovery after allocation and duplicate attempts when the provider
# may have allocated resources before returning an error.
_resources_allocated: bool = False
_reservation_uncertain: bool = False

# Track consecutive reservation failures so we can produce a
# structured response instead of burning iterations (#1128).
_reservation_failures: int = 0
_MAX_RESERVATION_FAILURES: int = 3

# Accumulates provider metadata across multiple reserve_resources calls
# (e.g., separate calls for controller and endpoints).
_last_reservation: dict[str, Any] = {}

# Accumulates validate_host results keyed by host IP/hostname.
_host_inventory: dict[str, dict[str, Any]] = {}


async def _ensure_init():
    """Lazily initialize providers and SSH from env vars on first tool call."""
    global _initialized, _ssh, _ticket, _registry
    if _initialized:
        return
    _ssh, _ticket = await build_ssh_from_ticket()
    secrets = build_secrets_provider()
    from paths import get_instance_name
    from providers.resource.registry import ResourceProviderRegistry

    _registry = ResourceProviderRegistry(secrets, instance_name=get_instance_name())
    _initialized = True
    fields = _ticket.get("custom_fields", {})
    if fields.get("resource_reservation_outcome_unknown") is True:
        _latch_unknown_reservation()


async def _get_skill_provider():
    """Initialize the context resolver without constructing resource clients."""
    global _skill_provider
    if _skill_provider is None:
        _skill_provider = await build_skill_provider_async(skill_phase="resource")
    return _skill_provider


@mcp.tool(description=SKILL_GATEWAY_TOOL_DESCRIPTION)
async def get_skill_context(
    subject: str,
    operation: str = "bootstrap",
    ref: str = "",
    path: str = "",
    from_ref: str = "",
    query: str = "",
    max_bytes: int = 16384,
    offset_bytes: int = 0,
) -> str:
    """Retrieve resource guidance through the subject-scoped context gateway."""
    return await skill_context_gateway(
        await _get_skill_provider(),
        ticket_id=os.environ.get("TICKET_ID", ""),
        agent_name="resource-agent",
        phase="resource",
        subject=subject,
        operation=operation,
        ref=ref,
        path=path,
        from_ref=from_ref,
        query=query,
        max_bytes=max_bytes,
        offset_bytes=offset_bytes,
        local_skills_dir=SKILLS_DIR,
    )


def _latch_unknown_reservation() -> None:
    """Block allocation and discovery until a human reconciles provider state."""
    global _resources_allocated, _reservation_uncertain
    _resources_allocated = True
    _reservation_uncertain = True


def _unknown_reservation_response(provider: str | None = None) -> dict[str, Any]:
    """Return the common fail-closed response for a persisted uncertainty marker."""
    response: dict[str, Any] = {
        "status": "unknown",
        "allocation_unknown": True,
        "retry_blocked": True,
        "error": (
            "Reservation outcome is unknown. Provider reconciliation is required "
            "before discovery or another reservation can run."
        ),
        "message": (
            "Reconcile provider state first. If an allocation is active, record its "
            "verified reservation ID and provider metadata for teardown. If none is "
            "active, clear stale reservation ID and provider metadata. Then clear "
            "resource_reservation_outcome_unknown."
        ),
    }
    if provider:
        response["provider"] = provider
    return response


def _ticket_resource_provider() -> tuple[str | None, bool]:
    """Return the authoritative managed provider and user-provided-only flag."""
    custom_fields = _ticket.get("custom_fields", {})
    directives = custom_fields.get("directives", {})
    configured = custom_fields.get("resource_provider")
    directed = directives.get("resource_provider")
    for candidate in (configured, directed):
        if candidate and candidate != "user_provided":
            return str(candidate), False
    return None, configured == "user_provided" or directed == "user_provided"


def _reservation_identity(result: dict[str, Any]) -> str | None:
    """Extract a provider reservation identifier from a result and its metadata."""
    metadata = result.get("provider_metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    reservation_id = (
        result.get("resource_reservation_id")
        or result.get("reservation_id")
        or result.get("lease_id")
        or result.get("assignment_id")
        or metadata.get("reservation_id")
        or metadata.get("lease_id")
        or metadata.get("assignment_id")
    )
    instance_ids = result.get("instance_ids") or metadata.get("instance_ids")
    if not reservation_id and isinstance(instance_ids, (list, tuple, set)):
        reservation_id = ",".join(str(value) for value in instance_ids if value)
    elif not reservation_id and isinstance(instance_ids, str) and instance_ids.strip():
        reservation_id = instance_ids.strip()
    if reservation_id is None or not str(reservation_id).strip():
        return None
    return str(reservation_id)


def _record_reservation_selection(
    result: dict[str, Any], selection: dict[str, Any], duration_hours: int
) -> None:
    """Persist the exact request inputs alongside provider allocation identity."""
    metadata = result.get("provider_metadata")
    metadata = dict(metadata) if isinstance(metadata, dict) else {}
    history = metadata.get("reservation_selections")
    history = list(history) if isinstance(history, list) else []
    record = dict(selection)
    record["duration_hours"] = duration_hours
    history.append(record)
    metadata["reservation_selections"] = history
    result["provider_metadata"] = metadata


def _split_reservation_ids(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        values = [str(item).strip() for item in value if str(item).strip()]
    elif isinstance(value, str):
        values = [item.strip() for item in value.split(",") if item.strip()]
    elif value is None:
        values = []
    else:
        values = [str(value).strip()]
    return list(dict.fromkeys(values))


def _combined_reservation_outcome(
    provider: str, result: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    """Merge known prior identity into the current response for durable state.

    AWS supports a comma-separated reservation ID and instance ID lists, so its
    multi-call allocations can be persisted as one cleanup identity. The other
    providers expose a single reservation identity; if two calls return
    different IDs, preserve both for reconciliation and leave the safety latch
    set rather than inventing an unsupported composite identifier.
    """
    previous = _last_reservation
    previous_identity = _reservation_identity(previous)
    if previous_identity is None and reservation_failed(previous):
        # A definitive no-allocation failure releases the provider choice. Its
        # diagnostic metadata must not leak into a later reservation outcome.
        previous = {}
    previous_metadata = previous.get("provider_metadata")
    previous_metadata = previous_metadata if isinstance(previous_metadata, dict) else {}
    current_metadata = result.get("provider_metadata")
    current_metadata = current_metadata if isinstance(current_metadata, dict) else {}

    metadata = dict(previous_metadata)
    for key, value in current_metadata.items():
        old_value = metadata.get(key)
        if key == "reservation_selections" and isinstance(value, list):
            prior_selections = old_value if isinstance(old_value, list) else []
            metadata[key] = [*prior_selections, *value]
        elif isinstance(old_value, list) and isinstance(value, list):
            metadata[key] = list(dict.fromkeys([*old_value, *value]))
        elif (
            key == "ip_mapping"
            and isinstance(old_value, dict)
            and isinstance(value, dict)
        ):
            metadata[key] = {**old_value, **value}
        else:
            metadata[key] = value

    combined = dict(result)
    if metadata:
        combined["provider_metadata"] = metadata

    previous_id = _reservation_identity(previous)
    current_id = _reservation_identity(result)
    if provider == "aws":
        instance_ids = _split_reservation_ids(
            previous_metadata.get("instance_ids") or previous.get("instance_ids")
        )
        instance_ids.extend(
            item
            for item in _split_reservation_ids(
                current_metadata.get("instance_ids") or result.get("instance_ids")
            )
            if item not in instance_ids
        )
        if not instance_ids:
            instance_ids = _split_reservation_ids(previous_id)
            instance_ids.extend(
                item
                for item in _split_reservation_ids(current_id)
                if item not in instance_ids
            )
        if instance_ids:
            metadata["instance_ids"] = instance_ids
            combined["instance_ids"] = instance_ids
            combined["reservation_id"] = ",".join(instance_ids)
        return combined, True

    if previous_id and current_id and previous_id != current_id:
        identities = list(
            dict.fromkeys(
                [
                    *_split_reservation_ids(previous_id),
                    *_split_reservation_ids(current_id),
                ]
            )
        )
        metadata["reconciliation_reservation_ids"] = identities
        combined["provider_metadata"] = metadata
        return combined, False

    if not current_id and previous_id:
        combined["reservation_id"] = previous_id
    return combined, True


async def _persist_unknown_reservation_marker(
    ticket_id: str | None,
    *,
    provider: str | None = None,
    result: dict[str, Any] | None = None,
) -> None:
    """Persist the fail-closed marker through the audited ticket state boundary."""
    ticket_id = ticket_id or os.environ.get("TICKET_ID", "") or _ticket.get("id", "")
    if not ticket_id:
        raise RuntimeError(
            "Cannot persist an unknown reservation outcome without a ticket ID"
        )

    result = result or {}
    metadata = result.get("provider_metadata")
    metadata = dict(metadata) if isinstance(metadata, dict) else {}
    fields: dict[str, Any] = {"resource_reservation_outcome_unknown": True}
    resolved_provider = provider or result.get("provider")
    if resolved_provider:
        fields["resource_provider"] = resolved_provider
    if metadata:
        fields["resource_provider_metadata"] = metadata
    reservation_id = _reservation_identity(result)
    if reservation_id:
        fields["resource_reservation_id"] = reservation_id

    custom_fields = _ticket.setdefault("custom_fields", {})
    custom_fields.update(fields)

    from providers.execution import AuditedAsyncHTTPClient
    from state_store.auth import read_token_from_file

    store_url = os.environ.get("STATE_STORE_URL", "http://localhost:8090")
    token = read_token_from_file()
    async with AuditedAsyncHTTPClient(
        base_url=store_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    ) as client:
        response = await client.patch(
            f"/api/v1/tickets/{ticket_id}/fields",
            json={"fields": fields},
        )
        response.raise_for_status()


async def _persist_known_reservation_outcome(
    ticket_id: str | None,
    provider: str,
    result: dict[str, Any],
    *,
    preserve_provider: bool = True,
) -> None:
    """Clear the latch with verified cleanup identity in the same state write."""
    ticket_id = ticket_id or os.environ.get("TICKET_ID", "") or _ticket.get("id", "")
    if not ticket_id:
        raise RuntimeError(
            "Cannot persist a resolved reservation outcome without a ticket ID"
        )

    metadata = result.get("provider_metadata")
    metadata = dict(metadata) if isinstance(metadata, dict) else {}
    fields: dict[str, Any] = {
        "resource_reservation_outcome_unknown": False,
        # A failed no-allocation attempt must not pin a provider that was only
        # selected by the LLM. Keep configured providers and providers with an
        # earlier active allocation; clear the write-ahead choice otherwise.
        "resource_provider": provider if preserve_provider else None,
    }
    if metadata:
        fields["resource_provider_metadata"] = metadata

    reservation_id = _reservation_identity(result)
    if reservation_id:
        fields["resource_reservation_id"] = reservation_id

    from providers.execution import AuditedAsyncHTTPClient
    from state_store.auth import read_token_from_file

    store_url = os.environ.get("STATE_STORE_URL", "http://localhost:8090")
    token = read_token_from_file()
    async with AuditedAsyncHTTPClient(
        base_url=store_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    ) as client:
        response = await client.patch(
            f"/api/v1/tickets/{ticket_id}/fields",
            json={"fields": fields},
        )
        response.raise_for_status()

    custom_fields = _ticket.setdefault("custom_fields", {})
    custom_fields.update(fields)


async def _persist_fleet_exhaustion_marker(ticket_id: str, exhausted: bool) -> None:
    """Persist the provider's current, confirmed fleet exhaustion result."""
    if not ticket_id:
        return
    from providers.execution import AuditedAsyncHTTPClient
    from state_store.auth import read_token_from_file

    store_url = os.environ.get("STATE_STORE_URL", "http://localhost:8090")
    token = read_token_from_file()
    async with AuditedAsyncHTTPClient(
        base_url=store_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    ) as client:
        response = await client.patch(
            f"/api/v1/tickets/{ticket_id}/fields",
            json={"fields": {"resource_fleet_exhaustion_detected": exhausted}},
        )
        response.raise_for_status()


def _is_confirmed_fleet_exhaustion(result: dict, tested_host_ids: set[str]) -> bool:
    """Only treat matching boards already tested by the fleet as exhaustion."""
    excluded_hosts = set(result.get("excluded_hosts") or [])
    return bool(
        result.get("all_excluded")
        and not result.get("available")
        and excluded_hosts
        and excluded_hosts.issubset(tested_host_ids)
    )


# ---------------------------------------------------------------------------
# Regex helpers — two-stage scan + validate
# ---------------------------------------------------------------------------

# Stage-1 candidate patterns: broad regexes that search within free-form
# text (e.g. "controller=10.1.2.3", "root@host.example.com:22").
# Candidates are validated in stage 2 before acceptance.
# Do not accept an IPv4-looking substring from a larger hostname or dotted
# sequence (``10.1.2.3.999`` must not yield ``10.1.2.3``).  A trailing dot is
# still allowed when it is sentence punctuation rather than another label.
_IP_CANDIDATE = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:\d{1,3}\.){3}\d{1,3}(?![A-Za-z0-9_-])(?!\.[A-Za-z0-9_-])"
)
_FQDN_CANDIDATE = re.compile(
    r"(?<![A-Za-z0-9_.-])"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)"
    r"+[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?![A-Za-z0-9_-])(?!\.[A-Za-z0-9_-])",
)

# Stage-2 FQDN label validation
_DNS_LABEL = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$")


def _is_valid_ip(candidate: str) -> bool:
    try:
        ipaddress.ip_address(candidate)
        return True
    except ValueError:
        return False


def _is_fqdn(token: str) -> bool:
    if len(token) > 253 or "." not in token:
        return False
    labels = token.rstrip(".").split(".")
    if len(labels) < 2:
        return False
    if not all(_DNS_LABEL.fullmatch(lb) for lb in labels):
        return False
    # Final label must be alphabetic — this naturally rejects bare IPs
    # that _FQDN_CANDIDATE may surface (e.g. "10.1.2.3" has final label "3").
    return labels[-1].isalpha() and len(labels[-1]) >= 2


def _extract_hosts(line: str) -> list[str]:
    """Extract IPs and FQDNs from a line via two-stage scan + validate.

    Stage 1 finds candidates by scanning the raw text (handles
    ``key=ip``, ``user@host``, ``host:port`` etc.).
    Stage 2 validates each candidate before acceptance.
    Overlapping matches are resolved by span containment: an IP
    embedded inside a validated FQDN (e.g. ``10.1.2.3.example.com``)
    is suppressed so only the FQDN is returned.
    """
    ip_hits = [
        (m.group(), m.start(), m.end())
        for m in _IP_CANDIDATE.finditer(line)
        if _is_valid_ip(m.group())
    ]
    fqdn_hits = [
        (m.group(), m.start(), m.end())
        for m in _FQDN_CANDIDATE.finditer(line)
        if _is_fqdn(m.group())
    ]
    fqdn_spans = [(s, e) for _, s, e in fqdn_hits]
    filtered_ips = [
        hit
        for hit in ip_hits
        if not any(fs <= hit[1] and hit[2] <= fe for fs, fe in fqdn_spans)
    ]
    all_hits = sorted(filtered_ips + fqdn_hits, key=lambda t: t[1])
    return list(dict.fromkeys(text for text, _, _ in all_hits))


# ---------------------------------------------------------------------------
# MCP Tools (6 tools -- everything except submit_resource_result)
# ---------------------------------------------------------------------------


@mcp.tool()
async def parse_host_config(text: str) -> str:
    """Extract structured host configuration from free-form text. Parses IP addresses, hostnames, roles (controller/target/client/server), SSH user, and SSH key path."""
    result: dict[str, Any] = {
        "controller": None,
        "targets": [],
        "ssh_user": "root",
        "ssh_key_path": get_default_ssh_key(),
    }

    lines = text.split("\n")
    all_hosts: list[str] = []

    for line in lines:
        lower = line.lower().strip()

        user_match = re.search(r"(?:user|ssh_user|ssh-user)\s*[:=]\s*(\S+)", lower)
        if user_match:
            result["ssh_user"] = user_match.group(1)

        key_match = re.search(
            r"(?:key|ssh_key|ssh-key|ssh_key_path)\s*[:=]\s*(\S+)", lower
        )
        if key_match:
            result["ssh_key_path"] = key_match.group(1)

        hosts_in_line = _extract_hosts(line)

        if hosts_in_line:
            if re.search(r"controller|server", lower):
                result["controller"] = hosts_in_line[0]
                if len(hosts_in_line) > 1:
                    result["targets"].extend(hosts_in_line[1:])
            elif re.search(r"target|client", lower):
                result["targets"].extend(hosts_in_line)
            else:
                all_hosts.extend(hosts_in_line)

    if result["controller"]:
        result["targets"] = [h for h in result["targets"] if h != result["controller"]]

    if not result["controller"] and all_hosts:
        all_hosts = list(dict.fromkeys(all_hosts))
        result["controller"] = all_hosts[0]
        result["targets"] = all_hosts[1:]
    result["targets"] = list(dict.fromkeys(result["targets"]))

    return json.dumps(result)


@mcp.tool()
async def list_resource_providers() -> str:
    """List resource providers that are configured and available. Returns provider names and types (bare_metal, cloud). Call this first if no resource_provider directive is set."""
    if _reservation_uncertain:
        return json.dumps(_unknown_reservation_response())
    if _resources_allocated:
        return json.dumps(
            {
                "error": "Resources already allocated. "
                "Call submit_resource_result to complete.",
                "already_allocated": True,
            }
        )
    await _ensure_init()
    if _reservation_uncertain:
        return json.dumps(_unknown_reservation_response())
    providers = await _registry.list_configured_providers()
    return json.dumps(
        {
            "configured_providers": providers,
            "count": len(providers),
        }
    )


@mcp.tool()
async def check_available_resources(
    provider: str,
    requirements: dict | None = None,
    required_hosts: list[dict] | None = None,
) -> str:
    """Check what resources are available from a specific provider. Use required_hosts (preferred) to get per-host recommendations based on the ticket's required_hosts entries with hardware specs, or requirements for a single uniform recommendation."""
    if _reservation_uncertain:
        return json.dumps(_unknown_reservation_response(provider))
    if _resources_allocated:
        return json.dumps(
            {
                "error": "Resources already allocated. "
                "Call submit_resource_result to complete.",
                "already_allocated": True,
            }
        )
    await _ensure_init()
    if _reservation_uncertain:
        return json.dumps(_unknown_reservation_response(provider))
    prov = await _registry.get_provider(provider)

    # Code-enforce the directive's board_selector for
    # Jumpstarter. The LLM may use a wrong selector key.
    if provider == "jumpstarter":
        directive_selector = get_board_selector(_ticket)
        if directive_selector:
            req = requirements or {}
            llm_selector = req.get("jumpstarter_selector", "")
            if llm_selector != directive_selector:
                requirements = dict(req)
                requirements["jumpstarter_selector"] = directive_selector

    # Fleet investigation: automatically exclude already-tested
    # hosts so the resource agent acquires a new board each
    # iteration. Code-enforced — the LLM doesn't need to know.
    # Re-fetch ticket for fresh fleet state (the cached _ticket
    # may not have tested_hosts from the coordinator).
    from providers.fleet import get_tested_host_ids, is_fleet_investigation

    fresh_cf = _ticket.get("custom_fields", {})
    ticket_id = os.environ.get("TICKET_ID", "") or _ticket.get("id", "")
    store_url = os.environ.get("STATE_STORE_URL", "http://localhost:8090")
    if ticket_id:
        try:
            from providers.execution import AuditedAsyncHTTPClient
            from state_store.auth import read_token_from_file

            token = read_token_from_file()
            async with AuditedAsyncHTTPClient(
                base_url=store_url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=10.0,
            ) as client:
                r = await client.get(f"/api/v1/tickets/{ticket_id}")
                if r.status_code == 200:
                    fresh_cf = r.json().get("custom_fields", {})
        except Exception:
            pass
    if is_fleet_investigation(fresh_cf):
        tested_host_ids = set(get_tested_host_ids(fresh_cf))
        exclude = list(tested_host_ids)
        # Merge three exclusion sources so nothing is lost:
        # 1. tested_hosts (fleet iteration tracking)
        # 2. user-provided exclude_hosts from directives
        # 3. LLM-provided exclude_hosts from tool arguments
        user_excludes = fresh_cf.get("directives", {}).get("exclude_hosts") or []
        if isinstance(user_excludes, str):
            user_excludes = [h.strip() for h in user_excludes.split(",") if h.strip()]
        llm_excludes = (requirements or {}).get("exclude_hosts") or []
        if isinstance(llm_excludes, str):
            llm_excludes = [h.strip() for h in llm_excludes.split(",") if h.strip()]
        combined = list(set(exclude) | set(user_excludes) | set(llm_excludes))
        if combined:
            requirements = dict(requirements or {})
            requirements["exclude_hosts"] = combined
            exclude = combined  # use merged list for per-host reqs too

    if required_hosts:
        recommendations = []
        availability_results = []
        for host_req in required_hosts:
            if is_fleet_investigation(fresh_cf) and exclude:
                host_req = dict(host_req)
                # Merge with any LLM-provided per-host exclusions
                existing = host_req.get("exclude_hosts") or []
                if isinstance(existing, str):
                    existing = [h.strip() for h in existing.split(",") if h.strip()]
                host_req["exclude_hosts"] = list(set(exclude) | set(existing))
            result = await prov.check_available(host_req)
            availability_results.append(result)
            rec = dict(host_req)
            if result.get("options"):
                rec["recommended"] = result["options"][0]
            recommendations.append(rec)
        response = {
            "provider": prov.provider_name,
            "per_host_recommendations": recommendations,
        }
        if is_fleet_investigation(fresh_cf) and ticket_id:
            fleet_exhausted = bool(availability_results) and all(
                _is_confirmed_fleet_exhaustion(item, tested_host_ids)
                for item in availability_results
            )
            await _persist_fleet_exhaustion_marker(ticket_id, fleet_exhausted)
            if fleet_exhausted:
                response["fleet_exhausted"] = True
                response["message"] = (
                    "All matching devices have been tested. "
                    "Fleet exhaustion detected — routing to coordinator."
                )
        return json.dumps(response)
    result = await prov.check_available(requirements or {})

    # Code-enforce: when a specific device was requested by
    # name and is unavailable, escalate to HITL immediately.
    # Retrying won't help — the board is leased, offline, or
    # doesn't exist.  The result includes the reason and any
    # alternatives of the same board type.
    if not result.get("available") and result.get("selector", "").startswith("name="):
        await _auto_escalate_named_device(result)

    # Fleet: deterministic exhaustion detection.
    # When all matching devices are excluded (i.e., already tested),
    # route to the fleet coordinator instead of letting the LLM
    # decide — the LLM may skip request_clarification and go
    # straight to HITL, bypassing fleet exhaustion handling (#994).
    if is_fleet_investigation(fresh_cf) and ticket_id:
        fleet_exhausted = _is_confirmed_fleet_exhaustion(result, tested_host_ids)
        await _persist_fleet_exhaustion_marker(ticket_id, fleet_exhausted)
    else:
        fleet_exhausted = False
    if fleet_exhausted:
        logger.info(
            "[resource] Fleet exhaustion detected for %s — all matching devices tested",
            ticket_id,
        )
        result["fleet_exhausted"] = True
        result["message"] = (
            "All matching devices have been tested. "
            "Fleet exhaustion detected — routing to coordinator."
        )

    # Fleet: remember the first available device so
    # reserve_resources can target it by name.
    global _fleet_next_device
    if is_fleet_investigation(fresh_cf):
        devices = result.get("devices", [])
        _fleet_next_device = devices[0]["name"] if devices else None

    return json.dumps(result)


def _infer_os_from_ticket() -> str:
    """Extract the OS from the ticket's required_hosts.

    Returns the OS if all non-controller hosts share the same value,
    otherwise returns empty string.
    """
    required_hosts = _ticket.get("custom_fields", {}).get("required_hosts", [])
    os_values = set()
    for h in required_hosts:
        os_val = h.get("os", "")
        if os_val and "controller" not in h.get("roles", []):
            os_values.add(os_val)
    if len(os_values) == 1:
        return os_values.pop()
    return ""


@mcp.tool()
async def reserve_resources(
    provider: str,
    selection: dict,
    description: str,
    ticket_id: str | None = None,
    duration_hours: int = 36,
) -> str:
    """Reserve resources from a provider. For bare-metal (quads), this creates an assignment, schedules hosts, waits for validation (~30-45 min), and sets up SSH access. For cloud (aws), this launches instances, waits until running, and verifies SSH connectivity. Pass {instance_type, count} for uniform instances or {instance_specs: [{instance_type, count, role}, ...]} for per-role instance types. For GPU cluster (psap-cc), this creates a cluster reservation -- returns cluster access info in provider_metadata (no SSH hosts). Returns a reservation ID for teardown."""
    global _resources_allocated, _reservation_failures, _reservation_uncertain
    if _reservation_uncertain:
        return json.dumps(_unknown_reservation_response(provider))
    await _ensure_init()
    if _reservation_uncertain:
        return json.dumps(_unknown_reservation_response(provider))
    expected_provider, user_provided_only = _ticket_resource_provider()
    if user_provided_only or (expected_provider and provider != expected_provider):
        expected = expected_provider or "user_provided"
        result = {
            "status": "failed",
            "provider": expected,
            "provider_mismatch": True,
            "provider_call_started": False,
            "error": (
                f"Ticket is configured for resource provider '{expected}', but "
                f"reserve_resources was called for '{provider}'."
            ),
            "message": (
                "No provider allocation was attempted. Use the ticket's configured "
                "provider; for user-provided resources, do not call reserve_resources."
            ),
        }
        return json.dumps(result)
    # Inject OS from ticket required_hosts when the LLM doesn't
    # include it in the selection — ensures AMI resolution fires
    # in the provider regardless of LLM behavior.
    if not selection.get("ami") and not selection.get("os"):
        os_name = _infer_os_from_ticket()
        if os_name:
            selection = dict(selection)
            selection["os"] = os_name
    # Code-enforce the directive's board_selector for
    # Jumpstarter. The LLM may substitute a different
    # (broader) selector; the directive is authoritative.
    if provider == "jumpstarter":
        directive_selector = get_board_selector(_ticket)
        if directive_selector:
            llm_selector = selection.get("jumpstarter_selector", "")
            if llm_selector != directive_selector:
                logger.warning(
                    "Overriding LLM selector %r with directive %r",
                    llm_selector,
                    directive_selector,
                )
                selection = dict(selection)
                selection["jumpstarter_selector"] = directive_selector

    # Fleet: target a specific device by name to ensure
    # we get an untested board. The name was determined
    # during check_available_resources from the filtered
    # device list.
    if _fleet_next_device:
        selection = dict(selection)
        selection["exporter_name"] = _fleet_next_device
        logger.info("Fleet: targeting %s", _fleet_next_device)

    prov = await _registry.get_provider(provider)
    resources_allocated_before = _resources_allocated
    _latch_unknown_reservation()
    try:
        # This write-ahead latch protects against MCP/process loss during the
        # provider call. A definitive response clears it atomically with any
        # reservation identity needed for teardown.
        await _persist_unknown_reservation_marker(ticket_id, provider=provider)
    except Exception as exc:
        logger.exception(
            "[resource] Could not persist pre-reservation uncertainty marker"
        )
        result = _unknown_reservation_response(provider)
        result["error"] = (
            "Could not persist the reservation safety marker; provider.reserve "
            "was not called."
        )
        result["message"] = (
            "No provider allocation was attempted by this call. Confirm provider "
            "state, clear any stale reservation ID or metadata if the marker was "
            "saved, and resolve the marker before retrying."
        )
        result["marker_persisted"] = False
        result["provider_call_started"] = False
        result["provider_metadata"] = dict(
            _last_reservation.get("provider_metadata") or {}
        )
        result["error"] += f" State-store error: {type(exc).__name__}."
        _last_reservation.clear()
        _last_reservation.update(result)
        return json.dumps(result)

    try:
        result = await prov.reserve(
            selection, description, duration_hours, ticket_id=ticket_id
        )
    except asyncio.CancelledError:
        # Provider calls can be cancelled after creating an allocation. Persist
        # the latch while the ticket trace context is still bound, then preserve
        # cancellation so the MCP middleware records the cancelled operation.
        logger.warning(
            "[resource] Provider %s reservation cancelled; outcome unknown", provider
        )
        _latch_unknown_reservation()
        _reservation_failures += 1
        prior_metadata = dict(_last_reservation.get("provider_metadata") or {})
        for key in ("lease_id", "instance_ids", "assignment_id", "reservation_id"):
            if key in _last_reservation:
                prior_metadata.setdefault(key, _last_reservation[key])
        cancelled_result = {
            "status": "unknown",
            "provider": provider,
            "allocation_unknown": True,
            "retry_blocked": True,
            "error": "Provider reserve was cancelled; allocation outcome is unknown.",
            "message": (
                "The provider may have allocated resources before cancellation. "
                "Do not retry or run discovery. If an allocation is active, record "
                "its verified reservation ID and provider metadata for teardown; "
                "if none is active, clear stale reservation ID and provider "
                "metadata before clearing the unknown marker."
            ),
            "provider_metadata": prior_metadata,
        }
        _record_reservation_selection(cancelled_result, selection, duration_hours)
        _last_reservation.clear()
        _last_reservation.update(cancelled_result)
        try:
            await asyncio.shield(
                _persist_unknown_reservation_marker(
                    ticket_id, provider=provider, result=cancelled_result
                )
            )
        except Exception:
            logger.exception(
                "[resource] Failed to persist cancellation uncertainty marker"
            )
        raise
    except Exception as exc:
        # A provider can allocate resources before a later setup step raises.
        # Mark the outcome unknown to prevent duplicate allocations and
        # preserve any prior reservation metadata for eventual cleanup.
        logger.exception(
            "[resource] Provider %s raised during reservation; outcome unknown",
            provider,
        )
        result = {
            "status": "unknown",
            "provider": provider,
            "allocation_unknown": True,
            "retry_blocked": True,
            "error": f"Provider reserve raised {type(exc).__name__}.",
            "message": (
                "The provider may have allocated resources before the error. "
                "Do not retry or run discovery tools; manual provider review "
                "is required."
            ),
            "provider_metadata": dict(_last_reservation.get("provider_metadata") or {}),
        }

    _record_reservation_selection(result, selection, duration_hours)

    # Provider argument is the selected registry key and remains authoritative
    # if a provider response includes its own provider field.
    result["provider"] = provider

    # Mark resources as allocated so discovery tools are blocked (#1128).
    # Providers use both error fields and status-only failure results.
    # Multi-call reservations (controller + endpoints) still work because
    # reserve_resources itself is not blocked — only discovery tools are.
    unknown_outcome = result.get("allocation_unknown") is True or str(
        result.get("status", "")
    ).strip().lower() in {"unknown", "uncertain", "indeterminate"}
    has_identity = has_reservation_metadata(provider, result) or (
        has_reservation_metadata(provider, result.get("provider_metadata"))
    )
    provider_failed = reservation_failed(result)
    if not unknown_outcome and provider_failed and has_identity:
        unknown_outcome = True
        result["allocation_unknown"] = True
        result["retry_blocked"] = True
        result["message"] = (
            f"{result.get('message', '').strip()} Provider reported failure but "
            "returned allocation identity; inspect and reconcile before retrying."
        ).strip()
    elif not unknown_outcome and not provider_failed and not has_identity:
        unknown_outcome = True
        result["status"] = "unknown"
        result["error"] = (
            "Provider returned no verifiable reservation ID or allocation metadata."
        )
        result["message"] = (
            "The provider response did not identify the allocation. Do not retry "
            "or run discovery; inspect provider state before resuming."
        )

    result, identity_is_combinable = _combined_reservation_outcome(provider, result)
    if not identity_is_combinable:
        unknown_outcome = True
        result["status"] = "unknown"
        result["allocation_unknown"] = True
        result["retry_blocked"] = True
        result["error"] = (
            "Multiple reservation IDs from this provider cannot be represented "
            "as one cleanup identity."
        )
        result["message"] = (
            "The provider returned multiple allocations that cannot be safely "
            "combined for automated teardown. Reconcile every ID in provider "
            "metadata before clearing the unknown marker."
        )

    if not unknown_outcome:
        try:
            preserve_provider = not (
                provider_failed
                and not has_identity
                and not resources_allocated_before
                and expected_provider is None
            )
            await _persist_known_reservation_outcome(
                ticket_id,
                provider,
                result,
                preserve_provider=preserve_provider,
            )
        except Exception as exc:
            logger.exception(
                "[resource] Could not persist definitive reservation outcome"
            )
            unknown_outcome = True
            result["status"] = "unknown"
            result["allocation_unknown"] = True
            result["retry_blocked"] = True
            result["error"] = (
                f"Could not durably record provider outcome ({type(exc).__name__})."
            )
            result["message"] = (
                "The provider responded, but ticket state could not be safely "
                "updated. Do not retry or discover; reconcile provider state."
            )
        else:
            _reservation_uncertain = False
            _resources_allocated = resources_allocated_before
    if unknown_outcome:
        _latch_unknown_reservation()
    if not reservation_failed(result):
        _resources_allocated = True
        _reservation_failures = 0
    else:
        _reservation_failures += 1
        if unknown_outcome:
            result["allocation_unknown"] = True
            result["retry_blocked"] = True
            result["message"] = (
                f"{result.get('message', '').strip()} "
                "The allocation outcome is unknown; do not retry or discover. "
                "Manual provider reconciliation is required."
            ).strip()
        elif _reservation_failures >= _MAX_RESERVATION_FAILURES:
            result["repeated_failure"] = True
            result["message"] = (
                f"Reservation failed {_reservation_failures} consecutive "
                f"times. The requested boards may be transiently "
                f"unavailable (leased by other users). Call "
                f"submit_resource_result with an error status, or "
                f"retry later."
            )
        else:
            result["retry_suggestion"] = (
                "Reservation failed. Try reserving a different "
                "board — do NOT call list_resource_providers or "
                "check_available_resources again."
            )

    _last_reservation.clear()
    _last_reservation.update(result)

    if unknown_outcome:
        try:
            await _persist_unknown_reservation_marker(
                ticket_id, provider=provider, result=result
            )
            result["marker_persisted"] = True
            _last_reservation["marker_persisted"] = True
        except Exception as exc:
            # Keep the current process latched even if the state store is
            # unavailable. The agent will make a second audited field update
            # from the returned unknown result before it exits.
            logger.exception("[resource] Failed to persist unknown reservation marker")
            result["marker_persisted"] = False
            result["error"] = (
                f"{result.get('error', 'Reservation outcome unknown')} "
                f"Ticket marker persistence failed ({type(exc).__name__})."
            )
            result["message"] = (
                f"{result.get('message', '').strip()} The uncertainty marker "
                "could not be saved; do not resume automated allocation."
            ).strip()
            _last_reservation.update(
                {
                    "marker_persisted": False,
                    "error": result["error"],
                    "message": result["message"],
                }
            )

    return json.dumps(result)


@mcp.tool()
async def get_reservation_status(provider: str, reservation_id: str) -> str:
    """Check the status of an existing resource reservation."""
    await _ensure_init()
    prov = await _registry.get_provider(provider)
    result = await prov.get_reservation_status(reservation_id)
    return json.dumps(result)


@mcp.tool()
async def validate_host(
    host: str, ssh_key_path: str = "", ssh_user: str = "root"
) -> str:
    """Validate that a host is reachable via SSH. Returns connectivity status, FQDN, basic system info (OS, CPU count, RAM), and NIC details (interface names and link speeds from ethtool). Pass ssh_key_path from the reserve_resources result to use the correct key."""
    await _ensure_init()
    from providers.ssh import SSHExecutor

    if ssh_key_path:
        ssh = SSHExecutor(
            user=ssh_user,
            key_path=ssh_key_path,
            trace_context=getattr(_ssh, "trace_context", None),
            trace_recorder=getattr(_ssh, "trace_recorder", None),
        )
    else:
        ssh = _ssh
    result = await ssh.run(host, "echo SSH_OK", timeout=15)

    if result.exit_code != 0 or "SSH_OK" not in result.stdout:
        return json.dumps(
            {
                "host": host,
                "reachable": False,
                "message": f"SSH failed: {result.stderr.strip() or 'no response'}",
            }
        )

    info_cmd = (
        "hostname -f 2>/dev/null || hostname; "
        "cat /etc/redhat-release 2>/dev/null || head -1 /etc/os-release; "
        "nproc; "
        "awk '/MemTotal/{printf \"%.0f\", $2/1024/1024}' /proc/meminfo"
    )
    info = await ssh.run(host, info_cmd, timeout=15)
    lines = info.stdout.strip().splitlines()

    fqdn = lines[0].strip() if len(lines) > 0 else host
    os_info = lines[1].strip() if len(lines) > 1 else "unknown"
    try:
        cpu_count = int(lines[2].strip()) if len(lines) > 2 else 0
    except ValueError:
        cpu_count = 0
    try:
        ram_gb = int(lines[3].strip()) if len(lines) > 3 else 0
    except ValueError:
        ram_gb = 0

    nic_cmd = (
        "for iface in $(ip -o link show "
        "| awk -F'[ :]+' '/^[0-9]+: (eth|ens|eno|enp)/"
        "{print $2}'); do "
        'speed=$(ethtool "$iface" 2>/dev/null '
        "| awk '/Speed:/{print $2}'); "
        "numa=$(cat /sys/class/net/$iface/device/numa_node "
        "2>/dev/null || echo -1); "
        'echo "${iface}:${speed:-unknown}:${numa}"; '
        "done"
    )
    nic_result = await ssh.run(host, nic_cmd, timeout=15)
    nic_info = []
    if nic_result.exit_code == 0 and nic_result.stdout.strip():
        for nic_line in nic_result.stdout.strip().splitlines():
            parts = nic_line.split(":", 2)
            if len(parts) >= 2:
                entry: dict[str, Any] = {
                    "name": parts[0],
                    "speed": parts[1],
                }
                if len(parts) == 3:
                    try:
                        entry["numa_node"] = int(parts[2])
                    except ValueError:
                        entry["numa_node"] = -1
                nic_info.append(entry)

    numa_cmd = (
        "for node in /sys/devices/system/node/node[0-9]*; do "
        "n=${node##*node}; "
        "cpus=$(cat $node/cpulist); "
        'echo "${n}:${cpus}"; '
        "done"
    )
    numa_result = await ssh.run(host, numa_cmd, timeout=15)
    numa_topology = []
    if numa_result.exit_code == 0 and numa_result.stdout.strip():
        for numa_line in numa_result.stdout.strip().splitlines():
            parts = numa_line.split(":", 1)
            if len(parts) == 2:
                try:
                    numa_topology.append({"node": int(parts[0]), "cpus": parts[1]})
                except ValueError:
                    pass

    inventory = {
        "host": host,
        "fqdn": fqdn,
        "reachable": True,
        "os": os_info,
        "cpu_count": cpu_count,
        "ram_gb": ram_gb,
        "nic_info": nic_info,
        "numa_topology": numa_topology,
        "message": f"Host {host} validated via SSH",
    }
    _host_inventory[host] = inventory
    return json.dumps(inventory)


@mcp.tool()
async def get_host_inventory() -> str:
    """Return accumulated host inventory from prior validate_host calls. Keyed by host IP/hostname, includes OS, CPU, RAM, NIC info with NUMA mapping, and NUMA topology."""
    return json.dumps(_host_inventory)


@mcp.tool()
async def get_accumulated_metadata() -> str:
    """Return accumulated provider metadata from prior reserve_resources calls.

    Merges provider_metadata sub-dict with top-level
    reservation fields. Providers may place metadata at
    either level — this ensures all fields are available
    regardless of provider convention.
    """
    # Start with any explicit provider_metadata sub-dict
    result = dict(_last_reservation.get("provider_metadata", {}))
    # Promote all top-level reservation fields except
    # transient/internal keys. Providers like Jumpstarter
    # put lease_id, selector, etc. at the top level.
    _SKIP_KEYS = frozenset(
        {
            "provider_metadata",
            "error",
            "available",
            "status",
            # Standard reservation fields that are not metadata
            "provider",
            "hosts",
            "matching_devices",
            "requested",
            "count",
            "message",
            "ssh_user",
            "ssh_key_path",
            "reservation_id",
            "fresh_host",
            "lease_expiration",
        }
    )
    for key, val in _last_reservation.items():
        if key not in _SKIP_KEYS and key not in result:
            result[key] = val
    return json.dumps(result)


async def _auto_escalate_named_device(result: dict) -> None:
    """Transition ticket to HITL when a named device is unavailable.

    Called from check_available_resources when a name= selector
    returns unavailable.  Retrying is pointless for a specific
    device — escalate immediately so the user can choose an
    alternative or wait.
    """
    ticket_id = os.environ.get("TICKET_ID", "")
    store_url = os.environ.get("STATE_STORE_URL", "http://localhost:8090")
    token = os.environ.get("AGENTIC_PERF_API_TOKEN", "")
    if not ticket_id:
        return

    # This is deliberately below the read-only MCP tool boundary.  A normal
    # availability query has no operation identity; only the conditional
    # ticket transition needs a durable fence and replay identity.
    parent = current_trace_context()
    if parent is None or parent.ticket_id != ticket_id:
        raise RuntimeError("named-device escalation requires the MCP trace context")
    if not token:
        raise RuntimeError("named-device escalation requires operation registry access")

    selector = str(result.get("selector", ""))
    immutable = {
        "ticket_id": ticket_id,
        "selector": selector,
        "transition": "awaiting_customer_guidance",
    }
    request_hash = hashlib.sha256(
        json.dumps(immutable, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    operation_key = f"named-device-escalation:{ticket_id}:{selector}"
    from providers.tracing.client import TraceClient

    registry = TraceClient(store_url, token)
    try:
        acquired = await asyncio.to_thread(
            registry.operation_acquire, operation_key, request_hash, 300
        )
        status = acquired.get("status")
        operation = acquired.get("operation", {})
        if status == "terminal":
            # The ticket transition was already durably acknowledged for this
            # unavailable named selector; do not send it again on replay.
            return
        if status != "acquired" or not operation.get("fencing_generation"):
            raise RuntimeError("named-device escalation is already in progress")
        fencing_token = int(operation["fencing_generation"])
        await asyncio.to_thread(
            registry.operation_transition, operation_key, "prepared", fencing_token
        )
        await asyncio.to_thread(
            registry.operation_transition,
            operation_key,
            "side-effect-started",
            fencing_token,
        )

        escalation_trace = child_context(
            parent,
            idempotency_key=operation_key,
            idempotency_request_hash=request_hash,
        )
        trace_token = bind_trace_context(escalation_trace)
        try:
            from providers.execution import AuditedAsyncHTTPClient

            reason = result.get("error", "Device unavailable")
            alternatives = result.get("alternatives", [])
            comment = f"Requested device unavailable: {reason}"
            if alternatives:
                comment += f" Available alternatives: {', '.join(alternatives)}"
            headers = {"Authorization": f"Bearer {token}"}
            async with AuditedAsyncHTTPClient(timeout=10.0, headers=headers) as client:
                response = await client.post(
                    f"{store_url}/api/v1/tickets/{ticket_id}/transition",
                    json={
                        "status": "awaiting_customer_guidance",
                        "comment": comment,
                    },
                )
                response.raise_for_status()
        except asyncio.CancelledError:
            # Once the durable operation says its effect started, cancellation
            # cannot safely be treated as a harmless retry: the transition may
            # already have reached the state store.  Make reconciliation
            # explicit before preserving the caller's cancellation signal.
            try:
                await asyncio.to_thread(
                    registry.operation_transition,
                    operation_key,
                    "indeterminate",
                    fencing_token,
                    descriptor={"outcome": "cancelled_after_transition_start"},
                )
            except Exception:
                logger.exception("Failed to mark cancelled named-device escalation")
            raise
        except Exception:
            # A request may have reached the state store before its response
            # was lost.  Preserve that ambiguity for reconciliation instead
            # of issuing a second transition.
            await asyncio.to_thread(
                registry.operation_transition,
                operation_key,
                "indeterminate",
                fencing_token,
                descriptor={"outcome": "transition_request_failed"},
            )
            raise
        finally:
            reset_trace_context(trace_token)
        try:
            await asyncio.to_thread(
                registry.operation_transition,
                operation_key,
                "complete",
                fencing_token,
                descriptor={"transition": "awaiting_customer_guidance"},
            )
        except Exception:
            # The transition may have committed before its operation
            # acknowledgement was lost.  Do not leave a replayable live lease
            # in that ambiguity.
            try:
                await asyncio.to_thread(
                    registry.operation_transition,
                    operation_key,
                    "indeterminate",
                    fencing_token,
                    descriptor={"outcome": "terminal_acknowledgement_failed"},
                )
            except Exception:
                pass
            raise
    finally:
        await asyncio.to_thread(registry.close)


async def get_registered_tools():
    """Introspect this server's registered @mcp.tool() functions."""
    from providers.llm.base import ToolDefinition

    tools = await mcp.list_tools()
    return [
        ToolDefinition(
            name=t.name,
            description=t.description or "",
            input_schema=t.parameters,
        )
        for t in tools
    ]


if __name__ == "__main__":
    mcp.run()


def _get_board_selector(ticket: dict) -> str:
    """Get board_selector from directives or top-level custom_fields.

    Triage may place board_selector in either location depending
    on the model. Check directives first (authoritative), then
    fall back to top-level custom_fields for model-agnostic
    behavior.
    """
    cf = ticket.get("custom_fields", {})
    directives = cf.get("directives", {})
    return directives.get("board_selector", "") or cf.get("board_selector", "")
