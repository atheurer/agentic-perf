"""Foreman REST client with explicit, fail-closed discovery operations."""

from __future__ import annotations

import ssl
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from providers.execution import AuditedAsyncHTTPClient


class ForemanError(Exception):
    """A sanitized Foreman failure suitable for reporting to callers."""

    def __init__(self, category: str, message: str) -> None:
        self.category = category
        super().__init__(message)


class ForemanClient:
    """Provider-independent Foreman client; credentials never enter results."""

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
            raise ForemanError("configuration", "Foreman endpoint must be an HTTPS URL")
        if not username or not password:
            raise ForemanError("configuration", "Foreman credentials are missing")
        self._endpoint = endpoint.rstrip("/")
        self._auth = (username, password)
        self._client = client or AuditedAsyncHTTPClient(
            timeout=30.0, verify=ssl.create_default_context(cafile=ca_bundle)
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, **params: Any) -> dict[str, Any]:
        try:
            response = await self._client.get(
                f"{self._endpoint}/api/v2/{path}", auth=self._auth, params=params
            )
        except httpx.HTTPError:
            raise ForemanError("transport", "Foreman request failed") from None
        if response.status_code in (401, 403):
            raise ForemanError(
                "authentication", "Foreman authentication or authorization failed"
            )
        if not 200 <= response.status_code < 300:
            raise ForemanError("api", f"Foreman returned HTTP {response.status_code}")
        try:
            value = response.json()
        except ValueError:
            raise ForemanError("api", "Foreman returned invalid JSON") from None
        if not isinstance(value, dict):
            raise ForemanError("api", "Foreman returned an invalid response")
        return value

    async def _list(self, path: str, **params: Any) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        page = 1
        while True:
            data = await self._get(path, page=page, per_page=100, **params)
            items = data.get("results")
            total = data.get("subtotal", data.get("total"))
            if not isinstance(items, list) or any(
                not isinstance(i, dict) for i in items
            ):
                raise ForemanError("api", "Foreman returned an invalid catalog")
            if type(total) is not int or total < 0:
                raise ForemanError("api", "Foreman catalog has no valid total")
            result.extend(items)
            ids = [item.get("id") for item in result]
            if any(type(value) is not int or value <= 0 for value in ids) or len(
                ids
            ) != len(set(ids)):
                raise ForemanError(
                    "api", "Foreman catalog has invalid or duplicate identities"
                )
            if len(result) >= total:
                if len(result) != total:
                    raise ForemanError(
                        "api", "Foreman catalog changed during pagination"
                    )
                return result
            if not items or page >= 1000:
                raise ForemanError("api", "Foreman catalog pagination is incomplete")
            page += 1

    async def find_host(self, name: str) -> dict[str, Any]:
        """Resolve a full stable name, never a partial search match."""
        if not name or any(c in name for c in '"\\\n\r'):
            raise ForemanError("host", "Invalid Foreman host identity")
        candidates = await self._list("hosts", search=f'name = "{name}"')
        matches = [item for item in candidates if item.get("name") == name]
        if len(matches) != 1 or type(matches[0].get("id")) is not int:
            raise ForemanError("host", "Foreman host was not found or is ambiguous")
        host = await self._get(f"hosts/{matches[0]['id']}")
        if host.get("name") != name or host.get("id") != matches[0]["id"]:
            raise ForemanError("host", "Foreman host identity changed during lookup")
        return host

    async def list_operating_systems(self) -> list[dict[str, Any]]:
        return await self._list("operatingsystems")

    async def get_operating_system(self, os_id: int) -> dict[str, Any]:
        if type(os_id) is not int or os_id <= 0:
            raise ForemanError("configuration", "Invalid Foreman operating system ID")
        return await self._get(f"operatingsystems/{quote(str(os_id), safe='')}")

    async def resolve_install_options(
        self,
        host: dict[str, Any],
        os_name: str,
        *,
        medium_id: int | None = None,
        ptable_id: int | None = None,
    ) -> dict[str, int]:
        """Revalidate the exact OS and select only unambiguous options."""
        catalog = await self.list_operating_systems()
        matches = [entry for entry in catalog if entry.get("title") == os_name]
        if len(matches) != 1 or type(matches[0].get("id")) is not int:
            raise ForemanError("os", "Requested OS is missing or ambiguous in Foreman")
        os_id = matches[0]["id"]
        details = await self.get_operating_system(os_id)
        arch = host.get("architecture_id")
        architectures = details.get("architectures")
        if (
            type(arch) is not int
            or not isinstance(architectures, list)
            or not any(
                isinstance(x, dict) and x.get("id") == arch for x in architectures
            )
        ):
            raise ForemanError("os", "Host architecture is not compatible with the OS")
        selected: dict[str, int] = {"operatingsystem_id": os_id}
        for key, field, chosen in (
            ("media", "medium_id", medium_id),
            ("ptables", "ptable_id", ptable_id),
        ):
            options = details.get(key)
            if not isinstance(options, list) or not options:
                raise ForemanError("os", f"Foreman has no {key} for this OS")
            valid = [
                x
                for x in options
                if isinstance(x, dict) and type(x.get("id")) is int and x["id"] > 0
            ]
            if len(valid) != len(options):
                raise ForemanError("os", f"Foreman returned invalid {key} options")
            ids = [x["id"] for x in valid]
            if len(ids) != len(set(ids)):
                raise ForemanError("os", f"Foreman returned duplicate {key} options")
            if chosen is None and len(ids) == 1:
                chosen = ids[0]
            elif chosen is None:
                choices = ", ".join(str(value) for value in ids[:50])
                raise ForemanError(
                    "os", f"Foreman {key} is ambiguous; available IDs: {choices}"
                )
            if type(chosen) is not int or chosen not in ids:
                raise ForemanError(
                    "os", f"Requested {key} is not compatible with the OS"
                )
            selected[field] = chosen
        return selected

    async def set_build(self, host_id: int, options: dict[str, int]) -> None:
        """Initiate installation once the caller has durably recorded intent.

        A transport failure may have occurred after the server accepted the
        request; callers must reconcile state rather than retry automatically.
        """
        if (
            type(host_id) is not int
            or host_id <= 0
            or set(options) != {"operatingsystem_id", "medium_id", "ptable_id"}
            or any(type(v) is not int or v <= 0 for v in options.values())
        ):
            raise ForemanError("build", "Invalid Foreman build options")
        try:
            response = await self._client.put(
                f"{self._endpoint}/api/v2/hosts/{host_id}",
                auth=self._auth,
                json={"host": {**options, "build": True}},
            )
        except httpx.HTTPError:
            raise ForemanError(
                "build_unknown", "Foreman build outcome is unknown"
            ) from None
        if response.status_code in (401, 403):
            raise ForemanError(
                "authentication", "Foreman authentication or authorization failed"
            )
        if not 200 <= response.status_code < 300:
            raise ForemanError(
                "build_unknown",
                f"Foreman build returned HTTP {response.status_code}; outcome unknown",
            )
