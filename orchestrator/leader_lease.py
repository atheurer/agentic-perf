from __future__ import annotations

import asyncio
import os
import socket
import uuid
from pathlib import Path

import httpx

_RETRYABLE_ACQUIRE_STATUSES = frozenset({408, 425, 429})
_ACQUIRE_RETRY_BACKOFF_MAX_SECONDS = 30.0


class LeaderLeaseUnavailable(RuntimeError):
    """The leader lease could not be acquired because the control plane was unable."""


def _acquire_retryable_status(status_code: int) -> bool:
    return status_code in _RETRYABLE_ACQUIRE_STATUSES or 500 <= status_code < 600


def _acquire_retry_delay(attempt: int, retry_interval: float) -> float:
    base = max(0.0, retry_interval)
    cap = max(base, _ACQUIRE_RETRY_BACKOFF_MAX_SECONDS)
    return min(base * (2 ** min(attempt - 1, 3)), cap)


def process_start_id() -> str:
    """Return a PID-reuse-resistant identity for this process."""
    boot = ""
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        stat = Path(f"/proc/{os.getpid()}/stat").read_text()
        start = stat.rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        start = str(os.times().elapsed)
    return f"{boot}:{start}"


class LeaderLeaseClient:
    def __init__(
        self,
        store_url: str,
        *,
        instance_name: str,
        ttl_seconds: float = 30.0,
        session_id: uuid.UUID | None = None,
    ) -> None:
        self.store_url = store_url.rstrip("/")
        self.instance_name = instance_name
        self.ttl_seconds = ttl_seconds
        self.session_id = session_id or uuid.uuid4()
        self.epoch: int | None = None
        self.confirmed_deadline: float | None = None

    async def acquire(
        self,
        *,
        max_attempts: int = 12,
        retry_interval: float = 5.0,
    ) -> dict:
        """Acquire the leader lease, retrying temporary control-plane failures.

        After a pod restart the previous process's lease may still be
        valid (up to *ttl_seconds*).  Retrying avoids a crash-loop
        where Kubernetes restarts the container, the new process
        immediately fails, and the backoff delay compounds.

        Timeouts, throttling responses, and server errors are also retryable:
        the state store can be temporarily overloaded while still recovering.
        Authentication and other terminal client errors remain immediately
        fatal.
        """
        import logging

        logger = logging.getLogger(__name__)
        body = {
            "session_id": str(self.session_id),
            "instance_name": self.instance_name,
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "process_start_id": process_start_id(),
            "ttl_seconds": self.ttl_seconds,
        }
        for attempt in range(1, max_attempts + 1):
            retry_delay = _acquire_retry_delay(attempt, retry_interval)
            try:
                request_started = asyncio.get_running_loop().time()
                async with httpx.AsyncClient(
                    timeout=10.0, headers=self._headers()
                ) as client:
                    response = await client.post(
                        f"{self.store_url}/api/v1/control/orchestrator-lease/acquire",
                        json=body,
                    )
            except httpx.RequestError as exc:
                # State store not ready yet or transient network issue.
                if attempt < max_attempts:
                    logger.warning(
                        "Leader lease acquire failed (%s), retrying in %.1fs (%d/%d)",
                        exc,
                        retry_delay,
                        attempt,
                        max_attempts,
                    )
                    await asyncio.sleep(retry_delay)
                    continue
                raise LeaderLeaseUnavailable(
                    f"orchestrator leader lease unavailable after "
                    f"{max_attempts} attempts: {exc}"
                ) from exc
            if response.status_code == 409:
                if attempt < max_attempts:
                    logger.warning(
                        "Leader lease held by another session, retrying in %.1fs "
                        "(%d/%d)",
                        retry_delay,
                        attempt,
                        max_attempts,
                    )
                    await asyncio.sleep(retry_delay)
                    continue
                raise LeaderLeaseUnavailable(
                    f"orchestrator leader lease unavailable after "
                    f"{max_attempts} attempts (held by another session)"
                )
            if _acquire_retryable_status(response.status_code):
                if attempt < max_attempts:
                    logger.warning(
                        "Leader lease acquire temporarily unavailable "
                        "(HTTP %d), retrying in %.1fs (%d/%d)",
                        response.status_code,
                        retry_delay,
                        attempt,
                        max_attempts,
                    )
                    await asyncio.sleep(retry_delay)
                    continue
                raise LeaderLeaseUnavailable(
                    f"orchestrator leader lease unavailable after "
                    f"{max_attempts} attempts (HTTP {response.status_code})"
                )
            response.raise_for_status()
            lease = response.json()
            self.epoch = int(lease["epoch"])
            self.confirmed_deadline = request_started + self.ttl_seconds
            if attempt > 1:
                logger.info("Leader lease acquired after %d attempt(s)", attempt)
            return lease
        # unreachable but keeps type checkers happy
        raise LeaderLeaseUnavailable(
            "leader lease acquisition loop exited unexpectedly"
        )

    async def renew(self) -> dict:
        if self.epoch is None:
            raise RuntimeError("leader lease has not been acquired")
        request_started = asyncio.get_running_loop().time()
        async with httpx.AsyncClient(timeout=10.0, headers=self._headers()) as client:
            response = await client.post(
                f"{self.store_url}/api/v1/control/orchestrator-lease/renew",
                json={
                    "session_id": str(self.session_id),
                    "epoch": self.epoch,
                    "ttl_seconds": self.ttl_seconds,
                },
            )
        response.raise_for_status()
        lease = response.json()
        self.confirmed_deadline = request_started + self.ttl_seconds
        return lease

    async def release(self) -> bool:
        if self.epoch is None:
            return False
        try:
            async with httpx.AsyncClient(
                timeout=5.0, headers=self._headers()
            ) as client:
                response = await client.post(
                    f"{self.store_url}/api/v1/control/orchestrator-lease/release",
                    json={"session_id": str(self.session_id), "epoch": self.epoch},
                )
            return response.status_code == 200 and bool(response.json().get("released"))
        except Exception:
            return False

    @staticmethod
    def _headers() -> dict[str, str]:
        token = os.environ.get("AGENTIC_PERF_API_TOKEN", "")
        return {"Authorization": f"Bearer {token}"} if token else {}
