"""Pydantic models for Investigation Records.

These define the backend-agnostic contract for storing and querying
investigation outcomes. Any storage backend (file, OpenSearch,
Horreum, PostgreSQL, etc.) must serialize and deserialize
these models.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class InvestigationState(str, Enum):
    """Lifecycle state of an investigation."""

    OPEN = "open"
    RESOLVED = "resolved"
    EXPECTED = "expected"  # intentional trade-off, not a bug


class AnomalyContext(BaseModel):
    """What was detected — the trigger for the investigation."""

    subsystem: str
    metric: str
    direction: str = "degrading"
    platform: str = ""
    magnitude: str = ""


class ChangeAttribution(BaseModel):
    """Links a regression to specific code changes."""

    classification: str = ""  # ISOLATION or EXPECTED_REGRESSION
    causal_commits: list[str] = Field(default_factory=list)
    change_summary: str = ""
    trade_off_assessment: dict[str, Any] = Field(
        default_factory=dict,
    )


class ResourceConsumption(BaseModel):
    """Cost and resource usage for an investigation."""

    llm_tokens_total: int = 0
    llm_invocations: int = 0
    estimated_cost_usd: float = 0.0
    hardware_time_mins: float = 0.0
    by_cycle: list[dict[str, Any]] = Field(
        default_factory=list,
    )


class OperationalMetrics(BaseModel):
    """How the agent worked — cycle counts, timing, convergence."""

    provision_cycles: int = 0
    wall_clock_mins: float = 0.0
    convergence_outcome: str = ""
    info_gain_trajectory: list[float] = Field(
        default_factory=list,
    )
    stall_events: int = 0
    resource_consumption: ResourceConsumption = Field(
        default_factory=ResourceConsumption,
    )
    mcp_calls: dict[str, Any] = Field(
        default_factory=dict,
    )


class BuildHistoryEntry(BaseModel):
    """Tracks a regression across nightly builds."""

    build_id: str
    action: str  # FULL_INVESTIGATION or SKIP_MATCHED
    comment: str = ""
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class SkillApplied(BaseModel):
    """Records which Investigation Skill was used."""

    skill_id: str = ""
    name: str = ""
    accelerated_convergence: bool = False
    cycles_saved_estimate: int = 0


class InvestigationRecord(BaseModel):
    """A complete investigation outcome.

    This is the core data model for cross-investigation memory.
    Each record captures what was found, how the agent worked,
    and which builds have shown the regression.
    """

    investigation_id: str = Field(
        default_factory=lambda: f"RCA-{uuid.uuid4().hex[:8].upper()}",
    )
    state: InvestigationState = InvestigationState.OPEN
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )

    # What was detected
    anomaly_context: AnomalyContext

    # What was found
    root_cause_summary: str = ""
    jira_ticket: str = ""
    confidence: float = 0.0

    # Code change correlation
    change_attribution: ChangeAttribution = Field(
        default_factory=ChangeAttribution,
    )

    # How the agent worked
    operational_metrics: OperationalMetrics = Field(
        default_factory=OperationalMetrics,
    )

    # Build-level tracking
    build_history: list[BuildHistoryEntry] = Field(
        default_factory=list,
    )

    # Investigation Skill usage (future, issue #17)
    skill_applied: SkillApplied = Field(
        default_factory=SkillApplied,
    )

    # Link to detailed trace in observability store
    trace_ref: str = ""
