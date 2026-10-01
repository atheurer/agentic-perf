from __future__ import annotations

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

    async def acquire(self) -> dict:
        body = {
            "session_id": str(self.session_id),
            "instance_name": self.instance_name,
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "process_start_id": process_start_id(),
            "ttl_seconds": self.ttl_seconds,
        }
        async with httpx.AsyncClient(timeout=10.0, headers=self._headers()) as client:
            response = await client.post(
                f"{self.store_url}/api/v1/control/orchestrator-lease/acquire",
                json=body,
            )
        if response.status_code == 409:
            raise RuntimeError(
                f"orchestrator leader lease unavailable: {response.text}"
            )
        response.raise_for_status()
        lease = response.json()
        self.epoch = int(lease["epoch"])
        return lease

    async def renew(self) -> dict:
        if self.epoch is None:
            raise RuntimeError("leader lease has not been acquired")
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
        return response.json()

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
