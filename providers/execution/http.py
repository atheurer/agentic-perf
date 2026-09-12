"""Audited outbound HTTP clients.

This is the single boundary for ticket-scoped provider and state-store HTTP.
It intentionally records descriptors rather than request or response bodies:
credentials are useful to the remote service, never to the audit log.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from providers.tracing import (
    ActionDescriptor,
    ActionType,
    ErrorDescriptor,
    IdempotencyDescriptor,
    IdempotencyOutcome,
    LifecycleDescriptor,
    LifecycleState,
    OperationOutcome,
    PayloadDescriptor,
    RetryKind,
    TraceEventV1,
    child_context,
    current_trace_context,
    trace_headers,
)
from providers.tracing.client import TraceClient, TraceDeliveryError

_SAFE_HEADERS = frozenset({"accept", "content-type", "user-agent", "x-requested-with"})
_REQUEST_ID_HEADERS = frozenset(
    {
        "x-request-id",
        "x-requestid",
        "x-amzn-requestid",
        "x-amz-request-id",
        "x-correlation-id",
    }
)
_READ_ONLY = frozenset({"GET", "HEAD", "OPTIONS"})
_SECRET_PATH_LABELS = frozenset(
    {"callback", "callbacks", "hook", "hooks", "token", "tokens", "webhook", "webhooks"}
)


class AmbiguousHTTPReplayError(httpx.RequestError):
    """A mutating request may have reached its peer and must not be replayed."""


def _safe_target(url: str | httpx.URL) -> str:
    """Keep a useful target without retaining signed or secret path material."""
    parsed = urlsplit(str(url))
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    segments = parsed.path.split("/")
    sanitized: list[str] = []
    redact_next = False
    for segment in segments:
        lower = segment.lower()
        high_entropy = (
            len(segment) >= 24
            and any(char.isdigit() for char in segment)
            and sum(char.isalnum() or char in "-_=." for char in segment)
            == len(segment)
        )
        if redact_next or high_entropy:
            digest = hashlib.sha256(segment.encode()).hexdigest()[:16]
            sanitized.append(f"[redacted:{digest}]")
        else:
            sanitized.append(segment)
        redact_next = lower in _SECRET_PATH_LABELS
    return urlunsplit((parsed.scheme, host, "/".join(sanitized), "", ""))


def _safe_headers(headers: Any) -> dict[str, str]:
    if not isinstance(headers, Mapping):
        return {}
    try:
        values = dict(headers or {})
    except (TypeError, ValueError):
        return {}
    return {
        str(k).lower(): str(v)[:256]
        for k, v in values.items()
        if str(k).lower() in _SAFE_HEADERS
    }


def _descriptor(value: Any, media_type: str | None = None) -> PayloadDescriptor | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        raw = value.encode()
    else:
        try:
            raw = json.dumps(value, sort_keys=True, default=str).encode()
        except (TypeError, ValueError):
            raw = repr(value).encode()
    return PayloadDescriptor(
        size_bytes=len(raw),
        original_size_bytes=len(raw),
        media_type=media_type if isinstance(media_type, str) else None,
        digest=hashlib.sha256(raw).hexdigest()[:32],
        digest_kind="sha256",
        truncated=False,
        redaction_applied=True,
    )


def _request_payload(kwargs: dict[str, Any]) -> PayloadDescriptor | None:
    for name in ("json", "content", "data"):
        if name in kwargs and kwargs[name] is not None:
            return _descriptor(
                kwargs[name], _safe_headers(kwargs.get("headers")).get("content-type")
            )
    return None


class _AuditedHTTPBase:
    _default_recorder: TraceClient | None = None
    _default_recorder_lock = threading.Lock()

    def __init__(
        self,
        client: Any,
        *,
        emit: Callable[[TraceEventV1], Awaitable[Any]] | None = None,
        recorder: TraceClient | None = None,
        retries: int = 0,
        retry_statuses: set[int] | None = None,
        supports_idempotency: bool = False,
    ) -> None:
        self._client = client
        self._emit = emit
        self._recorder = recorder
        self.retries = retries
        self.retry_statuses = retry_statuses or {429, 502, 503, 504}
        self.supports_idempotency = supports_idempotency

    @classmethod
    def reset_default_recorder(cls) -> None:
        with cls._default_recorder_lock:
            recorder, cls._default_recorder = cls._default_recorder, None
        if recorder is not None:
            recorder.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def _context(self, method: str) -> Any:
        context = current_trace_context()
        if context is None:
            headers = getattr(self._client, "headers", {})
            if (
                isinstance(headers, Mapping)
                and headers.get("X-Agentic-Perf-Causal-Context") == "v1"
            ):
                traceparent = str(headers.get("traceparent", "")).split("-")
                try:
                    from providers.tracing import TraceContext

                    context = TraceContext(
                        ticket_id=headers.get("X-Agentic-Perf-Ticket-Id") or None,
                        agent_id=headers.get("X-Agentic-Perf-Agent-Id") or None,
                        invocation_id=headers.get("X-Agentic-Perf-Invocation-Id")
                        or None,
                        trace_id=traceparent[1],
                        action_id=headers.get("X-Agentic-Perf-Action-Id")
                        or traceparent[2],
                        parent_action_id=headers.get("X-Agentic-Perf-Parent-Action-Id")
                        or None,
                    )
                except (IndexError, ValueError):
                    context = None
        if context is None:
            return None
        return child_context(context)

    def _event(
        self,
        context: Any,
        state: LifecycleState,
        *,
        method: str,
        target: str,
        attempt: int,
        idempotency_key: str | None,
        input: PayloadDescriptor | None,
        output: PayloadDescriptor | None = None,
        duration_ms: float | None = None,
        response: httpx.Response | None = None,
        error: BaseException | None = None,
        retry_kind: RetryKind = RetryKind.NONE,
    ) -> TraceEventV1 | None:
        if context is None or not context.ticket_id:
            return None
        terminal = state in {
            LifecycleState.COMPLETED,
            LifecycleState.FAILED,
            LifecycleState.CANCELLED,
            LifecycleState.TIMED_OUT,
            LifecycleState.INDETERMINATE,
        }
        outcome = None
        if terminal:
            outcome = (
                OperationOutcome.CANCELLED
                if state == LifecycleState.CANCELLED
                else OperationOutcome.TIMED_OUT
                if state == LifecycleState.TIMED_OUT
                else OperationOutcome.INDETERMINATE
                if state == LifecycleState.INDETERMINATE
                else OperationOutcome.SUCCESS
                if state == LifecycleState.COMPLETED
                else OperationOutcome.FAILURE
            )
        request_headers = None
        if response is not None:
            try:
                request_headers = response.request.headers
            except RuntimeError:
                # Lightweight provider test doubles may not attach a request.
                pass
        attrs: dict[str, Any] = {
            "method": method,
            "target": target,
            "safe_headers": _safe_headers(request_headers),
        }
        if response is not None:
            attrs["status_code"] = response.status_code
            attrs["provider_request_id"] = next(
                (
                    str(response.headers[h])[:256]
                    for h in _REQUEST_ID_HEADERS
                    if h in response.headers
                ),
                None,
            )
        return TraceEventV1(
            ticket_id=context.ticket_id,
            agent_id=context.agent_id,
            invocation_id=context.invocation_id,
            trace_id=context.trace_id,
            action_id=context.action_id,
            parent_action_id=context.parent_action_id,
            tool_call_id=context.tool_call_id,
            action=ActionDescriptor(type=ActionType.API, target=target),
            lifecycle=LifecycleDescriptor(
                state=state, attempt=attempt, retry_kind=retry_kind
            ),
            idempotency=IdempotencyDescriptor(
                key=idempotency_key,
                outcome=IdempotencyOutcome.CLAIMED
                if idempotency_key
                else IdempotencyOutcome.NOT_APPLICABLE,
            ),
            input=input,
            output=output,
            duration_ms=duration_ms if terminal else None,
            outcome=outcome,
            error=ErrorDescriptor(
                type=type(error).__name__,
                code=hashlib.sha256(str(error).encode()).hexdigest()[:16],
                message="outbound HTTP failure (details redacted)",
                retryable=False,
            )
            if error
            else None,
            attributes=attrs,
        )

    async def _record(
        self, event: TraceEventV1 | None, *, critical: bool = False
    ) -> None:
        if event is None:
            if critical:
                raise TraceDeliveryError(
                    "mutating HTTP requires a ticket trace context"
                )
            return
        if self._emit is not None:
            await self._emit(event)
            return
        if self._recorder is None:
            url, token = (
                os.environ.get("STATE_STORE_URL"),
                os.environ.get("AGENTIC_PERF_API_TOKEN"),
            )
            if url and token:
                with self._default_recorder_lock:
                    if self.__class__._default_recorder is None:
                        self.__class__._default_recorder = TraceClient(url, token)
                    self._recorder = self.__class__._default_recorder
        if self._recorder is None:
            if critical:
                raise TraceDeliveryError(
                    "mutating HTTP requires central trace readiness"
                )
            return
        await asyncio.to_thread(
            self._recorder.record_critical if critical else self._recorder.record, event
        )

    def _headers(self, headers: Any, context: Any, key: str | None) -> dict[str, str]:
        result = dict(headers or {})
        if context is not None:
            result.update(trace_headers(context))
        if key:
            result.setdefault("Idempotency-Key", key)
        return result

    def _idempotency_key(self, context: Any, method: str, target: str) -> str | None:
        if method in _READ_ONLY:
            return None
        if context is None:
            return None
        if context.idempotency_key:
            return context.idempotency_key
        if not self.supports_idempotency:
            return None
        return hashlib.sha256(
            f"{context.ticket_id}:{context.action_id}:{method}:{target}".encode()
        ).hexdigest()

    def _retryable_before_send(self, error: BaseException) -> bool:
        return isinstance(
            error, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
        )


class AuditedAsyncHTTPClient(_AuditedHTTPBase):
    """Drop-in async httpx facade with causal headers and bounded trace events."""

    def __init__(
        self, *args: Any, client: httpx.AsyncClient | None = None, **kwargs: Any
    ) -> None:
        audit = {
            key: kwargs.pop(key)
            for key in (
                "emit",
                "recorder",
                "retries",
                "retry_statuses",
                "supports_idempotency",
            )
            if key in kwargs
        }
        super().__init__(client or httpx.AsyncClient(*args, **kwargs), **audit)

    async def __aenter__(self) -> "AuditedAsyncHTTPClient":
        # httpx returns itself, while retaining this detail also keeps injected
        # test/transports that yield a dedicated request object compatible.
        self._context_manager = self._client
        entered = await self._client.__aenter__()
        if entered is not None:
            self._client = entered
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self._context_manager.__aexit__(*args)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(
        self, method: str, url: str | httpx.URL, **kwargs: Any
    ) -> httpx.Response:
        send_method = kwargs.pop("_audit_send_method", None)
        if kwargs.pop("follow_redirects", False):
            raise ValueError(
                "automatic redirects are disabled for audited HTTP; issue a separately audited request"
            )
        # Override both per-call and client-constructor defaults.  A redirect
        # is a new target and must be initiated explicitly so it is audited.
        kwargs["follow_redirects"] = False
        method, target = method.upper(), _safe_target(url)
        context = self._context(method)
        mutating = method not in _READ_ONLY
        key = self._idempotency_key(context, method, target)
        can_replay = method in _READ_ONLY or self.supports_idempotency
        payload = _request_payload(kwargs)
        if mutating and context is None:
            raise TraceDeliveryError("mutating HTTP requires a ticket trace context")
        # An operation owns the complete request, while each wire attempt is a
        # child action.  This makes joins deterministic even after retries.
        operation_started = time.monotonic()
        await self._record(
            self._event(
                context,
                LifecycleState.REQUESTED,
                method=method,
                target=target,
                attempt=1,
                idempotency_key=key,
                input=payload,
            ),
            critical=mutating,
        )
        for attempt in range(1, self.retries + 2):
            request_context = child_context(context) if context is not None else None
            kwargs["headers"] = self._headers(
                kwargs.get("headers"), request_context, key
            )
            await self._record(
                self._event(
                    request_context,
                    LifecycleState.REQUESTED,
                    method=method,
                    target=target,
                    attempt=attempt,
                    idempotency_key=key,
                    input=payload,
                ),
                critical=mutating,
            )
            started = time.monotonic()
            try:
                if send_method and hasattr(self._client, send_method):
                    response = await getattr(self._client, send_method)(url, **kwargs)
                else:
                    response = await self._client.request(method, url, **kwargs)
            except asyncio.CancelledError:
                await self._record(
                    self._event(
                        request_context,
                        LifecycleState.CANCELLED,
                        method=method,
                        target=target,
                        attempt=attempt,
                        idempotency_key=key,
                        input=payload,
                        duration_ms=(time.monotonic() - started) * 1000,
                    ),
                    critical=mutating,
                )
                await self._record(
                    self._event(
                        context,
                        LifecycleState.CANCELLED,
                        method=method,
                        target=target,
                        attempt=1,
                        idempotency_key=key,
                        input=payload,
                        duration_ms=(time.monotonic() - operation_started) * 1000,
                    ),
                    critical=mutating,
                )
                raise
            except httpx.TimeoutException as exc:
                state = LifecycleState.TIMED_OUT
                await self._record(
                    self._event(
                        request_context,
                        state,
                        method=method,
                        target=target,
                        attempt=attempt,
                        idempotency_key=key,
                        input=payload,
                        duration_ms=(time.monotonic() - started) * 1000,
                        error=exc,
                    ),
                    critical=mutating,
                )
                if attempt <= self.retries and self._retryable_before_send(exc):
                    continue
                if mutating and not self._retryable_before_send(exc):
                    await self._record(
                        self._event(
                            context,
                            LifecycleState.INDETERMINATE,
                            method=method,
                            target=target,
                            attempt=1,
                            idempotency_key=key,
                            input=payload,
                            duration_ms=(time.monotonic() - operation_started) * 1000,
                            error=exc,
                        ),
                        critical=True,
                    )
                    raise AmbiguousHTTPReplayError(
                        "mutating request timed out after send; refusing replay",
                        request=getattr(exc, "request", None),
                    ) from exc
                await self._record(
                    self._event(
                        context,
                        state,
                        method=method,
                        target=target,
                        attempt=1,
                        idempotency_key=key,
                        input=payload,
                        duration_ms=(time.monotonic() - operation_started) * 1000,
                        error=exc,
                    ),
                    critical=mutating,
                )
                raise
            except httpx.HTTPError as exc:
                await self._record(
                    self._event(
                        request_context,
                        LifecycleState.FAILED,
                        method=method,
                        target=target,
                        attempt=attempt,
                        idempotency_key=key,
                        input=payload,
                        duration_ms=(time.monotonic() - started) * 1000,
                        error=exc,
                    ),
                    critical=mutating,
                )
                if attempt <= self.retries and self._retryable_before_send(exc):
                    continue
                if mutating and not self._retryable_before_send(exc):
                    await self._record(
                        self._event(
                            context,
                            LifecycleState.INDETERMINATE,
                            method=method,
                            target=target,
                            attempt=1,
                            idempotency_key=key,
                            input=payload,
                            duration_ms=(time.monotonic() - operation_started) * 1000,
                            error=exc,
                        ),
                        critical=True,
                    )
                    raise AmbiguousHTTPReplayError(
                        "mutating request failed after send; refusing replay",
                        request=getattr(exc, "request", None),
                    ) from exc
                await self._record(
                    self._event(
                        context,
                        LifecycleState.FAILED,
                        method=method,
                        target=target,
                        attempt=1,
                        idempotency_key=key,
                        input=payload,
                        duration_ms=(time.monotonic() - operation_started) * 1000,
                        error=exc,
                    ),
                    critical=mutating,
                )
                raise
            headers = getattr(response, "headers", {})
            content_type = (
                headers.get("content-type") if isinstance(headers, Mapping) else None
            )
            output = _descriptor(getattr(response, "content", None), content_type)
            status_code = getattr(response, "status_code", 200)
            if not isinstance(status_code, int):
                status_code = 200
            state = (
                LifecycleState.COMPLETED if status_code < 400 else LifecycleState.FAILED
            )
            await self._record(
                self._event(
                    request_context,
                    state,
                    method=method,
                    target=target,
                    attempt=attempt,
                    idempotency_key=key,
                    input=payload,
                    output=output,
                    duration_ms=(time.monotonic() - started) * 1000,
                    response=response,
                ),
                critical=mutating,
            )
            if (
                status_code in self.retry_statuses
                and attempt <= self.retries
                and can_replay
            ):
                await self._record(
                    self._event(
                        context,
                        LifecycleState.RETRY_SCHEDULED,
                        method=method,
                        target=target,
                        attempt=attempt,
                        idempotency_key=key,
                        input=payload,
                        retry_kind=RetryKind.TRANSPORT_REPLAY,
                    ),
                    critical=mutating,
                )
                continue
            await self._record(
                self._event(
                    context,
                    state,
                    method=method,
                    target=target,
                    attempt=1,
                    idempotency_key=key,
                    input=payload,
                    output=output,
                    duration_ms=(time.monotonic() - operation_started) * 1000,
                    response=response,
                ),
                critical=mutating,
            )
            return response
        raise AssertionError("unreachable")

    async def get(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", url, _audit_send_method="get", **kwargs)

    async def post(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", url, _audit_send_method="post", **kwargs)

    async def put(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self.request("PUT", url, _audit_send_method="put", **kwargs)

    async def patch(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self.request("PATCH", url, _audit_send_method="patch", **kwargs)

    async def delete(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self.request("DELETE", url, _audit_send_method="delete", **kwargs)


class AuditedHTTPClient(_AuditedHTTPBase):
    """Synchronous companion used by dispatcher and provider setup paths."""

    def __init__(
        self, *args: Any, client: httpx.Client | None = None, **kwargs: Any
    ) -> None:
        audit = {
            key: kwargs.pop(key)
            for key in (
                "emit",
                "recorder",
                "retries",
                "retry_statuses",
                "supports_idempotency",
            )
            if key in kwargs
        }
        super().__init__(client or httpx.Client(*args, **kwargs), **audit)

    def __enter__(self) -> "AuditedHTTPClient":
        self._client.__enter__()
        return self

    def __exit__(self, *args: Any) -> None:
        self._client.__exit__(*args)

    def close(self) -> None:
        self._client.close()

    def request(
        self, method: str, url: str | httpx.URL, **kwargs: Any
    ) -> httpx.Response:
        # Sync callers have no event loop; use the identical async contract in a worker.
        async def call() -> httpx.Response:
            wrapper = AuditedAsyncHTTPClient(
                client=_AsyncAdapter(self._client),
                emit=self._emit,
                recorder=self._recorder,
                retries=self.retries,
                retry_statuses=self.retry_statuses,
                supports_idempotency=self.supports_idempotency,
            )
            return await wrapper.request(method, url, **kwargs)

        return asyncio.run(call())

    def get(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return self.request("POST", url, **kwargs)

    def put(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return self.request("PUT", url, **kwargs)

    def patch(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return self.request("PATCH", url, **kwargs)

    def delete(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return self.request("DELETE", url, **kwargs)


class _AsyncAdapter:
    """Adapt a sync client for the small shared request implementation."""

    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    async def request(self, *args: Any, **kwargs: Any) -> httpx.Response:
        # The sync facade owns the calling thread; keeping its httpx transport
        # on that thread avoids cross-thread connection-pool deadlocks.
        return self.client.request(*args, **kwargs)
