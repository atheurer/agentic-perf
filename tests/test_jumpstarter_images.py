"""Tests for safe Jumpstarter image-server redirect handling."""

from __future__ import annotations

import httpx
import pytest

from providers.execution import AuditedAsyncHTTPClient
from providers.resource import jumpstarter_images
from providers.resource.jumpstarter_images import (
    _audited_get_follow_redirects,
    resolve_image_urls,
)
from providers.tracing import (
    bind_trace_context,
    new_trace_context,
    reset_trace_context,
)


@pytest.mark.asyncio
async def test_image_redirect_resolves_relative_location_and_forwards_causal_headers():
    seen: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (
                str(request.url),
                request.headers.get("traceparent", ""),
                request.headers.get("x-agentic-perf-ticket-id", ""),
            )
        )
        if request.url.path == "/manifest":
            return httpx.Response(302, headers={"location": "/next"})
        return httpx.Response(200, json={"ok": True})

    token = bind_trace_context(
        new_trace_context(ticket_id="PERF-images", agent_id="test-agent")
    )
    try:
        async with AuditedAsyncHTTPClient(
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        ) as client:
            response = await _audited_get_follow_redirects(
                client, "https://images.example/manifest"
            )
    finally:
        reset_trace_context(token)

    assert response.status_code == 200
    assert [url for url, _, _ in seen] == [
        "https://images.example/manifest",
        "https://images.example/next",
    ]
    assert all(traceparent for _, traceparent, _ in seen)
    assert all(ticket_id == "PERF-images" for _, _, ticket_id in seen)


@pytest.mark.asyncio
async def test_image_redirect_allows_trusted_download_host_without_causal_headers():
    seen: list[tuple[str, str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (
                str(request.url),
                request.headers.get("traceparent", ""),
                request.headers.get("x-agentic-perf-ticket-id", ""),
                request.headers.get("x-agentic-perf-action-id", ""),
            )
        )
        if request.url.host == "autosd.sig.centos.org":
            return httpx.Response(
                302,
                headers={"location": "https://download.autosd.sig.centos.org/manifest"},
            )
        if request.url.path == "/manifest":
            return httpx.Response(302, headers={"location": "/final"})
        return httpx.Response(200, json={"ok": True})

    token = bind_trace_context(
        new_trace_context(ticket_id="PERF-images", agent_id="test-agent")
    )
    try:
        async with AuditedAsyncHTTPClient(
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        ) as client:
            response = await _audited_get_follow_redirects(
                client, "https://autosd.sig.centos.org/manifest"
            )
    finally:
        reset_trace_context(token)

    assert response.status_code == 200
    assert [url for url, *_ in seen] == [
        "https://autosd.sig.centos.org/manifest",
        "https://download.autosd.sig.centos.org/manifest",
        "https://download.autosd.sig.centos.org/final",
    ]
    assert all(seen[0][1:])
    assert seen[1][1:] == ("", "", "")
    assert seen[2][1:] == ("", "", "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location",
    [
        "https://other.example/next",
        "http://images.example/next",
    ],
)
async def test_image_redirect_rejects_cross_origin_or_https_downgrade(location: str):
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("traceparent", "")))
        return httpx.Response(302, headers={"location": location})

    token = bind_trace_context(
        new_trace_context(ticket_id="PERF-images", agent_id="test-agent")
    )
    try:
        async with AuditedAsyncHTTPClient(
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        ) as client:
            response = await _audited_get_follow_redirects(
                client, "https://images.example/manifest"
            )
    finally:
        reset_trace_context(token)

    assert response.status_code == 302
    assert [url for url, _ in seen] == ["https://images.example/manifest"]
    assert seen[0][1]


@pytest.mark.asyncio
async def test_fallback_listing_and_manifest_requests_use_redirect_helper(monkeypatch):
    client_holder: list[object] = []
    manifest = {
        "board": [{"image_name": "ps", "image_type": "regular", "path": "image.img"}]
    }

    class FakeClient:
        def __init__(self, **_: object) -> None:
            self.calls: list[str] = []
            client_holder.append(self)

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get(self, url: str, **_: object) -> httpx.Response:
            self.calls.append(url)
            if url.endswith("/info/test_images_info.json"):
                if "latest-RHIVOS-2.1-202607240103" in url:
                    return httpx.Response(404, request=httpx.Request("GET", url))
                if "latest-RHIVOS-2/" in url:
                    return httpx.Response(
                        200,
                        json=manifest,
                        request=httpx.Request("GET", url),
                    )
                return httpx.Response(404, request=httpx.Request("GET", url))
            return httpx.Response(
                200,
                text='<a href="latest-RHIVOS-2.1-202607240103/">build</a>',
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr(jumpstarter_images, "AuditedAsyncHTTPClient", FakeClient)

    result = await resolve_image_urls(
        base_url="https://autosd.sig.centos.org",
        image_version="RHIVOS-2",
        release="latest-RHIVOS-2-202607240103",
        board_target="board",
    )

    assert result["manifest_url"].endswith(
        "/RHIVOS-2/latest-RHIVOS-2/info/test_images_info.json"
    )
    assert len(client_holder) == 1
    assert client_holder[0].calls == [
        "https://autosd.sig.centos.org/RHIVOS-2/latest-RHIVOS-2-202607240103/info/test_images_info.json",
        "https://autosd.sig.centos.org/RHIVOS-2/",
        "https://autosd.sig.centos.org/RHIVOS-2/latest-RHIVOS-2.1-202607240103/info/test_images_info.json",
        "https://autosd.sig.centos.org/RHIVOS-2/latest-RHIVOS-2/info/test_images_info.json",
    ]
