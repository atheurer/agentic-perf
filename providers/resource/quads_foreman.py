"""QUADS-specific authorization and credential adapter for Foreman."""

from __future__ import annotations

from typing import Any

from providers.foreman import ForemanError

_QUADS_DIRECTIVE_CONTAINERS = ("quads_selection",)


def quads_assignment_directive(custom_fields: Any) -> dict[str, str] | None:
    """Read a user-authored formal QUADS assignment identity.

    Formal (council-allocated) assignments never pass through this system, so
    there is no reservation metadata for them. Read the directive only from
    QUADS-specific dictionaries; an unrelated top-level ``ticket`` or ``cloud``
    field must never become a Foreman credential.
    """
    if not isinstance(custom_fields, dict):
        return None
    candidates: list[tuple[Any, bool]] = []
    directives = custom_fields.get("directives")
    if isinstance(directives, dict):
        candidates.append((directives.get("quads"), True))
        for key in _QUADS_DIRECTIVE_CONTAINERS:
            candidates.append((directives.get(key), False))
    for key in _QUADS_DIRECTIVE_CONTAINERS:
        candidates.append((custom_fields.get(key), False))
    resolved: dict[str, str] | None = None
    for value, explicit in candidates:
        if not isinstance(value, dict):
            continue
        nested = value.get("quads")
        if isinstance(nested, dict):
            candidate = nested
            explicit = True
        else:
            candidate = value
        cloud = candidate.get("cloud") or candidate.get("cloud_name")
        ticket = candidate.get("ticket")
        if cloud is None and ticket is None:
            # quads_selection also carries normal reservation data such as
            # os/hostnames; only an explicit quads directive or a candidate
            # that already names part of an identity is treated as formal.
            if explicit:
                raise ForemanError(
                    "configuration",
                    "QUADS assignment directive requires cloud and ticket",
                )
            continue
        if (
            not isinstance(cloud, str)
            or not cloud.strip()
            or not isinstance(ticket, (str, int))
            or isinstance(ticket, bool)
            or not str(ticket).strip()
        ):
            raise ForemanError(
                "configuration", "QUADS assignment directive requires cloud and ticket"
            )
        identity = {"cloud_name": cloud.strip(), "ticket": str(ticket).strip()}
        if resolved is not None and resolved != identity:
            raise ForemanError(
                "configuration", "QUADS assignment directive is ambiguous"
            )
        resolved = identity
    return resolved


def derive_quads_credentials(
    auth: dict[str, Any], metadata: dict[str, Any]
) -> tuple[str, str]:
    """Derive credentials only for a deployment that confirmed the convention."""
    cloud = metadata.get("cloud_name")
    ticket = metadata.get("ticket")
    location = auth.get("infra_location")
    if auth.get("convention_verified") is not True or any(
        not isinstance(value, str) or not value.strip()
        for value in (cloud, ticket, location)
    ):
        raise ForemanError(
            "configuration",
            "Verified QUADS cloud, assignment ticket and infra location are required",
        )
    return cloud, f"{location}@{ticket}"


async def verify_quads_ownership(
    client: Any,
    metadata: dict[str, Any],
    hostnames: list[str],
    *,
    require_active_assignment: bool = True,
) -> None:
    """Check assignment identity and every host's current cloud.

    Formal assignments have no local assignment id, so callers with a
    user-authored directive can require only the cloud/ticket and each host's
    current cloud. The default keeps the self-serve active-assignment check.
    """
    assignment = metadata.get("assignment_id")
    cloud = metadata.get("cloud_name")
    ticket = metadata.get("ticket")
    if (
        not isinstance(cloud, str)
        or not cloud
        or not isinstance(ticket, str)
        or not ticket
        or not hostnames
        or any(not isinstance(host, str) or not host for host in hostnames)
    ):
        raise ForemanError("host", "QUADS allocation identity is incomplete")
    try:
        if require_active_assignment:
            if isinstance(assignment, bool) or not str(assignment or "").isdigit():
                raise ForemanError("host", "QUADS allocation identity is incomplete")
            status = await client.get_foreman_assignment_identity(int(assignment))
            if (
                status.get("active") is not True
                or status.get("validated") is not True
                or status.get("cloud") != cloud
                or str(status.get("ticket") or "") != ticket
            ):
                raise ForemanError(
                    "host", "QUADS assignment is not active or does not match"
                )
        for host in hostnames:
            if await client.get_host_cloud(host) != cloud:
                raise ForemanError("host", "Host is not in the active QUADS assignment")
    except ForemanError:
        raise
    except Exception:
        raise ForemanError(
            "host", "Unable to verify QUADS allocation ownership"
        ) from None
