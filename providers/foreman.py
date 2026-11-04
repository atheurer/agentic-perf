from __future__ import annotations

import logging
import ssl
from pathlib import Path
from typing import Any

from providers.execution import AuditedAsyncHTTPClient

logger = logging.getLogger(__name__)

# Lab Foreman instances hold tens of OS/media/ptable records, not thousands;
# one page at this size avoids pagination without unbounded responses.
FOREMAN_PER_PAGE = 200


class ForemanAPIError(Exception):
    def __init__(self, status_code: int, message: str, path: str) -> None:
        self.status_code = status_code
        self.message = message
        self.path = path
        super().__init__(f"Foreman API error {status_code} on {path}: {message}")


class ForemanClient:
    """Async client for the Foreman REST API, scoped to one QUADS cloud.

    QUADS labs create a Foreman account per active assignment, named after
    the cloud (e.g. ``cloud05``), that owns exactly the hosts scheduled to
    that cloud. Authenticating with that account limits every call here to
    the caller's own reservation — Foreman rejects writes to other hosts.
    """

    def __init__(
        self,
        api_url: str,
        username: str,
        password: str,
        ca_bundle: str | None = None,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._username = username
        self._password = password
        client_kwargs: dict[str, Any] = {"timeout": 30.0}
        if self._api_url.startswith("https://"):
            ca_file = str(Path(ca_bundle).expanduser()) if ca_bundle else None
            client_kwargs["verify"] = ssl.create_default_context(cafile=ca_file)
        else:
            logger.warning(
                "Foreman API using plaintext HTTP — credentials are not "
                "encrypted in transit"
            )
        self._client = AuditedAsyncHTTPClient(**client_kwargs)

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        r = await self._client.request(
            method,
            f"{self._api_url}{path}",
            auth=(self._username, self._password),
            headers={"Content-Type": "application/json"},
            **kwargs,
        )
        if r.status_code >= 400:
            try:
                error = r.json().get("error", {})
                if isinstance(error, dict):
                    msg = str(error.get("message") or error)
                else:
                    msg = str(error)
            except Exception:
                msg = r.text
            raise ForemanAPIError(r.status_code, msg, path)
        return r.json()

    @staticmethod
    def _results(data: Any) -> list[dict[str, Any]]:
        results = data.get("results", []) if isinstance(data, dict) else []
        return [entry for entry in results if isinstance(entry, dict)]

    async def get_operatingsystems(self) -> list[dict[str, Any]]:
        data = await self._request(
            "GET", "/operatingsystems", params={"per_page": FOREMAN_PER_PAGE}
        )
        return self._results(data)

    async def find_operatingsystem(self, title: str) -> dict[str, Any] | None:
        """Return the OS whose Foreman title matches, ignoring case/spacing."""
        wanted = " ".join(title.split()).casefold()
        for entry in await self.get_operatingsystems():
            entry_title = entry.get("title") or entry.get("description") or ""
            if " ".join(str(entry_title).split()).casefold() == wanted:
                return entry
        return None

    async def get_media(self, os_id: int) -> list[dict[str, Any]]:
        data = await self._request(
            "GET",
            f"/operatingsystems/{os_id}/media",
            params={"per_page": FOREMAN_PER_PAGE},
        )
        return self._results(data)

    async def get_ptables(self, os_id: int) -> list[dict[str, Any]]:
        data = await self._request(
            "GET",
            f"/operatingsystems/{os_id}/ptables",
            params={"per_page": FOREMAN_PER_PAGE},
        )
        return self._results(data)

    async def get_host(self, fqdn: str) -> dict[str, Any]:
        data = await self._request("GET", f"/hosts/{fqdn}")
        if not isinstance(data, dict):
            raise ForemanAPIError(502, "Host response was not an object", fqdn)
        return data

    async def get_build_flag(self, fqdn: str) -> bool:
        """Whether the host is still marked for rebuild.

        Foreman clears the flag when the installer's postinstall phase calls
        back, so a cleared flag means the OS install finished.
        """
        return bool((await self.get_host(fqdn)).get("build", False))

    async def set_host_provisioning(
        self, fqdn: str, os_id: int, medium_id: int, ptable_id: int
    ) -> None:
        """Point the host at an OS and mark it for rebuild on next PXE boot."""
        await self._request(
            "PUT",
            f"/hosts/{fqdn}",
            json={
                "host": {
                    "operatingsystem_id": os_id,
                    "medium_id": medium_id,
                    "ptable_id": ptable_id,
                    "build": True,
                }
            },
        )
