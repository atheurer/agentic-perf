"""Fleet investigation tracking for multi-host comparative testing.

Tracks which hosts have been tested during a fleet-wide investigation,
enabling the investigation loop-back to iterate through all available
devices of a given type.

Fleet investigations are ONLY available on the investigation path —
standard benchmark tickets cannot use fleet tracking. This is enforced
by checking for investigation context (anomaly_context or
investigation_ledger) before activating fleet features.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def is_fleet_investigation(custom_fields: dict[str, Any]) -> bool:
    """Check if a ticket is a fleet investigation.

    Fleet investigations require BOTH fleet_investigation.enabled
    AND an investigation context (anomaly_context). Standard
    benchmark tickets cannot use fleet tracking.
    """
    fleet = custom_fields.get("fleet_investigation", {})
    if not fleet.get("enabled"):
        return False
    # Guardrail: only investigation-path tickets
    has_investigation = bool(
        custom_fields.get("anomaly_context")
        or custom_fields.get("investigation_ledger")
    )
    if not has_investigation:
        logger.warning(
            "[fleet] fleet_investigation.enabled but no "
            "investigation context — ignoring"
        )
        return False
    return True


def get_tested_host_ids(
    custom_fields: dict[str, Any],
) -> list[str]:
    """Return list of host IDs already tested."""
    fleet = custom_fields.get("fleet_investigation", {})
    return [h["host_id"] for h in fleet.get("tested_hosts", []) if h.get("host_id")]


def get_fleet_progress(
    custom_fields: dict[str, Any],
) -> dict[str, Any]:
    """Return fleet investigation progress summary."""
    fleet = custom_fields.get("fleet_investigation", {})
    tested = fleet.get("tested_hosts", [])
    exhaustion = fleet.get("fleet_exhausted", {})
    completed = [h for h in tested if h.get("status") == "completed"]
    failed = [h for h in tested if h.get("status") == "partial"]

    # Exhaustion is a dict with:
    #   "hard": true if every device has been tested
    #   "soft": true if untested devices exist but are
    #           currently unavailable
    #   "unavailable_hosts": list of device names that
    #           were not tested because they were leased
    is_exhausted = bool(exhaustion)
    return {
        "tested": len(tested),
        "completed": len(completed),
        "partial": len(failed),
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
    samples_collected: int = 0,
    samples_requested: int = 0,
    failure_reason: str | None = None,
    kpis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a tested_hosts entry for appending to the fleet."""
    entry: dict[str, Any] = {
        "host_id": host_id,
        "lease_id": lease_id,
        "ip": ip,
        "status": status,
        "samples_collected": samples_collected,
        "samples_requested": samples_requested,
    }
    if failure_reason:
        entry["failure_reason"] = failure_reason
    if kpis:
        entry["kpis"] = kpis
    return entry
