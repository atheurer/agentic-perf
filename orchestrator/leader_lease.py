from __future__ import annotations

import asyncio
import os
import socket
import uuid
from pathlib import Path

import httpx


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
        """Acquire the leader lease, retrying on 409 (held by another session).

        After a pod restart the previous process's lease may still be
        valid (up to *ttl_seconds*).  Retrying avoids a crash-loop
        where Kubernetes restarts the container, the new process
        immediately fails, and the backoff delay compounds.
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
                        "Leader lease acquire failed (%s), retrying in %.0fs (%d/%d)",
                        exc,
                        retry_interval,
                        attempt,
                        max_attempts,
                    )
                    await asyncio.sleep(retry_interval)
                    continue
                raise RuntimeError(
                    f"orchestrator leader lease unavailable after "
                    f"{max_attempts} attempts: {exc}"
                ) from exc
            if response.status_code == 409 or response.status_code >= 500:
                reason = (
                    "held by another session"
                    if response.status_code == 409
                    else f"server error {response.status_code}"
                )
                if attempt < max_attempts:
                    logger.warning(
                        "Leader lease %s, retrying in %.0fs (%d/%d)",
                        reason,
                        retry_interval,
                        attempt,
                        max_attempts,
                    )
                    await asyncio.sleep(retry_interval)
                    continue
                raise RuntimeError(
                    f"orchestrator leader lease unavailable after "
                    f"{max_attempts} attempts ({reason}): {response.text}"
                )
            response.raise_for_status()
            lease = response.json()
            self.epoch = int(lease["epoch"])
            self.confirmed_deadline = request_started + self.ttl_seconds
            if attempt > 1:
                logger.info("Leader lease acquired after %d attempt(s)", attempt)
            return lease
        # unreachable but keeps type checkers happy
        raise RuntimeError("leader lease acquisition loop exited unexpectedly")

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
