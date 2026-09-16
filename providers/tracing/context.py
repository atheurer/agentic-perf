"""Context-variable propagation for immutable trace causality."""

from __future__ import annotations

import os
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
    # model_copy(update=...) trusts values in Pydantic v2.  Re-validating makes
    # the helper a safe boundary for values supplied by future trace producers.
    return TraceContext.model_validate(parent.model_dump() | values)


def current_trace_context() -> TraceContext | None:
    """Return the task context, restoring ticket-child context when needed.

    FastMCP invokes each tool in its own task. Those tasks do not inherit a
    context bound while the server initialized, but ticket-owned MCP processes
    receive a serialized causal context at launch.
    """
    context = _TRACE_CONTEXT.get()
    if context is not None:
        return context
    return trace_context_from_environment(
        ticket_id=os.environ.get("TICKET_ID", ""),
        agent_id=os.environ.get("AGENT_NAME"),
    )


def bind_trace_context(context: TraceContext) -> Token[TraceContext | None]:
    """Bind context for the current task and return the reset token."""
    return _TRACE_CONTEXT.set(context)


def reset_trace_context(token: Token[TraceContext | None]) -> None:
    """Restore the previous task-local context."""
    _TRACE_CONTEXT.reset(token)


def trace_context_environment(context: TraceContext) -> dict[str, str]:
    """Serialize a causal context for a ticket-owned child process."""
    return {
        "AGENTIC_PERF_TRACE_ID": context.trace_id,
        "AGENTIC_PERF_TRACE_INVOCATION_ID": str(context.invocation_id),
        "AGENTIC_PERF_TRACE_ACTION_ID": context.action_id,
        "AGENTIC_PERF_TRACE_PARENT_ACTION_ID": context.parent_action_id or "",
        "AGENTIC_PERF_TRACE_ITERATION": ""
        if context.iteration is None
        else str(context.iteration),
    }


def trace_context_from_environment(
    *, ticket_id: str, agent_id: str | None
) -> TraceContext | None:
    """Restore a causal context passed to a ticket-owned child process."""
    trace_id = os.environ.get("AGENTIC_PERF_TRACE_ID", "")
    invocation_id = os.environ.get("AGENTIC_PERF_TRACE_INVOCATION_ID", "")
    action_id = os.environ.get("AGENTIC_PERF_TRACE_ACTION_ID", "")
    if not (ticket_id and trace_id and invocation_id and action_id):
        return None
    try:
        iteration_text = os.environ.get("AGENTIC_PERF_TRACE_ITERATION", "")
        return TraceContext(
            ticket_id=ticket_id,
            agent_id=agent_id,
            invocation_id=uuid.UUID(invocation_id),
            trace_id=trace_id,
            action_id=action_id,
            parent_action_id=os.environ.get("AGENTIC_PERF_TRACE_PARENT_ACTION_ID")
            or None,
            iteration=int(iteration_text) if iteration_text else None,
        )
    except (TypeError, ValueError):
        return None
