from __future__ import annotations

import pytest

from providers.resource import jumpstarter_images, jumpstarter_lifecycle


class _Response:
    status_code = 200

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class _HTTPClient:
    def __init__(self, ticket, patches):
        self._ticket = ticket
        self._patches = patches

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def get(self, _url):
        return _Response({"custom_fields": self._ticket["custom_fields"]})

    async def patch(self, _url, *, json):
        self._patches.append(json)


def _install_http_client(monkeypatch, ticket):
    patches = []
    monkeypatch.setattr(
        jumpstarter_lifecycle,
        "AuditedAsyncHTTPClient",
        lambda **_kwargs: _HTTPClient(ticket, patches),
    )
    return patches


@pytest.mark.asyncio
async def test_invalid_json_image_directives_fall_back_without_aborting(
    monkeypatch,
):
    ticket = {
        "custom_fields": {
            "resource_provider": "jumpstarter",
            "directives": {
                "image_version": None,
                "image_name": 17,
                "image_type": [],
                "release": {"unexpected": "object"},
            },
        }
    }
    patches = _install_http_client(monkeypatch, ticket)

    await jumpstarter_lifecycle.resolve_images("https://store", "PERF-TEST")

    assert len(patches) == 1
    flash = patches[0]["fields"]["jumpstarter_flash"]
    assert "No OS image version" in flash["error"]


@pytest.mark.asyncio
async def test_image_version_suffix_and_string_fields_are_normalized(monkeypatch):
    ticket = {
        "custom_fields": {
            "resource_provider": "jumpstarter",
            "directives": {
                "image_version": "AutoSD-10-NIGHTLY",
                "image_name": " developer-vm ",
                "image_type": "OSTREE",
            },
        }
    }
    patches = _install_http_client(monkeypatch, ticket)
    resolved = {}

    async def resolve_image_urls(**kwargs):
        resolved.update(kwargs)
        return {"flash_targets": []}

    monkeypatch.setattr(jumpstarter_images, "resolve_image_urls", resolve_image_urls)

    await jumpstarter_lifecycle.resolve_images(
        "https://store",
        "PERF-TEST",
        image_config={"server": "https://images.example/"},
    )

    assert resolved["image_version"] == "AutoSD-10"
    assert resolved["release"] == "nightly"
    assert resolved["image_name"] == "developer-vm"
    assert resolved["image_type"] == "ostree"
    assert len(patches) == 1
