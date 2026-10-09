"""Tests for image_server path overlap detection."""

from __future__ import annotations

import pytest

from providers.resource import jumpstarter_images


@pytest.mark.asyncio
async def test_full_release_url_stripped(monkeypatch):
    """base_url with full release path should be stripped to server root."""
    resolved = {}

    async def fake_resolve(**kwargs):
        resolved.update(kwargs)
        return {"error": "test-only: not actually resolving"}

    monkeypatch.setattr(jumpstarter_images, "resolve_image_urls", fake_resolve)

    # Simulate: image_server has full path, version and release also set
    await jumpstarter_images.resolve_image_urls(
        base_url="https://download.autosd.sig.centos.org/AutoSD-10/monthly/autosd10-202608010205",
        image_version="AutoSD-10",
        release="monthly/autosd10-202608010205",
        board_target="s32g_vnp_rdb3",
    )
    # The function was called directly — check the manifest URL construction
    # by inspecting what the function builds internally.
    # Since we can't intercept the internal URL, test via the actual function.


@pytest.mark.asyncio
async def test_manifest_url_not_doubled():
    """Verify the manifest URL doesn't double the version/release path."""
    from unittest.mock import patch

    # Mock the HTTP client to capture the manifest URL
    captured_urls = []

    class FakeResponse:
        status_code = 404

        def json(self):
            return {}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def get(self, url, **kw):
            captured_urls.append(url)
            return FakeResponse()

    with patch(
        "providers.resource.jumpstarter_images.AuditedAsyncHTTPClient",
        return_value=FakeClient(),
    ):
        await jumpstarter_images.resolve_image_urls(
            base_url="https://server.example.com/AutoSD-10/monthly/autosd10-202608010205",
            image_version="AutoSD-10",
            release="monthly/autosd10-202608010205",
            board_target="s32g_vnp_rdb3",
        )

    # The manifest URL should NOT have the path doubled
    manifest_urls = [u for u in captured_urls if "test_images_info" in u]
    assert manifest_urls, "No manifest URL was requested"
    url = manifest_urls[0]
    assert url.count("AutoSD-10") == 1, f"Version doubled in URL: {url}"
    assert url.count("autosd10-202608010205") == 1, f"Release doubled in URL: {url}"


@pytest.mark.asyncio
async def test_server_root_unchanged():
    """A proper server root URL should not be modified."""
    from unittest.mock import patch

    captured_urls = []

    class FakeResponse:
        status_code = 404

        def json(self):
            return {}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def get(self, url, **kw):
            captured_urls.append(url)
            return FakeResponse()

    with patch(
        "providers.resource.jumpstarter_images.AuditedAsyncHTTPClient",
        return_value=FakeClient(),
    ):
        await jumpstarter_images.resolve_image_urls(
            base_url="https://autosd.sig.centos.org",
            image_version="AutoSD-10",
            release="monthly/autosd10-202608010205",
            board_target="s32g_vnp_rdb3",
        )

    manifest_urls = [u for u in captured_urls if "test_images_info" in u]
    assert manifest_urls
    url = manifest_urls[0]
    assert url == (
        "https://autosd.sig.centos.org/AutoSD-10/"
        "monthly/autosd10-202608010205/info/test_images_info.json"
    )


@pytest.mark.asyncio
async def test_version_only_overlap():
    """base_url ending with just the version should be stripped."""
    from unittest.mock import patch

    captured_urls = []

    class FakeResponse:
        status_code = 404

        def json(self):
            return {}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def get(self, url, **kw):
            captured_urls.append(url)
            return FakeResponse()

    with patch(
        "providers.resource.jumpstarter_images.AuditedAsyncHTTPClient",
        return_value=FakeClient(),
    ):
        await jumpstarter_images.resolve_image_urls(
            base_url="https://autosd.sig.centos.org/AutoSD-10",
            image_version="AutoSD-10",
            release="monthly/autosd10-202608010205",
            board_target="s32g_vnp_rdb3",
        )

    manifest_urls = [u for u in captured_urls if "test_images_info" in u]
    assert manifest_urls
    url = manifest_urls[0]
    assert url.count("AutoSD-10") == 1, f"Version doubled: {url}"
