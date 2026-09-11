"""Authenticated producer client with durable-before-send semantics."""

from __future__ import annotations

import socket
from pathlib import Path

import httpx

from paths import TRACE_SPOOL_DIR

from .models import TraceEventV1
from .spool import SpoolError, TraceSpool, drain_abandoned_spools


class TraceDeliveryError(RuntimeError):
    """Central trace persistence did not succeed."""


class TraceClient:
    def __init__(
        self,
        url: str,
        token: str,
        *,
        spool_dir: Path = TRACE_SPOOL_DIR,
        instance_id: str | None = None,
        max_spool_bytes: int = 64 * 1024 * 1024,
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
        # Best effort at startup: an outage must not prevent the caller from
        # recording new durable events, which can be replayed on a later flush.
        try:
            self.flush()
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

    def flush(self) -> int:
        delivered = 0
        try:
            batch: list[tuple[int, TraceEventV1]] = []
            for item in self.spool.pending():
                batch.append(item)
                if len(batch) == 100:
                    delivered += self._send_batch(batch)
                    batch = []
            if batch:
                delivered += self._send_batch(batch)
            self.spool.compact()
            return delivered
        except (httpx.HTTPError, ValueError, KeyError, SpoolError, OSError) as exc:
            raise TraceDeliveryError("trace spool delivery failed") from exc

    def _send_batch(self, batch: list[tuple[int, TraceEventV1]]) -> int:
        response = self._client.post(
            self.url + "/batch",
            headers=self._headers,
            json={"events": [event.model_dump(mode="json") for _, event in batch]},
        )
        response.raise_for_status()
        acks = response.json()["acknowledgements"]
        if len(acks) != len(batch):
            raise TraceDeliveryError("incomplete trace batch acknowledgement")
        delivered = 0
        for (offset, event), ack in zip(batch, acks, strict=True):
            if (
                ack.get("event_id") != str(event.event_id)
                or not ack.get("accepted")
                or ack.get("status") not in {"stored", "duplicate"}
            ):
                raise TraceDeliveryError("trace batch event was not acknowledged")
            self.spool.acknowledge(offset)
            delivered += 1
        return delivered

    def close(self) -> None:
        if not self._closed:
            try:
                self.flush()
            finally:
                self._client.close()
                self.spool.close()
                self._closed = True

    def sweep_abandoned(self) -> int:
        """Drain other producer spools using this authenticated transport."""

        def deliver(event: TraceEventV1) -> None:
            self.record_critical(event)

        return drain_abandoned_spools(self.spool.directory, deliver)
