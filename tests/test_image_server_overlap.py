"""Tests for image_server URL normalization and path overlap detection."""

from __future__ import annotations

import pytest

from providers.resource.jumpstarter_images import _normalize_server_url


class TestNormalizeServerUrl:
    """Verify _normalize_server_url strips path overlap in all variants."""

    def test_server_root_unchanged(self):
        result = _normalize_server_url(
            "https://autosd.sig.centos.org",
            "AutoSD-10",
            "monthly/autosd10-202608010205",
        )
        assert result.rstrip("/") == "https://autosd.sig.centos.org"

    def test_server_root_trailing_slash(self):
        result = _normalize_server_url(
            "https://autosd.sig.centos.org/",
            "AutoSD-10",
            "monthly/autosd10-202608010205",
        )
        assert result.rstrip("/") == "https://autosd.sig.centos.org"

    def test_full_release_path_stripped(self):
        result = _normalize_server_url(
            "https://server/AutoSD-10/monthly/autosd10-202608010205",
            "AutoSD-10",
            "monthly/autosd10-202608010205",
        )
        assert result == "https://server"

    def test_full_release_path_trailing_slash(self):
        result = _normalize_server_url(
            "https://server/AutoSD-10/monthly/autosd10-202608010205/",
            "AutoSD-10",
            "monthly/autosd10-202608010205",
        )
        assert result == "https://server"

    def test_version_only_stripped(self):
        result = _normalize_server_url(
            "https://server/AutoSD-10",
            "AutoSD-10",
            "monthly/autosd10-202608010205",
        )
        assert result == "https://server"

    def test_version_only_trailing_slash(self):
        result = _normalize_server_url(
            "https://server/AutoSD-10/",
            "AutoSD-10",
            "monthly/autosd10-202608010205",
        )
        assert result == "https://server"

    def test_ebbr_suffix_stripped(self):
        result = _normalize_server_url(
            "https://server/AutoSD-10/monthly/autosd10-202608010205/EBBR/",
            "AutoSD-10",
            "monthly/autosd10-202608010205",
        )
        assert result == "https://server"

    def test_info_suffix_stripped(self):
        result = _normalize_server_url(
            "https://server/AutoSD-10/monthly/autosd10-202608010205/info",
            "AutoSD-10",
            "monthly/autosd10-202608010205",
        )
        assert result == "https://server"

    def test_different_server_host(self):
        result = _normalize_server_url(
            "https://download.autosd.sig.centos.org/AutoSD-10/monthly/autosd10-202608010205",
            "AutoSD-10",
            "monthly/autosd10-202608010205",
        )
        assert result == "https://download.autosd.sig.centos.org"

    def test_rhivos_server(self):
        result = _normalize_server_url(
            "https://rhivos.example.com/in-vehicle-os",
            "RHIVOS-2",
            "latest-RHIVOS-2",
        )
        # No version match in path — path preserved as-is
        assert "in-vehicle-os" in result

    def test_rhivos_with_version(self):
        result = _normalize_server_url(
            "https://rhivos.example.com/RHIVOS-2/latest-RHIVOS-2",
            "RHIVOS-2",
            "latest-RHIVOS-2",
        )
        assert result == "https://rhivos.example.com"

    def test_empty_path(self):
        result = _normalize_server_url(
            "https://server",
            "AutoSD-10",
            "nightly",
        )
        assert result == "https://server"

    def test_nightly_release(self):
        result = _normalize_server_url(
            "https://server/AutoSD-10/nightly",
            "AutoSD-10",
            "nightly",
        )
        assert result == "https://server"


class TestManifestUrlConstruction:
    """Integration tests verifying the full manifest URL is correct."""

    @pytest.mark.asyncio
    async def test_doubled_url_prevented(self):
        from unittest.mock import patch

        from providers.resource import jumpstarter_images

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
                base_url="https://server/AutoSD-10/monthly/build123",
                image_version="AutoSD-10",
                release="monthly/build123",
                board_target="s32g_vnp_rdb3",
            )

        manifest_urls = [u for u in captured_urls if "test_images_info" in u]
        assert manifest_urls
        url = manifest_urls[0]
        assert url.count("AutoSD-10") == 1, f"Version doubled: {url}"
        assert url.count("build123") == 1, f"Release doubled: {url}"

    @pytest.mark.asyncio
    async def test_clean_server_root_produces_correct_url(self):
        from unittest.mock import patch

        from providers.resource import jumpstarter_images

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
        assert manifest_urls[0] == (
            "https://autosd.sig.centos.org/AutoSD-10/"
            "monthly/autosd10-202608010205/info/test_images_info.json"
        )
