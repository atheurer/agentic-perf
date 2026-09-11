"""Versioned, immutable trace-event contracts.

This module deliberately has no persistence or instrumentation dependency.  It
is the single wire contract that later trace producers and the trace store use.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "v1"


class ActionType(str, Enum):
    """Controlled domains that can produce a trace event."""

    DISPATCH = "dispatch"
    AGENT = "agent"
    HANDOFF = "handoff"
    LLM = "llm"
    TOOL = "tool"
    MCP = "mcp"
    OPERATION = "operation"
    SSH = "ssh"
    SUBPROCESS = "subprocess"
    API = "api"
    FILESYSTEM = "filesystem"
    CONTAINER = "container"
    STATE = "state"


class LifecycleState(str, Enum):
    """Controlled event lifecycle vocabulary shared by every action type."""

    REQUESTED = "requested"
    CLAIMED = "claimed"
    STARTED = "started"
    PAUSED = "paused"
    RESUMED = "resumed"
    ABORTED = "aborted"
    COMPLETED = "completed"
    FAILED = "failed"
    RELEASED = "released"
    PROPOSED = "proposed"
    REJECTED = "rejected"
    SHORT_CIRCUITED = "short_circuited"
    CANCELLED = "cancelled"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    REQUEST_SENT = "request_sent"
    REQUEST_RECEIVED = "request_received"
    DUPLICATE_DETECTED = "duplicate_detected"
    RESPONSE_SENT = "response_sent"
    DISCONNECTED = "disconnected"
    RECONNECTED = "reconnected"
    REGISTERED = "registered"
    LEASE_ACQUIRED = "lease_acquired"
    PREPARED = "prepared"
    SIDE_EFFECT_STARTED = "side_effect_started"
    RECONCILED = "reconciled"
    INDETERMINATE = "indeterminate"
    LAUNCHED = "launched"
    TIMED_OUT = "timed_out"
    CLEANUP_STARTED = "cleanup_started"
    CLEANUP_COMPLETED = "cleanup_completed"
    REQUEST_STARTED = "request_started"
    RESPONSE_RECEIVED = "response_received"
    RATE_LIMITED = "rate_limited"
    RETRY_SCHEDULED = "retry_scheduled"


class RetryKind(str, Enum):
    """Why an attempt exists, without conflating it with a replay."""

    NONE = "none"
    VALIDATION = "validation"
    INTENTIONAL_AGENT_RETRY = "intentional_agent_retry"
    TRANSPORT_BEFORE_SEND = "transport_before_send"
    AMBIGUOUS_AFTER_SEND = "ambiguous_after_send"
    TRANSPORT_REPLAY = "transport_replay"


class IdempotencyOutcome(str, Enum):
    """Result of resolving an idempotency identity."""

    NOT_APPLICABLE = "not_applicable"
    CLAIMED = "claimed"
    IN_PROGRESS = "in_progress"
    REUSED = "reused"
    REJECTED = "rejected"
    INDETERMINATE = "indeterminate"


class OperationOutcome(str, Enum):
    """Terminal or current outcome for an action."""

    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    TIMED_OUT = "timed_out"
    INDETERMINATE = "indeterminate"


class TraceModel(BaseModel):
    """Common immutable, closed-model configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ProducerIdentity(TraceModel):
    component: str | None = None
    instance_id: str | None = None
    host: str | None = None
    pid: int | None = None
    process_start_id: str | None = None
    boot_id: str | None = None


class MCPIdentity(TraceModel):
    server: str | None = None
    transport: str | None = None
    session_id: str | None = None
    protocol_request_id: str | None = None
    correlation_request_id: str | None = None
    server_pid: int | None = None


class ActionDescriptor(TraceModel):
    type: ActionType
    phase: str | None = None
    target_type: str | None = None
    target: str | None = None


class LifecycleDescriptor(TraceModel):
    state: LifecycleState
    attempt: int = Field(default=1, ge=1)
    retry_kind: RetryKind = RetryKind.NONE
    replay_of_action_id: str | None = None


class IdempotencyDescriptor(TraceModel):
    key: str | None = None
    request_hash: str | None = None
    outcome: IdempotencyOutcome = IdempotencyOutcome.NOT_APPLICABLE
    fencing_token: int | None = Field(default=None, ge=0)


class PayloadDescriptor(TraceModel):
    """Bounded payload metadata; it never contains the unbounded payload."""

    size_bytes: int | None = Field(default=None, ge=0)
    media_type: str | None = None
    digest: str | None = None
    preview: str | None = None
    blob_ref: str | None = None
    truncated: bool | None = None
    redaction_applied: bool | None = None


class ErrorDescriptor(TraceModel):
    type: str | None = None
    code: str | None = None
    message: str | None = None
    retryable: bool | None = None


_TERMINAL_STATES = frozenset(
    {
        LifecycleState.ABORTED,
        LifecycleState.CANCELLED,
        LifecycleState.COMPLETED,
        LifecycleState.FAILED,
        LifecycleState.INDETERMINATE,
        LifecycleState.REJECTED,
        LifecycleState.SHORT_CIRCUITED,
        LifecycleState.TIMED_OUT,
    }
)


class TraceEventV1(TraceModel):
    """The complete immutable trace envelope for schema version 1."""

    schema_version: str = SCHEMA_VERSION
    event_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    recorded_at: datetime | None = None
    duration_ms: float | None = Field(default=None, ge=0)
    global_seq: int | None = Field(default=None, ge=0)
    ticket_seq: int | None = Field(default=None, ge=0)
    ticket_id: str | None = None
    agent_id: str | None = None
    invocation_id: uuid.UUID | None = None
    iteration: int | None = Field(default=None, ge=0)
    trace_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    action_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    parent_action_id: str | None = None
    tool_call_id: str | None = None
    producer: ProducerIdentity = Field(default_factory=ProducerIdentity)
    mcp: MCPIdentity = Field(default_factory=MCPIdentity)
    action: ActionDescriptor
    lifecycle: LifecycleDescriptor
    idempotency: IdempotencyDescriptor = Field(default_factory=IdempotencyDescriptor)
    input: PayloadDescriptor | None = None
    output: PayloadDescriptor | None = None
    outcome: OperationOutcome | None = None
    error: ErrorDescriptor | None = None
    attributes: dict[str, Any] | None = None

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: str) -> str:
        if value != SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {SCHEMA_VERSION!r}")
        return value

    @field_validator("occurred_at", "recorded_at")
    @classmethod
    def require_utc_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and (
            value.tzinfo is None or value.utcoffset() != timedelta(0)
        ):
            raise ValueError("timestamps must be timezone-aware UTC values")
        return value

    @field_validator("trace_id")
    @classmethod
    def validate_trace_id(cls, value: str) -> str:
        if (
            len(value) != 32
            or set(value) == {"0"}
            or any(char not in "0123456789abcdef" for char in value)
        ):
            raise ValueError("trace_id must be 32 lowercase hexadecimal characters")
        return value

    @field_validator("action_id", "parent_action_id")
    @classmethod
    def validate_action_id(cls, value: str | None) -> str | None:
        if value is not None and (
            len(value) != 16
            or set(value) == {"0"}
            or any(char not in "0123456789abcdef" for char in value)
        ):
            raise ValueError("action IDs must be 16 lowercase hexadecimal characters")
        return value

    @model_validator(mode="after")
    def validate_action_requirements(self) -> TraceEventV1:
        if not self.ticket_id:
            raise ValueError("ticket-scoped events require a non-empty ticket_id")
        if self.action.type == ActionType.AGENT and self.invocation_id is None:
            raise ValueError("agent actions require invocation_id")
        if self.action.type == ActionType.TOOL and (
            not self.tool_call_id or not self.parent_action_id
        ):
            raise ValueError("tool actions require tool_call_id and parent_action_id")
        if self.action.type == ActionType.MCP and (
            not self.mcp.session_id or not self.mcp.correlation_request_id
        ):
            raise ValueError(
                "MCP actions require session_id and correlation_request_id"
            )
        if self.lifecycle.state in _TERMINAL_STATES and (
            self.outcome is None or self.duration_ms is None
        ):
            raise ValueError("terminal events require outcome and duration_ms")
        return self

    @classmethod
    def from_exception(
        cls,
        *,
        action: ActionDescriptor,
        lifecycle: LifecycleDescriptor,
        exception: BaseException,
        duration_ms: float,
        **kwargs: Any,
    ) -> TraceEventV1:
        """Create a valid failed terminal event from an exception."""
        return cls(
            action=action,
            lifecycle=lifecycle.model_copy(update={"state": LifecycleState.FAILED}),
            duration_ms=duration_ms,
            outcome=OperationOutcome.FAILURE,
            error=ErrorDescriptor(
                type=type(exception).__name__, message=str(exception), retryable=False
            ),
            **kwargs,
        )

    @classmethod
    def from_cancellation(
        cls,
        *,
        action: ActionDescriptor,
        lifecycle: LifecycleDescriptor,
        duration_ms: float,
        **kwargs: Any,
    ) -> TraceEventV1:
        """Create a valid cancelled terminal event."""
        return cls(
            action=action,
            lifecycle=lifecycle.model_copy(update={"state": LifecycleState.CANCELLED}),
            duration_ms=duration_ms,
            outcome=OperationOutcome.CANCELLED,
            error=ErrorDescriptor(type="CancelledError", retryable=False),
            **kwargs,
        )


class MonotonicTimer:
    """Local-only timer that derives a duration without wall-clock jumps."""

    def __init__(self) -> None:
        self._started_at = time.monotonic()

    def elapsed_ms(self) -> float:
        return (time.monotonic() - self._started_at) * 1000
