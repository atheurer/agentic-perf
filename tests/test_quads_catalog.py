from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.resource import server
from providers.resource.quads import QuadsOSResolutionError, QuadsResourceProvider


async def test_catalog_reports_exact_releases_and_source():
    client = MagicMock()
    client.api_host = "quads.example"
    client.get_os_list = AsyncMock(
        return_value=[
            {"Title": "RHEL 8.2"},
            {"Title": "RHEL 8.9"},
            {"Title": "RHEL 8.2"},
        ]
    )
    catalog = await QuadsResourceProvider(client).discover_os_catalog()
    assert catalog["choices"] == ["RHEL 8.2", "RHEL 8.9"]
    assert catalog["source"] == "quads"
    assert catalog["site"] == "quads.example"
    assert catalog["discovered_at"]


async def test_empty_and_invalid_catalog_are_distinct():
    client = MagicMock()
    client.api_host = "quads.example"
    client.get_os_list = AsyncMock(return_value=[])
    provider = QuadsResourceProvider(client)
    assert (await provider.discover_os_catalog())["choices"] == []
    client.get_os_list.return_value = [{"unexpected": "entry"}]
    with pytest.raises(QuadsOSResolutionError, match="invalid"):
        await provider.discover_os_catalog()
    client.get_os_list.side_effect = RuntimeError("credential is secret")
    with pytest.raises(QuadsOSResolutionError) as error:
        await provider.discover_os_catalog()
    assert "secret" not in str(error.value)


async def test_catalog_tool_reports_failure_as_unknown(monkeypatch):
    monkeypatch.setattr(server, "_ensure_init", AsyncMock())
    provider = MagicMock()
    provider.discover_os_catalog = AsyncMock(side_effect=RuntimeError("secret"))
    registry = MagicMock()
    registry.get_provider = AsyncMock(return_value=provider)
    monkeypatch.setattr(server, "_registry", registry)
    result = json.loads(await server.discover_quads_os_catalog())
    assert result["status"] == "unknown"
    assert "secret" not in json.dumps(result)
    provider.discover_os_catalog = AsyncMock(
        return_value={
            "source": "quads",
            "site": "lab",
            "discovered_at": "now",
            "choices": [],
        }
    )
    result = json.loads(await server.discover_quads_os_catalog())
    assert result["status"] == "ok"
    assert result["choices"] == []


async def test_catalog_site_does_not_expose_url_userinfo():
    client = MagicMock()
    client.api_host = "user:secret@quads.example"
    client.get_os_list = AsyncMock(return_value=[])
    catalog = await QuadsResourceProvider(client).discover_os_catalog()
    assert catalog["site"] == "quads.example"
    assert "secret" not in json.dumps(catalog)
