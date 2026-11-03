"""QUADS-specific authorization and credential adapter for Foreman."""

from __future__ import annotations

from typing import Any

from providers.foreman import ForemanError


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
    client: Any, metadata: dict[str, Any], hostnames: list[str]
) -> None:
    """Check current assignment identity and every host's current cloud."""
    assignment = metadata.get("assignment_id")
    cloud = metadata.get("cloud_name")
    ticket = metadata.get("ticket")
    if (
        isinstance(assignment, bool)
        or not str(assignment or "").isdigit()
        or not isinstance(cloud, str)
        or not cloud
        or not isinstance(ticket, str)
        or not ticket
        or not hostnames
        or any(not isinstance(host, str) or not host for host in hostnames)
    ):
        raise ForemanError("host", "QUADS allocation identity is incomplete")
    try:
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
