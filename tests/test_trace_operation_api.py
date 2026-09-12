"""Authenticated operation API contract."""

from __future__ import annotations

import json

import httpx
import pytest

from providers.tracing.client import TraceClient, TraceDeliveryError
from tests.test_trace_ingestion import make_app


async def test_service_acquire_existing_terminal_and_actions(tmp_path) -> None:
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=make_app(tmp_path)), base_url="http://test"
    )
    headers = {"Authorization": "Bearer service"}
    body = {"operation_key": "key", "request_hash": "hash", "ttl_seconds": 60}
    first = await client.post(
        "/api/v1/traces/operations/acquire", json=body, headers=headers
    )
    assert first.status_code == 200 and first.json()["status"] == "acquired"
    token = first.json()["operation"]["fencing_generation"]
    second = await client.post(
        "/api/v1/traces/operations/acquire", json=body, headers=headers
    )
    assert second.json()["status"] == "existing"
    assert (
        await client.post(
            "/api/v1/traces/operations/key/prepared",
            json={"fencing_token": token},
            headers=headers,
        )
    ).status_code == 200
    assert (
        await client.post(
            "/api/v1/traces/operations/key/complete",
            json={"fencing_token": token, "descriptor": {"id": "safe"}},
            headers=headers,
        )
    ).status_code == 200
    cached = await client.post(
        "/api/v1/traces/operations/acquire", json=body, headers=headers
    )
    assert cached.json()["status"] == "terminal"
    assert cached.json()["operation"]["result_descriptor"] == {"id": "safe"}
    conflict = await client.post(
        "/api/v1/traces/operations/register",
        json={"operation_key": "key", "request_hash": "other"},
        headers=headers,
    )
    assert conflict.status_code == 409
    assert (
        await client.post(
            "/api/v1/traces/operations/key/renew",
            json={"fencing_token": token},
            headers=headers,
        )
    ).status_code == 422
    assert (
        await client.post(
            "/api/v1/traces/operations/key/nope",
            json={"fencing_token": token},
            headers=headers,
        )
    ).status_code == 404
    assert (
        await client.post(
            "/api/v1/traces/operations/key/reconcile",
            json={"fencing_token": token, "reconciliation_outcome": "unknown"},
            headers=headers,
        )
    ).status_code == 422
    await client.aclose()


async def test_operation_api_requires_service_principal(tmp_path) -> None:
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=make_app(tmp_path)), base_url="http://test"
    )
    response = await client.post(
        "/api/v1/traces/operations/register",
        json={"operation_key": "key", "request_hash": "hash"},
    )
    assert response.status_code == 401
    await client.aclose()


def test_trace_client_forwards_only_requested_transition_fields(tmp_path) -> None:
    requests: list[tuple[str, dict]] = []

    def accepted(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        return httpx.Response(200, json={"operation": {}, "status": "existing"})

    client = TraceClient(
        "http://store",
        "token",
        spool_dir=tmp_path,
        client=httpx.Client(transport=httpx.MockTransport(accepted)),
    )
    assert client.operation_transition("key", "prepared", 1)["status"] == "existing"
    client.operation_transition("key", "renew", 1, ttl_seconds=30)
    client.operation_transition(
        "key",
        "reconcile",
        1,
        descriptor={"id": "safe"},
        reconciliation_outcome="success",
    )
    client.close()

    assert requests == [
        ("/api/v1/traces/operations/key/prepared", {"fencing_token": 1}),
        (
            "/api/v1/traces/operations/key/renew",
            {"fencing_token": 1, "ttl_seconds": 30},
        ),
        (
            "/api/v1/traces/operations/key/reconcile",
            {
                "fencing_token": 1,
                "descriptor": {"id": "safe"},
                "reconciliation_outcome": "success",
            },
        ),
    ]


def test_trace_client_maps_operation_http_errors(tmp_path) -> None:
    client = TraceClient(
        "http://store",
        "token",
        spool_dir=tmp_path,
        client=httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(409))
        ),
    )
    with pytest.raises(TraceDeliveryError):
        client.operation_acquire("key", "hash", 30)
    client._client.close()
    client.spool.close()
