"""Trace propagation and audit middleware for ticket-local FastMCP servers."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from mcp import McpError
from mcp.types import ErrorData

from providers.tracing import (
    ActionDescriptor,
    ActionType,
    ErrorDescriptor,
    IdempotencyDescriptor,
    LifecycleDescriptor,
    LifecycleState,
    MCPIdentity,
    OperationOutcome,
    ProducerIdentity,
    RetryKind,
    TraceContext,
    TraceEventV1,
    bind_trace_context,
    reset_trace_context,
)
from providers.tracing.client import TraceClient

META_KEY = "agentic-perf"
_MAX_REPLAY_CACHE = 1024


def _meta_values(message: Any) -> dict[str, Any]:
    """Return extension values from an MCP request without depending on internals."""
    meta = getattr(message, "meta", None) or getattr(message, "_meta", None)
    if meta is None:
        return {}
    values = getattr(meta, "model_extra", None)
    if values is None and isinstance(meta, dict):
        values = meta
    return dict(values or {})


def _context_from_meta(values: dict[str, Any]) -> TraceContext | None:
    payload = values.get(META_KEY)
    if not isinstance(payload, dict):
        return None
    try:
        return TraceContext.model_validate(
            {
                "ticket_id": payload.get("ticket_id"),
                "agent_id": payload.get("agent_id"),
                "invocation_id": payload.get("invocation_id"),
                "trace_id": payload["trace_id"],
                "action_id": payload["action_id"],
                "parent_action_id": payload.get("parent_action_id"),
                "iteration": payload.get("iteration"),
                "tool_call_id": payload.get("tool_call_id"),
                "mcp_server": payload.get("mcp_server"),
                "mcp_session_id": payload.get("mcp_session_id"),
                "mcp_correlation_request_id": payload["correlation_request_id"],
                "idempotency_key": payload.get("idempotency_key"),
                "idempotency_request_hash": payload.get("idempotency_request_hash"),
            }
        )
    except (KeyError, ValueError):
        return None


class MCPAuditMiddleware(Middleware):
    """Audit MCP calls before dispatch and bind propagated causality to tools."""

    def __init__(
        self,
        server_name: str,
        *,
        ticket_id: str | None = None,
        agent_id: str | None = None,
        record: Callable[[TraceEventV1], None] | None = None,
    ) -> None:
        self.server_name = server_name
        self.ticket_id = (
            ticket_id if ticket_id is not None else os.environ.get("TICKET_ID")
        )
        self.agent_id = (
            agent_id if agent_id is not None else os.environ.get("AGENT_NAME")
        )
        self._record = record
        self._client: TraceClient | None = None
        token = os.environ.get("AGENTIC_PERF_API_TOKEN", "")
        url = os.environ.get("STATE_STORE_URL", "")
        if record is None and token and url:
            self._client = TraceClient(url, token)
        self._seen: OrderedDict[tuple[str, str], str] = OrderedDict()

    def _emit(
        self,
        context: TraceContext,
        fastmcp_context: Any,
        state: LifecycleState,
        *,
        tool_name: str,
        duration_ms: float | None = None,
        outcome: OperationOutcome | None = None,
        error: BaseException | None = None,
        retry_kind: RetryKind = RetryKind.NONE,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        try:
            protocol_id = str(fastmcp_context.request_id)
            session_id = str(fastmcp_context.session_id)
        except (AttributeError, RuntimeError):
            protocol_id = None
            session_id = context.mcp_session_id or "unknown"
        if not context.ticket_id:
            return
        event = TraceEventV1(
            ticket_id=context.ticket_id,
            agent_id=context.agent_id,
            invocation_id=context.invocation_id,
            trace_id=context.trace_id,
            action_id=context.action_id,
            parent_action_id=context.parent_action_id,
            tool_call_id=context.tool_call_id,
            iteration=context.iteration,
            producer=ProducerIdentity(
                component="mcp_server", pid=os.getpid(), instance_id=self.server_name
            ),
            mcp=MCPIdentity(
                server=self.server_name,
                transport="local",
                session_id=session_id,
                protocol_request_id=protocol_id,
                correlation_request_id=context.mcp_correlation_request_id or "unknown",
                server_pid=os.getpid(),
            ),
            action=ActionDescriptor(type=ActionType.MCP, phase=tool_name),
            lifecycle=LifecycleDescriptor(state=state, retry_kind=retry_kind),
            idempotency=IdempotencyDescriptor(
                key=context.idempotency_key,
                request_hash=context.idempotency_request_hash,
            ),
            duration_ms=duration_ms,
            outcome=outcome,
            error=(
                ErrorDescriptor(type=type(error).__name__, message=str(error))
                if error is not None
                else None
            ),
            attributes=attributes,
        )
        if self._record is not None:
            self._record(event)
        elif self._client is not None:
            self._client.record(event)

    def _validate_identity(self, context: TraceContext | None) -> TraceContext:
        if context is None:
            raise McpError(
                ErrorData(code=-32602, message="missing agentic-perf trace metadata")
            )
        if self.ticket_id and context.ticket_id != self.ticket_id:
            raise McpError(
                ErrorData(
                    code=-32602,
                    message="trace ticket identity does not match server ticket",
                )
            )
        if self.agent_id and context.agent_id != self.agent_id:
            raise McpError(
                ErrorData(
                    code=-32602,
                    message="trace agent identity does not match server agent",
                )
            )
        return context

    async def on_call_tool(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Any],
    ) -> Any:
        started = time.monotonic()
        values = _meta_values(context.message)
        propagated = _context_from_meta(values)
        tool_name = str(getattr(context.message, "name", "unknown"))
        # A server can still be used in isolated unit tests without ticket env.
        # Ticket-local processes, however, reject an unjoinable tool invocation.
        if propagated is None and not self.ticket_id:
            propagated = TraceContext(
                ticket_id="test", mcp_correlation_request_id=uuid.uuid4().hex
            )
        try:
            trace = self._validate_identity(propagated)
        except McpError as exc:
            fallback = propagated or TraceContext(
                ticket_id=self.ticket_id or "unknown",
                agent_id=self.agent_id,
                mcp_correlation_request_id=uuid.uuid4().hex,
            )
            self._emit(
                fallback,
                context.fastmcp_context,
                LifecycleState.REQUEST_RECEIVED,
                tool_name=tool_name,
            )
            self._emit(
                fallback,
                context.fastmcp_context,
                LifecycleState.REJECTED,
                tool_name=tool_name,
                duration_ms=(time.monotonic() - started) * 1000,
                outcome=OperationOutcome.REJECTED,
                error=exc,
            )
            raise

        self._emit(
            trace,
            context.fastmcp_context,
            LifecycleState.REQUEST_RECEIVED,
            tool_name=tool_name,
        )
        try:
            session_id = str(context.fastmcp_context.session_id)
        except (AttributeError, RuntimeError):
            session_id = trace.mcp_session_id or "unknown"
        key = (session_id, trace.mcp_correlation_request_id or "")
        prior = self._seen.get(key)
        if prior is not None:
            self._seen.move_to_end(key)
            self._emit(
                trace,
                context.fastmcp_context,
                LifecycleState.DUPLICATE_DETECTED,
                tool_name=tool_name,
                duration_ms=(time.monotonic() - started) * 1000,
                outcome=OperationOutcome.SUCCESS,
                retry_kind=RetryKind.TRANSPORT_REPLAY,
                attributes={"replay_of_action_id": prior},
            )
            raise McpError(
                ErrorData(code=-32000, message="duplicate MCP delivery detected")
            )
        self._seen[key] = trace.action_id
        if len(self._seen) > _MAX_REPLAY_CACHE:
            self._seen.popitem(last=False)

        token = bind_trace_context(trace)
        try:
            result = await call_next(context)
        except asyncio.CancelledError:
            self._emit(
                trace,
                context.fastmcp_context,
                LifecycleState.CANCELLED,
                tool_name=tool_name,
                duration_ms=(time.monotonic() - started) * 1000,
                outcome=OperationOutcome.CANCELLED,
            )
            raise
        except Exception as exc:
            self._emit(
                trace,
                context.fastmcp_context,
                LifecycleState.FAILED,
                tool_name=tool_name,
                duration_ms=(time.monotonic() - started) * 1000,
                outcome=OperationOutcome.FAILURE,
                error=exc,
            )
            raise
        finally:
            reset_trace_context(token)
        self._emit(
            trace,
            context.fastmcp_context,
            LifecycleState.RESPONSE_SENT,
            tool_name=tool_name,
            duration_ms=(time.monotonic() - started) * 1000,
            outcome=OperationOutcome.SUCCESS,
        )
        return result


def create_ticket_mcp(server_name: str) -> FastMCP:
    """Create the required audited FastMCP instance for a local ticket server."""
    return FastMCP(server_name, middleware=[MCPAuditMiddleware(server_name)])


def assert_fastmcp_audit_compatibility() -> None:
    """Fail clearly when the pinned SDK no longer exposes the audited contract."""
    from mcp import ClientSession

    if "meta" not in ClientSession.call_tool.__code__.co_varnames:
        raise RuntimeError("MCP SDK lacks ClientSession.call_tool(..., meta=...)")
    required = {"request_id", "session_id"}
    # The public FastMCP Context properties are the server-side audit contract.
    from fastmcp.server.context import Context

    missing = [
        name
        for name in required
        if not isinstance(getattr(Context, name, None), property)
    ]
    if missing:
        raise RuntimeError(
            "FastMCP SDK lacks audit context properties: " + ", ".join(missing)
        )
