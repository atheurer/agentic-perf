"""Small shared runtime bridge from agent lifecycles to trace envelopes.

The immutable models remain the public contract.  This module only supplies
the repetitive, task-local mechanics needed by dispatchers and agents.
"""

from __future__ import annotations

import logging
from typing import Any

from providers.redaction import get_shared_redactor

from .context import TraceContext, child_context, new_trace_context
from .models import (
    ActionDescriptor,
    ActionType,
    ErrorDescriptor,
    LifecycleDescriptor,
    LifecycleState,
    MonotonicTimer,
    OperationOutcome,
    RetryKind,
    TraceEventV1,
)

logger = logging.getLogger(__name__)


def trace_headers(context: TraceContext) -> dict[str, str]:
    """Return W3C and agentic-perf correlation headers for state-store calls."""
    return {
        "traceparent": f"00-{context.trace_id}-{context.action_id}-01",
        "X-Agentic-Perf-Ticket-Id": context.ticket_id or "",
        "X-Agentic-Perf-Agent-Id": context.agent_id or "",
        "X-Agentic-Perf-Invocation-Id": str(context.invocation_id or ""),
        "X-Agentic-Perf-Action-Id": context.action_id,
        "X-Agentic-Perf-Parent-Action-Id": context.parent_action_id or "",
        # This marker is only emitted by the internal causal transport helper.
        # State-store middleware ignores correlation-shaped user request headers
        # unless this complete envelope is present.
        "X-Agentic-Perf-Causal-Context": "v1",
    }


class TraceRecorder:
    """Create valid causal envelopes and optionally durably spool them.

    ``client`` is intentionally duck-typed so tests and embedded deployments
    can provide a recording sink without making tracing availability affect an
    agent's work.
    """

    def __init__(self, context: TraceContext | None = None, client: Any = None) -> None:
        self.context = context
        self.client = client

    def start(
        self,
        action_type: ActionType,
        *,
        phase: str | None = None,
        target: str | None = None,
        iteration: int | None = None,
        tool_call_id: str | None = None,
        parent: TraceContext | None = None,
        retry_kind: RetryKind = RetryKind.NONE,
    ) -> tuple[TraceContext, MonotonicTimer]:
        base = parent or self.context
        context = (
            child_context(base, iteration=iteration, tool_call_id=tool_call_id)
            if base
            else new_trace_context(iteration=iteration)
        )
        self.record(
            context,
            action_type,
            LifecycleState.STARTED,
            phase=phase,
            target=target,
            retry_kind=retry_kind,
        )
        return context, MonotonicTimer()

    def record(
        self,
        context: TraceContext,
        action_type: ActionType,
        state: LifecycleState,
        *,
        phase: str | None = None,
        target: str | None = None,
        duration_ms: float | None = None,
        outcome: OperationOutcome | None = None,
        error: BaseException | None = None,
        iteration: int | None = None,
        retry_kind: RetryKind = RetryKind.NONE,
        attributes: dict[str, Any] | None = None,
    ) -> TraceEventV1:
        terminal = state in {
            LifecycleState.COMPLETED,
            LifecycleState.FAILED,
            LifecycleState.CANCELLED,
            LifecycleState.ABORTED,
            LifecycleState.REJECTED,
            LifecycleState.TIMED_OUT,
            LifecycleState.SHORT_CIRCUITED,
            LifecycleState.INDETERMINATE,
        }
        if terminal and outcome is None:
            outcome = (
                OperationOutcome.FAILURE
                if state in {LifecycleState.FAILED, LifecycleState.ABORTED}
                else OperationOutcome.CANCELLED
                if state == LifecycleState.CANCELLED
                else OperationOutcome.REJECTED
                if state == LifecycleState.REJECTED
                else OperationOutcome.SUCCESS
            )
        event = TraceEventV1(
            ticket_id=context.ticket_id or "unknown",
            agent_id=context.agent_id,
            invocation_id=context.invocation_id,
            trace_id=context.trace_id,
            action_id=context.action_id,
            parent_action_id=context.parent_action_id,
            tool_call_id=context.tool_call_id,
            iteration=iteration if iteration is not None else context.iteration,
            action=ActionDescriptor(type=action_type, phase=phase, target=target),
            lifecycle=LifecycleDescriptor(state=state, retry_kind=retry_kind),
            duration_ms=duration_ms if terminal else None,
            outcome=outcome,
            error=ErrorDescriptor(
                type=type(error).__name__,
                message=get_shared_redactor().redact_string(
                    context.ticket_id or "unknown", str(error)
                )[:4096],
                retryable=False,
            )
            if error
            else None,
            attributes=attributes,
        )
        if self.client is not None:
            try:
                self.client.record(event)
            except Exception:
                # Trace delivery is deliberately non-fatal for agent work, but
                # losing the audit trail must remain visible to operators.
                logger.exception(
                    "failed to durably record trace event %s", event.event_id
                )
        return event
