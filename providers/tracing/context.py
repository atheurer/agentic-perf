"""Context-variable propagation for immutable trace causality."""

from __future__ import annotations

import uuid
from contextvars import ContextVar, Token

from pydantic import Field, field_validator

from .models import TraceModel


class TraceContext(TraceModel):
    """Correlation identity propagated from dispatch to child actions."""

    ticket_id: str | None = None
    agent_id: str | None = None
    invocation_id: uuid.UUID | None = None
    trace_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    action_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    parent_action_id: str | None = None
    iteration: int | None = None
    tool_call_id: str | None = None
    mcp_server: str | None = None
    mcp_session_id: str | None = None
    mcp_correlation_request_id: str | None = None
    idempotency_key: str | None = None
    idempotency_request_hash: str | None = None

    @field_validator("trace_id")
    @classmethod
    def validate_trace_id(cls, value: str) -> str:
        if len(value) != 32 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("trace_id must be 32 lowercase hexadecimal characters")
        return value

    @field_validator("action_id", "parent_action_id")
    @classmethod
    def validate_action_id(cls, value: str | None) -> str | None:
        if value is not None and (
            len(value) != 16 or any(char not in "0123456789abcdef" for char in value)
        ):
            raise ValueError("action IDs must be 16 lowercase hexadecimal characters")
        return value


_TRACE_CONTEXT: ContextVar[TraceContext | None] = ContextVar(
    "agentic_perf_trace_context", default=None
)


def new_trace_context(
    *,
    ticket_id: str | None = None,
    agent_id: str | None = None,
    invocation_id: uuid.UUID | None = None,
    iteration: int | None = None,
) -> TraceContext:
    """Create a root context with caller-supplied or newly-generated identity."""
    return TraceContext(
        ticket_id=ticket_id,
        agent_id=agent_id,
        invocation_id=invocation_id or uuid.uuid4(),
        iteration=iteration,
        trace_id=uuid.uuid4().hex,
        action_id=uuid.uuid4().hex[:16],
    )


def child_context(
    parent: TraceContext | None = None, **updates: object
) -> TraceContext:
    """Return a sibling-safe child; neither parent nor ambient context mutates."""
    parent = parent or current_trace_context()
    if parent is None:
        raise RuntimeError("cannot create a child trace context without a parent")
    values = {
        "parent_action_id": parent.action_id,
        "action_id": uuid.uuid4().hex[:16],
        **updates,
    }
    return parent.model_copy(update=values)


def current_trace_context() -> TraceContext | None:
    """Return the context inherited by this async task, if one is bound."""
    return _TRACE_CONTEXT.get()


def bind_trace_context(context: TraceContext) -> Token[TraceContext | None]:
    """Bind context for the current task and return the reset token."""
    return _TRACE_CONTEXT.set(context)


def reset_trace_context(token: Token[TraceContext | None]) -> None:
    """Restore the previous task-local context."""
    _TRACE_CONTEXT.reset(token)
