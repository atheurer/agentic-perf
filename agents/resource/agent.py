from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from agents.base import AgentBase
from agents.infra.server import cleanup_passwordless_ssh
from agents.mcp_client import AgentMCPClient
from agents.provisioning.server import cleanup_harness
from agents.server_utils import (
    _resolve_vault_secret_name,
    make_traced_ssh,
    resolve_ssh_key,
)
from paths import get_default_ssh_key
from providers.events import EventBus
from providers.llm.base import LLMProvider, LLMResponse, ToolDefinition
from providers.resource.base import has_reservation_metadata, reservation_failed
from providers.resource.registry import PROVIDER_REGISTRY, ResourceProviderRegistry
from providers.secrets.base import SecretsProvider
from providers.ssh import SSHExecutor

from .prompts import RESOURCE_BASE_PROMPT

logger = logging.getLogger(__name__)

_LOCAL_TOOLS = [
    ToolDefinition(
        name="get_accumulated_metadata",
        description=(
            "Return accumulated provider metadata from prior "
            "reserve_resources calls. Includes public_ips, private_ips, "
            "and ip_mapping needed for splitting SSH vs benchmark IPs."
        ),
        input_schema={
            "type": "object",
            "properties": {},
        },
    ),
    ToolDefinition(
        name="submit_resource_result",
        description=(
            "Submit the resource allocation result when host validation is complete."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "assigned_hardware_ips": {
                    "type": "object",
                    "description": "Controller and target host IPs/hostnames",
                    "properties": {
                        "controller": {"type": "string"},
                        "targets": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
                "ssh_user": {"type": "string"},
                "ssh_key_path": {"type": "string"},
                "resource_provider": {
                    "type": "string",
                    "description": (
                        "Provider used: 'quads', 'aws', 'user_provided', etc."
                    ),
                },
                "resource_reservation_id": {
                    "type": ["string", "null"],
                    "description": (
                        "Reservation ID for teardown (from reserve_resources result)"
                    ),
                },
                "resource_provider_metadata": {
                    "type": ["object", "null"],
                    "description": (
                        "Provider-specific metadata for teardown. "
                        "QUADS: {assignment_id, cloud_name}. "
                        "AWS: {instance_ids, region, instance_type}."
                    ),
                },
                "lease_expiration": {"type": ["string", "null"]},
                "fresh_host": {
                    "type": "boolean",
                    "description": (
                        "True if hosts were freshly provisioned and need "
                        "a full harness install. Set true for QUADS and "
                        "cloud providers."
                    ),
                },
                "notes": {"type": "string"},
                "quads_assignment_id": {
                    "type": ["integer", "null"],
                    "description": ("Deprecated: use resource_reservation_id instead"),
                },
                "quads_cloud_name": {
                    "type": ["string", "null"],
                    "description": (
                        "Deprecated: use resource_provider_metadata instead"
                    ),
                },
            },
            "required": ["assigned_hardware_ips", "ssh_user"],
        },
    ),
]


def _match_to_provider_ip(
    host: str, ip_mapping: dict[str, str]
) -> tuple[str, str] | None:
    """Match a host identifier to its (public_ip, private_ip) pair.

    Handles raw IPs and AWS-style hostnames (ip-W-X-Y-Z.*.compute.internal).
    Returns None if no match is found.
    """
    if host in ip_mapping:
        return host, ip_mapping[host]
    reverse = {v: k for k, v in ip_mapping.items()}
    if host in reverse:
        return reverse[host], host
    m = re.match(r"ip-(\d+)-(\d+)-(\d+)-(\d+)", host)
    if m:
        extracted = f"{m.group(1)}.{m.group(2)}.{m.group(3)}.{m.group(4)}"
        if extracted in reverse:
            return reverse[extracted], extracted
    return None


def _auto_reservation_selection(
    provider: str, ticket: dict[str, Any]
) -> dict[str, Any]:
    """Build a complete provider selection from explicit ticket data.

    Automatic reservation must not silently select a provider default. The
    ticket must identify every provider-specific resource needed for this
    fallback, except Jumpstarter's documented one-board selector behavior.
    """
    cf = ticket.get("custom_fields", {})
    directives = cf.get("directives", {})
    allowed_fields = {
        "jumpstarter": {
            "jumpstarter_selector",
            "board_selector",
            "count",
            "lease_duration_seconds",
            "exporter_name",
        },
        "aws": {
            "instance_specs",
            "instance_type",
            "instance_count",
            "count",
            "ami",
            "os",
            "root_volume_gb",
        },
        "quads": {"hostnames", "duration_hours"},
        "psap-cc": {"cluster_id", "duration_hours"},
    }
    if provider not in allowed_fields:
        raise ValueError(
            f"No safe auto-reservation selection for provider '{provider}'"
        )

    selection: dict[str, Any] = {}
    sources: list[dict[str, Any]] = []
    for source in (cf, directives):
        for key in ("resource_selection", f"{provider}_selection", provider):
            value = source.get(key)
            if isinstance(value, dict):
                provider_selection = value.get(provider)
                sources.append(
                    provider_selection
                    if isinstance(provider_selection, dict)
                    else value
                )
        sources.append(source)
    for source in sources:
        for key in allowed_fields[provider]:
            if key in source and source[key] is not None:
                selection[key] = source[key]

    required_hosts = cf.get("required_hosts") or []
    required_hosts = [host for host in required_hosts if isinstance(host, dict)]
    managed_hosts = [host for host in required_hosts if not host.get("host")]

    if provider == "jumpstarter":
        selector = selection.get("jumpstarter_selector") or selection.get(
            "board_selector"
        )
        if not isinstance(selector, str) or not selector.strip():
            raise ValueError("ticket has no board_selector for Jumpstarter")
        selection["jumpstarter_selector"] = selector
        if len(managed_hosts) > 1:
            raise ValueError(
                "Jumpstarter auto-reservation can lease one device per call, "
                f"but the ticket requires {len(managed_hosts)} hosts"
            )
        count = selection.get("count", 1)
        if count != 1:
            raise ValueError("Jumpstarter auto-reservation requires count=1")
        return selection

    if provider == "aws":
        expected_count = len(managed_hosts) if required_hosts else None
        specs = selection.get("instance_specs")
        if not specs and managed_hosts:
            derived_specs = []
            for host in managed_hosts:
                recommended = host.get("recommended") or {}
                instance_type = host.get("instance_type") or recommended.get(
                    "instance_type"
                )
                if not instance_type:
                    derived_specs = []
                    break
                roles = host.get("roles") or []
                if isinstance(roles, str):
                    roles = [roles]
                role = host.get("role") or (roles[0] if roles else None)
                derived_specs.append(
                    {"instance_type": instance_type, "count": 1, "role": role}
                )
            if derived_specs:
                specs = derived_specs
                selection["instance_specs"] = specs

        if specs:
            if not isinstance(specs, list) or not specs:
                raise ValueError("AWS instance_specs must be a nonempty list")
            normalized_specs = []
            total_count = 0
            for spec in specs:
                if not isinstance(spec, dict) or not spec.get("instance_type"):
                    raise ValueError(
                        "each AWS instance_specs entry needs an instance_type"
                    )
                try:
                    count = int(spec.get("count"))
                except (TypeError, ValueError):
                    raise ValueError(
                        "each AWS instance_specs entry needs a positive count"
                    ) from None
                if count < 1:
                    raise ValueError(
                        "each AWS instance_specs entry needs a positive count"
                    )
                normalized_specs.append(
                    {
                        "instance_type": spec["instance_type"],
                        "count": count,
                        **({"role": spec["role"]} if spec.get("role") else {}),
                    }
                )
                total_count += count
            if expected_count is not None and total_count != expected_count:
                raise ValueError(
                    f"AWS selection covers {total_count} instance(s), "
                    f"but the ticket requires {expected_count} managed host(s)"
                )
            selection["instance_specs"] = normalized_specs
            selection.pop("instance_type", None)
            selection.pop("instance_count", None)
            selection.pop("count", None)
            return selection

        instance_type = selection.get("instance_type")
        count = selection.get("count", selection.get("instance_count"))
        if count is None:
            count = expected_count or cf.get("min_hosts")
        if not instance_type or count is None:
            raise ValueError(
                "AWS fallback needs instance_specs or an explicit instance_type "
                "and count (or an exact required_hosts count)"
            )
        try:
            count = int(count)
        except (TypeError, ValueError):
            raise ValueError("AWS count must be a positive integer") from None
        if count < 1 or (expected_count is not None and count != expected_count):
            raise ValueError(
                f"AWS count {count} does not match the ticket's "
                f"{expected_count} managed host(s)"
            )
        selection["count"] = count
        selection.pop("instance_count", None)
        return selection

    if provider == "quads":
        hostnames = selection.get("hostnames")
        if hostnames is None and managed_hosts:
            hostnames = []
            for host in managed_hosts:
                recommended = host.get("recommended") or {}
                hostname = (
                    host.get("quads_hostname")
                    or host.get("provider_hostname")
                    or recommended.get("hostname")
                )
                if not hostname:
                    hostnames = []
                    break
                hostnames.append(hostname)
        if isinstance(hostnames, str):
            hostnames = [name.strip() for name in hostnames.split(",") if name.strip()]
        if not isinstance(hostnames, list) or not hostnames:
            raise ValueError("QUADS fallback requires explicit hostnames")
        if any(not isinstance(name, str) or not name.strip() for name in hostnames):
            raise ValueError("QUADS hostnames must be nonempty strings")
        hostnames = [name.strip() for name in hostnames]
        if len(set(hostnames)) != len(hostnames):
            raise ValueError("QUADS fallback selection contains duplicate hostnames")
        expected_count = len(managed_hosts) if required_hosts else None
        if expected_count is not None and len(hostnames) != expected_count:
            raise ValueError(
                f"QUADS selection has {len(hostnames)} hostname(s), "
                f"but the ticket requires {expected_count} managed host(s)"
            )
        selection["hostnames"] = hostnames
        return selection

    if provider == "psap-cc":
        cluster_id = selection.get("cluster_id")
        if not cluster_id:
            raise ValueError("PSAP-CC fallback requires an explicit cluster_id")
        return selection

    raise ValueError(f"No safe auto-reservation selection for provider '{provider}'")


def _reservation_id_from_metadata(provider: str, metadata: dict[str, Any]) -> str:
    """Return the provider reservation identifier used by teardown."""
    if provider == "jumpstarter":
        value = metadata.get("lease_id")
    elif provider == "aws":
        value = metadata.get("instance_ids")
        if isinstance(value, (list, tuple)):
            value = ",".join(str(item) for item in value if item)
    elif provider == "quads":
        value = metadata.get("assignment_id")
    elif provider == "psap-cc":
        value = metadata.get("reservation_id")
    else:
        value = metadata.get("reservation_id") or metadata.get("lease_id")
    if not value:
        value = metadata.get("reservation_id")
    return str(value) if value is not None else ""


def _jumpstarter_selector_changed(
    fields: dict[str, Any], directives: dict[str, Any]
) -> bool:
    """Return whether an explicit board selector differs from the active lease."""
    requested = (
        directives.get("jumpstarter_selector")
        or directives.get("board_selector")
        or fields.get("jumpstarter_selector")
        or fields.get("board_selector")
    )
    requested_exporter = directives.get("exporter_name") or fields.get("exporter_name")
    metadata = fields.get("resource_provider_metadata") or {}
    if not isinstance(metadata, dict):
        return True
    if requested_exporter and metadata.get("exporter_name") != requested_exporter:
        return True
    if not isinstance(requested, str) or not requested.strip():
        return False

    selector = metadata.get("selector") or metadata.get("jumpstarter_selector")
    exporter_name = metadata.get("exporter_name")
    requested_terms = {}
    for term in requested.split(","):
        key, separator, value = term.strip().partition("=")
        if not separator or not key or not value:
            return True
        requested_terms[key] = value

    if len(requested_terms) == 1:
        key, value = next(iter(requested_terms.items()))
        if key in {"name", "device"}:
            return exporter_name != value

    if not isinstance(selector, str):
        return True
    actual_terms = {}
    for term in selector.split(","):
        key, separator, value = term.strip().partition("=")
        if separator and key not in {"enabled", "pool"}:
            actual_terms[key] = value

    for key, value in requested_terms.items():
        actual_key = "board-type" if key == "target" else key
        if actual_terms.get(actual_key) != value:
            return True
    return False


def _selection_sources(
    provider: str, fields: dict[str, Any], directives: dict[str, Any]
) -> list[dict[str, Any]]:
    """Return provider-specific selection maps in directive precedence order."""
    sources: list[dict[str, Any]] = []
    for source in (fields, directives):
        for key in ("resource_selection", f"{provider}_selection", provider):
            value = source.get(key)
            if isinstance(value, dict):
                nested = value.get(provider)
                sources.append(nested if isinstance(nested, dict) else value)
        sources.append(source)
    return sources


def _explicit_provider_selection(
    provider: str, fields: dict[str, Any], directives: dict[str, Any]
) -> dict[str, Any]:
    allowed = {
        "jumpstarter": {"lease_duration_seconds"},
        "aws": {
            "instance_specs",
            "instance_type",
            "instance_count",
            "count",
            "ami",
            "os",
            "root_volume_gb",
        },
        "quads": {"hostnames", "duration_hours"},
        "psap-cc": {"cluster_id", "duration_hours"},
    }.get(provider, set())
    selection: dict[str, Any] = {}
    for source in _selection_sources(provider, fields, directives):
        for key in allowed:
            if key in source and source[key] is not None:
                selection[key] = source[key]
    return selection


def _saved_aws_specs(
    metadata: dict[str, Any],
) -> tuple[tuple[str, str, int], ...] | None:
    selections = metadata.get("reservation_selections")
    if not isinstance(selections, list) or not selections:
        return None

    totals: dict[tuple[str, str], int] = {}
    for selection in selections:
        if not isinstance(selection, dict):
            return None
        specs = selection.get("instance_specs")
        if isinstance(specs, list) and specs:
            entries = specs
        elif selection.get("instance_type"):
            entries = [
                {
                    "instance_type": selection["instance_type"],
                    "count": selection.get("count", selection.get("instance_count", 1)),
                    "role": selection.get("role"),
                }
            ]
        else:
            return None

        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("instance_type"):
                return None
            try:
                count = int(entry.get("count", 1))
            except (TypeError, ValueError):
                return None
            if count < 1:
                return None
            key = (str(entry.get("role") or ""), str(entry["instance_type"]))
            totals[key] = totals.get(key, 0) + count
    return tuple(
        sorted(
            (role, instance_type, count)
            for (role, instance_type), count in totals.items()
        )
    )


def _requested_aws_specs(
    fields: dict[str, Any], directives: dict[str, Any]
) -> tuple[tuple[str, str, int], ...] | None:
    ticket_fields = dict(fields)
    ticket_fields["directives"] = directives
    try:
        selection = _auto_reservation_selection("aws", {"custom_fields": ticket_fields})
    except ValueError:
        return None

    specs = selection.get("instance_specs")
    if not isinstance(specs, list):
        return None
    return tuple(
        sorted(
            (
                str(spec.get("role") or ""),
                str(spec["instance_type"]),
                int(spec["count"]),
            )
            for spec in specs
        )
    )


def _saved_selection_records(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    records = metadata.get("reservation_selections")
    if isinstance(records, list):
        return [item for item in records if isinstance(item, dict)]
    return []


def _duration_requirement_changed(
    requested: Any, metadata: dict[str, Any], key: str
) -> bool:
    """Require saved selection history to verify an explicit lease duration."""
    if requested is None:
        return False

    def parse_duration(value: Any) -> int | None:
        if isinstance(value, bool) or (
            isinstance(value, float) and not value.is_integer()
        ):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    requested_duration = parse_duration(requested)
    if requested_duration is None:
        return True

    records = _saved_selection_records(metadata)
    if not records:
        return True
    for record in records:
        saved_duration = parse_duration(record.get(key))
        if saved_duration is None:
            return True
        if saved_duration != requested_duration:
            return True
    return False


def _provider_selection_changed(
    provider: str, fields: dict[str, Any], directives: dict[str, Any]
) -> bool:
    """Detect material provider requirements that differ from the saved lease."""
    metadata = fields.get("resource_provider_metadata") or {}
    if not isinstance(metadata, dict):
        return True

    if provider == "jumpstarter":
        requested = _explicit_provider_selection(provider, fields, directives)
        return _jumpstarter_selector_changed(fields, directives) or (
            _duration_requirement_changed(
                requested.get("lease_duration_seconds"),
                metadata,
                "lease_duration_seconds",
            )
        )

    if provider == "aws":
        requested = _explicit_provider_selection(provider, fields, directives)
        for key in ("ami",):
            if requested.get(key) is not None and requested[key] != metadata.get(key):
                return True

        records = _saved_selection_records(metadata)
        for key in ("os", "root_volume_gb"):
            if requested.get(key) is not None and (
                not records
                or any(record.get(key) != requested[key] for record in records)
            ):
                return True

        requested_specs = _requested_aws_specs(fields, directives)
        if requested_specs is not None:
            saved_specs = _saved_aws_specs(metadata)
            if saved_specs is None or saved_specs != requested_specs:
                return True
        elif requested.get("instance_specs") is not None:
            # Malformed or incomplete requirements cannot safely reuse a lease.
            return True

        requested_type = requested.get("instance_type")
        if requested_type:
            active_types = set()
            if metadata.get("instance_type"):
                active_types.add(str(metadata["instance_type"]))
            instance_types = metadata.get("instance_types")
            if isinstance(instance_types, dict):
                active_types.update(str(value) for value in instance_types.values())
            if requested_type not in active_types:
                return True

        requested_count = requested.get("count", requested.get("instance_count"))
        if requested_count is None and fields.get("required_hosts"):
            required_hosts = fields.get("required_hosts") or []
            requested_count = sum(
                1
                for host in required_hosts
                if isinstance(host, dict) and not host.get("host")
            )
        if requested_count is not None:
            try:
                expected_count = int(requested_count)
            except (TypeError, ValueError):
                return True
            instance_ids = metadata.get("instance_ids")
            if isinstance(instance_ids, str):
                actual_count = len(
                    [value for value in instance_ids.split(",") if value]
                )
            elif isinstance(instance_ids, (list, tuple, set)):
                actual_count = len(instance_ids)
            else:
                saved_specs = _saved_aws_specs(metadata)
                actual_count = (
                    sum(item[2] for item in saved_specs) if saved_specs else None
                )
            if actual_count is None or actual_count != expected_count:
                return True

    if provider == "quads":
        requested = _explicit_provider_selection(provider, fields, directives)
        if _duration_requirement_changed(
            requested.get("duration_hours"), metadata, "duration_hours"
        ):
            return True
        requested_hostnames = requested.get("hostnames")
        if requested_hostnames is None:
            try:
                ticket_fields = dict(fields)
                ticket_fields["directives"] = directives
                requested_hostnames = _auto_reservation_selection(
                    "quads", {"custom_fields": ticket_fields}
                ).get("hostnames")
            except ValueError:
                if fields.get("required_hosts"):
                    return True
        if requested_hostnames is not None:
            if isinstance(requested_hostnames, str):
                requested_hostnames = [
                    name.strip()
                    for name in requested_hostnames.split(",")
                    if name.strip()
                ]
            if not isinstance(requested_hostnames, list) or any(
                not isinstance(name, str) or not name.strip()
                for name in requested_hostnames
            ):
                return True
            records = _saved_selection_records(metadata)
            saved_hostnames = [
                hostname
                for record in records
                for hostname in record.get("hostnames", [])
                if isinstance(hostname, str)
            ]
            if not saved_hostnames and isinstance(metadata.get("hostnames"), list):
                saved_hostnames = metadata["hostnames"]
            if not saved_hostnames or sorted(saved_hostnames) != sorted(
                requested_hostnames
            ):
                return True

    if provider == "psap-cc":
        selection = _explicit_provider_selection(provider, fields, directives)
        if _duration_requirement_changed(
            selection.get("duration_hours"), metadata, "duration_hours"
        ):
            return True
        requested = selection.get("cluster_id")
        active = metadata.get("cluster_id")
        if requested is not None and requested != active:
            return True

    return False


def _assigned_hardware_from_reservation(
    ticket: dict[str, Any], hosts: Any, selection: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Map newly reserved provider hosts to the ticket's required roles."""
    if not isinstance(hosts, list) or any(
        not isinstance(host, str) or not host.strip() for host in hosts
    ):
        raise ValueError("provider did not return a list of host identifiers")
    hosts = [host.strip() for host in hosts]
    if len(set(hosts)) != len(hosts):
        raise ValueError("provider returned duplicate host identifiers")

    cf = ticket.get("custom_fields", {})
    required_hosts = [
        item for item in (cf.get("required_hosts") or []) if isinstance(item, dict)
    ]
    managed_hosts = [item for item in required_hosts if not item.get("host")]
    if required_hosts and len(hosts) != len(managed_hosts):
        raise ValueError(
            f"provider returned {len(hosts)} host(s), but the ticket requires "
            f"{len(managed_hosts)} managed host(s)"
        )

    controller = ""
    targets: list[str] = []
    provider_roles: list[str] = []
    instance_specs = (selection or {}).get("instance_specs")
    if isinstance(instance_specs, list):
        for spec in instance_specs:
            if not isinstance(spec, dict) or not spec.get("role"):
                provider_roles = []
                break
            try:
                count = int(spec.get("count", 1))
            except (TypeError, ValueError):
                provider_roles = []
                break
            if count < 1:
                provider_roles = []
                break
            provider_roles.extend([str(spec["role"])] * count)
        if len(provider_roles) != len(hosts):
            provider_roles = []

    assignments: list[tuple[str, list[str]]] = []
    if required_hosts:
        unassigned = set(range(len(hosts)))
        for item in required_hosts:
            roles = item.get("roles") or []
            if isinstance(roles, str):
                roles = [roles]
            host = item.get("host")
            if not host and unassigned:
                if provider_roles and roles:
                    matching = [
                        index
                        for index in sorted(unassigned)
                        if provider_roles[index] in roles
                    ]
                    if not matching:
                        raise ValueError(
                            "AWS instance_specs roles do not match the ticket's "
                            f"required host roles {roles}"
                        )
                    index = matching[0]
                else:
                    index = min(unassigned)
                host = hosts[index]
                unassigned.remove(index)
            if host:
                assignments.append((host, roles))
    else:
        assignments = [
            (host, [provider_roles[index]] if provider_roles else [])
            for index, host in enumerate(hosts)
        ]

    for host, roles in assignments:
        if "controller" in roles and not controller:
            controller = host
        else:
            targets.append(host)
    if not controller and assignments:
        controller = assignments[0][0]
        targets = [host for host, _ in assignments[1:]]

    if not controller:
        raise ValueError("no controller host could be assigned")
    return {"controller": controller, "targets": targets}


class ResourceAgent(AgentBase):
    def __init__(
        self,
        llm_provider: LLMProvider,
        state_store_url: str,
        mode: str = "create",
        secrets_provider: SecretsProvider | None = None,
        event_bus: EventBus | None = None,
        instance_name: str | None = None,
    ) -> None:
        self._mode = mode
        self._ticket_id: str | None = None
        self._secrets = secrets_provider
        self._registry = (
            ResourceProviderRegistry(secrets_provider, instance_name=instance_name)
            if secrets_provider
            else None
        )

        self._ssh: SSHExecutor | None = None

        # Only keep local tools (submit_resource_result) -- MCP tools
        # are added dynamically in run() for create mode.
        local_tools = list(_LOCAL_TOOLS) if mode == "create" else []

        super().__init__(
            agent_name="resource-agent",
            llm_provider=llm_provider,
            state_store_url=state_store_url,
            tools=local_tools,
            tool_handlers={},
            event_bus=event_bus,
        )

    async def _check_fleet_exhaustion(self, context: str = "") -> bool:
        """Route to fleet coordinator if all boards are tested.

        Returns True (and raises HITLDriftError) if fleet exhaustion
        was detected, False otherwise.  Called from both
        _do_request_clarification and _request_human_input so the
        detection is deterministic regardless of which path the
        LLM takes (#994).
        """
        if not self._ticket_id:
            return False
        from providers.fleet import is_fleet_investigation

        ticket = await self._get_ticket(self._ticket_id)
        cf = ticket.get("custom_fields", {})
        if not is_fleet_investigation(cf) or not cf.get(
            "resource_fleet_exhaustion_detected"
        ):
            return False

        await self._add_comment(
            self._ticket_id,
            f"**Fleet: no more boards available**\n\n{context[:500]}",
        )
        await self._transition_ticket(
            self._ticket_id,
            "coordinating_fleet",
            comment="Fleet: no resources available, coordinating exhaustion",
        )
        from agents.base import HITLDriftError

        raise HITLDriftError("Fleet: routed to coordinator")

    async def _do_request_clarification(self, question: str) -> str:
        if self._ticket_id:
            # Fleet exhaustion is checked in _request_human_input
            # override — no need to duplicate here.
            return await self._request_human_input(self._ticket_id, question)
        return "No ticket context available."

    async def _request_human_input(self, ticket_id: str, question: str) -> str:
        """Override base to catch fleet exhaustion on unstructured ends."""
        await self._check_fleet_exhaustion(question)
        return await super()._request_human_input(ticket_id, question)

    async def _reconcile_provider_directive(
        self, ticket_id: str, ticket: dict[str, Any]
    ) -> bool:
        """Release a prior allocation before honoring changed provider intent."""

        async def pause(message: str) -> bool:
            if ticket.get("status") == "awaiting_customer_guidance":
                await self._add_comment(ticket_id, message)
            else:
                await self._transition_ticket(
                    ticket_id,
                    "awaiting_customer_guidance",
                    comment=message,
                )
            return False

        fields = ticket.get("custom_fields", {})
        directives = fields.get("directives", {})
        recorded_provider = fields.get("resource_provider")
        current_provider = recorded_provider
        if not current_provider and fields.get("quads_assignment_id"):
            current_provider = "quads"
        if current_provider and (
            not isinstance(current_provider, str)
            or current_provider not in {*PROVIDER_REGISTRY, "user_provided"}
        ):
            return await pause(
                "**Resource provider reconciliation paused:** The recorded provider "
                f"({current_provider}) is unknown. Allocation details were retained; "
                "identify the provider and reconcile the old allocation before retrying.",
            )
        requested_provider = directives.get("resource_provider") or current_provider
        if not requested_provider:
            metadata = fields.get("resource_provider_metadata") or {}
            metadata = metadata if isinstance(metadata, dict) else {}
            has_unknown_allocation = bool(fields.get("resource_reservation_id")) or any(
                metadata.get(key)
                for key in (
                    "reservation_id",
                    "lease_id",
                    "assignment_id",
                    "instance_ids",
                )
            )
            if has_unknown_allocation:
                return await pause(
                    "**Resource provider reconciliation paused:** A prior reservation "
                    "identity is present, but its provider is unknown. Allocation "
                    "details were retained; identify the provider and reconcile the "
                    "old allocation before retrying.",
                )
            return True

        changed = current_provider != requested_provider
        if current_provider == requested_provider:
            changed = _provider_selection_changed(
                str(current_provider), fields, directives
            )
        if not changed:
            return True

        if (
            requested_provider != current_provider
            and requested_provider != "user_provided"
        ):
            if not self._registry:
                return await pause(
                    "**Resource provider change paused:** The requested provider "
                    f"({requested_provider}) cannot be verified because no provider "
                    "registry is available. The previous allocation was retained.",
                )
            try:
                # Resolve the target before releasing the active allocation. This
                # verifies that it is a known, configured provider while preserving
                # the old resources if the requested provider cannot be used.
                await self._registry.get_provider(str(requested_provider))
            except Exception as exc:
                logger.warning(
                    "[resource] Requested provider %s is unavailable: %s",
                    requested_provider,
                    type(exc).__name__,
                )
                return await pause(
                    "**Resource provider change paused:** The requested provider "
                    f"({requested_provider}) is unknown or not configured. The "
                    "previous allocation was retained; configure the provider or "
                    "choose another directive before retrying.",
                )

        metadata = fields.get("resource_provider_metadata") or {}
        metadata = metadata if isinstance(metadata, dict) else {}
        reservation_id = fields.get("resource_reservation_id")
        if not reservation_id and current_provider:
            reservation_id = _reservation_id_from_metadata(
                str(current_provider), metadata
            )
        allocations: list[tuple[str, str, dict[str, Any]]] = []
        if current_provider and current_provider != "user_provided":
            if not reservation_id and current_provider == "quads":
                reservation_id = fields.get("quads_assignment_id")
                if reservation_id:
                    metadata = {
                        **metadata,
                        "assignment_id": reservation_id,
                        "cloud_name": fields.get("quads_cloud_name"),
                    }
            if not reservation_id:
                return await pause(
                    "**Resource provider change paused:** The previous managed "
                    f"provider ({current_provider}) has no verifiable reservation "
                    "ID. Its allocation details were retained, and no new provider "
                    "was selected. Reconcile the old allocation before retrying.",
                )
            allocations.append((str(current_provider), str(reservation_id), metadata))
        elif not current_provider and (
            fields.get("resource_reservation_id")
            or any(
                metadata.get(key)
                for key in (
                    "reservation_id",
                    "lease_id",
                    "assignment_id",
                    "instance_ids",
                )
            )
        ):
            return await pause(
                "**Resource provider change paused:** A prior reservation identity "
                "is present but its provider is unknown. Allocation details were "
                "retained, and no new provider was selected.",
            )

        legacy_assignment_id = fields.get("quads_assignment_id")
        if legacy_assignment_id and not (
            current_provider == "quads"
            and reservation_id is not None
            and str(reservation_id) == str(legacy_assignment_id)
        ):
            allocations.append(
                (
                    "quads",
                    str(legacy_assignment_id),
                    {
                        "assignment_id": legacy_assignment_id,
                        "cloud_name": fields.get("quads_cloud_name"),
                    },
                )
            )

        for provider_name, allocation_id, allocation_metadata in allocations:
            if not self._registry:
                return await pause(
                    "**Resource provider change paused:** The previous managed "
                    f"reservation ({provider_name} {allocation_id}) cannot be "
                    "released because no provider registry is available. Its "
                    "allocation details were retained, and no new provider was "
                    "selected.",
                )
            if not await self._terminate_provider_resources(
                ticket_id,
                provider_name,
                allocation_id,
                allocation_metadata,
            ):
                return await pause(
                    "**Resource provider change paused:** Release of the previous "
                    f"{provider_name} reservation {allocation_id} was not "
                    "confirmed. Its allocation details were retained, and no new "
                    "provider was selected.",
                )

        reset_fields = {
            "resource_provider": None,
            "resource_reservation_id": None,
            "resource_provider_metadata": {},
            "quads_assignment_id": None,
            "quads_cloud_name": None,
            "assigned_hardware_ips": {"controller": "", "targets": []},
            "ssh_hardware_ips": {"controller": "", "targets": []},
            "lease_expiration": None,
            "fresh_host": False,
            "jumpstarter_flash": None,
            "platform_ready": False,
        }
        await self._update_fields(ticket_id, reset_fields)
        fields.update(reset_fields)
        return True

    async def run(self, ticket_id: str) -> None:
        if self._mode == "teardown":
            await self._run_teardown(ticket_id)
            return
        self._ticket_id = ticket_id

        from providers.tracing import (
            bind_trace_context,
            current_trace_context,
            new_trace_context,
            reset_trace_context,
        )

        trace_context = (
            self.trace_context
            or current_trace_context()
            or new_trace_context(
                ticket_id=ticket_id,
                agent_id=self.agent_name,
            )
        )
        self.trace_context = trace_context
        trace_token = bind_trace_context(trace_context)
        try:
            ticket = await self._get_ticket(ticket_id)
            ticket_fields = ticket.get("custom_fields", {})
            if ticket_fields.get("resource_reservation_outcome_unknown") is True:
                await self._add_comment(
                    ticket_id,
                    "**Resource allocation remains paused:** Reconcile the provider "
                    "state for the interrupted reservation. If an allocation is "
                    "active, record its verified reservation ID and provider "
                    "metadata for teardown. If none is active, clear stale "
                    "reservation ID and provider metadata. Then clear "
                    "`resource_reservation_outcome_unknown`. No new allocation was "
                    "attempted.",
                )
                if ticket.get("status") != "awaiting_customer_guidance":
                    await self._transition_ticket(
                        ticket_id,
                        "awaiting_customer_guidance",
                        comment="Provider reservation requires human reconciliation",
                    )
                return
            if not await self._reconcile_provider_directive(ticket_id, ticket):
                return
        finally:
            reset_trace_context(trace_token)

        resource_server = str(Path(__file__).with_name("server.py"))

        mcp = AgentMCPClient()
        await mcp.connect_ticket_server(
            resource_server,
            name="resource",
            ticket_id=ticket_id,
            state_store_url=self.store_url,
            agent_name=self.agent_name,
        )
        self._mcp = mcp

        mcp_tools = [
            t for t in await mcp.list_tools() if t.name != "get_accumulated_metadata"
        ]
        self.tools = mcp_tools + self.tools

        try:
            await super().run(ticket_id)
        finally:
            await mcp.disconnect()
            self._mcp = None

        # Fleet: if the agent escalated to HITL (via any
        # path — request_clarification or end_turn), redirect
        # to the fleet coordinator.
        from providers.fleet import is_fleet_investigation

        try:
            ticket = await self._get_ticket(ticket_id)
            if (
                ticket.get("status") == "awaiting_customer_guidance"
                and is_fleet_investigation(ticket.get("custom_fields", {}))
                and ticket.get("custom_fields", {}).get(
                    "resource_fleet_exhaustion_detected"
                )
            ):
                await self._add_comment(
                    ticket_id,
                    "**Fleet: resource agent could not "
                    "acquire a board — routing to "
                    "coordinator.**",
                )
                await self._transition_ticket(
                    ticket_id,
                    "coordinating_fleet",
                    comment=("Fleet: resource exhaustion, coordinating"),
                )
        except Exception:
            pass

    async def _run_teardown(self, ticket_id: str) -> None:
        logger.info(f"[resource-agent] Teardown for ticket {ticket_id}")
        ticket = await self._get_ticket(ticket_id)
        fields = ticket.get("custom_fields", {})
        directives = fields.get("directives", {})

        async def pause_teardown(message: str) -> None:
            if ticket.get("status") != "awaiting_customer_guidance":
                await self._transition_ticket(
                    ticket_id,
                    "awaiting_customer_guidance",
                    comment=message,
                )

        skip_teardown = directives.get("skip_teardown")
        if skip_teardown is None:
            # Ticket did not express a preference; fall back to
            # the operator's persistent config default.
            from orchestrator.config import _load_config_file

            skip_teardown = _load_config_file().get("skip_teardown", False)

        if skip_teardown:
            logger.info(
                f"[resource-agent] skip_teardown set, skipping cleanup for {ticket_id}"
            )
            await self._add_comment(
                ticket_id,
                "Teardown skipped per skip_teardown directive."
                " Hosts and data preserved.",
            )
            if await self._plan_controls_next_transition(ticket_id):
                return
            await self._transition_ticket(
                ticket_id,
                "retrospective_pending",
                comment="Teardown skipped, starting retrospective",
            )
            return

        host_cleanup = directives.get(
            "host_cleanup", fields.get("host_cleanup", "required")
        )

        preserve_roles = fields.get("teardown_preserve_roles", [])
        selective = bool(preserve_roles)

        if selective:
            teardown_confirmed = await self._run_selective_teardown(
                ticket_id,
                fields,
                preserve_roles,
                host_cleanup,
            )
            if not teardown_confirmed:
                await pause_teardown(
                    "Resource teardown could not confirm release; allocation "
                    "details were retained for manual reconciliation."
                )
                return
        else:
            if host_cleanup == "required":
                await self._run_host_cleanup(ticket_id, fields)
            if not await self._terminate_all(ticket_id, fields):
                await pause_teardown(
                    "Resource teardown could not confirm release; allocation "
                    "details were retained for manual reconciliation."
                )
                return

        # Clear the transient flag
        if selective:
            await self._update_fields(
                ticket_id,
                {"teardown_preserve_roles": None},
            )

        if await self._plan_controls_next_transition(ticket_id):
            logger.info(f"[resource-agent] Teardown complete for {ticket_id}")
            return
        await self._transition_ticket(
            ticket_id,
            "retrospective_pending",
            comment="Resource teardown complete, starting retrospective",
        )
        logger.info(f"[resource-agent] Teardown complete for {ticket_id}")

    async def _terminate_all(
        self,
        ticket_id: str,
        fields: dict,
    ) -> bool:
        provider_name = fields.get("resource_provider")
        reservation_id = fields.get("resource_reservation_id")
        provider_metadata = fields.get("resource_provider_metadata") or {}
        if not isinstance(provider_metadata, dict):
            provider_metadata = {}

        if not provider_name and fields.get("quads_assignment_id"):
            provider_name = "quads"
            reservation_id = str(fields["quads_assignment_id"])
            provider_metadata = {
                "assignment_id": fields["quads_assignment_id"],
                "cloud_name": fields.get("quads_cloud_name"),
            }

        if provider_name and provider_name != "user_provided":
            reservation_id = reservation_id or _reservation_id_from_metadata(
                str(provider_name), provider_metadata
            )
            if not reservation_id:
                await self._add_comment(
                    ticket_id,
                    f"Cannot confirm teardown for {provider_name}: no reservation "
                    "ID is available. Allocation details were retained.",
                )
                return False
            return await self._terminate_provider_resources(
                ticket_id, str(provider_name), str(reservation_id), provider_metadata
            )

        has_allocation_identity = bool(
            reservation_id
            or fields.get("quads_assignment_id")
            or any(
                provider_metadata.get(key)
                for key in (
                    "reservation_id",
                    "lease_id",
                    "assignment_id",
                    "instance_ids",
                )
            )
        )
        if has_allocation_identity:
            await self._add_comment(
                ticket_id,
                "Cannot safely confirm teardown because allocation identity is "
                "present but no managed provider is recorded. Allocation details "
                "were retained for provider reconciliation.",
            )
            return False

        await self._add_comment(
            ticket_id,
            "Resources released (no managed reservation to terminate).",
        )
        return True

    async def _run_selective_teardown(
        self,
        ticket_id: str,
        fields: dict,
        preserve_roles: list[str],
        host_cleanup: str,
    ) -> bool:
        """Teardown only hosts whose roles are NOT in preserve_roles."""
        hw = fields.get("ssh_hardware_ips") or fields.get(
            "assigned_hardware_ips",
            {},
        )
        assigned = fields.get("assigned_hardware_ips", {})
        controller = hw.get("controller")
        targets = hw.get("targets", [])
        assigned_targets = assigned.get("targets", [])

        # Determine which hosts to keep vs tear down
        keep_controller = "controller" in preserve_roles
        teardown_targets = list(targets)
        teardown_assigned_targets = list(assigned_targets)

        teardown_hosts = []
        if not keep_controller and controller:
            teardown_hosts.append(controller)
        teardown_hosts.extend(teardown_targets)

        preserve_summary = ", ".join(preserve_roles)
        await self._add_comment(
            ticket_id,
            f"**Selective teardown** — preserving roles: {preserve_summary}",
        )

        if host_cleanup == "required" and teardown_hosts:
            ssh_key_path = fields.get("ssh_key_path")
            harness_name = fields.get("harness_name")
            vault_secret = _resolve_vault_secret_name(fields)
            async with resolve_ssh_key(
                ssh_key_path,
                self._secrets,
                vault_secret,
            ) as resolved_key:
                ssh = make_traced_ssh(key_path=resolved_key)
                cleanup_summary = []
                if harness_name:
                    for host in teardown_hosts:
                        try:
                            result = await cleanup_harness(
                                ssh,
                                host,
                                harness_name,
                            )
                            cleanup_summary.append(
                                f"Harness on {host}: {result['status']}",
                            )
                        except Exception as e:
                            cleanup_summary.append(
                                f"Harness on {host}: failed ({e})",
                            )
                if cleanup_summary:
                    await self._add_comment(
                        ticket_id,
                        "**Host Cleanup (selective)**\n\n"
                        + "\n".join(f"- {s}" for s in cleanup_summary),
                    )

        # Terminate only the non-preserved instances
        provider_name = fields.get("resource_provider")
        if not provider_name and fields.get("quads_assignment_id"):
            provider_name = "quads"
        provider_metadata = fields.get("resource_provider_metadata") or {}
        if not isinstance(provider_metadata, dict):
            provider_metadata = {}
        if (
            provider_name
            and provider_name != "user_provided"
            and provider_metadata.get("instance_ids")
        ):
            all_instance_ids = provider_metadata["instance_ids"]
            if isinstance(all_instance_ids, str):
                all_instance_ids = [
                    item.strip() for item in all_instance_ids.split(",") if item.strip()
                ]
            all_public_ips = provider_metadata.get("public_ips", [])
            all_private_ips = provider_metadata.get("private_ips", [])

            # Build list of instance IDs to terminate by matching
            # teardown hosts to provider IPs
            teardown_set = set(teardown_hosts)
            if assigned_targets:
                teardown_set.update(teardown_assigned_targets)
            mapped_hosts = set()
            for i in range(len(all_instance_ids)):
                pub = all_public_ips[i] if i < len(all_public_ips) else ""
                priv = all_private_ips[i] if i < len(all_private_ips) else ""
                if pub:
                    mapped_hosts.add(pub)
                if priv:
                    mapped_hosts.add(priv)
            unmapped_hosts = teardown_set - mapped_hosts
            if unmapped_hosts:
                await self._add_comment(
                    ticket_id,
                    "Selective teardown could not map every requested host to a "
                    "provider instance ID. Allocation details were retained.",
                )
                return False

            terminate_ids = []
            keep_ids = []
            for i, iid in enumerate(all_instance_ids):
                pub = all_public_ips[i] if i < len(all_public_ips) else ""
                priv = all_private_ips[i] if i < len(all_private_ips) else ""
                if pub in teardown_set or priv in teardown_set:
                    terminate_ids.append(iid)
                else:
                    keep_ids.append(iid)

            if terminate_ids:
                teardown_metadata = dict(provider_metadata)
                teardown_metadata["instance_ids"] = terminate_ids
                if not await self._terminate_provider_resources(
                    ticket_id,
                    provider_name,
                    ",".join(str(item) for item in terminate_ids),
                    teardown_metadata,
                ):
                    return False
            elif teardown_hosts:
                await self._add_comment(
                    ticket_id,
                    "Selective teardown could not match the requested hosts to "
                    "provider instance IDs. Allocation details were retained.",
                )
                return False

            # Update assigned_hardware_ips to reflect preserved hosts
            new_hw: dict[str, Any] = {}
            new_ssh_hw: dict[str, Any] = {}
            if keep_controller and controller:
                new_hw["controller"] = assigned.get("controller", controller)
                new_ssh_hw["controller"] = controller
            new_hw["targets"] = [t for t in assigned_targets if t not in teardown_set]
            new_ssh_hw["targets"] = [t for t in targets if t not in teardown_set]

            # Update provider_metadata to only track kept instances
            new_metadata = dict(provider_metadata)
            new_metadata["instance_ids"] = keep_ids
            new_metadata["public_ips"] = [
                ip for ip in all_public_ips if ip not in teardown_set
            ]
            new_metadata["private_ips"] = [
                ip for ip in all_private_ips if ip not in teardown_set
            ]

            await self._update_fields(
                ticket_id,
                {
                    "assigned_hardware_ips": new_hw,
                    "ssh_hardware_ips": new_ssh_hw,
                    "resource_provider_metadata": new_metadata,
                },
            )
            return True
        else:
            if provider_name and provider_name != "user_provided":
                await self._add_comment(
                    ticket_id,
                    f"Cannot safely perform selective teardown for provider "
                    f"{provider_name}; allocation details were retained.",
                )
                return False
            if fields.get("quads_assignment_id"):
                await self._add_comment(
                    ticket_id,
                    "Cannot safely perform selective teardown for the legacy QUADS "
                    "assignment; allocation details were retained.",
                )
                return False
            if fields.get("resource_reservation_id") or any(
                provider_metadata.get(key)
                for key in (
                    "reservation_id",
                    "lease_id",
                    "assignment_id",
                    "instance_ids",
                )
            ):
                await self._add_comment(
                    ticket_id,
                    "Cannot safely perform selective teardown because the previous "
                    "provider is unknown. Allocation details were retained.",
                )
                return False
            await self._add_comment(
                ticket_id,
                "Selective teardown: no managed instances to terminate.",
            )
            return True

    async def _terminate_provider_resources(
        self,
        ticket_id: str,
        provider_name: str,
        reservation_id: str,
        provider_metadata: dict[str, Any],
    ) -> bool:
        if not self._registry:
            await self._add_comment(
                ticket_id,
                f"Cannot terminate {provider_name} reservation {reservation_id}: "
                f"no secrets provider configured. Manual cleanup required.",
            )
            return False

        try:
            provider = await self._registry.get_provider(provider_name)
            result = await provider.terminate(reservation_id, provider_metadata)
            status = str(result.get("status", "")).strip().lower()
            if status not in {"terminated", "released"}:
                await self._add_comment(
                    ticket_id,
                    f"Could not confirm release of {provider_name} reservation "
                    f"{reservation_id} (provider status: {status or 'missing'}). "
                    "Allocation details were retained.",
                )
                return False
            await self._add_comment(
                ticket_id,
                f"{provider_name} reservation {reservation_id} terminated.",
            )
            logger.info(
                f"[resource-agent] {provider_name} reservation "
                f"{reservation_id} terminated: {result}"
            )
            return True
        except Exception as e:
            logger.exception(
                f"[resource-agent] Failed to terminate {provider_name} "
                f"reservation {reservation_id}"
            )
            await self._add_comment(
                ticket_id,
                f"Failed to terminate {provider_name} reservation "
                f"{reservation_id}: {e}",
            )
            return False

    async def _run_host_cleanup(self, ticket_id: str, fields: dict) -> None:
        hw = fields.get("ssh_hardware_ips") or fields.get("assigned_hardware_ips", {})
        controller = hw.get("controller")
        targets = hw.get("targets", [])
        ssh_key_path = fields.get("ssh_key_path")
        harness_name = fields.get("harness_name")
        all_hosts = ([controller] if controller else []) + targets

        if not all_hosts:
            logger.info("[resource-agent] No hosts to clean up")
            return

        vault_secret = _resolve_vault_secret_name(fields)
        async with resolve_ssh_key(
            ssh_key_path,
            self._secrets,
            vault_secret,
        ) as resolved_key:
            ssh = make_traced_ssh(key_path=resolved_key)
            cleanup_summary = []

            if controller and targets:
                try:
                    result = await cleanup_passwordless_ssh(
                        ssh,
                        controller,
                        targets,
                    )
                    cleanup_summary.append(
                        f"Controller SSH keys: {result['status']}",
                    )
                    logger.info(
                        "[resource-agent] Controller key cleanup: %s",
                        result,
                    )
                except Exception as e:
                    cleanup_summary.append(
                        f"Controller SSH keys: failed ({e})",
                    )
                    logger.exception(
                        "[resource-agent] Controller key cleanup failed",
                    )

            if harness_name:
                for host in all_hosts:
                    try:
                        result = await cleanup_harness(ssh, host, harness_name)
                        cleanup_summary.append(
                            f"Harness on {host}: {result['status']}",
                        )
                        logger.info(
                            "[resource-agent] Harness cleanup on %s: %s",
                            host,
                            result,
                        )
                    except Exception as e:
                        cleanup_summary.append(
                            f"Harness on {host}: failed ({e})",
                        )
                        logger.exception(
                            "[resource-agent] Harness cleanup on %s failed",
                            host,
                        )

            # Provider-specific SSH key cleanup
            provider_name = fields.get("resource_provider")
            if not provider_name and fields.get("quads_assignment_id"):
                provider_name = "quads"

            if provider_name and provider_name != "user_provided" and self._registry:
                try:
                    provider = await self._registry.get_provider(
                        provider_name,
                    )
                    result = await provider.cleanup_ssh_keys(all_hosts)
                    cleanup_summary.append(
                        f"{provider_name} SSH keys: {result.get('status', 'done')}",
                    )
                    logger.info(
                        "[resource-agent] %s key cleanup: %s",
                        provider_name,
                        result,
                    )
                except Exception as e:
                    cleanup_summary.append(
                        f"{provider_name} SSH keys: failed ({e})",
                    )
                    logger.exception(
                        "[resource-agent] %s key cleanup failed",
                        provider_name,
                    )

            if cleanup_summary:
                await self._add_comment(
                    ticket_id,
                    "**Host Cleanup**\n\n"
                    + "\n".join(f"- {s}" for s in cleanup_summary),
                )

    def _system_prompt(self, ticket: dict[str, Any]) -> str:
        cf = ticket.get("custom_fields", {})
        directives = cf.get("directives", {})
        provider = directives.get("resource_provider") or cf.get("resource_provider")
        endpoint = directives.get("endpoint_type", "remotehosts")

        fragments = self._load_prompt_fragments(
            Path(__file__).parent,
            resource_provider=provider,
            endpoint_type=endpoint,
        )
        if fragments:
            return f"{RESOURCE_BASE_PROMPT}\n\n{fragments}"
        return RESOURCE_BASE_PROMPT

    def _build_messages(self, ticket: dict[str, Any]) -> list[dict[str, Any]]:
        scoped = self._get_scoped_context(ticket, "resource")
        if scoped is not None:
            content = (
                f"## Performance Test Request\n\n"
                f"**Ticket ID:** {ticket['id']}\n\n"
                f"{scoped}\n"
            )
        else:
            content = (
                f"## Performance Test Request\n\n"
                f"**Ticket ID:** {ticket['id']}\n"
                f"**Summary:** {ticket['summary']}\n\n"
                f"**Description:**\n{ticket['description']}\n"
            )

        fields = ticket.get("custom_fields", {})
        directives = fields.get("directives", {})
        if directives:
            content += "\n## Directives\n"
            for key, val in directives.items():
                content += f"- **{key}:** {val}\n"

        required_hosts = fields.get("required_hosts", [])
        endpoint_type = directives.get("endpoint_type", "remotehosts")
        if required_hosts:
            content += "\n## Resource Requirements\n"
            has_identity = any(h.get("host") for h in required_hosts)
            all_identity = has_identity and all(h.get("host") for h in required_hosts)
            for i, h in enumerate(required_hosts, 1):
                roles_str = "+".join(h.get("roles", ["?"]))
                specs = []
                if h.get("host"):
                    specs.append(f"host: {h['host']}")
                if h.get("nic_speed"):
                    specs.append(f"NIC: {h['nic_speed']}Gbps")
                if h.get("min_memory_gb"):
                    specs.append(f"RAM: ≥{h['min_memory_gb']}GB")
                if h.get("min_cores"):
                    specs.append(f"CPU: ≥{h['min_cores']} cores")
                if h.get("os"):
                    specs.append(f"OS: {h['os']}")
                spec_str = f" ({', '.join(specs)})" if specs else ""
                content += f"- Host {i}: **{roles_str}**{spec_str}\n"
            if all_identity:
                content += (
                    "\n**All hosts are user-provided existing machines.** "
                    "Validate each with validate_host and submit these "
                    "exact identities — do not allocate from a provider.\n"
                )
            elif has_identity:
                content += (
                    "\n**Some hosts are user-provided existing machines** "
                    "(those with a 'host' value above). Validate those "
                    "with validate_host and submit their exact identities.\n"
                )
            if endpoint_type == "kube":
                content += "- **Endpoint type:** kube (workloads run as pods)\n"
                content += "- **Total hosts to provision:** 1 (single host: controller + K8s cluster)\n"

        # Fleet investigation context
        fleet = fields.get("fleet_investigation", {})
        if fleet.get("enabled"):
            tested = fleet.get("tested_hosts", [])
            if tested:
                content += "\n## Fleet Investigation\n"
                content += (
                    f"This is a fleet investigation "
                    f"(iteration {len(tested) + 1}). "
                    f"Already tested {len(tested)} host(s). "
                    f"Acquire the NEXT untested board. "
                    f"Exclude hosts are handled automatically "
                    f"by the system.\n"
                )
            else:
                content += "\n## Fleet Investigation\n"
                content += (
                    "This is a fleet investigation "
                    "(first iteration). Acquire one board "
                    "to begin testing.\n"
                )

        user_comments = self._user_comments(ticket)
        if user_comments:
            content += "\n## Previous Comments\n"
            for comment in user_comments:
                content += f"\n**{comment['author']}:** {comment['body']}\n"

        return [{"role": "user", "content": content}]

    async def _handle_completion(self, ticket_id: str, response: LLMResponse) -> None:
        result = self._get_submit_result(response)
        if not result:
            result = self._parse_json_response(response.text)
        if not result:
            result = {
                "assigned_hardware_ips": {},
                "ssh_user": "root",
                "ssh_key_path": get_default_ssh_key(),
                "notes": "Could not produce structured output",
            }

        # Do not let an LLM-submitted allocation bypass a confirmed
        # exhaustion result from check_available_resources.
        await self._check_fleet_exhaustion(str(result.get("notes", "")))

        ticket_context = await self._get_ticket(ticket_id)
        ticket_cf = ticket_context.get("custom_fields", {})
        ticket_directives = ticket_cf.get("directives", {})
        directed_provider = ticket_directives.get("resource_provider")
        rp = (
            directed_provider
            or ticket_cf.get("resource_provider")
            or result.get("resource_provider")
            or "user_provided"
        )
        existing_reservation_id = ticket_cf.get("resource_reservation_id")
        submitted_reservation_id = result.get("resource_reservation_id")

        fields: dict[str, Any] = {
            "assigned_hardware_ips": result.get("assigned_hardware_ips", {}),
            "ssh_user": result.get("ssh_user", "root"),
            "ssh_key_path": result.get("ssh_key_path") or get_default_ssh_key(),
            "lease_expiration": result.get("lease_expiration"),
            "resource_provider": rp,
        }

        if existing_reservation_id:
            fields["resource_reservation_id"] = existing_reservation_id
        elif rp == "user_provided" and submitted_reservation_id:
            fields["resource_reservation_id"] = submitted_reservation_id

        submitted_metadata = dict(result.get("resource_provider_metadata") or {})
        reservation_metadata: dict[str, Any] = {}
        if self._mcp:
            try:
                raw = await self._mcp.call_tool("get_accumulated_metadata", {})
                fetched_metadata = json.loads(raw) if raw else {}
                if isinstance(fetched_metadata, dict):
                    reservation_metadata = fetched_metadata
            except Exception:
                logger.debug("get_accumulated_metadata unavailable, skipping")
        if reservation_metadata.get("allocation_unknown") is True:
            prior_metadata = {
                key: value
                for key, value in reservation_metadata.items()
                if key not in {"allocation_unknown", "retry_blocked"}
            }
            if not prior_metadata:
                ticket_metadata = ticket_cf.get("resource_provider_metadata") or {}
                if isinstance(ticket_metadata, dict):
                    prior_metadata = dict(ticket_metadata)
            review_fields: dict[str, Any] = {
                "resource_provider": rp,
                "resource_reservation_outcome_unknown": True,
            }
            known_reservation_id = existing_reservation_id or (
                _reservation_id_from_metadata(rp, prior_metadata)
            )
            if known_reservation_id:
                review_fields["resource_reservation_id"] = known_reservation_id
            if prior_metadata:
                review_fields["resource_provider_metadata"] = prior_metadata
            await self._update_fields(ticket_id, review_fields)
            message = (
                "**Resource reservation outcome unknown:** The provider may have "
                "allocated resources before an error. Automatic reservation is "
                "paused to avoid duplicates. If an allocation is active, record "
                "its verified reservation ID and provider metadata for teardown. "
                "If none is active, clear stale reservation ID and provider "
                "metadata, then clear `resource_reservation_outcome_unknown`."
            )
            await self._add_comment(ticket_id, message)
            await self._transition_ticket(
                ticket_id,
                "awaiting_customer_guidance",
                comment="Resource reservation outcome is unknown; manual review required",
            )
            return
        # The reservation server is the source of truth for managed-provider
        # metadata. Do not let LLM-supplied IDs or selectors suppress a needed
        # reservation or redirect later provisioning and teardown actions.
        if rp and rp != "user_provided":
            provider_metadata = reservation_metadata or dict(
                ticket_cf.get("resource_provider_metadata") or {}
            )
        else:
            provider_metadata = submitted_metadata
        # Always set provider_metadata when we have a
        # reservation — downstream agents (platform,
        # provisioning) require it for lease operations.
        if provider_metadata or reservation_metadata:
            fields["resource_provider_metadata"] = (
                provider_metadata or reservation_metadata
            )
        metadata_reservation_id = _reservation_id_from_metadata(rp, provider_metadata)
        if not fields.get("resource_reservation_id") and metadata_reservation_id:
            fields["resource_reservation_id"] = metadata_reservation_id

        # Invariant: managed providers must have a reservation.
        # If the LLM skipped reserve_resources, call it now
        # using the ticket's directives.  "LLM decides intent;
        # code enforces invariants" — the LLM chose the board,
        # but actually reserving it is not optional (#1128).
        meta = fields.get("resource_provider_metadata") or {}
        if rp and rp != "user_provided":
            has_reservation_fields = has_reservation_metadata(rp, meta) or bool(
                existing_reservation_id
            )
            if not has_reservation_fields:
                if not self._mcp:
                    await self._add_comment(
                        ticket_id,
                        "**Resource submission rejected:** Could not verify a "
                        f"{rp} reservation because the resource service is "
                        "unavailable. No provider defaults were used.",
                    )
                    return
                logger.warning(
                    "[resource] No reservation metadata — auto-reserving for %s via %s",
                    ticket_id,
                    rp,
                )
                try:
                    selection = _auto_reservation_selection(rp, ticket_context)
                except ValueError as exc:
                    await self._add_comment(
                        ticket_id,
                        "**Auto-reservation failed:** Cannot safely select "
                        f"{rp} resources: {exc}. Add a complete provider "
                        "selection to the ticket and retry.",
                    )
                    return

                selection_record = dict(selection)
                duration_hours = selection.pop("duration_hours", None)
                selection_record["duration_hours"] = (
                    duration_hours if duration_hours is not None else 36
                )
                reserve_args: dict[str, Any] = {
                    "provider": rp,
                    "selection": selection,
                    "description": ticket_context.get("summary", ""),
                    "ticket_id": ticket_id,
                }
                if duration_hours is not None:
                    reserve_args["duration_hours"] = duration_hours
                try:
                    raw = await self._mcp.call_tool("reserve_resources", reserve_args)
                    parsed_result = json.loads(raw) if raw else {}
                    reserve_result = (
                        parsed_result if isinstance(parsed_result, dict) else {}
                    )
                    if reservation_failed(reserve_result):
                        failure = (
                            reserve_result.get("error")
                            or reserve_result.get("message")
                            or reserve_result.get("status")
                            or "provider reported failure"
                        )
                        if reserve_result.get("allocation_unknown") is True:
                            unknown_metadata = dict(
                                reserve_result.get("provider_metadata") or {}
                            )
                            for key in (
                                "lease_id",
                                "instance_ids",
                                "assignment_id",
                                "reservation_id",
                            ):
                                if key in reserve_result:
                                    unknown_metadata.setdefault(
                                        key, reserve_result[key]
                                    )
                            unknown_id = (
                                reserve_result.get("reservation_id")
                                or reserve_result.get("lease_id")
                                or _reservation_id_from_metadata(rp, unknown_metadata)
                            )
                            review_fields: dict[str, Any] = {
                                "resource_provider": rp,
                                "resource_reservation_outcome_unknown": True,
                            }
                            if unknown_id:
                                review_fields["resource_reservation_id"] = str(
                                    unknown_id
                                )
                            if unknown_metadata:
                                review_fields["resource_provider_metadata"] = (
                                    unknown_metadata
                                )
                            await self._update_fields(ticket_id, review_fields)
                            await self._add_comment(
                                ticket_id,
                                "**Resource reservation outcome unknown:** "
                                f"{failure} The provider may have allocated "
                                "resources before the error. Automatic retries "
                                "are blocked. If an allocation is active, record "
                                "its verified reservation ID and provider metadata "
                                "for teardown. If none is active, clear stale "
                                "reservation ID and provider metadata, then clear "
                                "`resource_reservation_outcome_unknown`.",
                            )
                            await self._transition_ticket(
                                ticket_id,
                                "awaiting_customer_guidance",
                                comment="Provider allocation outcome is unknown",
                            )
                            return
                        await self._add_comment(
                            ticket_id,
                            f"**Auto-reservation failed:** {failure}",
                        )
                        return

                    fallback_id = reserve_result.get(
                        "reservation_id"
                    ) or reserve_result.get("lease_id")
                    if fallback_id and not fields.get("resource_reservation_id"):
                        fields["resource_reservation_id"] = str(fallback_id)

                    result_metadata = dict(
                        reserve_result.get("provider_metadata") or {}
                    )
                    for key in (
                        "lease_id",
                        "instance_ids",
                        "assignment_id",
                        "reservation_id",
                        "ssh_user",
                        "ssh_key_path",
                    ):
                        if key in reserve_result and key not in result_metadata:
                            result_metadata[key] = reserve_result[key]
                    try:
                        raw = await self._mcp.call_tool("get_accumulated_metadata", {})
                        fetched_metadata = json.loads(raw) if raw else {}
                        reservation_metadata = (
                            fetched_metadata
                            if isinstance(fetched_metadata, dict)
                            else {}
                        )
                    except Exception:
                        logger.debug(
                            "Could not re-fetch reservation metadata after reserve"
                        )
                    if not reservation_metadata:
                        reservation_metadata = result_metadata
                    selection_history = reservation_metadata.get(
                        "reservation_selections"
                    )
                    selection_history = (
                        list(selection_history)
                        if isinstance(selection_history, list)
                        else []
                    )
                    if selection_record not in selection_history:
                        selection_history.append(selection_record)
                    reservation_metadata["reservation_selections"] = selection_history
                    if fallback_id and not has_reservation_metadata(
                        rp, reservation_metadata
                    ):
                        if rp == "aws":
                            reservation_metadata["instance_ids"] = str(
                                fallback_id
                            ).split(",")
                        elif rp == "quads":
                            try:
                                reservation_metadata["assignment_id"] = int(fallback_id)
                            except (TypeError, ValueError):
                                reservation_metadata["assignment_id"] = fallback_id
                        elif rp == "psap-cc":
                            reservation_metadata["reservation_id"] = str(fallback_id)
                        elif rp == "jumpstarter":
                            reservation_metadata["lease_id"] = str(fallback_id)
                    if not has_reservation_metadata(rp, reservation_metadata):
                        await self._add_comment(
                            ticket_id,
                            "**Auto-reservation failed:** The provider did not "
                            "return reservation metadata or an ID. Manual "
                            "provider cleanup may be required.",
                        )
                        return
                    if reservation_metadata:
                        fields["resource_provider_metadata"] = reservation_metadata
                        if reservation_metadata.get("ssh_user"):
                            fields["ssh_user"] = reservation_metadata["ssh_user"]
                        if reservation_metadata.get("ssh_key_path"):
                            fields["ssh_key_path"] = reservation_metadata[
                                "ssh_key_path"
                            ]
                    reservation_id = fields.get(
                        "resource_reservation_id"
                    ) or _reservation_id_from_metadata(rp, reservation_metadata)
                    if reservation_id:
                        fields["resource_reservation_id"] = reservation_id
                    if rp in {"aws", "quads"} and reserve_result.get("hosts"):
                        try:
                            fields["assigned_hardware_ips"] = (
                                _assigned_hardware_from_reservation(
                                    ticket_context,
                                    reserve_result["hosts"],
                                    selection,
                                )
                            )
                        except ValueError as exc:
                            review_fields = {
                                "resource_provider": rp,
                                "resource_reservation_id": fields.get(
                                    "resource_reservation_id"
                                ),
                                "resource_provider_metadata": reservation_metadata,
                                "assigned_hardware_ips": {
                                    "controller": "",
                                    "targets": [],
                                },
                                "ssh_hardware_ips": {
                                    "controller": "",
                                    "targets": [],
                                },
                            }
                            await self._update_fields(ticket_id, review_fields)
                            message = (
                                "**Auto-reservation needs review:** Resources were "
                                f"reserved, but host assignment failed: {exc}. "
                                "The verified reservation ID and metadata were "
                                "saved; host mappings were cleared. Confirm the "
                                "provider assignment before resuming."
                            )
                            await self._add_comment(ticket_id, message)
                            await self._transition_ticket(
                                ticket_id,
                                "awaiting_customer_guidance",
                                comment="Auto-reserved hosts need manual assignment review",
                            )
                            return
                    if rp in {"aws", "quads", "jumpstarter"}:
                        fields["fresh_host"] = True
                    logger.info(
                        "[resource] Auto-reservation succeeded for %s (%s=%s)",
                        ticket_id,
                        rp,
                        fields.get("resource_reservation_id", "?"),
                    )
                except Exception as exc:
                    logger.warning(
                        "[resource] Auto-reservation failed: %s",
                        exc,
                    )
                    await self._add_comment(
                        ticket_id,
                        "**Auto-reservation failed:** Could not "
                        "reserve resources automatically. "
                        f"Error: {exc}",
                    )
                    return

        if reservation_metadata.get("ssh_user"):
            fields["ssh_user"] = reservation_metadata["ssh_user"]
        if reservation_metadata.get("ssh_key_path"):
            fields["ssh_key_path"] = reservation_metadata["ssh_key_path"]

        if self._mcp:
            try:
                raw = await self._mcp.call_tool("get_host_inventory", {})
                host_inventory = json.loads(raw) if raw else {}
                if host_inventory:
                    fields["host_inventory"] = host_inventory
            except Exception:
                logger.debug("get_host_inventory unavailable, skipping")

        if result.get("fresh_host") and rp != "psap-cc":
            fields["fresh_host"] = True

        ip_mapping = reservation_metadata.get("ip_mapping", {})
        hw = fields["assigned_hardware_ips"]

        if ip_mapping and hw:
            ssh_hw: dict[str, Any] = {}
            private_hw: dict[str, Any] = {}

            ctrl = hw.get("controller", "")
            if ctrl:
                match = _match_to_provider_ip(ctrl, ip_mapping)
                if match:
                    ssh_hw["controller"] = match[0]
                    private_hw["controller"] = match[1]
                else:
                    ssh_hw["controller"] = ctrl
                    private_hw["controller"] = ctrl

            targets = hw.get("targets", [])
            ssh_targets = []
            private_targets = []
            for t in targets:
                match = _match_to_provider_ip(t, ip_mapping)
                if match:
                    ssh_targets.append(match[0])
                    private_targets.append(match[1])
                else:
                    ssh_targets.append(t)
                    private_targets.append(t)
            ssh_hw["targets"] = ssh_targets
            private_hw["targets"] = private_targets

            fields["ssh_hardware_ips"] = ssh_hw
            fields["assigned_hardware_ips"] = private_hw

        # Merge with preserved hosts from selective teardown.
        # If a prior teardown kept the controller alive, the new
        # allocation only covers targets. The LLM may have included
        # the controller IP in its result (it sees it in comments),
        # but it wasn't newly allocated — use the existing SSH
        # mapping for the controller.
        ticket = await self._get_ticket(ticket_id)
        existing_cf = ticket.get("custom_fields", {})
        existing_hw = existing_cf.get("assigned_hardware_ips", {})
        existing_ssh = existing_cf.get("ssh_hardware_ips", {})
        existing_meta = existing_cf.get("resource_provider_metadata", {})
        new_hw = fields.get("assigned_hardware_ips", {})
        new_ssh = fields.get("ssh_hardware_ips", {})
        new_meta = fields.get("resource_provider_metadata", {})
        new_instance_ids = set(new_meta.get("instance_ids", []))

        # Check if the controller was preserved (exists in prior
        # state but not in the new allocation's instance IDs)
        ctrl_preserved = bool(
            existing_hw.get("controller")
            and existing_meta.get("instance_ids")
            and not new_instance_ids.intersection(
                existing_meta.get("instance_ids", []),
            )
        )

        if ctrl_preserved:
            new_hw["controller"] = existing_hw["controller"]
            fields["assigned_hardware_ips"] = new_hw
            if existing_ssh.get("controller"):
                new_ssh["controller"] = existing_ssh["controller"]
                fields["ssh_hardware_ips"] = new_ssh

            # Merge provider metadata (keep preserved instance IDs)
            if existing_meta.get("instance_ids") and new_meta.get(
                "instance_ids",
            ):
                new_meta["instance_ids"] = (
                    existing_meta["instance_ids"] + new_meta["instance_ids"]
                )
                new_meta["public_ips"] = existing_meta.get(
                    "public_ips",
                    [],
                ) + new_meta.get("public_ips", [])
                new_meta["private_ips"] = existing_meta.get(
                    "private_ips",
                    [],
                ) + new_meta.get("private_ips", [])
                fields["resource_provider_metadata"] = new_meta

        # Backward compat: write legacy QUADS fields when provider is quads
        provider = fields.get("resource_provider")
        if provider == "quads":
            meta = fields.get("resource_provider_metadata") or reservation_metadata
            if meta.get("assignment_id"):
                fields["quads_assignment_id"] = meta["assignment_id"]
            elif result.get("quads_assignment_id"):
                fields["quads_assignment_id"] = result["quads_assignment_id"]
            if meta.get("cloud_name"):
                fields["quads_cloud_name"] = meta["cloud_name"]
            elif result.get("quads_cloud_name"):
                fields["quads_cloud_name"] = result["quads_cloud_name"]

        await self._update_fields(ticket_id, fields)

        hw = fields["assigned_hardware_ips"]
        summary = (
            f"**Resource Allocation Complete**\n\n"
            f"- **Provider:** {fields.get('resource_provider', 'unknown')}\n"
            f"- **Controller:** {hw.get('controller', 'N/A')}\n"
            f"- **Targets:** {', '.join(hw.get('targets', []))}\n"
            f"- **SSH User:** {fields['ssh_user']}\n"
        )
        if result.get("notes"):
            summary += f"- **Notes:** {result['notes']}\n"

        await self._add_comment(ticket_id, summary)
        if await self._plan_controls_next_transition(ticket_id):
            return
        await self._transition_ticket(
            ticket_id,
            "preparing_platform",
            comment="Hardware allocated, preparing platform",
        )
