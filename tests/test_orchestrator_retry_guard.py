from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

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

    persist = AsyncMock()
    with patch.object(main, "_persist_retry_state", persist):
        await main._prune_stale_retry_state("http://store", ticket)

    persist.assert_awaited_once_with("http://store", "PERF-3", None)
    assert ticket["custom_fields"][RETRY_STATE_FIELD] is None
