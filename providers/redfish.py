"""Redfish power/boot client with fail-closed validation, mirroring Foreman style."""

from __future__ import annotations

import asyncio
import ssl
from urllib.parse import urlsplit

import httpx

from providers.execution import AuditedAsyncHTTPClient


class RedfishError(Exception):
    def __init__(self, category: str, message: str) -> None:
        self.category = category
        super().__init__(message)


class RedfishClient:
    def __init__(
        self,
        endpoint: str,
        username: str,
        password: str,
        *,
        ca_bundle: str | None = None,
        client: AuditedAsyncHTTPClient | None = None,
    ) -> None:
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise RedfishError(
                "configuration",
                "Redfish endpoint must be an HTTPS URL without embedded credentials",
            )
        if not username or not password:
            raise RedfishError("configuration", "Redfish credentials are missing")
        self._endpoint = endpoint.rstrip("/")
        self._auth = (username, password)
        self._client = client or AuditedAsyncHTTPClient(
            timeout=15.0, verify=ssl.create_default_context(cafile=ca_bundle)
        )
        # Dell iDRAC uses System.Embedded.1; verified live on r7725 (mgmt-* @ 10.6.61.186)
        self._system_path = "/redfish/v1/Systems/System.Embedded.1"

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs):
        try:
            resp = await self._client.request(
                method, f"{self._endpoint}{path}", auth=self._auth, **kwargs
            )
        except httpx.HTTPError:
            raise RedfishError("transport", "Redfish request failed") from None
        if resp.status_code in (401, 403):
            raise RedfishError("authentication", "Redfish authentication failed")
        return resp

    async def get_power_state(self) -> str:
        resp = await self._request("GET", self._system_path)
        if resp.status_code == 404:
            raise RedfishError("api", "Redfish ComputerSystem not found")
        if not 200 <= resp.status_code < 300:
            raise RedfishError("api", f"Redfish returned HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError:
            raise RedfishError("api", "Redfish returned invalid JSON") from None
        state = data.get("PowerState")
        if state not in ("On", "Off"):
            raise RedfishError("api", "Redfish power state missing or invalid")
        return state

    async def set_one_shot_pxe(self) -> None:
        resp = await self._request(
            "PATCH",
            self._system_path,
            json={
                "Boot": {
                    "BootSourceOverrideTarget": "Pxe",
                    "BootSourceOverrideEnabled": "Once",
                }
            },
        )
        if not 200 <= resp.status_code < 300:
            raise RedfishError(
                "api", f"Redfish set boot returned HTTP {resp.status_code}"
            )
        # Read back
        resp2 = await self._request("GET", self._system_path)
        try:
            data = resp2.json()
        except ValueError:
            raise RedfishError("api", "Redfish returned invalid JSON") from None
        boot = data.get("Boot") or {}
        if (
            boot.get("BootSourceOverrideTarget") != "Pxe"
            or boot.get("BootSourceOverrideEnabled") != "Once"
        ):
            raise RedfishError("api", "Redfish one-shot PXE override not verified")

    async def reset(self, reset_type: str) -> None:
        if reset_type not in ("ForceOn", "ForceRestart", "GracefulRestart", "On"):
            raise RedfishError("configuration", "Invalid ResetType")
        resp = await self._request(
            "POST",
            f"{self._system_path}/Actions/ComputerSystem.Reset",
            json={"ResetType": reset_type},
        )
        if not 200 <= resp.status_code < 300:
            raise RedfishError(
                "power", f"Redfish reset returned HTTP {resp.status_code}"
            )

    async def poll_until_ssh_reachable(
        self, check_ssh, timeout: float = 300, interval: float = 10
    ) -> None:
        elapsed = 0.0
        while elapsed < timeout:
            if await check_ssh():
                return
            await asyncio.sleep(interval)
            elapsed += interval
        raise RedfishError("power", "Timed out waiting for OS SSH to become reachable")
