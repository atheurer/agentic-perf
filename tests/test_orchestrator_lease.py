from __future__ import annotations

from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import Depends, FastAPI

from orchestrator.leader_lease import LeaderLeaseClient
from state_store.api.health import health
from state_store.api.router import api_router
from state_store.auth import make_auth_dependency
from state_store.identity import UserStore
from state_store.models import AcquireOrchestratorLeaseRequest, CreateTicketRequest
from state_store.store import OrchestratorLeaseHeld, TicketStore


@pytest.mark.asyncio
async def test_leader_lease_acquire_is_control_plane_not_ticket_audited(
    monkeypatch: pytest.MonkeyPatch,
):
    """Startup must acquire its lease before a ticket trace can exist."""

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, json):
            assert url.endswith("/orchestrator-lease/acquire")
            assert json["instance_name"] == "test-instance"
            return httpx.Response(
                200,
                json={"epoch": 7},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(
        "orchestrator.leader_lease.httpx.AsyncClient", lambda **_kwargs: Client()
    )

    client = LeaderLeaseClient("http://state-store", instance_name="test-instance")
    assert (await client.acquire())["epoch"] == 7
    assert client.epoch == 7


def _request(session_id=None, *, instance_name="shared"):
    return AcquireOrchestratorLeaseRequest(
        session_id=session_id or uuid4(),
        instance_name=instance_name,
        host="host-a",
        pid=123,
        process_start_id="boot:123",
        ttl_seconds=10,
    )


def test_same_session_acquire_is_idempotent_and_other_session_is_rejected():
    with TemporaryDirectory() as directory:
        store = TicketStore(persist_dir=directory)
        first = _request()
        lease = store.acquire_orchestrator_lease(first)
        assert store.acquire_orchestrator_lease(first).epoch == lease.epoch
        with pytest.raises(OrchestratorLeaseHeld) as error:
            store.acquire_orchestrator_lease(
                _request(instance_name=first.instance_name)
            )
        assert error.value.holder.session_id == first.session_id
        assert error.value.remaining_seconds > 0


def test_expiry_takeover_fences_old_session_and_survives_restart():
    now = [datetime.now(timezone.utc)]
    with TemporaryDirectory() as directory:
        first_store = TicketStore(persist_dir=directory, clock=lambda: now[0])
        first = _request()
        lease = first_store.acquire_orchestrator_lease(first)
        restarted = TicketStore(persist_dir=directory, clock=lambda: now[0])
        assert restarted.get_orchestrator_lease().session_id == first.session_id
        now[0] += timedelta(seconds=11)
        second = _request()
        replacement = restarted.acquire_orchestrator_lease(second)
        assert replacement.epoch > lease.epoch
        assert not restarted.release_orchestrator_lease(first.session_id, lease.epoch)
        with pytest.raises(PermissionError):
            restarted.renew_orchestrator_lease(first.session_id, lease.epoch, 10)


def test_release_fsyncs_persistence_directory(tmp_path, monkeypatch):
    calls = []
    store = TicketStore(persist_dir=tmp_path)
    monkeypatch.setattr(
        store,
        "_fsync_lease_directory",
        lambda: calls.append(True),
    )
    request = _request()
    lease = store.acquire_orchestrator_lease(request)
    calls.clear()
    assert store.release_orchestrator_lease(lease.session_id, lease.epoch)
    assert calls, "lease deletion must fsync the persistence directory"
    assert not (tmp_path / "orchestrator-lease.json").exists()


def test_public_health_does_not_disclose_lease_holder_identity(tmp_path):
    store = TicketStore(persist_dir=tmp_path)
    store.acquire_orchestrator_lease(_request())
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                store=store,
                trace_health={},
            )
        ),
        client=None,
    )
    result = health(request)
    assert result["orchestrator_lease"] == {"active": True}


@pytest.mark.asyncio
async def test_user_token_cannot_use_or_inspect_control_lease(tmp_path):
    users = UserStore(tmp_path / "users.json")
    _, user_token = users.create_user("alice")
    app = FastAPI()
    app.state.store = TicketStore(persist_dir=tmp_path / "tickets")
    app.include_router(
        api_router,
        dependencies=[
            Depends(make_auth_dependency("service", multi_user=True, user_store=users))
        ],
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    try:
        headers = {"Authorization": f"Bearer {user_token}"}
        assert (
            await client.get("/api/v1/control/orchestrator-lease", headers=headers)
        ).status_code == 403
        response = await client.post(
            "/api/v1/control/orchestrator-lease/acquire",
            json=_request().model_dump(mode="json"),
            headers=headers,
        )
        assert response.status_code == 403
        ticket = app.state.store.create_ticket(
            CreateTicketRequest(summary="claim auth", description="claim auth")
        )
        fence = {
            "owner": "orch",
            "duration_seconds": 30,
            "session_id": str(uuid4()),
            "epoch": 1,
            "claim_id": "user-must-not-claim",
        }
        for method, path in (
            ("post", f"/api/v1/tickets/{ticket.id}/claim"),
            ("post", f"/api/v1/tickets/{ticket.id}/claim/renew"),
        ):
            response = await getattr(client, method)(path, json=fence, headers=headers)
            assert response.status_code == 403
        response = await client.request(
            "DELETE",
            f"/api/v1/tickets/{ticket.id}/claim",
            json=fence,
            headers=headers,
        )
        assert response.status_code == 403
    finally:
        await client.aclose()
