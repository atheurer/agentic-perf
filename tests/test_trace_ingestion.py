from __future__ import annotations

import httpx
from fastapi import Depends, FastAPI

from providers.tracing import (
    ActionDescriptor,
    ActionType,
    LifecycleDescriptor,
    LifecycleState,
    ProducerIdentity,
    TraceEventV1,
)
from state_store.api.router import api_router
from state_store.auth import make_auth_dependency
from state_store.identity import UserStore
from state_store.main import _set_audit_actor, create_app
from state_store.trace_store import TraceStore


def payload(event: TraceEventV1) -> dict:
    return event.model_dump(mode="json")


def make_app(tmp_path, user_store: UserStore | None = None) -> FastAPI:
    app = FastAPI()
    app.state.trace_store = TraceStore(tmp_path / "trace.db")
    app.state.trace_instance_id = "server-instance"
    app.state.trace_health = {
        "ingested": 0,
        "ingestion_failures": 0,
        "schema_rejections": 0,
        "quarantined_frames": 0,
    }
    app.include_router(
        api_router,
        dependencies=[
            Depends(
                make_auth_dependency("service", multi_user=True, user_store=user_store)
            ),
            Depends(_set_audit_actor),
        ],
    )
    return app


def event() -> TraceEventV1:
    return TraceEventV1(
        ticket_id="PERF-1",
        action=ActionDescriptor(type=ActionType.DISPATCH),
        lifecycle=LifecycleDescriptor(state=LifecycleState.STARTED),
    )


async def test_service_ingests_and_server_binds_identity(tmp_path) -> None:
    original = event().model_copy(
        update={
            "producer": ProducerIdentity(
                component="mcp",
                instance_id="forged-instance",
                host="producer-host",
                pid=123,
                process_start_id="start-1",
                boot_id="boot-1",
            ),
            "attributes": {
                "authenticated_principal": "forged-principal",
                "kept": True,
            },
        }
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=make_app(tmp_path)), base_url="http://test"
    )
    response = await client.post(
        "/api/v1/traces/events",
        json=payload(original),
        headers={"Authorization": "Bearer service", "X-Trace-Instance": "process-a"},
    )
    await client.aclose()
    assert response.status_code == 200
    assert response.json()["event"]["producer"] == {
        "component": "mcp",
        "instance_id": "server-instance",
        "host": "producer-host",
        "pid": 123,
        "process_start_id": "start-1",
        "boot_id": "boot-1",
    }
    assert (
        response.json()["event"]["attributes"]["authenticated_principal"]
        == "deployment"
    )
    assert response.json()["event"]["attributes"]["kept"] is True


async def test_user_or_anonymous_cannot_spoof_producer(tmp_path) -> None:
    users = UserStore(tmp_path / "users.json")
    _, token = users.create_user("alice")
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=make_app(tmp_path, users)),
        base_url="http://test",
    )
    missing = await client.post(
        "/api/v1/traces/events",
        json=payload(event()),
        headers={"X-Trace-Instance": "bad"},
    )
    response = await client.post(
        "/api/v1/traces/events",
        json=payload(event()),
        headers={"Authorization": f"Bearer {token}", "X-Trace-Instance": "bad"},
    )
    await client.aclose()
    assert missing.status_code == 401
    assert response.status_code == 403


async def test_partial_duplicate_batch_has_stable_acknowledgements(tmp_path) -> None:
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=make_app(tmp_path)), base_url="http://test"
    )
    one, two = event(), event()
    headers = {"Authorization": "Bearer service", "X-Trace-Instance": "process-a"}
    first = await client.post(
        "/api/v1/traces/events", json=payload(one), headers=headers
    )
    assert first.status_code == 200
    response = await client.post(
        "/api/v1/traces/events/batch",
        json={"events": [payload(one), payload(two)]},
        headers=headers,
    )
    await client.aclose()
    assert response.status_code == 200
    acknowledgements = response.json()["acknowledgements"]
    assert [ack["accepted"] for ack in acknowledgements] == [True, True]
    assert [ack["status"] for ack in acknowledgements] == ["duplicate", "stored"]
    assert (
        acknowledgements[0]["event"]["global_seq"]
        == first.json()["event"]["global_seq"]
    )


async def test_invalid_trace_schema_increments_health_counter() -> None:
    app = create_app(initialize_immediately=True)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    try:
        response = await client.post(
            "/api/v1/traces/events",
            json={"ticket_id": "PERF-invalid"},
            headers={"Authorization": f"Bearer {app.state.api_token}"},
        )
        assert response.status_code == 422
        assert app.state.trace_health["schema_rejections"] == 1
    finally:
        await client.aclose()
        app.state.trace_store.close()
