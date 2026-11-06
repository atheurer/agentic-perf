"""Dispatcher-level trace outcomes for claims and lease loss."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from orchestrator.dispatcher import Dispatcher
from providers.tracing import (
    LifecycleState,
    TraceRecorder,
    bind_trace_context,
    child_context,
    current_trace_context,
    new_trace_context,
    reset_trace_context,
)


class _Sink:
    def __init__(self) -> None:
        self.events = []

    def record(self, event) -> None:
        self.events.append(event)


def _dispatcher() -> tuple[Dispatcher, _Sink]:
    dispatcher = Dispatcher("http://store", MagicMock(), MagicMock())
    sink = _Sink()
    dispatcher._trace = TraceRecorder(client=sink)
    return dispatcher, sink


async def test_mark_done_cancellation_waits_for_renewal_and_fenced_release() -> None:
    dispatcher, _ = _dispatcher()
    dispatcher._session_id = "session-1"
    dispatcher._fencing_epoch = 7
    dispatcher._claim_ids["PERF-1"] = "claim-1"
    dispatch_context = new_trace_context(ticket_id="PERF-1", agent_id="benchmark")
    dispatcher._trace_contexts["PERF-1"] = dispatch_context
    renewal_started = asyncio.Event()
    renewal_stopping = asyncio.Event()
    allow_renewal_stop = asyncio.Event()
    release_started = asyncio.Event()
    allow_release = asyncio.Event()
    captured = {}

    async def renewal():
        renewal_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            renewal_stopping.set()
            await allow_renewal_stop.wait()

    renewal_task = asyncio.create_task(renewal())
    dispatcher._renewal_tasks["PERF-1"] = renewal_task
    await asyncio.wait_for(renewal_started.wait(), 2)

    class Client:
        def __init__(self, **kwargs):
            captured["headers"] = kwargs["headers"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def request(self, method, url, *, json):
            captured["method"] = method
            captured["body"] = json
            captured["context"] = current_trace_context()
            release_started.set()
            await allow_release.wait()

    token = bind_trace_context(dispatch_context)
    try:
        with patch("providers.execution.AuditedAsyncHTTPClient", Client):
            task = asyncio.create_task(dispatcher.mark_done("PERF-1"))
            dispatcher.set_task("PERF-1", task, "executing_benchmark")
            await asyncio.wait_for(renewal_stopping.wait(), 2)
            task.cancel()
            task.cancel()
            await asyncio.sleep(0)
            assert dispatcher.is_active("PERF-1")
            assert not release_started.is_set()
            assert dispatcher._claim_ids["PERF-1"] == "claim-1"
            allow_renewal_stop.set()
            await asyncio.wait_for(release_started.wait(), 2)
            task.cancel()
            await asyncio.sleep(0)
            assert dispatcher.is_active("PERF-1")
            allow_release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
    finally:
        reset_trace_context(token)

    assert renewal_task.done()
    assert captured["method"] == "DELETE"
    assert captured["body"]["claim_id"] == "claim-1"
    assert captured["body"]["session_id"] == "session-1"
    assert captured["body"]["epoch"] == 7
    assert captured["headers"]["X-Agentic-Perf-Orchestrator-Session"] == "session-1"
    assert captured["context"] == dispatch_context
    assert "PERF-1" not in dispatcher._claim_ids
    assert "PERF-1" not in dispatcher._trace_contexts
    assert not dispatcher.is_active("PERF-1")


async def test_direct_mark_done_does_not_expose_dispatch_during_snapshot_cleanup() -> (
    None
):
    from orchestrator.main import run_agent_task

    dispatcher, sink = _dispatcher()
    dispatcher._claim_ids["PERF-1"] = "claim-1"
    dispatcher._trace_contexts["PERF-1"] = new_trace_context(
        ticket_id="PERF-1", agent_id="benchmark"
    )
    dispatcher.release_claim = AsyncMock()
    snapshot_started = asyncio.Event()
    finish_snapshots = asyncio.Event()

    async def snapshots(*_args):
        snapshot_started.set()
        await finish_snapshots.wait()

    response = MagicMock(status_code=200)
    response.json.return_value = {"status": "closed", "custom_fields": {}}
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.get = AsyncMock(return_value=response)
    with (
        patch("orchestrator.main.AuditedAsyncHTTPClient", return_value=client),
        patch(
            "orchestrator.main._record_dispatch_retry_outcome", new_callable=AsyncMock
        ),
        patch(
            "orchestrator.main._cleanup_closed_ticket_skill_context",
            side_effect=snapshots,
        ),
    ):
        task = asyncio.create_task(
            run_agent_task(dispatcher, "executing_benchmark", "PERF-1")
        )
        dispatcher.set_task("PERF-1", task, "executing_benchmark")
        await asyncio.wait_for(snapshot_started.wait(), 2)
        await dispatcher.mark_done("PERF-1")
        assert dispatcher._tasks["PERF-1"] is task
        assert dispatcher.is_active("PERF-1")
        with patch("orchestrator.dispatcher.httpx.Client") as claim_client:
            assert not dispatcher.try_claim("PERF-1", "executing_benchmark")
            claim_client.assert_not_called()
        task.cancel()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        shutdown = asyncio.create_task(dispatcher.shutdown())
        await asyncio.sleep(0)
        assert not shutdown.done()
        finish_snapshots.set()
        await asyncio.wait_for(shutdown, 2)
        assert task.cancelled()

    assert not dispatcher.is_active("PERF-1")
    assert "PERF-1" not in dispatcher._claim_ids
    dispatcher.release_claim.assert_awaited_once_with("PERF-1")
    assert [event.lifecycle.state for event in sink.events] == [
        LifecycleState.COMPLETED
    ]
    assert not dispatcher._cleanup_tasks


async def test_run_agent_task_cleanup_uses_ticket_trace_not_control_trace() -> None:
    from orchestrator.main import run_agent_task
    from providers.execution import AuditedAsyncHTTPClient

    dispatcher, _ = _dispatcher()
    dispatcher._claim_ids["PERF-1"] = "claim-1"
    dispatch = new_trace_context(ticket_id="PERF-1", agent_id="benchmark")
    dispatcher._trace_contexts["PERF-1"] = dispatch
    agent = MagicMock()
    agent.trace_context = child_context(dispatch, agent_id="benchmark-agent")
    agent.run = AsyncMock()
    agent.close = AsyncMock()
    dispatcher.create_agent = MagicMock(return_value=agent)
    dispatcher.get_skill_provider_for_ticket = AsyncMock(return_value=MagicMock())
    dispatcher._get_secrets_for_ticket = MagicMock(return_value=None)
    snapshot_contexts = []
    wire_tickets = []
    events = []

    async def snapshot(*_args):
        snapshot_contexts.append(current_trace_context())

    def handler(request: httpx.Request) -> httpx.Response:
        wire_tickets.append(request.headers.get("x-agentic-perf-ticket-id"))
        return httpx.Response(200, request=request)

    async def emit(event):
        events.append(event)

    def factory(**kwargs):
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), headers=kwargs.get("headers")
        )
        return AuditedAsyncHTTPClient(client=client, emit=emit)

    response = MagicMock(status_code=200)
    response.json.return_value = {"status": "executing_benchmark", "custom_fields": {}}
    main_client = MagicMock()
    main_client.__aenter__ = AsyncMock(return_value=main_client)
    main_client.__aexit__ = AsyncMock(return_value=None)
    main_client.get = AsyncMock(return_value=response)
    control = new_trace_context(ticket_id="control", agent_id="orchestrator")
    token = bind_trace_context(control)
    try:
        with (
            patch("orchestrator.main.AuditedAsyncHTTPClient", return_value=main_client),
            patch("providers.execution.AuditedAsyncHTTPClient", side_effect=factory),
            patch("orchestrator.main._advance_plan", new_callable=AsyncMock),
            patch(
                "orchestrator.main._record_dispatch_retry_outcome",
                new_callable=AsyncMock,
            ),
            patch(
                "orchestrator.main._cleanup_closed_ticket_skill_context",
                side_effect=snapshot,
            ),
        ):
            await run_agent_task(dispatcher, "executing_benchmark", "PERF-1")
        assert current_trace_context() == control
    finally:
        reset_trace_context(token)

    agent.run.assert_awaited_once_with("PERF-1")
    assert snapshot_contexts == [dispatch]
    assert wire_tickets == ["PERF-1"]
    assert events
    assert {event.ticket_id for event in events} == {"PERF-1"}


async def test_stale_closed_ticket_stops_agent_before_one_fenced_release() -> None:
    from orchestrator.main import _check_stale_tasks, run_agent_task
    from providers.execution import AuditedAsyncHTTPClient

    dispatcher, sink = _dispatcher()
    dispatcher._session_id = "session-1"
    dispatcher._fencing_epoch = 7
    dispatcher._claim_ids["PERF-1"] = "claim-1"
    dispatch = new_trace_context(ticket_id="PERF-1", agent_id="benchmark")
    dispatcher._trace_contexts["PERF-1"] = dispatch
    started = asyncio.Event()
    stopping = asyncio.Event()
    allow_stop = asyncio.Event()
    closed = False
    wire = []
    audit = []

    async def agent_run(_ticket_id):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            stopping.set()
            await allow_stop.wait()
            raise

    agent = MagicMock()
    agent.trace_context = child_context(dispatch, agent_id="benchmark-agent")
    agent.run = agent_run
    agent.close = AsyncMock()
    dispatcher.create_agent = MagicMock(return_value=agent)
    dispatcher.get_skill_provider_for_ticket = AsyncMock(return_value=MagicMock())
    dispatcher._get_secrets_for_ticket = MagicMock(return_value=None)

    def handler(request: httpx.Request) -> httpx.Response:
        wire.append(
            (
                request.method,
                request.headers.get("x-agentic-perf-ticket-id"),
                request.content,
            )
        )
        return httpx.Response(200, request=request)

    async def emit(event):
        audit.append(event)

    def factory(**kwargs):
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), headers=kwargs.get("headers")
        )
        return AuditedAsyncHTTPClient(client=client, emit=emit)

    response = MagicMock(status_code=200)
    response.json.side_effect = lambda: {
        "status": "closed" if closed else "executing_benchmark",
        "custom_fields": {},
        "comments": [],
    }
    main_client = MagicMock()
    main_client.__aenter__ = AsyncMock(return_value=main_client)
    main_client.__aexit__ = AsyncMock(return_value=None)
    main_client.get = AsyncMock(return_value=response)
    events = MagicMock()
    events.latest_trace_event_time.return_value = 1.0
    control = new_trace_context(ticket_id="control", agent_id="orchestrator")
    token = bind_trace_context(control)
    try:
        with (
            patch("orchestrator.main.AuditedAsyncHTTPClient", return_value=main_client),
            patch("providers.execution.AuditedAsyncHTTPClient", side_effect=factory),
            patch(
                "orchestrator.main._record_dispatch_retry_outcome",
                new_callable=AsyncMock,
            ),
            patch(
                "orchestrator.main._cleanup_closed_ticket_skill_context",
                new_callable=AsyncMock,
            ),
        ):
            task = asyncio.create_task(
                run_agent_task(dispatcher, "executing_benchmark", "PERF-1")
            )
            dispatcher.set_task("PERF-1", task, "executing_benchmark")
            await asyncio.wait_for(started.wait(), 2)
            closed = True
            await _check_stale_tasks(dispatcher, events, 1, dispatcher.store_url)
            await asyncio.wait_for(stopping.wait(), 2)
            assert not wire
            assert dispatcher.is_active("PERF-1")
            allow_stop.set()
            await asyncio.wait_for(task, 2)
        assert current_trace_context() == control
    finally:
        reset_trace_context(token)

    assert len(wire) == 1
    method, ticket, body = wire[0]
    assert (method, ticket) == ("DELETE", "PERF-1")
    assert b'"claim_id":"claim-1"' in body
    assert {event.ticket_id for event in audit} == {"PERF-1"}
    assert [event.lifecycle.state for event in sink.events] == [
        LifecycleState.COMPLETED
    ]
    assert not dispatcher.is_active("PERF-1")


@pytest.mark.parametrize("already_cleaned", [False, True])
async def test_stale_watchdog_handles_task_finishing_during_ticket_read(
    already_cleaned,
):
    from orchestrator.main import _check_stale_tasks

    dispatcher, sink = _dispatcher()
    dispatcher._claim_ids["PERF-1"] = "claim-1"
    dispatcher._trace_contexts["PERF-1"] = new_trace_context(
        ticket_id="PERF-1", agent_id="benchmark"
    )
    dispatcher.release_claim = AsyncMock()
    read_started = asyncio.Event()
    finish_task = asyncio.Event()
    allow_read = asyncio.Event()

    async def worker():
        await finish_task.wait()
        if already_cleaned:
            await dispatcher.mark_done("PERF-1")

    task = asyncio.create_task(worker())
    dispatcher.set_task("PERF-1", task, "executing_benchmark")
    response = MagicMock(status_code=200)
    response.json.return_value = {"status": "closed"}
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)

    async def get(*_args):
        read_started.set()
        await allow_read.wait()
        return response

    client.get = AsyncMock(side_effect=get)
    events = MagicMock()
    events.latest_trace_event_time.return_value = 1.0
    with patch("orchestrator.main.AuditedAsyncHTTPClient", return_value=client):
        watchdog = asyncio.create_task(
            _check_stale_tasks(dispatcher, events, 1, dispatcher.store_url)
        )
        await asyncio.wait_for(read_started.wait(), 2)
        finish_task.set()
        await asyncio.wait_for(task, 2)
        allow_read.set()
        await asyncio.wait_for(watchdog, 2)

    dispatcher.release_claim.assert_awaited_once_with("PERF-1")
    assert [event.lifecycle.state for event in sink.events] == [
        LifecycleState.COMPLETED
    ]


def test_claim_rejection_is_a_durable_dispatch_outcome() -> None:
    dispatcher, sink = _dispatcher()
    client = MagicMock()
    client.__enter__.return_value.post.return_value.status_code = 409
    with patch("orchestrator.dispatcher.httpx.Client", return_value=client):
        assert not dispatcher.try_claim("PERF-1", "triage_pending")
    assert sink.events[-1].lifecycle.state == LifecycleState.REJECTED


async def test_repo_cache_refresh_binds_the_claimed_ticket_context() -> None:
    """Cache mutations are attributed to the ticket that triggered them."""
    from orchestrator.main import _refresh_harness_repos

    seen = []

    class Cache:
        async def ensure_repo(self, name, url):
            seen.append((name, url, current_trace_context()))

    context = new_trace_context(ticket_id="PERF-1", agent_id="triage")
    await _refresh_harness_repos(
        Cache(),
        {
            "crucible": "https://example.invalid/crucible.git",
            "other": "https://example.invalid/other.git",
        },
        context,
    )
    assert seen == [("other", "https://example.invalid/other.git", context)]
    assert current_trace_context() is None


async def test_renewal_loss_records_claim_failure() -> None:
    dispatcher, sink = _dispatcher()
    client = MagicMock()
    client.__enter__.return_value.post.return_value.status_code = 200
    with patch("orchestrator.dispatcher.httpx.Client", return_value=client):
        assert dispatcher.try_claim("PERF-1", "triage_pending")
    dispatcher.renew_claim = AsyncMock(return_value=False)
    dispatcher.lease_seconds = 0
    await dispatcher._renewal_loop("PERF-1")
    assert sink.events[-1].lifecycle.state == LifecycleState.FAILED
    assert sink.events[-1].action.phase == "claim_renewal"


async def test_introspection_is_a_sibling_child_of_dispatch() -> None:
    dispatcher, _ = _dispatcher()
    client = MagicMock()
    client.__enter__.return_value.post.return_value.status_code = 200
    with patch("orchestrator.dispatcher.httpx.Client", return_value=client):
        assert dispatcher.try_claim("PERF-1", "triage_pending")
    primary = dispatcher.create_agent("triage_pending", {"id": "PERF-1"})
    assert dispatcher.start_introspection("PERF-1")
    observer = dispatcher._introspection_agents["PERF-1"]
    dispatch = dispatcher._trace_contexts["PERF-1"]
    assert observer.trace_context.parent_action_id == dispatch.action_id
    assert primary.trace_context.parent_action_id == dispatch.action_id
    assert observer.trace_context.action_id != primary.trace_context.action_id
    task = dispatcher._introspection_tasks["PERF-1"]
    dispatcher.stop_introspection("PERF-1")
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_run_agent_task_binds_and_resets_agent_context() -> None:
    """Adapter code sees the invocation while an agent runs, never afterward."""
    from orchestrator.main import run_agent_task

    dispatcher, _ = _dispatcher()
    agent_context = new_trace_context(ticket_id="PERF-1", agent_id="triage")
    seen = []

    class Agent:
        trace_context = agent_context

        async def run(self, ticket_id):
            seen.append(current_trace_context())

        async def close(self):
            pass

    agent = Agent()
    dispatcher.create_agent = MagicMock(return_value=agent)
    dispatcher.release_claim = AsyncMock()
    await run_agent_task(dispatcher, "test_status", "PERF-1")
    assert seen == [agent_context]
    assert current_trace_context() is None


@pytest.mark.parametrize("status", ("preparing_platform", "awaiting_provision"))
async def test_lifecycle_image_resolution_uses_complete_claim_fence(
    monkeypatch, status: str
) -> None:
    """Image resolution keeps the dispatcher's ticket claim fence."""
    import orchestrator.main as mod

    monkeypatch.setenv("AGENTIC_PERF_API_TOKEN", "api-token")
    dispatcher = Dispatcher(
        "http://store",
        MagicMock(),
        MagicMock(),
        session_id="session-1",
        fencing_epoch=7,
    )
    dispatcher._claim_ids["PERF-1"] = "claim-1"
    dispatcher.events = None
    dispatcher.mark_done = AsyncMock()

    class Agent:
        trace_context = None

        async def run(self, ticket_id: str) -> None:
            pass

        async def close(self) -> None:
            pass

    dispatcher.create_agent = MagicMock(return_value=Agent())

    mock_client = MagicMock()
    get_response = MagicMock(status_code=200)
    get_response.json.return_value = {"status": status, "custom_fields": {}}
    get_response.raise_for_status = MagicMock()
    mock_client.get = AsyncMock(return_value=get_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    resolve_images = AsyncMock()

    with (
        patch.object(mod, "AuditedAsyncHTTPClient", lambda **kwargs: mock_client),
        patch.object(mod, "_resolve_jumpstarter_images", resolve_images),
        patch("orchestrator.config._load_config_file", return_value={}),
    ):
        await mod.run_agent_task(dispatcher, status, "PERF-1")

    resolve_images.assert_awaited_once_with(
        "http://store",
        "PERF-1",
        auth_headers={
            "Authorization": "Bearer api-token",
            "X-Agentic-Perf-Orchestrator-Session": "session-1",
            "X-Agentic-Perf-Orchestrator-Epoch": "7",
            "X-Agentic-Perf-Claim-Id": "claim-1",
        },
        image_config={},
    )


@pytest.mark.parametrize(
    "status",
    ("preparing_platform", "awaiting_provision", "executing_benchmark"),
)
async def test_changed_provider_selection_resume_reroutes_to_resource_agent(
    monkeypatch, status: str
) -> None:
    """Changed allocations return to resource reconciliation before downstream work."""
    import orchestrator.main as mod

    dispatcher, _ = _dispatcher()
    dispatcher._claim_ids["PERF-1"] = "claim-1"
    dispatcher.create_agent = MagicMock()
    dispatcher.mark_done = AsyncMock()
    ticket = {
        "id": "PERF-1",
        "status": status,
        "custom_fields": {
            "resource_provider": "jumpstarter",
            "resource_reservation_id": "lease-1",
            "resource_provider_metadata": {
                "lease_id": "lease-1",
                "selector": "board-type=ride4",
                "reservation_selections": [
                    {"jumpstarter_selector": "board-type=ride4"}
                ],
            },
            "directives": {"board_selector": "board-type=ride5"},
        },
    }
    get_response = MagicMock(status_code=200)
    get_response.json.return_value = ticket
    get_response.raise_for_status = MagicMock()
    transition_response = MagicMock()
    transition_response.raise_for_status = MagicMock()
    client = MagicMock()
    client.get = AsyncMock(return_value=get_response)
    client.post = AsyncMock(return_value=transition_response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)

    with (
        patch.object(mod, "AuditedAsyncHTTPClient", lambda **_kwargs: client),
        patch.object(mod, "_record_dispatch_retry_outcome", AsyncMock()),
    ):
        await mod.run_agent_task(
            dispatcher,
            status,
            "PERF-1",
            ticket_data=ticket,
        )

    dispatcher.create_agent.assert_not_called()
    client.post.assert_awaited_once_with(
        "http://store/api/v1/tickets/PERF-1/transition",
        json={
            "status": "awaiting_hardware",
            "comment": (
                "The active allocation requires reconciliation "
                f"before resuming {status}; returning to resource allocation."
            ),
        },
    )


async def test_unchanged_benchmark_resume_keeps_agent_dispatch() -> None:
    """An unchanged lease with a pending approval still reaches benchmark."""
    import orchestrator.main as mod

    dispatcher, _ = _dispatcher()
    dispatcher.create_agent = MagicMock()
    dispatcher.mark_done = AsyncMock()
    ticket = {
        "id": "PERF-1",
        "status": "executing_benchmark",
        "custom_fields": {
            "resource_provider": "jumpstarter",
            "resource_reservation_id": "lease-1",
            "resource_provider_metadata": {
                "lease_id": "lease-1",
                "selector": "board-type=ride4",
            },
            "directives": {"board_selector": "board-type=ride4"},
            "approval_requests": {
                "apr-1": {
                    "approval_request_id": "apr-1",
                    "status": "pending",
                }
            },
        },
    }
    get_response = MagicMock(status_code=200)
    get_response.json.return_value = ticket
    get_response.raise_for_status = MagicMock()
    client = MagicMock()
    client.get = AsyncMock(return_value=get_response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    agent = MagicMock()
    agent.trace_context = None
    agent.run = AsyncMock()
    agent.close = AsyncMock()
    dispatcher.create_agent.return_value = agent

    with (
        patch.object(mod, "AuditedAsyncHTTPClient", lambda **_kwargs: client),
        patch.object(mod, "_record_dispatch_retry_outcome", AsyncMock()),
        patch.object(mod, "_advance_plan", AsyncMock()),
    ):
        await mod.run_agent_task(
            dispatcher,
            "executing_benchmark",
            "PERF-1",
            ticket_data=ticket,
        )

    dispatcher.create_agent.assert_called_once()
    agent.run.assert_awaited_once_with("PERF-1")


async def test_resume_creates_a_new_invocation_linked_to_prior_dispatch() -> None:
    dispatcher, sink = _dispatcher()
    client = MagicMock()
    client.__enter__.return_value.post.return_value.status_code = 200
    with patch("orchestrator.dispatcher.httpx.Client", return_value=client):
        assert dispatcher.try_claim("PERF-1", "triage_pending")
    first = dispatcher._trace_contexts["PERF-1"]
    dispatcher.release_claim = AsyncMock()
    await dispatcher.mark_done("PERF-1")
    with patch("orchestrator.dispatcher.httpx.Client", return_value=client):
        assert dispatcher.try_claim("PERF-1", "triage_pending")
    second = dispatcher._trace_contexts["PERF-1"]
    assert second.invocation_id != first.invocation_id
    assert second.parent_action_id == first.action_id
    assert sink.events[-1].attributes["prior_invocation_id"] == str(first.invocation_id)


async def test_handoff_waits_for_claim_cleanup_before_new_dispatch() -> None:
    dispatcher, _ = _dispatcher()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_release(ticket_id: str) -> None:
        assert dispatcher._claim_ids[ticket_id] == "old-claim"
        entered.set()
        await release.wait()

    dispatcher.release_claim = slow_release
    dispatcher._claim_ids["PERF-1"] = "old-claim"
    cleanup = asyncio.create_task(dispatcher.mark_done("PERF-1"))
    dispatcher.set_task("PERF-1", cleanup, "triage_pending")
    await entered.wait()

    with patch("orchestrator.dispatcher.httpx.Client") as client:
        assert dispatcher.is_active("PERF-1")
        assert not dispatcher.try_claim("PERF-1", "awaiting_hardware")
        client.assert_not_called()
    assert dispatcher._claim_ids["PERF-1"] == "old-claim"

    release.set()
    await cleanup
    assert not dispatcher.is_active("PERF-1")
    client = MagicMock()
    response = client.__enter__.return_value.post.return_value
    response.status_code = 200
    response.json.return_value = {"claim_id": "new-claim"}
    with patch("orchestrator.dispatcher.httpx.Client", return_value=client):
        assert dispatcher.try_claim("PERF-1", "awaiting_hardware")
    assert dispatcher._claim_ids["PERF-1"] == "new-claim"


async def test_overlapping_cleanups_block_dispatch_until_both_finish() -> None:
    dispatcher, sink = _dispatcher()
    dispatcher._claim_ids["PERF-1"] = "old-claim"
    dispatcher._trace_contexts["PERF-1"] = new_trace_context(
        ticket_id="PERF-1", agent_id="benchmark"
    )
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def slow_release(ticket_id: str) -> None:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()

    dispatcher.release_claim = slow_release
    first = asyncio.create_task(dispatcher.mark_done("PERF-1"))
    second = asyncio.create_task(dispatcher.mark_done("PERF-1"))
    await asyncio.wait_for(started.wait(), 2)
    first.cancel()
    await asyncio.sleep(0)
    assert dispatcher.is_active("PERF-1")
    assert not dispatcher.try_claim("PERF-1", "awaiting_hardware")
    assert calls == 1
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second
    assert not dispatcher.is_active("PERF-1")
    assert calls == 1
    assert [event.lifecycle.state for event in sink.events] == [
        LifecycleState.COMPLETED
    ]


async def test_renewal_task_can_call_mark_done_without_self_wait() -> None:
    dispatcher, _ = _dispatcher()
    dispatcher._claim_ids["PERF-1"] = "old-claim"
    dispatcher.release_claim = AsyncMock()
    start = asyncio.Event()

    async def renewal():
        await start.wait()
        await dispatcher.mark_done("PERF-1")

    task = asyncio.create_task(renewal())
    dispatcher._renewal_tasks["PERF-1"] = task
    start.set()
    await asyncio.wait_for(task, 2)
    dispatcher.release_claim.assert_awaited_once_with("PERF-1")
    assert "PERF-1" not in dispatcher._renewal_tasks


async def test_deposed_renewal_is_joined_before_claim_release() -> None:
    dispatcher, _ = _dispatcher()
    dispatcher._claim_ids["PERF-1"] = "old-claim"
    renewal_started = asyncio.Event()
    renewal_stopping = asyncio.Event()
    allow_stop = asyncio.Event()

    async def renewal():
        renewal_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            renewal_stopping.set()
            await allow_stop.wait()

    renewal_task = asyncio.create_task(renewal())
    dispatcher._renewal_tasks["PERF-1"] = renewal_task

    async def release(_ticket_id):
        assert renewal_task.done(), "release preceded renewal shutdown"

    dispatcher.release_claim = AsyncMock(side_effect=release)
    await asyncio.wait_for(renewal_started.wait(), 2)
    dispatcher.mark_deposed()
    await asyncio.wait_for(renewal_stopping.wait(), 2)
    cleanup = asyncio.create_task(dispatcher.mark_done("PERF-1"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    dispatcher.release_claim.assert_not_awaited()
    assert dispatcher.is_active("PERF-1")
    allow_stop.set()
    await asyncio.wait_for(cleanup, 2)
    dispatcher.release_claim.assert_awaited_once_with("PERF-1")
    assert renewal_task.done()
    assert "PERF-1" not in dispatcher._renewal_tasks


async def test_shutdown_waits_for_already_cancelled_renewal() -> None:
    dispatcher, _ = _dispatcher()
    dispatcher._claim_ids["PERF-1"] = "old-claim"
    started = asyncio.Event()
    stopping = asyncio.Event()
    allow_stop = asyncio.Event()

    async def renewal():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            stopping.set()
            await allow_stop.wait()

    renewal_task = asyncio.create_task(renewal())
    dispatcher._renewal_tasks["PERF-1"] = renewal_task

    async def release(_ticket_id):
        assert renewal_task.done(), "release preceded renewal shutdown"

    dispatcher.release_claim = AsyncMock(side_effect=release)
    await asyncio.wait_for(started.wait(), 2)
    dispatcher.mark_deposed()
    await asyncio.wait_for(stopping.wait(), 2)
    shutdown = asyncio.create_task(dispatcher.shutdown())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not shutdown.done()
    assert not renewal_task.done()
    dispatcher.release_claim.assert_not_awaited()
    allow_stop.set()
    await asyncio.wait_for(shutdown, 2)
    dispatcher.release_claim.assert_awaited_once_with("PERF-1")


@pytest.mark.parametrize("phase", ["drain", "release"])
async def test_shutdown_cancellation_waits_for_cleanup_and_restores_context(
    phase,
) -> None:
    dispatcher, _ = _dispatcher()
    dispatcher._claim_ids["PERF-1"] = "claim-1"
    dispatch = new_trace_context(ticket_id="PERF-1", agent_id="benchmark")
    dispatcher._trace_contexts["PERF-1"] = dispatch
    worker_started = asyncio.Event()
    draining = asyncio.Event()
    release_started = asyncio.Event()
    allow_finish = asyncio.Event()
    release_contexts = []

    async def worker():
        worker_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            draining.set()
            if phase == "drain":
                await allow_finish.wait()

    async def release(_ticket_id):
        release_contexts.append(current_trace_context())
        release_started.set()
        if phase == "release":
            await allow_finish.wait()

    dispatcher.release_claim = AsyncMock(side_effect=release)
    task = asyncio.create_task(worker())
    dispatcher.set_task("PERF-1", task, "executing_benchmark")
    await asyncio.wait_for(worker_started.wait(), 2)
    control = new_trace_context(ticket_id="control", agent_id="orchestrator")
    token = bind_trace_context(control)
    try:
        shutdown = asyncio.create_task(dispatcher.shutdown())
        wait_for = draining if phase == "drain" else release_started
        await asyncio.wait_for(wait_for.wait(), 2)
        shutdown.cancel()
        shutdown.cancel()
        await asyncio.sleep(0)
        assert not shutdown.done()
        allow_finish.set()
        with pytest.raises(asyncio.CancelledError):
            await shutdown
        assert current_trace_context() == control
    finally:
        reset_trace_context(token)

    dispatcher.release_claim.assert_awaited_once_with("PERF-1")
    assert release_contexts == [dispatch]
    assert task.done()
    assert not dispatcher._claim_ids
    assert not dispatcher._tasks


async def test_shutdown_excludes_its_registered_caller() -> None:
    dispatcher, _ = _dispatcher()
    dispatcher._claim_ids["PERF-1"] = "claim-1"
    dispatcher.release_claim = AsyncMock()

    async def caller():
        dispatcher.set_task("PERF-1", asyncio.current_task(), "executing_benchmark")
        await dispatcher.shutdown()
        return "finished"

    task = asyncio.create_task(caller())
    assert await asyncio.wait_for(task, 2) == "finished"
    dispatcher.release_claim.assert_awaited_once_with("PERF-1")


async def test_shutdown_joins_unregistered_direct_mark_done() -> None:
    dispatcher, sink = _dispatcher()
    dispatcher._claim_ids["PERF-1"] = "claim-1"
    dispatch = new_trace_context(ticket_id="PERF-1", agent_id="benchmark")
    dispatcher._trace_contexts["PERF-1"] = dispatch
    entered = asyncio.Event()
    allow_release = asyncio.Event()
    release_contexts = []

    async def release(_ticket_id):
        release_contexts.append(current_trace_context())
        entered.set()
        await allow_release.wait()

    dispatcher.release_claim = AsyncMock(side_effect=release)
    control = new_trace_context(ticket_id="control", agent_id="orchestrator")
    token = bind_trace_context(control)
    try:
        mark_done = asyncio.create_task(dispatcher.mark_done("PERF-1"))
        await asyncio.wait_for(entered.wait(), 2)
        shutdown = asyncio.create_task(dispatcher.shutdown())
        await asyncio.sleep(0)
        shutdown.cancel()
        shutdown.cancel()
        await asyncio.sleep(0)
        assert not shutdown.done()
        assert not mark_done.done()
        allow_release.set()
        await asyncio.wait_for(mark_done, 2)
        with pytest.raises(asyncio.CancelledError):
            await shutdown
        assert current_trace_context() == control
    finally:
        reset_trace_context(token)

    dispatcher.release_claim.assert_awaited_once_with("PERF-1")
    assert release_contexts == [dispatch]
    assert [event.lifecycle.state for event in sink.events] == [
        LifecycleState.COMPLETED
    ]
    assert not dispatcher._claim_ids
    assert not dispatcher._cleanup_tasks


async def test_mark_done_still_releases_without_a_local_claim_id() -> None:
    dispatcher, _ = _dispatcher()
    captured = []

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def request(self, method, _url, *, json):
            captured.append((method, json))

    with patch("providers.execution.AuditedAsyncHTTPClient", Client):
        await dispatcher.mark_done("PERF-1")
    assert captured == [
        (
            "DELETE",
            {
                "owner": "unknown",
                "session_id": None,
                "epoch": None,
                "claim_id": None,
            },
        )
    ]


async def test_mark_done_preserves_cancellation_and_chains_release_failure() -> None:
    dispatcher, _ = _dispatcher()
    dispatcher._claim_ids["PERF-1"] = "claim-1"
    started = asyncio.Event()
    finish = asyncio.Event()

    async def release(_ticket_id):
        started.set()
        await finish.wait()
        raise RuntimeError("release failed")

    dispatcher.release_claim = AsyncMock(side_effect=release)
    task = asyncio.create_task(dispatcher.mark_done("PERF-1"))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    await asyncio.sleep(0)
    finish.set()
    with pytest.raises(asyncio.CancelledError) as cancelled:
        await task
    assert isinstance(cancelled.value.__cause__, RuntimeError)
    assert dispatcher._claim_ids["PERF-1"] == "claim-1"
    assert not dispatcher._cleanup_counts
    assert not dispatcher._cleanup_tasks


async def test_shutdown_cancels_and_awaits_all_dispatcher_background_work() -> None:
    dispatcher, _ = _dispatcher()
    stopped = []

    async def blocked() -> None:
        try:
            await asyncio.Future()
        finally:
            stopped.append(True)

    task = asyncio.create_task(blocked())
    await asyncio.sleep(0)
    dispatcher._tasks["PERF-1"] = task
    dispatcher._renewal_tasks["PERF-1"] = task
    dispatcher._introspection_tasks["PERF-1"] = task
    introspection_agent = MagicMock()
    dispatcher._introspection_agents["PERF-1"] = introspection_agent
    dispatcher._claim_ids["PERF-1"] = "claim-1"
    dispatcher.release_claim = AsyncMock()

    await dispatcher.shutdown()

    assert task.done()
    assert stopped == [True]
    introspection_agent.request_stop.assert_called_once()
    dispatcher.release_claim.assert_awaited_once_with("PERF-1")
    assert not dispatcher._tasks
    assert not dispatcher._renewal_tasks
    assert not dispatcher._introspection_tasks


def test_constructed_agent_is_a_distinct_child_of_dispatch() -> None:
    dispatcher, sink = _dispatcher()
    client = MagicMock()
    client.__enter__.return_value.post.return_value.status_code = 200
    with patch("orchestrator.dispatcher.httpx.Client", return_value=client):
        assert dispatcher.try_claim("PERF-1", "triage_pending")
    dispatch = dispatcher._trace_contexts["PERF-1"]
    agent = dispatcher.create_agent("triage_pending", {"id": "PERF-1"})
    assert agent.trace_context.parent_action_id == dispatch.action_id
    assert agent.trace_context.action_id != dispatch.action_id


async def test_task_started_at_tracks_dispatch_and_prunes_finished() -> None:
    dispatcher, _ = _dispatcher()
    task = asyncio.create_task(asyncio.sleep(0))

    assert dispatcher.task_started_at("PERF-1") is None
    dispatcher.set_task("PERF-1", task, status="running")
    started = dispatcher.task_started_at("PERF-1")
    assert started is not None
    assert dispatcher.task_started_at("PERF-2") is None
    assert "PERF-1" in dispatcher.active_tasks()

    await task

    assert dispatcher.active_tasks() == {}
    assert dispatcher.task_started_at("PERF-1") is None
