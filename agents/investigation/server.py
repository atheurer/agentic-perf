"""FastMCP server for Investigation Record tools.

Exposes CRUD operations for Investigation Records over stdio.
Any agent in the investigation loop (grounding, evaluate,
synthesize) connects to this server to query, create, and
update records.

The storage backend is pluggable — configured via
investigation_records.backend in ~/.agentic-perf/config.json.
Defaults to the file-based provider.

Run directly:  python agents/investigation/server.py
Connected via: AgentMCPClient (agents/mcp_client.py)
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

_project_root = str(Path(__file__).resolve().parents[2])
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from fastmcp import FastMCP

from providers.investigation.models import (
    AnomalyContext,
    BuildHistoryEntry,
    InvestigationRecord,
)
from providers.investigation.registry import (
    create_record_provider,
)

logger = logging.getLogger(__name__)

mcp = FastMCP("investigation-records")

_provider = None


def _get_provider():
    """Lazy-load the investigation record provider."""
    global _provider
    if _provider is None:
        _provider = create_record_provider()
    return _provider


@mcp.tool()
async def query_investigation_records(
    state: str = "",
    subsystem: str = "",
    platform: str = "",
    metric: str = "",
    limit: int = 20,
) -> str:
    """Query existing Investigation Records by field filters.

    Use this to check whether a regression has already been
    investigated before starting a new investigation. All filters
    are optional — omitted filters match everything. Returns
    records ordered by most recently updated first.
    """
    provider = _get_provider()
    records = await provider.query(
        state=state or None,
        subsystem=subsystem or None,
        platform=platform or None,
        metric=metric or None,
        limit=limit,
    )
    return json.dumps(
        {
            "count": len(records),
            "records": [
                {
                    "investigation_id": r.investigation_id,
                    "state": r.state.value,
                    "subsystem": r.anomaly_context.subsystem,
                    "metric": r.anomaly_context.metric,
                    "platform": r.anomaly_context.platform,
                    "magnitude": r.anomaly_context.magnitude,
                    "direction": r.anomaly_context.direction,
                    "root_cause_summary": r.root_cause_summary,
                    "confidence": r.confidence,
                    "jira_ticket": r.jira_ticket,
                    "build_count": len(r.build_history),
                    "updated_at": r.updated_at.isoformat(),
                }
                for r in records
            ],
        },
        indent=2,
    )


@mcp.tool()
async def get_investigation_record(
    investigation_id: str,
) -> str:
    """Get the full details of a single Investigation Record.

    Use this after query_investigation_records identifies a
    potential match — this returns the complete record including
    operational metrics, change attribution, and build history.
    """
    provider = _get_provider()
    record = await provider.get(investigation_id)
    if record is None:
        return json.dumps(
            {
                "found": False,
                "message": (f"No record found: {investigation_id}"),
            }
        )
    return json.dumps(
        {
            "found": True,
            "record": json.loads(record.model_dump_json()),
        },
        indent=2,
    )


@mcp.tool()
async def create_investigation_record(
    subsystem: str,
    metric: str,
    direction: str = "degrading",
    platform: str = "",
    magnitude: str = "",
    root_cause_summary: str = "",
    confidence: float = 0.0,
    jira_ticket: str = "",
    build_id: str = "",
) -> str:
    """Create a new Investigation Record.

    Call this when an investigation completes (convergence gate
    fires) to persist the outcome for future dedup checks. The
    record starts in OPEN state.
    """
    provider = _get_provider()
    record = InvestigationRecord(
        anomaly_context=AnomalyContext(
            subsystem=subsystem,
            metric=metric,
            direction=direction,
            platform=platform,
            magnitude=magnitude,
        ),
        root_cause_summary=root_cause_summary,
        confidence=confidence,
        jira_ticket=jira_ticket,
    )

    if build_id:
        record.build_history.append(
            BuildHistoryEntry(
                build_id=build_id,
                action="FULL_INVESTIGATION",
                comment="Initial discovery",
            )
        )

    rid = await provider.create(record)
    return json.dumps(
        {
            "status": "created",
            "investigation_id": rid,
            "state": record.state.value,
        },
        indent=2,
    )


@mcp.tool()
async def update_investigation_record(
    investigation_id: str,
    root_cause_summary: str = "",
    confidence: float = -1,
    jira_ticket: str = "",
    convergence_outcome: str = "",
) -> str:
    """Update fields on an existing Investigation Record.

    Use this to refine the root cause, update confidence, or
    link a Jira ticket as the investigation progresses. Only
    non-empty fields are updated.
    """
    provider = _get_provider()
    updates: dict[str, Any] = {}
    if root_cause_summary:
        updates["root_cause_summary"] = root_cause_summary
    if confidence >= 0:
        updates["confidence"] = confidence
    if jira_ticket:
        updates["jira_ticket"] = jira_ticket
    if convergence_outcome:
        updates["operational_metrics"] = {
            "convergence_outcome": convergence_outcome,
        }

    if not updates:
        return json.dumps(
            {
                "status": "no_changes",
                "message": "No fields to update",
            }
        )

    try:
        record = await provider.update(investigation_id, updates)
        return json.dumps(
            {
                "status": "updated",
                "investigation_id": investigation_id,
                "confidence": record.confidence,
            },
            indent=2,
        )
    except KeyError:
        return json.dumps(
            {
                "status": "not_found",
                "message": (f"No record found: {investigation_id}"),
            }
        )


@mcp.tool()
async def append_build_history(
    investigation_id: str,
    build_id: str,
    action: str = "SKIP_MATCHED",
    comment: str = "",
) -> str:
    """Append a build history entry to an Investigation Record.

    Call this when a known regression is detected in a new build
    — the agent skips the full investigation and records that the
    regression is still present. Action should be FULL_INVESTIGATION
    or SKIP_MATCHED.
    """
    provider = _get_provider()
    entry = BuildHistoryEntry(
        build_id=build_id,
        action=action,
        comment=comment,
    )
    try:
        await provider.append_build_history(investigation_id, entry)
        return json.dumps(
            {
                "status": "appended",
                "investigation_id": investigation_id,
                "build_id": build_id,
                "action": action,
            },
            indent=2,
        )
    except KeyError:
        return json.dumps(
            {
                "status": "not_found",
                "message": (f"No record found: {investigation_id}"),
            }
        )


@mcp.tool()
async def close_investigation_record(
    investigation_id: str,
) -> str:
    """Mark an Investigation Record as resolved.

    Call this when the regression is fixed and confirmed across
    builds. The record remains queryable but won't match as an
    open investigation for dedup purposes.
    """
    provider = _get_provider()
    try:
        await provider.close_record(investigation_id)
        return json.dumps(
            {
                "status": "closed",
                "investigation_id": investigation_id,
            },
            indent=2,
        )
    except KeyError:
        return json.dumps(
            {
                "status": "not_found",
                "message": (f"No record found: {investigation_id}"),
            }
        )


if __name__ == "__main__":
    mcp.run()
