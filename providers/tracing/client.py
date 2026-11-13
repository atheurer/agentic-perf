"""Authenticated producer client with durable-before-send semantics."""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from paths import TRACE_SPOOL_DIR

from .models import TraceEventV1
from .spool import (
    DEFAULT_TRACE_SPOOL_MAX_BYTES,
    SpoolError,
    TraceSpool,
    drain_abandoned_spools,
)


class TraceDeliveryError(RuntimeError):
    """Central trace persistence did not succeed."""


class _TraceFlushDeadlineExceeded(Exception):
    """Internal control flow used to stop a best-effort flush at its deadline."""


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise _TraceFlushDeadlineExceeded


class TraceClient:
    def __init__(
        self,
        url: str,
        token: str,
        *,
        spool_dir: Path = TRACE_SPOOL_DIR,
        instance_id: str | None = None,
        max_spool_bytes: int = DEFAULT_TRACE_SPOOL_MAX_BYTES,
        client: httpx.Client | None = None,
    ) -> None:
        self.url = url.rstrip("/") + "/api/v1/traces/events"
        self.instance_id = instance_id or socket.gethostname()
        self.spool = TraceSpool(spool_dir, max_bytes=max_spool_bytes)
        self._client = client or httpx.Client(timeout=10.0)
        self._headers = {
            "Authorization": f"Bearer {token}",
            "X-Trace-Instance": self.instance_id,
        }
        self._closed = False
        self._flush_state_lock = threading.Lock()
        self._flush_run_lock = threading.Lock()
        self._flush_thread: threading.Thread | None = None
        self._resources_closed = False
        self._resource_close_lock = threading.Lock()
        # Best effort at startup: an outage must not prevent the caller from
        # recording new durable events, which can be replayed on a later flush.
        try:
            self.flush(deadline=time.monotonic() + 5.0)
        except TraceDeliveryError:
            pass

    def record(self, event: TraceEventV1) -> None:
        if self._closed:
            raise TraceDeliveryError("trace client is closed")
        self.spool.append(event)

    def record_critical(self, event: TraceEventV1) -> TraceEventV1:
        if self._closed:
            raise TraceDeliveryError("trace client is closed")
        try:
            response = self._client.post(
                self.url, headers=self._headers, json=event.model_dump(mode="json")
            )
            response.raise_for_status()
            body = response.json()
            stored = TraceEventV1.model_validate(body["event"])
            if stored.event_id != event.event_id or body.get("status") not in {
                "stored",
                "duplicate",
            }:
                raise TraceDeliveryError("critical trace acknowledgement is invalid")
            return stored
        except (httpx.HTTPError, ValueError, KeyError, SpoolError, OSError) as exc:
            raise TraceDeliveryError(
                "critical trace event was not centrally persisted"
            ) from exc

    def flush(self, *, deadline: float | None = None) -> int:
        """Flush the spool while honoring a hard caller-visible deadline.

        A thread boundary is required because an already-blocked read, fsync,
        mutex wait, or HTTP request cannot be interrupted reliably from Python.
        The worker keeps the durable spool safe if it outlives the deadline;
        subsequent bounded flushes return immediately until that worker exits.
        """
        if deadline is None:
            return self._flush_until_deadline(None)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return 0
        with self._flush_state_lock:
            active = self._flush_thread
            if active is not None and active.is_alive():
                return 0
            outcome: list[tuple[bool, Any]] = []

            def run() -> None:
                try:
                    outcome.append((True, self._flush_until_deadline(deadline)))
                except BaseException as exc:  # re-raised in the caller thread
                    outcome.append((False, exc))

            worker = threading.Thread(
                target=run,
                name="trace-flush",
                daemon=True,
            )
            self._flush_thread = worker
            worker.start()

        worker.join(remaining)
        if worker.is_alive():
            return 0
        with self._flush_state_lock:
            if self._flush_thread is worker:
                self._flush_thread = None
        if not outcome:
            return 0
        succeeded, value = outcome[0]
        if not succeeded:
            raise value
        return int(value)

    def _flush_until_deadline(self, deadline: float | None) -> int:
        with self._flush_run_lock:
            return self._flush_locked(deadline)

    def _flush_locked(self, deadline: float | None) -> int:
        delivered = 0

        def check_cancelled() -> None:
            _check_deadline(deadline)

        try:
            while True:
                batch = self.spool.pending_batch(100, check_cancelled=check_cancelled)
                if not batch:
                    break
                check_cancelled()
                delivered += self._send_batch(batch, deadline=deadline)
            self.spool.compact(check_cancelled=check_cancelled)
            return delivered
        except _TraceFlushDeadlineExceeded:
            return delivered
        except (httpx.HTTPError, ValueError, KeyError, SpoolError, OSError) as exc:
            if deadline is not None and time.monotonic() >= deadline:
                return delivered
            raise TraceDeliveryError("trace spool delivery failed") from exc

    def _send_batch(
        self,
        batch: list[tuple[int, TraceEventV1]],
        *,
        deadline: float | None = None,
    ) -> int:
        _check_deadline(deadline)
        body = {"events": [event.model_dump(mode="json") for _, event in batch]}
        if deadline is None:
            response = self._client.post(
                self.url + "/batch", headers=self._headers, json=body
            )
            response.raise_for_status()
            payload = response.json()
        else:
            remaining = max(0.01, deadline - time.monotonic())
            timeout = httpx.Timeout(remaining)
            with self._client.stream(
                "POST",
                self.url + "/batch",
                headers=self._headers,
                json=body,
                timeout=timeout,
            ) as response:
                watchdog = threading.Timer(remaining, response.close)
                watchdog.daemon = True
                watchdog.start()
                try:
                    response.raise_for_status()
                    content = bytearray()
                    for chunk in response.iter_bytes():
                        _check_deadline(deadline)
                        content.extend(chunk)
                    _check_deadline(deadline)
                finally:
                    watchdog.cancel()
            payload = json.loads(content)
        acks = payload["acknowledgements"]
        if len(acks) != len(batch):
            raise TraceDeliveryError("incomplete trace batch acknowledgement")
        delivered = 0
        for (offset, event), ack in zip(batch, acks, strict=True):
            _check_deadline(deadline)
            if (
                ack.get("event_id") != str(event.event_id)
                or not ack.get("accepted")
                or ack.get("status") not in {"stored", "duplicate"}
            ):
                raise TraceDeliveryError("trace batch event was not acknowledged")
            self.spool.acknowledge(offset)
            delivered += 1
        return delivered

    def close(self, *, flush_timeout: float | None = 5.0) -> None:
        if self._closed:
            return
        self._closed = True
        failure: BaseException | None = None
        try:
            deadline = (
                None
                if flush_timeout is None
                else time.monotonic() + max(0.0, flush_timeout)
            )
            self.flush(deadline=deadline)
        except BaseException as exc:
            failure = exc

        with self._flush_state_lock:
            active_flush = self._flush_thread
        # Resource shutdown can itself wait on a wedged spool mutex. Keep it
        # behind the same caller deadline and detach when that deadline wins;
        # process exit is then independent of the durable spool.
        cleanup = threading.Thread(
            target=self._close_after_flush,
            args=(active_flush,),
            name="trace-flush-cleanup",
            daemon=True,
        )
        cleanup.start()
        if flush_timeout is None:
            cleanup.join()
        elif deadline is not None:
            cleanup.join(max(0.0, deadline - time.monotonic()))
        if failure is not None:
            raise failure

    def _close_after_flush(self, active_flush: threading.Thread | None) -> None:
        if active_flush is not None:
            active_flush.join()
        self._close_resources()

    def _close_resources(self) -> None:
        with self._resource_close_lock:
            if self._resources_closed:
                return
            self._resources_closed = True
            try:
                self._client.close()
            finally:
                self.spool.close()

    def sweep_abandoned(self) -> int:
        """Drain other producer spools using this authenticated transport."""

        def deliver(event: TraceEventV1) -> None:
            self.record_critical(event)

        return drain_abandoned_spools(self.spool.directory, deliver)

    def operation_register(self, operation_key: str, request_hash: str) -> dict:
        """Register immutable operation input through authenticated service API."""
        return self._operation_post(
            "/operations/register",
            {"operation_key": operation_key, "request_hash": request_hash},
        )

    def operation_acquire(
        self, operation_key: str, request_hash: str, ttl_seconds: float
    ) -> dict:
        return self._operation_post(
            "/operations/acquire",
            {
                "operation_key": operation_key,
                "request_hash": request_hash,
                "ttl_seconds": ttl_seconds,
            },
        )

    def operation_transition(
        self,
        operation_key: str,
        action: str,
        fencing_token: int,
        *,
        descriptor: dict | None = None,
        external_ids: dict | None = None,
        ttl_seconds: float | None = None,
        reconciliation_outcome: str | None = None,
    ) -> dict:
        """Submit a server-authorized fenced operation mutation."""
        payload: dict = {"fencing_token": fencing_token}
        if descriptor is not None:
            payload["descriptor"] = descriptor
        if external_ids is not None:
            payload["external_ids"] = external_ids
        if ttl_seconds is not None:
            payload["ttl_seconds"] = ttl_seconds
        if reconciliation_outcome is not None:
            payload["reconciliation_outcome"] = reconciliation_outcome
        return self._operation_post(
            f"/operations/{operation_key}/{action}",
            payload,
        )

    def _operation_post(self, suffix: str, payload: dict) -> dict:
        if self._closed:
            raise TraceDeliveryError("trace client is closed")
        try:
            response = self._client.post(
                self.url.removesuffix("/events") + suffix,
                headers=self._headers,
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict) or "operation" not in body:
                raise ValueError("invalid operation acknowledgement")
            return body
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise TraceDeliveryError("operation request was not accepted") from exc
