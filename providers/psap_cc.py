from __future__ import annotations

import json
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class PSAPCCAPIError(Exception):
    def __init__(self, status_code: int, message: str, path: str) -> None:
        self.status_code = status_code
        self.message = message
        self.path = path
        super().__init__(f"PSAP CC API error {status_code} on {path}: {message}")


class PSAPControlCenterClient:
    """Async client for the PSAP Control Center REST API."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        verify_ssl: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self._client = httpx.AsyncClient(
            timeout=30.0,
            verify=verify_ssl,
            auth=httpx.BasicAuth(username, password),
        )

    @classmethod
    async def from_secrets(cls, secrets_provider) -> PSAPControlCenterClient:
        raw = await secrets_provider.get_secret("psap-cc/config.json")
        if not raw:
            raise ValueError("PSAP CC config not found at secrets/psap-cc/config.json")
        config = json.loads(raw)
        required = ["base_url", "username", "password"]
        missing = [k for k in required if k not in config]
        if missing:
            raise ValueError(f"PSAP CC config missing required fields: {missing}")
        return cls(
            base_url=config["base_url"],
            username=config["username"],
            password=config["password"],
            verify_ssl=config.get("verify_ssl", False),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        url = f"{self.base_url}/api/v1{path}"
        r = await self._client.request(method, url, **kwargs)
        if r.status_code >= 400:
            try:
                body = r.json()
                msg = body.get("detail", body.get("message", r.text))
            except Exception:
                msg = r.text
            raise PSAPCCAPIError(r.status_code, msg, path)
        return r

    # ------------------------------------------------------------------
    # Clusters (read-only)
    # ------------------------------------------------------------------

    async def list_clusters(self, active_only: bool = True) -> list[dict[str, Any]]:
        r = await self._request("GET", "/clusters", params={"active_only": active_only})
        data = r.json()
        return data.get("clusters", data if isinstance(data, list) else [])

    async def get_cluster(self, cluster_id: str) -> dict[str, Any]:
        r = await self._request("GET", f"/clusters/{cluster_id}")
        return r.json()

    async def get_cluster_status(self, cluster_id: str) -> dict[str, Any]:
        r = await self._request("GET", f"/clusters/{cluster_id}/status")
        return r.json()

    async def get_cluster_gpu_status(self, cluster_id: str) -> dict[str, Any]:
        r = await self._request("GET", f"/clusters/{cluster_id}/gpu-status")
        return r.json()

    async def get_cluster_topology(self, cluster_id: str) -> dict[str, Any]:
        r = await self._request("GET", f"/clusters/{cluster_id}/topology")
        return r.json()

    # ------------------------------------------------------------------
    # Reservations (read-only)
    # ------------------------------------------------------------------

    async def list_reservations(
        self, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        r = await self._request("GET", "/reservations", params=params)
        data = r.json()
        return data.get("reservations", data if isinstance(data, list) else [])

    async def get_reservation(self, reservation_id: str) -> dict[str, Any]:
        r = await self._request("GET", f"/reservations/{reservation_id}")
        return r.json()

    async def get_current_reservation(self, cluster_id: str) -> dict[str, Any] | None:
        try:
            r = await self._request(
                "GET", f"/reservations/cluster/{cluster_id}/current"
            )
            return r.json()
        except PSAPCCAPIError as e:
            if e.status_code == 404:
                return None
            raise

    # ------------------------------------------------------------------
    # Reservations (mutating)
    # ------------------------------------------------------------------

    async def create_reservation(self, data: dict[str, Any]) -> dict[str, Any]:
        r = await self._request("POST", "/reservations", json=data)
        return r.json()

    async def cancel_reservation(self, reservation_id: str) -> dict[str, Any]:
        r = await self._request("POST", f"/reservations/{reservation_id}/cancel")
        return r.json()
