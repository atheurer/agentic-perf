from __future__ import annotations

import asyncio
import sys
from copy import deepcopy
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest

from orchestrator.retry_guard import (
    DISPATCH_RETRY_LIMIT,
    HANDOFF_RETRY_LIMIT,
    RETRY_STATE_FIELD,
    clear_retry_entry,
    prune_stale_retry_entries,
    record_retry_failure,
    retry_is_suppressed,
)


def test_handoff_backoff_is_exponential_and_bounded() -> None:
    custom_fields: dict = {}
    delays = []
    for attempt in range(1, HANDOFF_RETRY_LIMIT + 1):
        state, exhausted, recorded_attempt = record_retry_failure(
            custom_fields,
            "handoff",
            "executing_benchmark",
            now=1000.0 + attempt * 400,
            retry_limit=HANDOFF_RETRY_LIMIT,
            base_seconds=5.0,
            max_seconds=300.0,
        )
        assert state is not None
        custom_fields[RETRY_STATE_FIELD] = state
        assert recorded_attempt == attempt
        assert exhausted is (attempt == HANDOFF_RETRY_LIMIT)
        next_retry_at = state["handoff"]["next_retry_at"]
        if next_retry_at is not None:
            delays.append(next_retry_at - (1000.0 + attempt * 400))

    assert delays[:7] == [5.0, 10.0, 20.0, 40.0, 80.0, 160.0, 300.0]
    assert delays[-1] == 300.0
    assert retry_is_suppressed(
        custom_fields, "handoff", "executing_benchmark", now=100000.0
    )


def test_retry_due_and_status_change_clears_the_circuit_breaker() -> None:
    state, exhausted, _ = record_retry_failure(
        {},
        "dispatch",
        "executing_benchmark",
        now=20.0,
        retry_limit=DISPATCH_RETRY_LIMIT,
        base_seconds=5.0,
        max_seconds=300.0,
    )
    assert not exhausted
    custom_fields = {RETRY_STATE_FIELD: state}
    assert retry_is_suppressed(
        custom_fields, "dispatch", "executing_benchmark", now=24.99
    )
    assert not retry_is_suppressed(
        custom_fields, "dispatch", "executing_benchmark", now=25.0
    )
    assert not retry_is_suppressed(
        custom_fields, "dispatch", "awaiting_customer_guidance", now=21.0
    )
    assert (
        prune_stale_retry_entries(custom_fields, "awaiting_customer_guidance") is None
    )


def test_clear_retry_category_preserves_other_category() -> None:
    custom_fields = {
        RETRY_STATE_FIELD: {
            "handoff": {"status": "executing_benchmark", "attempts": 2},
            "dispatch": {"status": "executing_benchmark", "attempts": 3},
        }
    }
    assert clear_retry_entry(custom_fields, "handoff") == {
        "dispatch": {"status": "executing_benchmark", "attempts": 3}
    }


@pytest.mark.asyncio
async def test_dispatch_failure_cap_persists_human_review_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import orchestrator.main as main

    monkeypatch.setenv("AGENTIC_PERF_API_TOKEN", "api-token")
    monkeypatch.setenv(
        "AGENTIC_PERF_ORCHESTRATOR_SESSION_ID",
        "00000000-0000-0000-0000-000000000001",
    )
    monkeypatch.setenv("AGENTIC_PERF_ORCHESTRATOR_EPOCH", "2")

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    ticket_response = MagicMock(status_code=200)
    ticket_response.json.return_value = {
        "status": "executing_benchmark",
        "custom_fields": {
            RETRY_STATE_FIELD: {
                "dispatch": {
                    "status": "executing_benchmark",
                    "attempts": DISPATCH_RETRY_LIMIT - 1,
                    "next_retry_at": 0,
                    "exhausted": False,
                }
            }
        },
    }
    client.get = AsyncMock(return_value=ticket_response)
    client.patch = AsyncMock(return_value=MagicMock(status_code=200))
    client.patch.return_value.raise_for_status = MagicMock()
    client.post = AsyncMock(return_value=MagicMock(status_code=200))
    client.post.return_value.raise_for_status = MagicMock()

    dispatcher = MagicMock()
    dispatcher.is_deposed.return_value = False
    dispatcher.store_url = "http://store"
    dispatcher._trace_contexts = {}

    with patch.object(
        main,
        "AuditedAsyncHTTPClient",
        side_effect=lambda **kwargs: client,
    ) as client_factory:
        await main._record_dispatch_retry_outcome(
            dispatcher,
            "PERF-1",
            "executing_benchmark",
            claim_id="claim-1",
        )

    headers = client_factory.call_args.kwargs["headers"]
    assert headers["X-Agentic-Perf-Orchestrator-Session"].endswith("0001")
    assert headers["X-Agentic-Perf-Claim-Id"] == "claim-1"
    fields = client.patch.call_args.kwargs["json"]["fields"]
    assert fields[RETRY_STATE_FIELD]["dispatch"]["exhausted"] is True
    assert fields[RETRY_STATE_FIELD]["dispatch"]["attempts"] == DISPATCH_RETRY_LIMIT
    assert "Automatic dispatch paused" in client.post.call_args.kwargs["json"]["body"]


@pytest.mark.asyncio
async def test_agent_crash_guidance_transition_carries_ticket_claim() -> None:
    import orchestrator.main as main

    agent = MagicMock()
    agent.run = AsyncMock(side_effect=RuntimeError("MCP initialization failed"))
    agent.close = AsyncMock()

    dispatcher = MagicMock()
    dispatcher._claim_ids = {"PERF-2": "claim-2"}
    dispatcher.create_agent.return_value = agent
    dispatcher.store_url = "http://store"
    dispatcher.events = None
    dispatcher._trace_contexts = {}
    dispatcher.is_deposed.return_value = False
    dispatcher.clear_agent = MagicMock()
    dispatcher.mark_done = AsyncMock()

    transition = AsyncMock()
    with (
        patch.object(main, "_transition_to_guidance", transition),
        patch.object(main, "_record_dispatch_retry_outcome", AsyncMock()),
    ):
        await main.run_agent_task(dispatcher, "executing_benchmark", "PERF-2")

    transition.assert_awaited_once()
    assert transition.await_args.kwargs["claim_id"] == "claim-2"


@pytest.mark.asyncio
async def test_paused_status_clears_retry_limit_for_later_resume() -> None:
    import orchestrator.main as main

    ticket = {
        "id": "PERF-3",
        "status": "awaiting_customer_guidance",
        "custom_fields": {
            RETRY_STATE_FIELD: {
                "dispatch": {
                    "status": "executing_benchmark",
                    "attempts": DISPATCH_RETRY_LIMIT,
                    "exhausted": True,
                }
            }
        },
    }

    persist = AsyncMock(return_value=True)
    with patch.object(main, "_persist_retry_state", persist):
        await main._prune_stale_retry_state("http://store", ticket)

    persist.assert_awaited_once_with("http://store", "PERF-3", None)
    assert ticket["custom_fields"][RETRY_STATE_FIELD] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_path",
    [
        "failure_patch",
        "backoff_patch",
        "recovery_patch",
        "exhaustion_comment",
        "recovery_transport",
    ],
)
async def test_retry_failures_do_not_stop_polling_other_tickets(
    monkeypatch: pytest.MonkeyPatch, failure_path: str
) -> None:
    """Exercise retry PATCH/comment failure from the production poll loop."""
    import orchestrator.main as main
    from orchestrator.config import OrchestratorConfig

    monkeypatch.setattr(main, "_pending_retry_writes", {})
    # The production loop installs its lease identity in os.environ. Register
    # those keys with monkeypatch before startup so this test restores them.
    monkeypatch.setenv("AGENTIC_PERF_ORCHESTRATOR_SESSION_ID", "")
    monkeypatch.setenv("AGENTIC_PERF_ORCHESTRATOR_EPOCH", "")
    config = OrchestratorConfig(
        state_store_url="http://store", raw_config={"llm": {"provider": "mock"}}
    )
    config.poll_interval = 0.01
    config.stale_task_timeout = 0
    dispatcher = MagicMock()
    dispatcher.events = None
    dispatcher.active_tasks.return_value = []
    dispatcher.is_active.return_value = False
    dispatcher.is_handoff_blocked.return_value = False
    dispatcher.try_claim.return_value = False
    dispatcher.shutdown = AsyncMock()
    dispatcher._trace_contexts = {}
    monkeypatch.setattr(main, "Dispatcher", lambda *_args, **_kwargs: dispatcher)
    monkeypatch.setattr(main, "RepoCache", lambda: object())
    monkeypatch.setattr(main, "build_skill_provider", lambda **_kwargs: object())
    monkeypatch.setattr(main, "LocalSecretsProvider", lambda: object())
    monkeypatch.setattr(
        main,
        "_make_llm_provider",
        lambda _config: SimpleNamespace(
            default_timeout=None, reasoning_effort=None, max_tokens=None
        ),
    )
    monkeypatch.setattr(main, "_make_llm_factory", lambda _config: object())
    monkeypatch.setattr(main, "_validate_models", AsyncMock())
    monkeypatch.setattr(main, "EventBus", lambda **_kwargs: MagicMock())
    monkeypatch.setattr(
        main, "record_orchestrator_status", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(main, "_process_stop_requests", AsyncMock())
    monkeypatch.setattr(main, "_sweep_orphaned_leases", AsyncMock())
    monkeypatch.setattr(main, "_sweep_trace_spools", lambda: None)
    telemetry = ModuleType("providers.telemetry")
    telemetry.setup_telemetry = lambda **_kwargs: None
    monkeypatch.setitem(sys.modules, "providers.telemetry", telemetry)
    monkeypatch.setattr("providers.redaction.get_shared_redactor", lambda: object())
    monkeypatch.setattr(
        main,
        "check_handoff",
        lambda _status, ticket: (ticket["id"] == "PERF-healthy", "incomplete results"),
    )
    recover = AsyncMock(return_value=failure_path == "recovery_patch")
    if failure_path == "recovery_transport":
        recover.side_effect = httpx.ConnectError("store unavailable")
    monkeypatch.setattr(main, "_block_handoff_failed", recover)

    # Use real httpx errors so raise_for_status follows the production path.
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    patch_status = 200 if failure_path == "exhaustion_comment" else 503
    client.patch = AsyncMock(
        return_value=httpx.Response(
            patch_status, request=httpx.Request("PATCH", "http://store/fields")
        )
    )
    client.post = AsyncMock(
        return_value=httpx.Response(
            503, request=httpx.Request("POST", "http://store/comments")
        )
    )
    monkeypatch.setattr(main, "AuditedAsyncHTTPClient", lambda **_kwargs: client)
    retry_state = {
        "handoff": {
            "status": "executing_benchmark",
            "attempts": 0
            if failure_path == "backoff_patch"
            else HANDOFF_RETRY_LIMIT - 1,
            "next_retry_at": 0,
            "exhausted": False,
        }
    }
    tickets = [
        {
            "id": "PERF-failing",
            "status": "executing_benchmark",
            "custom_fields": {RETRY_STATE_FIELD: retry_state},
        },
        {"id": "PERF-healthy", "status": "executing_benchmark", "custom_fields": {}},
    ]

    async def patch_fields(_url, *, json):
        if patch_status == 200:
            tickets[0]["custom_fields"].update(deepcopy(json["fields"]))
        return httpx.Response(
            patch_status, request=httpx.Request("PATCH", "http://store/fields")
        )

    client.patch.side_effect = patch_fields
    second_poll = asyncio.Event()
    fetch_count = 0

    async def fetch(_url):
        nonlocal fetch_count
        fetch_count += 1
        if fetch_count >= 2:
            second_poll.set()
        # Each poll receives the old durable metadata while PATCH is failing.
        return deepcopy(tickets)

    monkeypatch.setattr(main, "fetch_all_tickets", fetch)
    lease = SimpleNamespace(
        session_id=uuid4(),
        epoch=None,
        ttl_seconds=config.leader_lease_ttl_seconds,
        confirmed_deadline=(
            asyncio.get_running_loop().time() + config.leader_lease_ttl_seconds
        ),
    )

    async def acquire():
        lease.epoch = 7
        lease.confirmed_deadline = asyncio.get_running_loop().time() + lease.ttl_seconds

    async def renew():
        lease.confirmed_deadline = asyncio.get_running_loop().time() + lease.ttl_seconds

    lease.acquire = acquire
    lease.renew = AsyncMock(side_effect=renew)
    lease.release = AsyncMock()
    lease_state = {"renew_task": None, "started": asyncio.Event()}
    task = asyncio.create_task(
        main._poll_loop_after_lease(config, lease, main._LeaseLossGate(), lease_state)
    )
    try:
        await asyncio.wait_for(second_poll.wait(), timeout=5)
        assert not task.done()
        dispatcher.try_claim.assert_any_call("PERF-healthy", "executing_benchmark")
        assert client.patch.await_count >= 1
        if failure_path != "recovery_patch":
            recover.assert_awaited_once()
        if failure_path in ("failure_patch", "recovery_transport", "backoff_patch"):
            assert client.patch.await_count == 1
            pending = main._pending_retry_writes["PERF-failing"][0]
            if failure_path == "backoff_patch":
                assert pending["handoff"]["attempts"] == 1
                assert pending["handoff"]["exhausted"] is False
                assert pending["handoff"]["next_retry_at"] > main.time.time()
            else:
                assert pending["handoff"]["exhausted"] is True
                assert pending["handoff"]["attempts"] == HANDOFF_RETRY_LIMIT
        if failure_path == "exhaustion_comment":
            assert client.post.await_count >= 1
            state = client.patch.await_args.kwargs["json"]["fields"][RETRY_STATE_FIELD]
            assert state["handoff"]["exhausted"] is True
        dispatcher.shutdown.assert_not_awaited()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_directive_normalization_feedback_is_ticket_traced_and_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import orchestrator.main as main
    from orchestrator.config import OrchestratorConfig
    from providers.tracing import (
        bind_trace_context,
        current_trace_context,
        new_trace_context,
        reset_trace_context,
    )

    monkeypatch.setenv("AGENTIC_PERF_ORCHESTRATOR_SESSION_ID", "")
    monkeypatch.setenv("AGENTIC_PERF_ORCHESTRATOR_EPOCH", "")
    config = OrchestratorConfig(
        state_store_url="http://store", raw_config={"llm": {"provider": "mock"}}
    )
    config.poll_interval = 0.01
    config.stale_task_timeout = 0
    ticket = {
        "id": "PERF-normalize",
        "status": "triage_pending",
        "custom_fields": {
            "directives": {
                "power_off_delay_seconds": 5,
                "sample_count": "3",
                "misspelled_directive": "value",
            }
        },
    }
    dispatcher = MagicMock()
    dispatcher.active_tasks.return_value = []
    dispatcher.is_active.return_value = False
    dispatcher.try_claim.return_value = False
    dispatcher.shutdown = AsyncMock()
    events = MagicMock()
    request_contexts = []
    comment_bodies = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def patch(self, url, *, json):
            request_contexts.append(("PATCH", current_trace_context().ticket_id))
            ticket["custom_fields"]["directives"] = deepcopy(
                json["fields"]["directives"]
            )
            return httpx.Response(200, request=httpx.Request("PATCH", url))

        async def post(self, url, *, json):
            request_contexts.append(("POST", current_trace_context().ticket_id))
            comment_bodies.append(json["body"])
            return httpx.Response(200, request=httpx.Request("POST", url))

    client = Client()
    monkeypatch.setattr(main, "Dispatcher", lambda *_args, **_kwargs: dispatcher)
    monkeypatch.setattr(main, "RepoCache", lambda: object())
    monkeypatch.setattr(main, "build_skill_provider", lambda **_kwargs: object())
    monkeypatch.setattr(main, "LocalSecretsProvider", lambda: object())
    monkeypatch.setattr(
        main,
        "_make_llm_provider",
        lambda _config: SimpleNamespace(
            default_timeout=None, reasoning_effort=None, max_tokens=None
        ),
    )
    monkeypatch.setattr(main, "_make_llm_factory", lambda _config: object())
    monkeypatch.setattr(main, "_validate_models", AsyncMock())
    monkeypatch.setattr(main, "EventBus", lambda **_kwargs: events)
    monkeypatch.setattr(
        main, "record_orchestrator_status", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(main, "_process_stop_requests", AsyncMock())
    monkeypatch.setattr(main, "_sweep_orphaned_leases", AsyncMock())
    monkeypatch.setattr(main, "_sweep_trace_spools", lambda: None)
    monkeypatch.setattr(main, "check_handoff", lambda *_args: (True, ""))
    monkeypatch.setattr(main, "AuditedAsyncHTTPClient", lambda **_kwargs: client)
    telemetry = ModuleType("providers.telemetry")
    telemetry.setup_telemetry = lambda **_kwargs: None
    monkeypatch.setitem(sys.modules, "providers.telemetry", telemetry)
    monkeypatch.setattr("providers.redaction.get_shared_redactor", lambda: object())

    third_poll = asyncio.Event()
    fetch_count = 0

    async def fetch(_url):
        nonlocal fetch_count
        fetch_count += 1
        if fetch_count >= 3:
            third_poll.set()
        return [deepcopy(ticket)]

    monkeypatch.setattr(main, "fetch_all_tickets", fetch)
    lease = SimpleNamespace(
        session_id=uuid4(),
        epoch=None,
        ttl_seconds=config.leader_lease_ttl_seconds,
        confirmed_deadline=(
            asyncio.get_running_loop().time() + config.leader_lease_ttl_seconds
        ),
    )

    async def acquire():
        lease.epoch = 7

    lease.acquire = acquire
    lease.renew = AsyncMock()
    lease.release = AsyncMock()
    lease_state = {"renew_task": None, "started": asyncio.Event()}
    previous_context = current_trace_context()
    control_context = new_trace_context(ticket_id="control", agent_id="orchestrator")
    control_token = bind_trace_context(control_context)
    task = asyncio.create_task(
        main._poll_loop_after_lease(config, lease, main._LeaseLossGate(), lease_state)
    )
    try:
        await asyncio.wait_for(third_poll.wait(), timeout=5)
        assert request_contexts == [
            ("PATCH", "PERF-normalize"),
            ("POST", "PERF-normalize"),
        ]
        events.emit.assert_called_once()
        assert ticket["custom_fields"]["directives"] == {
            "power_off_delay": 5,
            "sample_count": 3,
            "misspelled_directive": "value",
        }
        assert "converted to an integer" in comment_bodies[0]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        reset_trace_context(control_token)

    assert current_trace_context() == previous_context


@pytest.mark.asyncio
async def test_retry_persistence_cancellation_propagates() -> None:
    import orchestrator.main as main

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.patch = AsyncMock(side_effect=asyncio.CancelledError())
    with patch.object(main, "AuditedAsyncHTTPClient", return_value=client):
        with pytest.raises(asyncio.CancelledError):
            await main._persist_retry_state("http://store", "PERF-1", None)


@pytest.mark.asyncio
async def test_failed_pruning_keeps_retry_metadata_in_snapshot() -> None:
    import orchestrator.main as main

    ticket = {
        "id": "PERF-3",
        "status": "awaiting_customer_guidance",
        "custom_fields": {
            RETRY_STATE_FIELD: {
                "dispatch": {"status": "executing_benchmark", "exhausted": True}
            }
        },
    }
    with patch.object(main, "_persist_retry_state", AsyncMock(return_value=False)):
        await main._prune_stale_retry_state("http://store", ticket)
    assert ticket["custom_fields"][RETRY_STATE_FIELD]["dispatch"]["exhausted"]


@pytest.mark.asyncio
async def test_failed_dispatch_retry_writes_preserve_attempts_and_flush_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import orchestrator.main as main

    monkeypatch.setattr(main, "_pending_retry_writes", {})
    clock = [100.0]
    monkeypatch.setattr(main.time, "time", lambda: clock[0])
    dispatcher = MagicMock()
    dispatcher.is_deposed.return_value = False
    dispatcher.store_url = "http://store"
    dispatcher._trace_contexts = {}
    ticket = {"id": "PERF-1", "status": "executing_benchmark", "custom_fields": {}}
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    response = MagicMock(status_code=200)
    response.json.side_effect = lambda: deepcopy(ticket)
    client.get = AsyncMock(return_value=response)
    client.patch = AsyncMock(
        return_value=httpx.Response(
            503, request=httpx.Request("PATCH", "http://store/fields")
        )
    )
    monkeypatch.setattr(main, "AuditedAsyncHTTPClient", lambda **_kwargs: client)
    for attempt in range(1, DISPATCH_RETRY_LIMIT + 1):
        await main._record_dispatch_retry_outcome(
            dispatcher, "PERF-1", "executing_benchmark", claim_id="claim-1"
        )
        state = main._pending_retry_writes["PERF-1"][0]
        assert state["dispatch"]["attempts"] == attempt
        clock[0] += 10
    fetched = deepcopy(ticket)
    main._overlay_pending_retry_state(fetched)
    assert retry_is_suppressed(
        fetched["custom_fields"], "dispatch", "executing_benchmark", now=clock[0]
    )
    # Failed flushing advances its own write deadline; it does not reopen dispatch.
    await main._reconcile_pending_retry_state("http://store", deepcopy(ticket))
    patch_count = client.patch.await_count
    await main._reconcile_pending_retry_state("http://store", deepcopy(ticket))
    assert client.patch.await_count == patch_count
    clock[0] += 5
    client.patch.return_value = httpx.Response(
        200, request=httpx.Request("PATCH", "http://store/fields")
    )
    await main._reconcile_pending_retry_state("http://store", deepcopy(ticket))
    assert "PERF-1" not in main._pending_retry_writes


def test_status_change_clears_unsaved_retry_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import orchestrator.main as main

    state = {"dispatch": {"status": "executing_benchmark", "exhausted": True}}
    monkeypatch.setattr(main, "_pending_retry_writes", {"PERF-1": (state, 500.0)})
    ticket = {
        "id": "PERF-1",
        "status": "awaiting_customer_guidance",
        "custom_fields": {},
    }
    main._overlay_pending_retry_state(ticket)
    assert "PERF-1" not in main._pending_retry_writes
    assert RETRY_STATE_FIELD not in ticket["custom_fields"]


@pytest.mark.asyncio
async def test_delayed_old_retry_flush_cannot_rollback_new_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import orchestrator.main as main

    old = {"dispatch": {"status": "executing_benchmark", "attempts": 1}}
    newer = {"dispatch": {"status": "executing_benchmark", "attempts": 2}}
    pending = (old, 0.0)
    monkeypatch.setattr(main, "_pending_retry_writes", {"PERF-1": pending})
    monkeypatch.setattr(main, "_retry_write_locks", {})
    old_started = asyncio.Event()
    finish_old = asyncio.Event()
    requests = []

    async def patch_fields(_url, *, json):
        state = json["fields"][RETRY_STATE_FIELD]
        requests.append(state)
        if state is old:
            old_started.set()
            await finish_old.wait()
            status = 409
        else:
            status = 200
        return httpx.Response(
            status, request=httpx.Request("PATCH", "http://store/fields")
        )

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.patch = AsyncMock(side_effect=patch_fields)
    monkeypatch.setattr(main, "AuditedAsyncHTTPClient", lambda **_kwargs: client)
    old_flush = asyncio.create_task(
        main._persist_retry_state(
            "http://store", "PERF-1", old, expected_pending=pending
        )
    )
    await asyncio.wait_for(old_started.wait(), timeout=5)
    new_write = asyncio.create_task(
        main._persist_retry_state("http://store", "PERF-1", newer, claim_id="claim-1")
    )
    await asyncio.sleep(0)
    # The newer write waits until the earlier outbound PATCH completes.
    assert requests == [old]
    finish_old.set()
    assert await old_flush is False
    assert await new_write is True
    assert requests == [old, newer]
    assert "PERF-1" not in main._pending_retry_writes
    # A flush queued from an obsolete snapshot never sends it again.
    assert not await main._persist_retry_state(
        "http://store", "PERF-1", old, expected_pending=pending
    )
    assert requests == [old, newer]
