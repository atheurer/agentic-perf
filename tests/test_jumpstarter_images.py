"""Tests for safe Jumpstarter image-server redirect handling."""

from __future__ import annotations

import httpx
import pytest

from providers.execution import AuditedAsyncHTTPClient
from providers.resource.jumpstarter_images import _audited_get_follow_redirects
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
