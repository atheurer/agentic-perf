"""FastMCP server for Investigation Record tools.

Exposes operations for Investigation Records over stdio.
Any agent in the investigation loop (gathering_context,
evaluating_convergence, synthesizing_results) connects to
this server to query, create, and track records.

Records are write-once: investigation data is immutable after
creation. The only mutations are appending build history
(tracking regression across builds), linking a Jira ticket
(one-time), and closing the record (OPEN -> RESOLVED).

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
    records ordered by most recently created first.
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
                    "subsystem": (r.anomaly_context.subsystem),
                    "metric": r.anomaly_context.metric,
                    "platform": r.anomaly_context.platform,
                    "magnitude": (r.anomaly_context.magnitude),
                    "direction": (r.anomaly_context.direction),
                    "root_cause_summary": (r.root_cause_summary),
                    "confidence": r.confidence,
                    "jira_ticket": r.jira_ticket,
                    "build_count": len(r.build_history),
                    "created_at": r.created_at.isoformat(),
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
    convergence_outcome: str = "",
    provision_cycles: int = 0,
    wall_clock_mins: float = 0.0,
    info_gain_trajectory: str = "",
    stall_events: int = 0,
    llm_tokens_total: int = 0,
    llm_invocations: int = 0,
    estimated_cost_usd: float = 0.0,
    hardware_time_mins: float = 0.0,
    change_classification: str = "",
    causal_commits: str = "",
    change_summary: str = "",
) -> str:
    """Create a new Investigation Record.

    Call this when an investigation completes (convergence gate
    fires) to persist the outcome with operational metrics.
    Records are write-once — all investigation data must be
    provided at creation time. The record cannot be modified
    after creation except for build history (append-only),
    Jira linkage (one-time), and state transition (close).

    info_gain_trajectory: JSON array string, e.g. "[0.0, 0.5, 0.9]"
    causal_commits: comma-separated commit hashes
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

    # Operational metrics
    record.operational_metrics.convergence_outcome = convergence_outcome
    record.operational_metrics.provision_cycles = provision_cycles
    record.operational_metrics.wall_clock_mins = wall_clock_mins
    record.operational_metrics.stall_events = stall_events
    if info_gain_trajectory:
        try:
            record.operational_metrics.info_gain_trajectory = json.loads(
                info_gain_trajectory
            )
        except (json.JSONDecodeError, TypeError):
            pass
    record.operational_metrics.resource_consumption.llm_tokens_total = llm_tokens_total
    record.operational_metrics.resource_consumption.llm_invocations = llm_invocations
    record.operational_metrics.resource_consumption.estimated_cost_usd = (
        estimated_cost_usd
    )
    record.operational_metrics.resource_consumption.hardware_time_mins = (
        hardware_time_mins
    )

    # Change attribution
    if change_classification:
        record.change_attribution.classification = change_classification
    if causal_commits:
        record.change_attribution.causal_commits = [
            c.strip() for c in causal_commits.split(",") if c.strip()
        ]
    if change_summary:
        record.change_attribution.change_summary = change_summary

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
async def append_build_history(
    investigation_id: str,
    build_id: str,
    action: str = "SKIP_MATCHED",
    comment: str = "",
) -> str:
    """Append a build history entry to an Investigation Record.

    Call this when a known regression is detected in a new build
    — the agent skips the full investigation and records that the
    regression is still present. Action should be
    FULL_INVESTIGATION or SKIP_MATCHED.
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
async def link_jira_ticket(
    investigation_id: str,
    jira_ticket: str,
) -> str:
    """Link a Jira ticket to an Investigation Record.

    This can only be done once per record. Use this when
    the Jira ticket is created after the investigation
    completes. Raises an error if a ticket is already linked.
    """
    provider = _get_provider()
    try:
        await provider.link_jira(investigation_id, jira_ticket)
        return json.dumps(
            {
                "status": "linked",
                "investigation_id": investigation_id,
                "jira_ticket": jira_ticket,
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
    except ValueError as e:
        return json.dumps(
            {
                "status": "already_linked",
                "message": str(e),
            }
        )


@mcp.tool()
async def close_investigation_record(
    investigation_id: str,
) -> str:
    """Mark an Investigation Record as resolved.

    Call this when the regression is fixed and confirmed. The
    record remains queryable but won't match as an open
    investigation for dedup purposes. This is a one-way
    transition — records cannot be reopened.
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
