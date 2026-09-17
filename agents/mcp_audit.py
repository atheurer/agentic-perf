"""Trace propagation and audit middleware for ticket-local FastMCP servers."""

from __future__ import annotations

import asyncio
import inspect
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
# Only tools whose *whole invocation* is a durable side effect belong here.
# Resource availability normally reads provider state; its exceptional named
# device escalation has its own, narrower operation boundary in
# ``agents.resource.server``.
_PROTECTED_TOOLS = frozenset({"execute_benchmark"})
_OPERATION_LEASE_TTL_SECONDS = 300.0
_OPERATION_LEASE_RENEW_INTERVAL_SECONDS = 100.0
_MAX_OPERATION_RESULT_BYTES = 1024 * 1024
_MAX_ERROR_MESSAGE_BYTES = 4096
_MAX_REDACTED_KEY_BYTES = 4096


def _meta_values(message: Any) -> dict[str, Any]:
    """Return extension values from an MCP request without depending on internals."""
    if not isinstance(message, dict) and hasattr(message, "model_extra"):
        values = getattr(message, "model_extra", None)
        if values:
            return dict(values)
    if isinstance(message, dict):
        meta = message.get("meta") or message.get("_meta")
    else:
        meta = getattr(message, "meta", None) or getattr(message, "_meta", None)
    if meta is None:
        return {}
    values = getattr(meta, "model_extra", None)
    if values is None:
        if isinstance(meta, dict):
            values = meta
        elif hasattr(meta, "model_dump"):
            values = meta.model_dump()
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
        registration_path: str | None = None,
    ) -> None:
        self.server_name = server_name
        self.ticket_id = (
            ticket_id if ticket_id is not None else os.environ.get("TICKET_ID")
        )
        self.agent_id = (
            agent_id if agent_id is not None else os.environ.get("AGENT_NAME")
        )
        self._record = record
        self.registration_path = registration_path
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
        policy_attributes: dict[str, Any] = {}
        if self.registration_path:
            # The policy is resolved at the real FastMCP boundary, not from a
            # test-only symbol lookup.  A new registered tool therefore cannot
            # emit an apparently valid audit pair without its declared owner.
            from agents.tool_audit_policy import POLICY_BY_REGISTRATION

            policy = POLICY_BY_REGISTRATION.get(f"{self.registration_path}:{tool_name}")
            if policy is not None:
                policy_attributes = {
                    "policy_registration": policy.registration,
                    "policy_classification": policy.classification,
                    "operation_owner": policy.operation_owner,
                }
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
            attributes={
                # These fields make the runtime proof inspect the same
                # production boundary that the AST inventory permits.
                "audit_boundary": "MCPAuditMiddleware.on_call_tool",
                "audit_transport": "local",
                "causal_ancestor": context.parent_action_id,
                **policy_attributes,
                **(attributes or {}),
            },
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

    @staticmethod
    def _sanitize_value(ticket_id: str, value: Any) -> Any:
        """Redact text while leaving opaque binary content untouched."""
        redactor = get_shared_redactor()
        if isinstance(value, str):
            return redactor.redact_string(ticket_id, value)
        if isinstance(value, list):
            return [
                MCPAuditMiddleware._sanitize_value(ticket_id, item) for item in value
            ]
        if isinstance(value, dict):
            block_type = value.get("type")
            sanitized: dict[Any, Any] = {}
            for key, item in value.items():
                if isinstance(key, str):
                    safe_key = get_shared_redactor().redact_string(ticket_id, key)[
                        :_MAX_REDACTED_KEY_BYTES
                    ]
                elif not isinstance(key, (int, float, bool, type(None))):
                    # Arbitrary object keys cannot be represented safely in MCP
                    # JSON structures; stringify them without exposing repr data.
                    safe_key = get_shared_redactor().redact_string(ticket_id, str(key))[
                        :_MAX_REDACTED_KEY_BYTES
                    ]
                else:
                    safe_key = key
                # Redaction can map distinct schema keys to one marker. Keep
                # every field with a deterministic suffix rather than letting
                # a dict assignment silently discard a value.
                if safe_key in sanitized:
                    base = str(safe_key)
                    index = 2
                    candidate = f"{base}#{index}"
                    while candidate in sanitized:
                        index += 1
                        candidate = f"{base}#{index}"
                    safe_key = candidate
                sanitized[safe_key] = (
                    item
                    if safe_key == "data" and block_type in {"image", "audio"}
                    else MCPAuditMiddleware._sanitize_value(ticket_id, item)
                )
            return sanitized
        return value

    def _sanitize_result(self, trace: TraceContext, result: Any) -> Any:
        """Sanitize the exact ToolResult returned and durably replayed."""
        if not isinstance(result, ToolResult):
            return result
        ticket_id = trace.ticket_id or "unknown"
        blocks = []
        for block in result.content:
            fields = block.model_dump(mode="python")
            fields = self._sanitize_value(ticket_id, fields)
            blocks.append(type(block).model_validate(fields))
        structured = self._sanitize_value(ticket_id, result.structured_content)
        meta = self._sanitize_value(ticket_id, result.meta)
        return result.model_copy(
            update={"content": blocks, "structured_content": structured, "meta": meta}
        )

    @staticmethod
    def _redacted_exception(ticket_id: str, exc: BaseException) -> BaseException:
        message = get_shared_redactor().redact_string(ticket_id, str(exc))[
            :_MAX_ERROR_MESSAGE_BYTES
        ]
        try:
            return type(exc)(message)
        except Exception:
            return RuntimeError(message)

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
                # Leave room for the descriptor envelope in the state-store's
                # bounded terminal-operation record; the full result is in the
                # content-addressed blob.
                inline_bytes=1024,
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
                trace.idempotency_key,
                trace.idempotency_request_hash,
                _OPERATION_LEASE_TTL_SECONDS,
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
                    # The blob stores the canonical ToolResult object itself;
                    # the operation descriptor is only the bounded envelope.
                    payload = json.loads(content)
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

    async def _renew_operation_lease(
        self,
        trace: TraceContext,
        lease: dict[str, Any],
        stop: asyncio.Event,
    ) -> BaseException | None:
        """Renew a protected operation until its handler has returned.

        The registry client is synchronous, so renewal runs in a worker thread
        and cannot block the MCP event loop that is waiting for the benchmark.
        A failed acknowledgement stops the loop and is returned to the caller;
        the caller then records the completed handler as indeterminate rather
        than claiming a terminal success with an unconfirmed lease.
        """
        if self._client is None:
            return RuntimeError("operation registry unavailable")
        interval = min(
            _OPERATION_LEASE_RENEW_INTERVAL_SECONDS,
            _OPERATION_LEASE_TTL_SECONDS / 3,
        )
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return None
            except asyncio.TimeoutError:
                try:
                    await asyncio.to_thread(
                        self._client.operation_transition,
                        trace.idempotency_key or "",
                        "renew",
                        int(lease["fencing_generation"]),
                        ttl_seconds=_OPERATION_LEASE_TTL_SECONDS,
                    )
                except Exception as exc:
                    return exc

    @staticmethod
    async def _stop_operation_lease_renewal(
        task: asyncio.Task[BaseException | None] | None,
        stop: asyncio.Event | None,
    ) -> BaseException | None:
        """Stop a lease heartbeat and collect its explicit failure, if any."""
        if task is None or stop is None:
            return None
        stop.set()
        try:
            return await task
        except asyncio.CancelledError:
            # A cancelled MCP call is already indeterminate after the side
            # effect boundary.  Do not let heartbeat cleanup mask that result.
            return None

    def _mark_renewal_failure(
        self,
        trace: TraceContext,
        fastmcp_context: Any,
        *,
        tool_name: str,
        duration_ms: float,
        lease: dict[str, Any],
        failure: BaseException,
    ) -> None:
        """Persist a known renewal loss and fail closed for the MCP caller."""
        renewal_error = RuntimeError("protected operation lease renewal failed")
        self._emit_terminal(
            trace,
            fastmcp_context,
            LifecycleState.INDETERMINATE,
            tool_name=tool_name,
            duration_ms=duration_ms,
            outcome=OperationOutcome.INDETERMINATE,
            lease=lease,
            error=renewal_error,
        )
        try:
            self._client.operation_transition(
                trace.idempotency_key or "",
                "indeterminate",
                int(lease["fencing_generation"]),
                descriptor={
                    "outcome": "lease_renewal_failed",
                    "type": type(failure).__name__,
                },
            )
        except Exception as operation_error:
            raise McpError(
                ErrorData(
                    code=-32000,
                    message=(
                        "MCP tool outcome is indeterminate; operation lease "
                        "renewal was not acknowledged"
                    ),
                )
            ) from operation_error
        raise McpError(
            ErrorData(
                code=-32000,
                message=(
                    "MCP tool outcome is indeterminate; operation lease renewal "
                    "was not acknowledged"
                ),
            )
        ) from failure

    def _emit_terminal(
        self,
        trace: TraceContext,
        fastmcp_context: Any,
        state: LifecycleState,
        *,
        tool_name: str,
        duration_ms: float,
        outcome: OperationOutcome,
        lease: dict[str, Any] | None,
        error: BaseException | None = None,
    ) -> None:
        """Persist a terminal audit event or make a protected effect explicit.

        A started protected operation cannot safely be presented as completed
        when the terminal trace acknowledgement is lost.  Mark the operation
        indeterminate when possible and fail the MCP request so reconciliation
        is required instead of silently losing the audit pair.
        """
        try:
            self._emit(
                trace,
                fastmcp_context,
                state,
                tool_name=tool_name,
                duration_ms=duration_ms,
                outcome=outcome,
                error=error,
            )
        except Exception as audit_error:
            if lease is not None and self._client is not None:
                try:
                    self._client.operation_transition(
                        trace.idempotency_key or "",
                        "indeterminate",
                        int(lease["fencing_generation"]),
                        descriptor={"outcome": "terminal_audit_delivery_failed"},
                    )
                except Exception:
                    # The failed response below is deliberately the visible
                    # signal even if the operation registry is unavailable too.
                    pass
            raise McpError(
                ErrorData(
                    code=-32000,
                    message="MCP tool outcome is indeterminate; audit terminal was not persisted",
                )
            ) from audit_error

    async def on_call_tool(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Any],
    ) -> Any:
        started = time.monotonic()
        values = _meta_values(context.message)
        # FastMCP 3.4 reconstructs CallToolRequestParams before invoking
        # middleware, so request metadata is retained on its public request
        # context rather than on ``context.message``.
        if META_KEY not in values and context.fastmcp_context is not None:
            request_context = getattr(context.fastmcp_context, "request_context", None)
            if request_context is not None:
                values = _meta_values(request_context.meta)
                if META_KEY not in values:
                    request = getattr(request_context, "request", None)
                    values = _meta_values(getattr(request, "params", None))
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

        # A ticket-owned MCP process may not execute a tool with no durable
        # audit transport.  Unit-only servers without ticket identity retain
        # their in-memory recorder behaviour, but production callers fail
        # before the handler can cause an effect.
        if self.ticket_id and self._record is None and self._client is None:
            raise McpError(
                ErrorData(code=-32000, message="MCP audit transport is unavailable")
            )

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
        renewal_stop: asyncio.Event | None = None
        renewal_task: asyncio.Task[BaseException | None] | None = None
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
                renewal_stop = asyncio.Event()
                renewal_task = asyncio.create_task(
                    self._renew_operation_lease(trace, lease, renewal_stop),
                    name=f"mcp-operation-renew:{trace.idempotency_key}",
                )
            result = await call_next(context)
            renewal_failure = await self._stop_operation_lease_renewal(
                renewal_task, renewal_stop
            )
        except asyncio.CancelledError:
            await self._stop_operation_lease_renewal(renewal_task, renewal_stop)
            if lease is not None:
                self._client.operation_transition(
                    trace.idempotency_key or "",
                    "indeterminate",
                    int(lease["fencing_generation"]),
                    descriptor={"outcome": "cancelled_after_start"},
                )
            self._emit_terminal(
                trace,
                context.fastmcp_context,
                LifecycleState.CANCELLED,
                tool_name=tool_name,
                duration_ms=(time.monotonic() - started) * 1000,
                outcome=OperationOutcome.CANCELLED,
                lease=lease,
            )
            raise
        except Exception as exc:
            await self._stop_operation_lease_renewal(renewal_task, renewal_stop)
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
            self._emit_terminal(
                trace,
                context.fastmcp_context,
                LifecycleState.FAILED,
                tool_name=tool_name,
                duration_ms=(time.monotonic() - started) * 1000,
                outcome=OperationOutcome.FAILURE,
                lease=lease,
                error=exc,
            )
            raise self._redacted_exception(trace.ticket_id or "unknown", exc) from exc
        finally:
            reset_trace_context(token)
        result = self._sanitize_result(trace, result)
        if lease is not None and renewal_failure is not None:
            self._mark_renewal_failure(
                trace,
                context.fastmcp_context,
                tool_name=tool_name,
                duration_ms=(time.monotonic() - started) * 1000,
                lease=lease,
                failure=renewal_failure,
            )
        terminal_state = (
            LifecycleState.FAILED
            if getattr(result, "is_error", False)
            else LifecycleState.RESPONSE_SENT
        )
        self._emit_terminal(
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
            lease=lease,
        )
        # A protected operation must not become a durable success/failure
        # before its correlated terminal audit has been acknowledged.  If the
        # audit write above fails, ``_emit_terminal`` can still transition the
        # live side-effect-started operation to indeterminate.  Reversing this
        # order would leave an immutable terminal success behind after a lost
        # terminal trace and make the loss invisible to replay/reconciliation.
        if lease is not None:
            descriptor = self._result_descriptor(trace, result)
            try:
                self._client.operation_transition(
                    trace.idempotency_key or "",
                    "fail" if getattr(result, "is_error", False) else "complete",
                    int(lease["fencing_generation"]),
                    descriptor=descriptor,
                )
            except Exception as operation_error:
                # The trace is durable but we do not know whether the terminal
                # operation acknowledgement was lost before or after commit.
                # Do not return a success/failure result in that ambiguity;
                # preserve the side-effect-started record for reconciliation.
                raise McpError(
                    ErrorData(
                        code=-32000,
                        message=(
                            "MCP tool outcome is indeterminate; operation terminal "
                            "was not acknowledged"
                        ),
                    )
                ) from operation_error
        return result


def create_ticket_mcp(server_name: str) -> FastMCP:
    """Create the required audited FastMCP instance for a local ticket server."""
    bootstrap_shared_redactor_from_environment()
    caller = Path(inspect.stack()[1].filename).resolve()
    project_root = Path(__file__).resolve().parents[1]
    try:
        registration_path = caller.relative_to(project_root).as_posix()
    except ValueError:
        # External integration fixtures may create a private server script;
        # production package servers are always project-relative and enforced
        # below by the registration inventory test.
        registration_path = None
    middleware = MCPAuditMiddleware(server_name, registration_path=registration_path)

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
