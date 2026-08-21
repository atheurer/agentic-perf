"""Fleet investigation tracking for multi-host comparative testing.

Tracks which hosts have been tested during a fleet-wide investigation,
enabling the evaluate loop-back to iterate through all available
devices of a given type.

Data lives in ``custom_fields.fleet_investigation``::

    {
        "enabled": true,
        "tested_hosts": [
            {
                "host_id": "nxp-s32g-vnp-rdb3-01",
                "lease_id": "perf-abc123",
                "ip": "10.26.29.21",
                "status": "completed",       # or "partial"
                "failure_reason": null,       # set on partial
                "metrics": {"avg_total_boot_s": 17.88}
            }
        ],
        "fleet_exhausted": {
            "hard": true                      # every device tested
            # OR "soft": true, "unavailable_hosts": [...]
        }
    }
"""

from __future__ import annotations

from typing import Any


def is_fleet_investigation(custom_fields: dict[str, Any]) -> bool:
    """Check if a ticket is a fleet investigation.

    Returns ``True`` when ``fleet_investigation.enabled`` is set.
    """
    fleet = custom_fields.get("fleet_investigation", {})
    return bool(fleet.get("enabled"))


def get_tested_host_ids(
    custom_fields: dict[str, Any],
) -> list[str]:
    """Return list of host IDs already tested."""
    fleet = custom_fields.get("fleet_investigation", {})
    return [h["host_id"] for h in fleet.get("tested_hosts", []) if h.get("host_id")]


def get_fleet_progress(
    custom_fields: dict[str, Any],
) -> dict[str, Any]:
    """Return fleet investigation progress summary.

    Returns:
        Dict with keys: tested, completed, partial,
        fleet_exhausted, exhaustion_type, unavailable_hosts,
        converged.
    """
    fleet = custom_fields.get("fleet_investigation", {})
    tested = fleet.get("tested_hosts", [])
    exhaustion = fleet.get("fleet_exhausted", {})
    completed = [h for h in tested if h.get("status") == "completed"]
    partial = [h for h in tested if h.get("status") == "partial"]

    is_exhausted = bool(exhaustion)
    return {
        "tested": len(tested),
        "completed": len(completed),
        "partial": len(partial),
        "fleet_exhausted": is_exhausted,
        "exhaustion_type": (
            "hard"
            if exhaustion.get("hard")
            else "soft"
            if exhaustion.get("soft")
            else None
        ),
        "unavailable_hosts": exhaustion.get("unavailable_hosts", []),
        "converged": is_exhausted and len(tested) > 0,
    }


def build_tested_host_entry(
    host_id: str,
    lease_id: str = "",
    ip: str = "",
    status: str = "completed",
    failure_reason: str | None = None,
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a tested_hosts entry for the fleet tracker."""
    entry: dict[str, Any] = {
        "host_id": host_id,
        "lease_id": lease_id,
        "ip": ip,
        "status": status,
    }
    if failure_reason:
        entry["failure_reason"] = failure_reason
    if metrics:
        entry["metrics"] = metrics
    return entry


async def record_host_result(
    update_fields_fn,
    ticket_id: str,
    custom_fields: dict[str, Any],
    host_id: str,
    lease_id: str = "",
    ip: str = "",
    status: str = "completed",
    failure_reason: str | None = None,
    metrics: dict[str, Any] | None = None,
) -> None:
    """Record a host result in the fleet tracker.

    Appends a tested_host entry and persists it via the
    provided update_fields callback (typically agent's
    ``_update_fields`` method).
    """
    fleet = dict(custom_fields.get("fleet_investigation", {}))
    tested = list(fleet.get("tested_hosts", []))
    tested.append(
        build_tested_host_entry(
            host_id=host_id,
            lease_id=lease_id,
            ip=ip,
            status=status,
            failure_reason=failure_reason,
            metrics=metrics,
        )
    )
    fleet["tested_hosts"] = tested
    await update_fields_fn(
        ticket_id,
        {"fleet_investigation": fleet},
    )
