from __future__ import annotations

import asyncio
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from fastapi import Depends, FastAPI

from orchestrator.config import OrchestratorConfig
from orchestrator.leader_lease import LeaderLeaseClient, LeaderLeaseUnavailable
from orchestrator.main import (
    DEPOSED_CLAIM_RECONCILIATION_FIELD,
    LeaderLeaseLost,
    LeaderLeaseUnrecoverable,
    _agent_progress_fields,
    _closed_claim_page_state,
    _handle_shutdown_signal,
    _LeaderLeaseRenewer,
    _LeaseLossGate,
    _poll_loop_after_lease,
    _reconcile_deposed_claims,
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


class _RenewalLease:
    def __init__(self, outcomes=(), *, ttl_seconds=5.0):
        self.ttl_seconds = ttl_seconds
        self.confirmed_deadline = asyncio.get_running_loop().time() + ttl_seconds
        self.outcomes = list(outcomes)
        self.attempts = []
        self.released = 0
        self.cancelled = False
        self.recovered = asyncio.Event()

    async def renew(self):
        self.attempts.append(asyncio.get_running_loop().time())
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if outcome == "hang":
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        if isinstance(outcome, Exception):
            raise outcome
        self.confirmed_deadline = asyncio.get_running_loop().time() + self.ttl_seconds
        self.recovered.set()

    async def release(self):
        self.released += 1


def _lease_http_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://state-store/renew")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(
        f"HTTP {status_code}", request=request, response=response
    )


async def _wait_for_thread(thread: threading.Thread, timeout: float) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while thread.is_alive() and loop.time() < deadline:
        await asyncio.sleep(0.01)
    return not thread.is_alive()


def test_closed_claim_reconciliation_advances_after_ticket_failure() -> None:
    state = _closed_claim_page_state(
        {"next_cursor": "PERF-next"},
        {"PERF-failed"},
        cycle_failed=False,
    )
    assert state == (False, "PERF-next", True)

    state = _closed_claim_page_state(
        {"next_cursor": None},
        set(),
        cycle_failed=True,
    )
    assert state == (False, None, False)

    state = _closed_claim_page_state(
        {"next_cursor": None},
        set(),
        cycle_failed=False,
    )
    assert state == (True, None, False)

    state = _closed_claim_page_state(
        {"next_cursor": None, "read_errors": True},
        set(),
        cycle_failed=False,
    )
    assert state == (False, None, False)


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


@pytest.mark.asyncio
async def test_acquire_passes_max_attempts_from_config(
    monkeypatch: pytest.MonkeyPatch,
):
    """max_attempts flows from OrchestratorConfig to LeaderLeaseClient.acquire."""
    captured_kwargs: list[dict] = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, json):
            return httpx.Response(
                200,
                json={"epoch": 1},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(
        "orchestrator.leader_lease.httpx.AsyncClient", lambda **_kwargs: Client()
    )

    original_acquire = LeaderLeaseClient.acquire

    async def spy_acquire(self, **kwargs):
        captured_kwargs.append(kwargs)
        return await original_acquire(self, **kwargs)

    monkeypatch.setattr(LeaderLeaseClient, "acquire", spy_acquire)

    config = OrchestratorConfig(
        state_store_url="http://state-store",
        raw_config={
            "llm": {"provider": "mock"},
            "orchestrator_lease": {"max_attempts": 5},
        },
    )
    assert config.leader_lease_max_attempts == 5

    lc = LeaderLeaseClient("http://state-store", instance_name="test")
    await lc.acquire(max_attempts=config.leader_lease_max_attempts)
    assert captured_kwargs[0]["max_attempts"] == 5


@pytest.mark.asyncio
async def test_acquire_409_then_200_retries_successfully(
    monkeypatch: pytest.MonkeyPatch,
):
    """A 409 on the first attempt is retried and succeeds on the second."""
    attempt_count = 0

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, json):
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count == 1:
                return httpx.Response(
                    409,
                    json={"detail": "held"},
                    request=httpx.Request("POST", url),
                )
            return httpx.Response(
                200,
                json={"epoch": 42},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(
        "orchestrator.leader_lease.httpx.AsyncClient", lambda **_kwargs: Client()
    )

    lc = LeaderLeaseClient("http://state-store", instance_name="test")
    lease = await lc.acquire(max_attempts=3, retry_interval=0)
    assert lease["epoch"] == 42
    assert lc.epoch == 42
    assert attempt_count == 2


@pytest.mark.asyncio
async def test_acquire_409_exhaustion_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
):
    """409 through all max_attempts raises a recoverable acquisition error."""
    attempt_count = 0

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, json):
            nonlocal attempt_count
            attempt_count += 1
            return httpx.Response(
                409,
                json={"detail": "held"},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(
        "orchestrator.leader_lease.httpx.AsyncClient", lambda **_kwargs: Client()
    )

    lc = LeaderLeaseClient("http://state-store", instance_name="test")
    with pytest.raises(LeaderLeaseUnavailable, match="unavailable after 3 attempts"):
        await lc.acquire(max_attempts=3, retry_interval=0)
    assert attempt_count == 3


@pytest.mark.parametrize("status_code", [408, 425, 429, 500, 503])
@pytest.mark.asyncio
async def test_acquire_transient_status_retries_successfully(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
):
    """Temporary overload or timeout-style responses are retried."""
    attempt_count = 0

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, json):
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count == 1:
                return httpx.Response(
                    status_code,
                    text="temporarily unavailable",
                    request=httpx.Request("POST", url),
                )
            return httpx.Response(
                200,
                json={"epoch": 11},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(
        "orchestrator.leader_lease.httpx.AsyncClient", lambda **_kwargs: Client()
    )

    lc = LeaderLeaseClient("http://state-store", instance_name="test")
    lease = await lc.acquire(max_attempts=3, retry_interval=0)
    assert lease["epoch"] == 11
    assert attempt_count == 2


@pytest.mark.asyncio
async def test_acquire_transient_status_exhaustion_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
):
    attempt_count = 0

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, json):
            nonlocal attempt_count
            attempt_count += 1
            return httpx.Response(
                503,
                text="temporarily unavailable",
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(
        "orchestrator.leader_lease.httpx.AsyncClient", lambda **_kwargs: Client()
    )

    lc = LeaderLeaseClient("http://state-store", instance_name="test")
    with pytest.raises(LeaderLeaseUnavailable, match="unavailable after 3 attempts"):
        await lc.acquire(max_attempts=3, retry_interval=0)
    assert attempt_count == 3


@pytest.mark.parametrize("status_code", [401, 403, 410])
@pytest.mark.asyncio
async def test_acquire_terminal_status_is_immediately_fatal(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
):
    attempt_count = 0

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, json):
            nonlocal attempt_count
            attempt_count += 1
            return httpx.Response(
                status_code,
                text="rejected",
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(
        "orchestrator.leader_lease.httpx.AsyncClient", lambda **_kwargs: Client()
    )

    lc = LeaderLeaseClient("http://state-store", instance_name="test")
    with pytest.raises(httpx.HTTPStatusError):
        await lc.acquire(max_attempts=3, retry_interval=0)
    assert attempt_count == 1


@pytest.mark.asyncio
async def test_acquire_transport_error_then_success(
    monkeypatch: pytest.MonkeyPatch,
):
    """httpx.RequestError is retried and succeeds on subsequent attempt."""
    attempt_count = 0

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, json):
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count == 1:
                raise httpx.ConnectError(
                    "connection refused",
                    request=httpx.Request("POST", url),
                )
            return httpx.Response(
                200,
                json={"epoch": 10},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(
        "orchestrator.leader_lease.httpx.AsyncClient", lambda **_kwargs: Client()
    )

    lc = LeaderLeaseClient("http://state-store", instance_name="test")
    lease = await lc.acquire(max_attempts=3, retry_interval=0)
    assert lease["epoch"] == 10
    assert attempt_count == 2


@pytest.mark.asyncio
async def test_acquire_cancellation_during_backoff(
    monkeypatch: pytest.MonkeyPatch,
):
    """CancelledError during backoff sleep propagates immediately."""
    attempt_count = 0

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, json):
            nonlocal attempt_count
            attempt_count += 1
            return httpx.Response(
                409,
                json={"detail": "held"},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(
        "orchestrator.leader_lease.httpx.AsyncClient", lambda **_kwargs: Client()
    )

    lc = LeaderLeaseClient("http://state-store", instance_name="test")
    task = asyncio.create_task(lc.acquire(max_attempts=10, retry_interval=999))
    # Let first attempt run and hit the sleep
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert attempt_count == 1


def test_config_max_attempts_rejects_zero():
    """max_attempts=0 is rejected at config load time."""
    with pytest.raises(ValueError, match="must be an integer >= 1"):
        OrchestratorConfig(
            state_store_url="http://state-store",
            raw_config={
                "llm": {"provider": "mock"},
                "orchestrator_lease": {"max_attempts": 0},
            },
        )


def test_config_max_attempts_rejects_negative():
    """Negative max_attempts is rejected at config load time."""
    with pytest.raises(ValueError, match="must be an integer >= 1"):
        OrchestratorConfig(
            state_store_url="http://state-store",
            raw_config={
                "llm": {"provider": "mock"},
                "orchestrator_lease": {"max_attempts": -1},
            },
        )


@pytest.mark.parametrize("ttl_seconds", [0, -1, 3600.01, float("inf"), float("nan")])
def test_config_rejects_invalid_lease_ttl(
    monkeypatch: pytest.MonkeyPatch, ttl_seconds: float
) -> None:
    monkeypatch.delenv("ORCHESTRATOR_LEASE_TTL_SECONDS", raising=False)
    with pytest.raises(ValueError, match="leader_lease_ttl_seconds must be finite"):
        OrchestratorConfig(
            state_store_url="http://state-store",
            raw_config={
                "llm": {"provider": "mock"},
                "orchestrator_lease": {"ttl_seconds": ttl_seconds},
            },
        )


@pytest.mark.parametrize("renew_interval", [0, -1, float("inf"), float("nan")])
def test_config_rejects_invalid_lease_renew_interval(
    monkeypatch: pytest.MonkeyPatch, renew_interval: float
) -> None:
    monkeypatch.delenv("ORCHESTRATOR_LEASE_RENEW_INTERVAL", raising=False)
    with pytest.raises(
        ValueError, match="leader_lease_renew_interval must be finite and > 0"
    ):
        OrchestratorConfig(
            state_store_url="http://state-store",
            raw_config={
                "llm": {"provider": "mock"},
                "orchestrator_lease": {"renew_interval": renew_interval},
            },
        )


def test_config_accepts_maximum_lease_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ORCHESTRATOR_LEASE_TTL_SECONDS", raising=False)
    config = OrchestratorConfig(
        state_store_url="http://state-store",
        raw_config={
            "llm": {"provider": "mock"},
            "orchestrator_lease": {"ttl_seconds": 3600},
        },
    )

    assert config.leader_lease_ttl_seconds == 3600
    assert config.leader_lease_renew_interval == 1200


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("drain_timeout_seconds", float("inf")),
        ("drain_timeout_seconds", float("nan")),
        ("recovery_delay_seconds", float("inf")),
        ("recovery_delay_seconds", float("nan")),
    ],
)
def test_config_rejects_nonfinite_lease_delays(
    monkeypatch: pytest.MonkeyPatch, field: str, value: float
) -> None:
    env_name = {
        "drain_timeout_seconds": "ORCHESTRATOR_LEASE_DRAIN_TIMEOUT_SECONDS",
        "recovery_delay_seconds": "ORCHESTRATOR_LEASE_RECOVERY_DELAY_SECONDS",
    }[field]
    monkeypatch.delenv(env_name, raising=False)
    with pytest.raises(ValueError, match=rf"leader_lease_{field} must be finite"):
        OrchestratorConfig(
            state_store_url="http://state-store",
            raw_config={
                "llm": {"provider": "mock"},
                "orchestrator_lease": {field: value},
            },
        )


def test_sigterm_uses_asyncio_shutdown_path():
    with pytest.raises(KeyboardInterrupt):
        _handle_shutdown_signal(15, None)


@pytest.mark.asyncio
async def test_cancelled_lease_renewal_leaves_release_to_dispatcher_cleanup():
    lease = _RenewalLease(["hang"])
    lost = []
    task = asyncio.create_task(
        _renew_leader_lease(lease, 0, on_lost=lambda: lost.append(True))
    )
    while not lease.attempts:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert lease.cancelled
    assert lease.released == 0
    assert lost == []


@pytest.mark.asyncio
async def test_failed_lease_renewal_deposes_without_releasing_first():
    lease = _RenewalLease([RuntimeError("programming error")])
    lost = []
    with pytest.raises(RuntimeError, match="orchestrator leader lease lost"):
        await _renew_leader_lease(lease, 0, on_lost=lambda: lost.append(True))

    assert len(lease.attempts) == 1
    assert lease.released == 0
    assert lost == [True]


@pytest.mark.asyncio
async def test_transient_lease_failure_retries_at_backoff_and_recovers():
    request = httpx.Request("POST", "http://state-store/renew")
    lease = _RenewalLease([httpx.ReadError("temporary read failure", request=request)])
    lost = []
    task = asyncio.create_task(
        _renew_leader_lease(lease, 0.2, on_lost=lambda: lost.append(True))
    )

    await asyncio.wait_for(lease.recovered.wait(), timeout=1)
    retry_delay = lease.attempts[1] - lease.attempts[0]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert 0.05 <= retry_delay < 0.3
    assert len(lease.attempts) == 2
    assert lost == []
    assert lease.released == 0


@pytest.mark.asyncio
async def test_transient_failures_do_not_lose_lease_before_ttl():
    request = httpx.Request("POST", "http://state-store/renew")
    lease = _RenewalLease(
        [httpx.ConnectError("temporary", request=request)] * 4,
        ttl_seconds=3.0,
    )
    task = asyncio.create_task(_renew_leader_lease(lease, 0.05))

    await asyncio.wait_for(lease.recovered.wait(), timeout=3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(lease.attempts) == 5
    assert lease.released == 0


@pytest.mark.asyncio
async def test_dedicated_renewer_continues_while_main_loop_is_blocked():
    attempts = 0
    lost = threading.Event()
    forked = threading.Event()

    class ThreadLease:
        ttl_seconds = 2.0
        confirmed_deadline = time.monotonic() + 2.0

        async def renew(self):
            nonlocal attempts
            attempts += 1
            self.confirmed_deadline = time.monotonic() + self.ttl_seconds
            forked.set()

        async def release(self):
            return None

    renewer = _LeaderLeaseRenewer(ThreadLease(), 0.05, lost.set)
    renewer.start(asyncio.get_running_loop())
    time.sleep(0.3)
    stopper = threading.Thread(target=lambda: renewer.stop(timeout=2), daemon=True)
    stopper.start()

    assert await _wait_for_thread(stopper, 2.5)
    assert forked.is_set()
    assert attempts >= 2
    assert not lost.is_set()
    renewer.raise_if_failed()


@pytest.mark.asyncio
async def test_dedicated_renewer_stop_waits_for_task_publication(
    monkeypatch: pytest.MonkeyPatch,
):
    import orchestrator.main as orchestrator_main

    task_creation_entered = threading.Event()
    release_task_creation = threading.Event()

    class DelayedTaskLoop(asyncio.SelectorEventLoop):
        def create_task(self, coroutine, **kwargs):
            task_creation_entered.set()
            if not release_task_creation.wait(2):
                raise RuntimeError("task publication was not released")
            return super().create_task(coroutine, **kwargs)

    class ThreadLease:
        ttl_seconds = 2.0
        confirmed_deadline = time.monotonic() + 2.0

        async def renew(self):
            self.confirmed_deadline = time.monotonic() + self.ttl_seconds
            return {}

    monkeypatch.setattr(orchestrator_main.asyncio, "new_event_loop", DelayedTaskLoop)
    renewer = _LeaderLeaseRenewer(ThreadLease(), 0.05, lambda: None)
    renewer.start(asyncio.get_running_loop())
    stopper = threading.Thread(target=lambda: renewer.stop(timeout=1), daemon=True)
    try:
        deadline = asyncio.get_running_loop().time() + 1
        while (
            not task_creation_entered.is_set()
            and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.01)
        assert task_creation_entered.is_set()
        stopper.start()
        await asyncio.sleep(0.05)
        assert stopper.is_alive()
        release_task_creation.set()
        assert await _wait_for_thread(stopper, 2)
        assert not renewer._thread.is_alive()
    finally:
        release_task_creation.set()
        if stopper.is_alive():
            assert await _wait_for_thread(stopper, 2)
        if renewer._thread.is_alive():
            renewer.stop(timeout=1)


@pytest.mark.asyncio
async def test_deposed_claim_is_reconciled_to_explicit_guidance(
    monkeypatch: pytest.MonkeyPatch,
):
    current_session = str(uuid4())
    monkeypatch.setenv("AGENTIC_PERF_API_TOKEN", "service")
    monkeypatch.setenv("AGENTIC_PERF_ORCHESTRATOR_SESSION_ID", current_session)
    monkeypatch.setenv("AGENTIC_PERF_ORCHESTRATOR_EPOCH", "9")
    calls: list[tuple[str, str, dict, dict]] = []

    class Response:
        status_code = 200
        text = ""

    class Client:
        def __init__(self, **kwargs):
            self.headers = kwargs["headers"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def patch(self, url, *, json):
            calls.append(("patch", url, json, self.headers))
            return Response()

        async def post(self, url, *, json):
            calls.append(("post", url, json, self.headers))
            return Response()

    monkeypatch.setattr("orchestrator.main.AuditedAsyncHTTPClient", Client)
    old_claim = {
        "claim_id": "claim-old",
        "owner": "orchestrator-old",
        "session_id": str(uuid4()),
        "epoch": 4,
        "expires": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    }
    tickets = [
        {
            "id": "PERF-old",
            "status": "awaiting_provision",
            "custom_fields": {"claim": old_claim},
        },
        {
            "id": "PERF-current",
            "status": "awaiting_provision",
            "custom_fields": {
                "claim": {
                    **old_claim,
                    "claim_id": "claim-current",
                    "session_id": current_session,
                    "epoch": 9,
                }
            },
        },
    ]

    blocked, failed = await _reconcile_deposed_claims("http://store", tickets)

    assert blocked == {"PERF-old"}
    assert failed == set()
    assert [call[0] for call in calls] == ["patch", "post", "patch"]
    assert calls[0][2]["fields"][DEPOSED_CLAIM_RECONCILIATION_FIELD]["status"] == (
        "pending"
    )
    summary = calls[0][2]["fields"]["guidance_summary"]
    assert summary["reason"] == "claim_lost"
    assert summary["agent"] == "provisioning"
    assert "interrupted" in summary["details"]
    assert summary["suggested_actions"]
    assert calls[1][2]["status"] == "awaiting_customer_guidance"
    assert calls[2][2]["fields"]["claim"] is None
    assert calls[2][2]["fields"][DEPOSED_CLAIM_RECONCILIATION_FIELD]["status"] == (
        "reconciled"
    )
    assert all(call[3]["X-Agentic-Perf-Mutation-Scope"] == "leader" for call in calls)


@pytest.mark.asyncio
async def test_deposed_claim_reconciliation_aborts_when_lease_is_lost(
    monkeypatch: pytest.MonkeyPatch,
):
    current_session = str(uuid4())
    monkeypatch.setenv("AGENTIC_PERF_API_TOKEN", "service")
    monkeypatch.setenv("AGENTIC_PERF_ORCHESTRATOR_SESSION_ID", current_session)
    monkeypatch.setenv("AGENTIC_PERF_ORCHESTRATOR_EPOCH", "9")
    entered = asyncio.Event()

    class HangingClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def patch(self, _url, *, json):
            entered.set()
            await asyncio.sleep(60)

    monkeypatch.setattr("orchestrator.main.AuditedAsyncHTTPClient", HangingClient)
    ticket = {
        "id": "PERF-deposed",
        "status": "awaiting_provision",
        "custom_fields": {
            "claim": {
                "claim_id": "claim-old",
                "session_id": str(uuid4()),
                "epoch": 4,
                "expires": (
                    datetime.now(timezone.utc) + timedelta(minutes=5)
                ).isoformat(),
            }
        },
    }
    cancelled = threading.Event()
    task = asyncio.create_task(
        _reconcile_deposed_claims(
            "http://store",
            [ticket],
            cancelled=cancelled.is_set,
        )
    )

    await asyncio.wait_for(entered.wait(), timeout=1)
    started = time.monotonic()
    cancelled.set()
    with pytest.raises(LeaderLeaseLost):
        await asyncio.wait_for(task, timeout=1)

    assert time.monotonic() - started < 0.5


@pytest.mark.asyncio
async def test_deposed_claim_reconciliation_checks_lease_after_completed_request(
    monkeypatch: pytest.MonkeyPatch,
):
    current_session = str(uuid4())
    monkeypatch.setenv("AGENTIC_PERF_API_TOKEN", "service")
    monkeypatch.setenv("AGENTIC_PERF_ORCHESTRATOR_SESSION_ID", current_session)
    monkeypatch.setenv("AGENTIC_PERF_ORCHESTRATOR_EPOCH", "9")
    cancelled = threading.Event()
    calls: list[str] = []

    class Response:
        status_code = 200
        text = ""

    class CompletingClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def patch(self, url, *, json):
            calls.append(url)
            cancelled.set()
            return Response()

    monkeypatch.setattr("orchestrator.main.AuditedAsyncHTTPClient", CompletingClient)
    ticket = {
        "id": "PERF-deposed",
        "status": "awaiting_provision",
        "custom_fields": {
            "claim": {
                "claim_id": "claim-old",
                "session_id": str(uuid4()),
                "epoch": 4,
                "expires": (
                    datetime.now(timezone.utc) + timedelta(minutes=5)
                ).isoformat(),
            }
        },
    }

    with pytest.raises(LeaderLeaseLost):
        await _reconcile_deposed_claims(
            "http://store",
            [ticket],
            cancelled=cancelled.is_set,
        )

    assert calls == ["http://store/api/v1/tickets/PERF-deposed/fields"]


@pytest.mark.asyncio
async def test_deposed_claim_reconciliation_checks_lease_before_queued_request_starts(
    monkeypatch: pytest.MonkeyPatch,
):
    current_session = str(uuid4())
    monkeypatch.setenv("AGENTIC_PERF_API_TOKEN", "service")
    monkeypatch.setenv("AGENTIC_PERF_ORCHESTRATOR_SESSION_ID", current_session)
    monkeypatch.setenv("AGENTIC_PERF_ORCHESTRATOR_EPOCH", "9")
    calls: list[str] = []
    checks = 0

    def cancelled() -> bool:
        nonlocal checks
        checks += 1
        return checks >= 3

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def patch(self, url, *, json):
            calls.append(url)
            raise AssertionError("HTTP request started after lease loss")

    monkeypatch.setattr("orchestrator.main.AuditedAsyncHTTPClient", Client)
    ticket = {
        "id": "PERF-deposed",
        "status": "awaiting_provision",
        "custom_fields": {
            "claim": {
                "claim_id": "claim-old",
                "session_id": str(uuid4()),
                "epoch": 4,
                "expires": (
                    datetime.now(timezone.utc) + timedelta(minutes=5)
                ).isoformat(),
            }
        },
    }

    with pytest.raises(LeaderLeaseLost):
        await _reconcile_deposed_claims(
            "http://store",
            [ticket],
            cancelled=cancelled,
        )

    assert calls == []
    assert checks == 3


@pytest.mark.asyncio
async def test_closed_ticket_clears_deposed_claim_without_guidance_transition(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("AGENTIC_PERF_API_TOKEN", "service")
    monkeypatch.setenv("AGENTIC_PERF_ORCHESTRATOR_SESSION_ID", str(uuid4()))
    monkeypatch.setenv("AGENTIC_PERF_ORCHESTRATOR_EPOCH", "10")
    calls: list[tuple[str, str, dict]] = []

    class Response:
        status_code = 200
        text = ""

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def patch(self, url, *, json):
            calls.append(("patch", url, json))
            return Response()

        async def post(self, url, *, json):
            calls.append(("post", url, json))
            return Response()

    monkeypatch.setattr("orchestrator.main.AuditedAsyncHTTPClient", Client)
    ticket = {
        "id": "PERF-closed",
        "status": "closed",
        "custom_fields": {
            "claim": {
                "claim_id": "claim-old",
                "session_id": str(uuid4()),
                "epoch": 4,
                "expires": (
                    datetime.now(timezone.utc) - timedelta(minutes=1)
                ).isoformat(),
            }
        },
    }

    blocked, failed = await _reconcile_deposed_claims("http://store", [ticket])

    assert blocked == {"PERF-closed"}
    assert failed == set()
    assert [call[0] for call in calls] == ["patch"]
    assert calls[0][2]["fields"]["claim"] is None
    assert calls[0][2]["fields"][DEPOSED_CLAIM_RECONCILIATION_FIELD]["status"] == (
        "reconciled"
    )


@pytest.mark.asyncio
async def test_closed_ticket_reconciliation_propagates_lease_loss(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("AGENTIC_PERF_ORCHESTRATOR_SESSION_ID", str(uuid4()))
    monkeypatch.setenv("AGENTIC_PERF_ORCHESTRATOR_EPOCH", "10")
    cancelled = threading.Event()

    class Response:
        status_code = 200
        text = ""

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def patch(self, _url, *, json):
            cancelled.set()
            return Response()

    monkeypatch.setattr("orchestrator.main.AuditedAsyncHTTPClient", Client)
    ticket = {
        "id": "PERF-closed",
        "status": "closed",
        "custom_fields": {
            "claim": {
                "claim_id": "claim-old",
                "session_id": str(uuid4()),
                "epoch": 4,
                "expires": (
                    datetime.now(timezone.utc) - timedelta(minutes=1)
                ).isoformat(),
            }
        },
    }

    with pytest.raises(LeaderLeaseLost):
        await _reconcile_deposed_claims(
            "http://store",
            [ticket],
            cancelled=cancelled.is_set,
        )


@pytest.mark.asyncio
async def test_deposed_retrospective_claim_is_closed_before_claim_clear(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("AGENTIC_PERF_ORCHESTRATOR_SESSION_ID", str(uuid4()))
    monkeypatch.setenv("AGENTIC_PERF_ORCHESTRATOR_EPOCH", "10")
    calls: list[tuple[str, str, dict]] = []

    class Response:
        status_code = 200
        text = ""

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def patch(self, url, *, json):
            calls.append(("patch", url, json))
            return Response()

        async def post(self, url, *, json):
            calls.append(("post", url, json))
            return Response()

    monkeypatch.setattr("orchestrator.main.AuditedAsyncHTTPClient", Client)
    ticket = {
        "id": "PERF-retro",
        "status": "retrospective_pending",
        "custom_fields": {
            "claim": {
                "claim_id": "claim-old",
                "session_id": str(uuid4()),
                "epoch": 4,
                "expires": (
                    datetime.now(timezone.utc) - timedelta(minutes=1)
                ).isoformat(),
            }
        },
    }

    blocked, failed = await _reconcile_deposed_claims("http://store", [ticket])

    assert blocked == {"PERF-retro"}
    assert failed == set()
    assert [call[0] for call in calls] == ["post", "patch"]
    assert calls[0][2]["status"] == "closed"
    assert calls[1][2]["fields"]["claim"] is None
    assert calls[1][2]["fields"][DEPOSED_CLAIM_RECONCILIATION_FIELD]["status"] == (
        "reconciled"
    )


@pytest.mark.asyncio
async def test_retrospective_claim_clear_failure_remains_retryable(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("AGENTIC_PERF_ORCHESTRATOR_SESSION_ID", str(uuid4()))
    monkeypatch.setenv("AGENTIC_PERF_ORCHESTRATOR_EPOCH", "10")

    class Response:
        def __init__(self, status_code: int = 200):
            self.status_code = status_code
            self.text = "unavailable" if status_code >= 400 else ""

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def patch(self, _url, *, json):
            return Response(503)

        async def post(self, _url, *, json):
            return Response()

    monkeypatch.setattr("orchestrator.main.AuditedAsyncHTTPClient", Client)
    ticket = {
        "id": "PERF-retro",
        "status": "retrospective_pending",
        "custom_fields": {
            "claim": {
                "claim_id": "claim-old",
                "session_id": str(uuid4()),
                "epoch": 4,
                "expires": (
                    datetime.now(timezone.utc) - timedelta(minutes=1)
                ).isoformat(),
            }
        },
    }

    blocked, failed = await _reconcile_deposed_claims("http://store", [ticket])

    assert blocked == {"PERF-retro"}
    assert failed == {"PERF-retro"}


@pytest.mark.asyncio
async def test_closed_ticket_reconciliation_failure_is_retryable(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("AGENTIC_PERF_ORCHESTRATOR_SESSION_ID", str(uuid4()))
    monkeypatch.setenv("AGENTIC_PERF_ORCHESTRATOR_EPOCH", "10")

    class Response:
        status_code = 503
        text = "temporarily unavailable"

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def patch(self, _url, *, json):
            return Response()

    monkeypatch.setattr("orchestrator.main.AuditedAsyncHTTPClient", Client)
    ticket = {
        "id": "PERF-closed",
        "status": "closed",
        "custom_fields": {
            "claim": {
                "claim_id": "claim-old",
                "session_id": str(uuid4()),
                "epoch": 4,
                "expires": (
                    datetime.now(timezone.utc) - timedelta(minutes=1)
                ).isoformat(),
            }
        },
    }

    blocked, failed = await _reconcile_deposed_claims("http://store", [ticket])

    assert blocked == {"PERF-closed"}
    assert failed == {"PERF-closed"}


@pytest.mark.parametrize("claim_kind", ["bad_expiry", "bad_epoch", "string", "list"])
@pytest.mark.asyncio
async def test_closed_ticket_reconciles_malformed_claim(
    monkeypatch: pytest.MonkeyPatch, claim_kind: str
):
    session_id = str(uuid4())
    monkeypatch.setenv("AGENTIC_PERF_ORCHESTRATOR_SESSION_ID", session_id)
    monkeypatch.setenv("AGENTIC_PERF_ORCHESTRATOR_EPOCH", "10")
    calls = []

    class Response:
        status_code = 200
        text = ""

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def patch(self, url, *, json):
            calls.append((url, json))
            return Response()

    monkeypatch.setattr("orchestrator.main.AuditedAsyncHTTPClient", Client)
    if claim_kind == "string":
        claim = "corrupt"
    elif claim_kind == "list":
        claim = ["corrupt"]
    else:
        claim = {
            "claim_id": "claim-old",
            "session_id": session_id,
            "epoch": 10,
            "expires": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        }
        claim["expires" if claim_kind == "bad_expiry" else "epoch"] = (
            "unknown" if claim_kind == "bad_expiry" else "bad"
        )
    ticket = {
        "id": "PERF-closed",
        "status": "closed",
        "custom_fields": {"claim": claim},
    }

    blocked, failed = await _reconcile_deposed_claims("http://store", [ticket])

    assert blocked == {"PERF-closed"}
    assert failed == set()
    assert len(calls) == 1
    assert calls[0][1]["fields"]["claim"] is None


def test_reconciliation_marker_is_not_agent_progress():
    assert _agent_progress_fields(
        {
            DEPOSED_CLAIM_RECONCILIATION_FIELD: {"status": "reconciled"},
            "benchmark_result": {"score": 1},
        }
    ) == {"benchmark_result": {"score": 1}}


def test_lease_loss_gate_deposes_dispatcher_bound_after_loss():
    dispatcher = MagicMock()
    gate = _LeaseLossGate()

    gate.mark_deposed()
    gate.bind(dispatcher)

    dispatcher.mark_deposed.assert_called_once_with()
    with pytest.raises(LeaderLeaseLost, match="leader lease lost"):
        gate.raise_if_lost()


@pytest.mark.asyncio
async def test_lease_loss_starts_drain_independently_of_poll_task():
    drain_started = asyncio.Event()
    ordering: list[str] = []

    class Dispatcher:
        def mark_deposed(self):
            ordering.append("deposed")

        async def drain_for_lease_loss(self, _timeout):
            ordering.append("drain-started")
            drain_started.set()

    gate = _LeaseLossGate()
    dispatcher = Dispatcher()
    gate.bind(dispatcher)
    gate.bind_drain_starter(
        lambda: asyncio.create_task(dispatcher.drain_for_lease_loss(1.0))
    )
    gate.mark_deposed()

    await asyncio.wait_for(drain_started.wait(), 0.5)
    assert ordering == ["deposed", "drain-started"]


@pytest.mark.asyncio
async def test_poll_sleep_wakes_immediately_on_lease_loss():
    import orchestrator.main as orchestrator_main

    gate = _LeaseLossGate()
    sleeper = asyncio.create_task(orchestrator_main._sleep_or_lease_loss(60, gate))
    await asyncio.sleep(0)
    started = time.monotonic()
    gate.mark_deposed()
    await asyncio.wait_for(sleeper, 0.5)

    assert time.monotonic() - started < 0.5


@pytest.mark.asyncio
async def test_local_renewal_timeout_consumes_retry_budget(caplog):
    lease = _RenewalLease(["hang"], ttl_seconds=1.0)
    lost = []
    task = asyncio.create_task(
        _renew_leader_lease(lease, 0, on_lost=lambda: lost.append(True))
    )

    await asyncio.wait_for(lease.recovered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(lease.attempts) == 2
    assert lost == []
    assert lease.released == 0
    assert "transient failure 1" in caplog.text
    assert "TimeoutError" in caplog.text


@pytest.mark.parametrize(
    "error_type",
    [
        httpx.ReadError,
        httpx.WriteError,
        httpx.WriteTimeout,
        httpx.RemoteProtocolError,
    ],
)
@pytest.mark.asyncio
async def test_httpx_transport_errors_are_retryable(error_type):
    request = httpx.Request("POST", "http://state-store/renew")
    lease = _RenewalLease([error_type("temporary transport failure", request=request)])
    lost = []
    task = asyncio.create_task(
        _renew_leader_lease(lease, 0, on_lost=lambda: lost.append(True))
    )

    await asyncio.wait_for(lease.recovered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(lease.attempts) == 2
    assert lost == []
    assert lease.released == 0


@pytest.mark.parametrize("status_code", [408, 425, 429, 500, 503])
@pytest.mark.asyncio
async def test_http_transient_status_lease_renewal_is_retryable(status_code: int):
    lease = _RenewalLease([_lease_http_error(status_code)])
    lost = []
    task = asyncio.create_task(
        _renew_leader_lease(lease, 0, on_lost=lambda: lost.append(True))
    )

    await asyncio.wait_for(lease.recovered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(lease.attempts) == 2
    assert lost == []
    assert lease.released == 0


@pytest.mark.parametrize("status_code", [401, 403, 409, 410])
@pytest.mark.asyncio
async def test_http_rejection_lease_renewal_fails_immediately(status_code: int):
    lease = _RenewalLease([_lease_http_error(status_code)])
    lost = []

    with pytest.raises(RuntimeError, match="orchestrator leader lease lost"):
        await _renew_leader_lease(lease, 0, on_lost=lambda: lost.append(True))

    assert len(lease.attempts) == 1
    assert lost == [True]
    assert lease.released == 0


@pytest.mark.asyncio
async def test_transient_failures_retry_until_confirmed_deadline():
    request = httpx.Request("POST", "http://state-store/renew")
    lease = _RenewalLease(
        [httpx.ConnectError("temporary connection failure", request=request)] * 20,
        ttl_seconds=0.5,
    )
    lost = []

    with pytest.raises(RuntimeError, match="orchestrator leader lease lost"):
        await _renew_leader_lease(lease, 0.05, on_lost=lambda: lost.append(True))

    assert len(lease.attempts) > 3
    assert lost == [True]
    assert lease.released == 0


@pytest.mark.asyncio
async def test_hung_renewal_is_cancelled_before_confirmed_deadline():
    lease = _RenewalLease(["hang"] * 20, ttl_seconds=0.5)
    deadline = lease.confirmed_deadline
    lost_at = []

    with pytest.raises(RuntimeError, match="orchestrator leader lease lost"):
        await _renew_leader_lease(
            lease,
            0,
            on_lost=lambda: lost_at.append(asyncio.get_running_loop().time()),
        )

    assert len(lease.attempts) > 1
    assert lease.cancelled
    assert lost_at[0] < deadline
    assert lease.released == 0


@pytest.mark.asyncio
async def test_slow_success_schedules_another_attempt_before_confirmed_deadline(
    monkeypatch: pytest.MonkeyPatch,
):
    class SlowRenewalLease(_RenewalLease):
        def __init__(self):
            super().__init__(ttl_seconds=0.8)
            self.successful_deadline = None
            self.third_started = asyncio.Event()

        async def renew(self):
            loop = asyncio.get_running_loop()
            request_started = loop.time()
            self.attempts.append(request_started)
            if len(self.attempts) == 1:
                request = httpx.Request("POST", "http://state-store/renew")
                raise httpx.ReadError("temporary failure", request=request)
            if len(self.attempts) == 2:
                await asyncio.sleep(0.35)
                self.confirmed_deadline = request_started + self.ttl_seconds
                self.successful_deadline = self.confirmed_deadline
                self.recovered.set()
                return

            self.third_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    lease = SlowRenewalLease()
    lost = []
    delayed_recovery_logs = []

    def delay_recovery_log(message, *_args, **_kwargs):
        if "renewal recovered" in message:
            delayed_recovery_logs.append(True)
            time.sleep(0.25)

    monkeypatch.setattr("orchestrator.main.logger.info", delay_recovery_log)
    task = asyncio.create_task(
        _renew_leader_lease(lease, 0.2, on_lost=lambda: lost.append(True))
    )
    try:
        await asyncio.wait_for(lease.third_started.wait(), timeout=1.5)
        third_started_at = lease.attempts[2]
        assert third_started_at < lease.successful_deadline
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert delayed_recovery_logs == [True]
    assert lease.cancelled
    assert lost == []
    assert lease.released == 0


class _PollLoopLease:
    def __init__(self, *_args, **_kwargs):
        self.epoch = None
        self.session_id = uuid4()
        self.release_count = 0
        self.ttl_seconds = 30
        self.confirmed_deadline = asyncio.get_running_loop().time() + self.ttl_seconds

    async def acquire(self, **kwargs):
        self.epoch = 7
        self.confirmed_deadline = asyncio.get_running_loop().time() + self.ttl_seconds
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

    # Record even absent keys so monkeypatch removes values assigned by startup.
    monkeypatch.setenv("AGENTIC_PERF_ORCHESTRATOR_SESSION_ID", "")
    monkeypatch.setenv("AGENTIC_PERF_ORCHESTRATOR_EPOCH", "")
    config = _poll_config()
    config.poll_interval = 0.02
    config.stale_task_timeout = 0
    observed = asyncio.get_running_loop().create_future()
    dispatchers = []
    phases = []
    closed_ticket = {"id": "PERF-closed", "status": "closed", "custom_fields": {}}
    fetch_closed = AsyncMock(
        return_value={"tickets": [closed_ticket], "next_cursor": None}
    )
    reconcile_claims = AsyncMock(return_value=(set(), set()))

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

        def is_deposed(self):
            return False

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
    monkeypatch.setattr(
        orchestrator_main, "fetch_ticket_claim_reconciliation_page", fetch_closed
    )
    monkeypatch.setattr(
        orchestrator_main, "_reconcile_deposed_claims", reconcile_claims
    )
    telemetry_module = ModuleType("providers.telemetry")
    telemetry_module.setup_telemetry = lambda **_kwargs: None
    monkeypatch.setitem(sys.modules, "providers.telemetry", telemetry_module)
    monkeypatch.setattr("providers.redaction.get_shared_redactor", lambda: object())

    lease = Lease()
    lease_state = {"renewer": None, "started": threading.Event()}
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
        fetch_closed.assert_awaited_once_with("http://state-store", after=None)
        assert any(
            call.args == ("http://state-store", [closed_ticket])
            and callable(call.kwargs.get("cancelled"))
            for call in reconcile_claims.await_args_list
        )
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_poll_loop_repeated_cancellation_finishes_teardown(monkeypatch):
    import orchestrator.main as orchestrator_main

    monkeypatch.setenv("AGENTIC_PERF_ORCHESTRATOR_SESSION_ID", "")
    monkeypatch.setenv("AGENTIC_PERF_ORCHESTRATOR_EPOCH", "")
    config = _poll_config()
    config.poll_interval = 0.02
    config.stale_task_timeout = 0
    config.raw["external_mcp_servers"] = [
        {"name": "arcaflow", "transport": "stdio", "command": ["fake"]}
    ]
    polling = asyncio.Event()
    shutdown_started = asyncio.Event()
    allow_shutdown = asyncio.Event()
    finished = []

    class Lease(_PollLoopLease):
        async def renew(self):
            return {"epoch": self.epoch}

    class Events:
        def __init__(self, **_kwargs):
            pass

        def close(self):
            finished.append("events")

    class Dispatcher:
        def __init__(self, *_args, **kwargs):
            self._session_id = kwargs["session_id"]
            self._fencing_epoch = kwargs["fencing_epoch"]

        def reconcile_handoff_blocked(self, _tickets):
            pass

        def is_deposed(self):
            return False

        async def shutdown(self):
            shutdown_started.set()
            await allow_shutdown.wait()
            finished.append("dispatcher")

    async def fetch_tickets(_url):
        polling.set()
        return []

    async def renew_lease(_lease, _interval, _on_loss, started):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.append("lease")

    async def disconnect():
        finished.append("mcp")

    mcp = MagicMock()
    mcp.connect_command = AsyncMock()
    mcp.disconnect = AsyncMock(side_effect=disconnect)
    monkeypatch.setattr("agents.mcp_client.AgentMCPClient", lambda: mcp)
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
    monkeypatch.setattr(orchestrator_main, "_validate_models", AsyncMock())
    monkeypatch.setattr(orchestrator_main, "EventBus", Events)
    monkeypatch.setattr(orchestrator_main, "Dispatcher", Dispatcher)
    monkeypatch.setattr(
        orchestrator_main, "record_orchestrator_status", lambda *_a, **_k: None
    )
    monkeypatch.setattr(orchestrator_main, "_process_stop_requests", AsyncMock())
    monkeypatch.setattr(orchestrator_main, "_sweep_trace_spools", lambda: None)
    monkeypatch.setattr(orchestrator_main, "fetch_all_tickets", fetch_tickets)
    monkeypatch.setattr(
        orchestrator_main,
        "fetch_ticket_claim_reconciliation_page",
        AsyncMock(return_value={"tickets": [], "next_cursor": None}),
    )
    monkeypatch.setattr(orchestrator_main, "_renew_leader_lease", renew_lease)
    telemetry_module = ModuleType("providers.telemetry")
    telemetry_module.setup_telemetry = lambda **_kwargs: None
    monkeypatch.setitem(sys.modules, "providers.telemetry", telemetry_module)
    monkeypatch.setattr("providers.redaction.get_shared_redactor", lambda: object())

    lease_state = {"renewer": None, "started": threading.Event()}
    task = asyncio.create_task(
        _poll_loop_after_lease(config, Lease(), _LeaseLossGate(), lease_state)
    )
    await asyncio.wait_for(polling.wait(), 5)
    task.cancel()
    await asyncio.wait_for(shutdown_started.wait(), 5)
    task.cancel()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    allow_shutdown.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished == ["dispatcher", "lease", "mcp", "events"]
    mcp.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_lease_loss_drains_dispatcher_before_releasing_lease(monkeypatch):
    import orchestrator.main as orchestrator_main

    monkeypatch.setenv("AGENTIC_PERF_ORCHESTRATOR_SESSION_ID", "")
    monkeypatch.setenv("AGENTIC_PERF_ORCHESTRATOR_EPOCH", "")
    config = _poll_config()
    config.poll_interval = 0.02
    config.leader_lease_recovery_delay_seconds = 0.01
    config.stale_task_timeout = 0
    drain_started = asyncio.Event()
    allow_drain = asyncio.Event()
    release_called = asyncio.Event()
    order = []

    class Lease(_PollLoopLease):
        async def release(self):
            order.append("release")
            await super().release()
            release_called.set()

    class Events:
        def __init__(self, **_kwargs):
            pass

        def close(self):
            order.append("events")

    class Dispatcher:
        def __init__(self, *_args, **kwargs):
            self._session_id = kwargs["session_id"]
            self._fencing_epoch = kwargs["fencing_epoch"]
            self._deposed = False

        def reconcile_handoff_blocked(self, _tickets):
            pass

        def mark_deposed(self):
            self._deposed = True
            order.append("deposed")

        def is_deposed(self):
            return self._deposed

        async def drain_for_lease_loss(self, _timeout):
            order.append("drain-started")
            drain_started.set()
            await allow_drain.wait()
            order.append("drain-finished")

        async def shutdown(self):
            order.append("dispatcher-shutdown")

    async def fetch_tickets(_url):
        return []

    async def lose_lease(_lease, _interval, on_loss, started):
        started.set()
        try:
            await asyncio.sleep(0.02)
            on_loss()
            raise LeaderLeaseLost("lease lost")
        finally:
            order.append("renewer-stopped")

    async def no_op(*_args, **_kwargs):
        pass

    monkeypatch.setattr(
        "orchestrator.leader_lease.LeaderLeaseClient", lambda *_a, **_k: Lease()
    )
    monkeypatch.setattr("orchestrator.config.write_effective_config", lambda *_a: None)
    monkeypatch.setattr(
        orchestrator_main, "_ensure_state_store_environment", lambda *_a: None
    )
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
    monkeypatch.setattr(orchestrator_main, "_validate_models", AsyncMock())
    monkeypatch.setattr(orchestrator_main, "EventBus", Events)
    monkeypatch.setattr(orchestrator_main, "Dispatcher", Dispatcher)
    monkeypatch.setattr(
        orchestrator_main, "record_orchestrator_status", lambda *_a, **_k: None
    )
    monkeypatch.setattr(orchestrator_main, "_process_stop_requests", AsyncMock())
    monkeypatch.setattr(orchestrator_main, "_sweep_orphaned_leases", no_op)
    monkeypatch.setattr(orchestrator_main, "_sweep_trace_spools", lambda: None)
    monkeypatch.setattr(orchestrator_main, "fetch_all_tickets", fetch_tickets)
    monkeypatch.setattr(
        orchestrator_main,
        "fetch_ticket_claim_reconciliation_page",
        AsyncMock(return_value={"tickets": [], "next_cursor": None}),
    )
    monkeypatch.setattr(
        orchestrator_main,
        "_reconcile_deposed_claims",
        AsyncMock(return_value=(set(), set())),
    )
    monkeypatch.setattr(orchestrator_main, "_renew_leader_lease", lose_lease)
    telemetry_module = ModuleType("providers.telemetry")
    telemetry_module.setup_telemetry = lambda **_kwargs: None
    monkeypatch.setitem(sys.modules, "providers.telemetry", telemetry_module)
    monkeypatch.setattr("providers.redaction.get_shared_redactor", lambda: object())

    task = asyncio.create_task(poll_loop(config))
    try:
        await asyncio.wait_for(drain_started.wait(), 1)
        assert "release" not in order

        task.cancel()
        await asyncio.sleep(0)
        allow_drain.set()
        await asyncio.wait_for(release_called.wait(), 5)
        with pytest.raises(asyncio.CancelledError):
            await task

        assert order.index("drain-finished") < order.index("release")
        assert order.index("dispatcher-shutdown") < order.index("release")
    finally:
        if not task.done():
            task.cancel()
        allow_drain.set()
        try:
            await asyncio.wait_for(task, 2)
        except (asyncio.CancelledError, LeaderLeaseLost):
            pass
        except TimeoutError:
            pass


@pytest.mark.asyncio
async def test_poll_loop_repeated_cancellation_finishes_leader_release(monkeypatch):
    import orchestrator.main as orchestrator_main

    started = asyncio.Event()
    releasing = asyncio.Event()
    allow_release = asyncio.Event()
    lease = MagicMock()

    async def release():
        releasing.set()
        await allow_release.wait()

    async def run_until_cancelled(*_args):
        started.set()
        await asyncio.Event().wait()

    lease.release = AsyncMock(side_effect=release)
    monkeypatch.setattr(
        "orchestrator.leader_lease.LeaderLeaseClient", lambda *_a, **_k: lease
    )
    monkeypatch.setattr("orchestrator.config.write_effective_config", lambda *_a: None)
    monkeypatch.setattr(
        orchestrator_main, "_ensure_state_store_environment", lambda *_a: None
    )
    monkeypatch.setattr(
        orchestrator_main, "_poll_loop_after_lease", run_until_cancelled
    )
    task = asyncio.create_task(poll_loop(_poll_config()))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    await asyncio.wait_for(releasing.wait(), 2)
    task.cancel()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    allow_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    lease.release.assert_awaited_once()


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


@pytest.mark.asyncio
async def test_poll_loop_reacquires_after_lease_loss(monkeypatch):
    lease = _PollLoopLease()
    config = _poll_config()
    config.leader_lease_recovery_delay_seconds = 0
    attempts = 0
    reacquired = asyncio.Event()

    async def run_attempt(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise LeaderLeaseLost("lease lost")
        reacquired.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        "orchestrator.leader_lease.LeaderLeaseClient", lambda *_a, **_k: lease
    )
    monkeypatch.setattr("orchestrator.main._poll_loop_after_lease", run_attempt)
    task = asyncio.create_task(poll_loop(config))
    await asyncio.wait_for(reacquired.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert attempts == 2
    assert lease.release_count >= 1


@pytest.mark.asyncio
async def test_lease_loss_drain_stops_before_confirmed_deadline():
    import orchestrator.main as orchestrator_main

    config = _poll_config()
    config.leader_lease_drain_timeout_seconds = 20.0
    deadline = asyncio.get_running_loop().time() + 0.75
    lease = SimpleNamespace(confirmed_deadline=deadline)

    timeout = orchestrator_main._lease_loss_drain_timeout(config, lease)

    assert 0.0 <= timeout <= 0.5
    assert timeout < deadline - asyncio.get_running_loop().time()


@pytest.mark.asyncio
async def test_lease_loss_drain_is_immediate_after_confirmed_deadline():
    import orchestrator.main as orchestrator_main

    config = _poll_config()
    config.leader_lease_drain_timeout_seconds = 20.0
    lease = SimpleNamespace(confirmed_deadline=asyncio.get_running_loop().time() - 0.01)

    assert orchestrator_main._lease_loss_drain_timeout(config, lease) == 0.0


@pytest.mark.asyncio
async def test_cleanup_cancellation_preserves_unrecoverable_lease_loss():
    import orchestrator.main as orchestrator_main

    started = asyncio.Event()
    release = asyncio.Event()

    async def cleanup() -> None:
        started.set()
        await release.wait()
        raise LeaderLeaseUnrecoverable(("PERF-stuck",))

    waiter = asyncio.create_task(
        orchestrator_main._await_cleanup(
            asyncio.create_task(cleanup()),
            preserve_exceptions=(LeaderLeaseUnrecoverable,),
        )
    )
    await started.wait()
    waiter.cancel()
    await asyncio.sleep(0)
    release.set()

    with pytest.raises(LeaderLeaseUnrecoverable) as exc_info:
        await waiter

    assert exc_info.value.ticket_ids == ("PERF-stuck",)


@pytest.mark.asyncio
async def test_poll_loop_exits_instead_of_reacquiring_after_surviving_drain(
    monkeypatch: pytest.MonkeyPatch,
):
    import orchestrator.main as orchestrator_main

    lease = _PollLoopLease()
    config = _poll_config()
    config.leader_lease_recovery_delay_seconds = 0
    attempts = 0
    terminated: list[tuple[str, ...]] = []

    async def run_attempt(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise LeaderLeaseUnrecoverable(("PERF-stuck",))

    monkeypatch.setattr(
        "orchestrator.leader_lease.LeaderLeaseClient", lambda *_a, **_k: lease
    )
    monkeypatch.setattr(orchestrator_main, "_poll_loop_after_lease", run_attempt)
    monkeypatch.setattr(
        orchestrator_main,
        "_terminate_process_after_lease_loss",
        lambda ticket_ids: terminated.append(ticket_ids),
    )

    with pytest.raises(LeaderLeaseUnrecoverable) as exc_info:
        await poll_loop(config)

    assert exc_info.value.ticket_ids == ("PERF-stuck",)
    assert terminated == [("PERF-stuck",)]
    assert attempts == 1
    assert lease.release_count == 0


@pytest.mark.asyncio
async def test_poll_loop_uses_fresh_lease_session_after_recovery(monkeypatch):
    config = _poll_config()
    config.leader_lease_recovery_delay_seconds = 0
    leases = []
    sessions = []
    reacquired = asyncio.Event()

    def make_lease(*_args, **_kwargs):
        lease = _PollLoopLease()
        leases.append(lease)
        return lease

    async def run_attempt(_config, lease, _gate, _state):
        sessions.append(lease.session_id)
        if len(sessions) == 1:
            raise LeaderLeaseLost("lease lost")
        reacquired.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("orchestrator.leader_lease.LeaderLeaseClient", make_lease)
    monkeypatch.setattr("orchestrator.main._poll_loop_after_lease", run_attempt)
    task = asyncio.create_task(poll_loop(config))
    await asyncio.wait_for(reacquired.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(leases) == 2
    assert sessions[0] != sessions[1]
    assert leases[0].release_count == 1


@pytest.mark.asyncio
async def test_poll_loop_reacquires_after_lease_acquisition_exhaustion(monkeypatch):
    lease = _PollLoopLease()
    config = _poll_config()
    config.leader_lease_recovery_delay_seconds = 0
    attempts = 0
    reacquired = asyncio.Event()

    async def run_attempt(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise LeaderLeaseUnavailable("store temporarily unavailable")
        reacquired.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        "orchestrator.leader_lease.LeaderLeaseClient", lambda *_a, **_k: lease
    )
    monkeypatch.setattr("orchestrator.main._poll_loop_after_lease", run_attempt)
    task = asyncio.create_task(poll_loop(config))
    await asyncio.wait_for(reacquired.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert attempts == 2
    assert lease.release_count >= 1


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


def test_lease_cache_unchanged_when_disk_write_fails(tmp_path, monkeypatch):
    store = TicketStore(persist_dir=tmp_path)
    lease = store.acquire_orchestrator_lease(_request())

    def failing_write(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(store._system_filesystem, "write", failing_write)
    with pytest.raises(OSError):
        store.renew_orchestrator_lease(lease.session_id, lease.epoch, 30)

    current = store.get_orchestrator_lease()
    assert current is not None
    assert current.expires_at == lease.expires_at


@pytest.mark.asyncio
async def test_public_health_does_not_disclose_lease_holder_identity(tmp_path):
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
    result = await health(request)
    assert result["orchestrator_lease"] == {"active": True}
    assert result["total"] == 0
    assert all(count == 0 for count in result["ticket_counts"].values())


@pytest.mark.asyncio
async def test_public_health_counts_tickets_without_listing_them(tmp_path, monkeypatch):
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

    result = await health(request)

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
