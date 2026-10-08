from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import Depends, FastAPI

from orchestrator.config import OrchestratorConfig
from orchestrator.leader_lease import LeaderLeaseClient
from orchestrator.main import (
    _handle_shutdown_signal,
    _LeaseLossGate,
    _poll_loop_after_lease,
    _renew_leader_lease,
    poll_loop,
)
from state_store.api.health import health
from state_store.api.router import api_router
from state_store.auth import make_auth_dependency
from state_store.identity import UserStore
from state_store.models import (
    AcquireOrchestratorLeaseRequest,
    CreateTicketRequest,
    TicketStatus,
)
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


def test_sigterm_uses_asyncio_shutdown_path():
    with pytest.raises(KeyboardInterrupt):
        _handle_shutdown_signal(15, None)


@pytest.mark.asyncio
async def test_cancelled_lease_renewal_releases_leader_lease():
    class Lease:
        released = False

        async def renew(self):
            await asyncio.sleep(60)

        async def release(self):
            self.released = True

    lease = Lease()
    task = asyncio.create_task(_renew_leader_lease(lease, 60))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert lease.released


@pytest.mark.asyncio
async def test_failed_lease_renewal_deposes_and_releases_immediately():
    class Lease:
        renew_count = 0
        released = False

        async def renew(self):
            self.renew_count += 1
            raise RuntimeError("state store unavailable")

        async def release(self):
            self.released = True

    lease = Lease()
    lost = []
    with pytest.raises(RuntimeError, match="orchestrator leader lease lost"):
        await _renew_leader_lease(lease, 0, on_lost=lambda: lost.append(True))

    assert lease.renew_count == 1
    assert lease.released
    assert lost == [True]


class _PollLoopLease:
    def __init__(self, *_args, **_kwargs):
        self.epoch = None
        self.session_id = uuid4()
        self.release_count = 0

    async def acquire(self):
        self.epoch = 7
        return {"epoch": self.epoch}

    async def release(self):
        self.release_count += 1


def _poll_config() -> OrchestratorConfig:
    return OrchestratorConfig(
        state_store_url="http://state-store",
        raw_config={"llm": {"provider": "mock"}},
    )


@pytest.mark.asyncio
async def test_poll_loop_releases_lease_when_initialization_fails(
    monkeypatch: pytest.MonkeyPatch,
):
    lease = _PollLoopLease()
    monkeypatch.setattr(
        "orchestrator.leader_lease.LeaderLeaseClient", lambda *a, **k: lease
    )

    async def fail_initialization(*_args, **_kwargs):
        raise RuntimeError("initialization failed")

    monkeypatch.setattr("orchestrator.main._poll_loop_after_lease", fail_initialization)

    with pytest.raises(RuntimeError, match="initialization failed"):
        await poll_loop(_poll_config())

    assert lease.release_count == 1


@pytest.mark.asyncio
async def test_dispatcher_receives_lease_before_first_poll_request(
    monkeypatch: pytest.MonkeyPatch,
):
    import orchestrator.main as orchestrator_main

    config = _poll_config()
    config.poll_interval = 60
    config.stale_task_timeout = 0
    observed = asyncio.get_running_loop().create_future()
    dispatchers = []
    phases = []

    class Lease(_PollLoopLease):
        async def renew(self):
            return {"epoch": self.epoch}

    class Events:
        def __init__(self, **_kwargs):
            pass

        def close(self):
            pass

    class Dispatcher:
        def __init__(self, *_args, **kwargs):
            self._session_id = kwargs["session_id"]
            self._fencing_epoch = kwargs["fencing_epoch"]
            dispatchers.append(self)

        def reconcile_handoff_blocked(self, _tickets):
            pass

        async def shutdown(self):
            pass

    async def validate_models(*_args):
        pass

    async def fetch_tickets(_url):
        dispatcher = dispatchers[0]
        observed.set_result(
            (dispatcher._session_id, dispatcher._fencing_epoch, list(phases))
        )
        return []

    async def no_op(*_args, **_kwargs):
        pass

    monkeypatch.setattr(orchestrator_main, "RepoCache", lambda: object())
    monkeypatch.setattr(
        orchestrator_main,
        "build_skill_provider_async",
        AsyncMock(return_value=object()),
    )
    monkeypatch.setattr(orchestrator_main, "LocalSecretsProvider", lambda: object())
    monkeypatch.setattr(
        orchestrator_main,
        "_make_llm_provider",
        lambda _config: SimpleNamespace(
            default_timeout=None, reasoning_effort=None, max_tokens=None
        ),
    )
    monkeypatch.setattr(
        orchestrator_main, "_make_llm_factory", lambda _config: object()
    )
    monkeypatch.setattr(orchestrator_main, "_validate_models", validate_models)
    monkeypatch.setattr(orchestrator_main, "EventBus", Events)
    monkeypatch.setattr(orchestrator_main, "Dispatcher", Dispatcher)
    monkeypatch.setattr(
        orchestrator_main,
        "record_orchestrator_status",
        lambda phase, **_kwargs: phases.append(phase),
    )
    monkeypatch.setattr(orchestrator_main, "_process_stop_requests", no_op)
    monkeypatch.setattr(orchestrator_main, "_sweep_orphaned_leases", no_op)
    monkeypatch.setattr(orchestrator_main, "_sweep_trace_spools", lambda: None)
    monkeypatch.setattr(orchestrator_main, "fetch_all_tickets", fetch_tickets)
    telemetry_module = ModuleType("providers.telemetry")
    telemetry_module.setup_telemetry = lambda **_kwargs: None
    monkeypatch.setitem(sys.modules, "providers.telemetry", telemetry_module)
    monkeypatch.setattr("providers.redaction.get_shared_redactor", lambda: object())

    lease = Lease()
    lease_state = {"renew_task": None, "started": asyncio.Event()}
    task = asyncio.create_task(
        _poll_loop_after_lease(config, lease, _LeaseLossGate(), lease_state)
    )
    try:
        session_id, epoch, recorded_phases = await asyncio.wait_for(observed, timeout=5)
        assert session_id == str(lease.session_id)
        assert epoch == lease.epoch == 7
        assert recorded_phases[-3:] == [
            "acquiring_lease",
            "lease_acquired",
            "running",
        ]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_poll_loop_cancellation_releases_lease(
    monkeypatch: pytest.MonkeyPatch,
):
    lease = _PollLoopLease()
    monkeypatch.setattr(
        "orchestrator.leader_lease.LeaderLeaseClient", lambda *a, **k: lease
    )
    started = asyncio.Event()

    async def run_until_cancelled(*_args, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("orchestrator.main._poll_loop_after_lease", run_until_cancelled)
    task = asyncio.create_task(poll_loop(_poll_config()))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert lease.release_count == 1


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
    assert result["total"] == 0
    assert all(count == 0 for count in result["ticket_counts"].values())


def test_public_health_counts_tickets_without_listing_them(tmp_path, monkeypatch):
    store = TicketStore(persist_dir=tmp_path)
    for summary in ("first", "second"):
        store.create_ticket(CreateTicketRequest(summary=summary, description="test"))
    monkeypatch.setattr(
        store,
        "list_tickets",
        lambda: pytest.fail("health must not copy the full ticket list"),
    )
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

    assert result["ticket_counts"][TicketStatus.NEW.value] == 2
    assert set(result["ticket_counts"]) == {status.value for status in TicketStatus}
    assert result["total"] == 2


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
