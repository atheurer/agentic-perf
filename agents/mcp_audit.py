"""Trace propagation and audit middleware for ticket-local FastMCP servers."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.base import ToolResult
from mcp import McpError
from mcp.types import ErrorData
from pydantic import TypeAdapter

from providers.redaction import (
    bootstrap_shared_redactor_from_environment,
    get_shared_redactor,
)
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
from providers.tracing.payloads import (
    PayloadBlobStore,
    PayloadBuilder,
    PayloadStorageError,
)

META_KEY = "agentic-perf"
_MAX_REPLAY_CACHE = 1024
# #788 owns each tool's durable operation semantics.  This boundary merely
# refuses to dispatch a protected handler unless #787 has already accepted its
# immutable idempotency identity.
_PROTECTED_TOOLS = frozenset({"execute_benchmark"})
_MAX_OPERATION_RESULT_BYTES = 1024 * 1024
_MAX_ERROR_MESSAGE_BYTES = 4096


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
        # Correlation IDs remain stable across a transport reconnect.  Retain
        # them across SDK session IDs so a replay cannot become a second launch.
        self._seen: OrderedDict[str, str] = OrderedDict()

    async def close(self) -> None:
        """Flush and close the server-owned trace transport at process shutdown."""
        if self._client is not None:
            await asyncio.to_thread(self._client.close)

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
                ErrorDescriptor(
                    type=type(error).__name__,
                    message=get_shared_redactor().redact_string(
                        context.ticket_id or "unknown", str(error)
                    )[:_MAX_ERROR_MESSAGE_BYTES],
                )
                if error is not None
                else None
            ),
            attributes=attributes,
        )
        if self._record is not None:
            self._record(event)
        elif self._client is not None:
            # Server-side boundaries decide whether a protected action can be
            # retried.  Acknowledging them before replying makes that audit
            # trail durable across a subprocess restart.
            self._client.record_critical(event)

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

    def _protected_result(
        self, payload: dict[str, Any], *, is_error: bool = False
    ) -> ToolResult:
        return ToolResult(
            content=json.dumps(payload, sort_keys=True), is_error=is_error
        )

    def _result_descriptor(self, trace: TraceContext, result: Any) -> dict[str, Any]:
        """Serialize the actual FastMCP result, bounded for operation storage."""
        if not isinstance(result, ToolResult):
            return {"mcp_result": json.dumps(result, default=str)[:4096]}
        payload = result.model_dump(mode="json")
        encoded = json.dumps(payload, sort_keys=True)
        if len(encoded) > 4096:
            if len(encoded.encode()) > _MAX_OPERATION_RESULT_BYTES:
                raise PayloadStorageError(
                    "protected result exceeds operation result quota"
                )
            root = Path(os.environ.get("AGENTIC_PERF_HOME", ".")) / "trace-payloads"
            ticket_id = trace.ticket_id or "unknown"
            descriptor = PayloadBuilder(
                get_shared_redactor(),
                blob_store=PayloadBlobStore(root, ticket_id=ticket_id),
            ).build(ticket_id, payload)
            return {"operation_result": descriptor.model_dump(mode="json")}
        return {"tool_result": payload}

    def _protect_operation(
        self, trace: TraceContext, tool_name: str
    ) -> tuple[dict[str, Any] | None, ToolResult | None]:
        """Acquire durable protection or return a cached/in-progress response."""
        if tool_name not in _PROTECTED_TOOLS:
            return None, None
        if not trace.idempotency_key or not trace.idempotency_request_hash:
            return None, self._protected_result(
                {
                    "status": "rejected",
                    "reason": "protected tool lacks durable operation identity",
                },
                is_error=True,
            )
        if self._client is None:
            return None, self._protected_result(
                {"status": "rejected", "reason": "operation registry unavailable"},
                is_error=True,
            )
        try:
            acquired = self._client.operation_acquire(
                trace.idempotency_key, trace.idempotency_request_hash, 300
            )
        except Exception:
            return None, self._protected_result(
                {"status": "rejected", "reason": "operation registry unavailable"},
                is_error=True,
            )
        status = acquired.get("status")
        if status == "terminal":
            descriptor = acquired.get("operation", {}).get("result_descriptor", {})
            payload = descriptor.get("tool_result")
            if payload is None and isinstance(descriptor.get("operation_result"), dict):
                safe = descriptor["operation_result"]
                try:
                    root = (
                        Path(os.environ.get("AGENTIC_PERF_HOME", "."))
                        / "trace-payloads"
                    )
                    ticket_id = trace.ticket_id or "unknown"
                    content = PayloadBlobStore(root, ticket_id=ticket_id).get(
                        safe["blob_ref"], max_bytes=_MAX_OPERATION_RESULT_BYTES
                    )
                    if len(content) != safe.get("redacted_size_bytes"):
                        raise PayloadStorageError("payload size mismatch")
                    payload = json.loads(content).get("tool_result")
                except (KeyError, ValueError, PayloadStorageError):
                    payload = None
            if isinstance(payload, dict):
                from fastmcp.tools.base import ContentBlock

                return None, ToolResult(
                    content=TypeAdapter(list[ContentBlock]).validate_python(
                        payload.get("content", [])
                    ),
                    structured_content=payload.get("structured_content"),
                    meta=payload.get("meta"),
                    is_error=bool(payload.get("is_error")),
                )
            return None, self._protected_result(
                {
                    "status": "indeterminate",
                    "reason": "protected result integrity check failed",
                    "operation": descriptor,
                },
                is_error=True,
            )
        if status != "acquired":
            return None, self._protected_result(
                {
                    "status": status or "in_progress",
                    "operation": acquired.get("operation", {}),
                },
                is_error=True,
            )
        return acquired.get("operation", {}), None

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
        lease, protected_result = self._protect_operation(trace, tool_name)
        if protected_result is not None:
            self._emit(
                trace,
                context.fastmcp_context,
                LifecycleState.DUPLICATE_DETECTED
                if tool_name in _PROTECTED_TOOLS and trace.idempotency_key
                else LifecycleState.REJECTED,
                tool_name=tool_name,
                duration_ms=(time.monotonic() - started) * 1000,
                outcome=(
                    OperationOutcome.SUCCESS
                    if not protected_result.is_error
                    else OperationOutcome.REJECTED
                ),
                retry_kind=RetryKind.TRANSPORT_REPLAY,
            )
            return protected_result
        key = trace.mcp_correlation_request_id or ""
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
            if lease is not None:
                self._client.operation_transition(
                    trace.idempotency_key or "",
                    "prepared",
                    int(lease["fencing_generation"]),
                )
                self._client.operation_transition(
                    trace.idempotency_key or "",
                    "side-effect-started",
                    int(lease["fencing_generation"]),
                )
            result = await call_next(context)
        except asyncio.CancelledError:
            if lease is not None:
                self._client.operation_transition(
                    trace.idempotency_key or "",
                    "indeterminate",
                    int(lease["fencing_generation"]),
                    descriptor={"outcome": "cancelled_after_start"},
                )
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
            if lease is not None:
                self._client.operation_transition(
                    trace.idempotency_key or "",
                    "indeterminate",
                    int(lease["fencing_generation"]),
                    descriptor={
                        "outcome": "exception_after_start",
                        "type": type(exc).__name__,
                    },
                )
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
        if lease is not None:
            descriptor = self._result_descriptor(trace, result)
            self._client.operation_transition(
                trace.idempotency_key or "",
                "fail" if getattr(result, "is_error", False) else "complete",
                int(lease["fencing_generation"]),
                descriptor=descriptor,
            )
        terminal_state = (
            LifecycleState.FAILED
            if getattr(result, "is_error", False)
            else LifecycleState.RESPONSE_SENT
        )
        self._emit(
            trace,
            context.fastmcp_context,
            terminal_state,
            tool_name=tool_name,
            duration_ms=(time.monotonic() - started) * 1000,
            outcome=(
                OperationOutcome.FAILURE
                if getattr(result, "is_error", False)
                else OperationOutcome.SUCCESS
            ),
        )
        return result


def create_ticket_mcp(server_name: str) -> FastMCP:
    """Create the required audited FastMCP instance for a local ticket server."""
    bootstrap_shared_redactor_from_environment()
    middleware = MCPAuditMiddleware(server_name)

    @asynccontextmanager
    async def lifespan(_: FastMCP):
        try:
            yield {}
        finally:
            await middleware.close()

    return FastMCP(server_name, middleware=[middleware], lifespan=lifespan)


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
