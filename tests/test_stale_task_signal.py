"""Staleness signal for the orchestrator's stale-task detector.

The detector kills agents that stop producing durable trace events.  It must
ignore timer-driven store bookkeeping (claim renewal) and still evaluate a
dispatch that has produced no events at all.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from providers.events import EventBus
from providers.tracing import (
    ActionDescriptor,
    ActionType,
    LifecycleDescriptor,
    LifecycleState,
    OperationOutcome,
    TraceEventV1,
)


def _insert_state_event(
    events: EventBus,
    ticket_id: str,
    target: str,
    *,
    age_seconds: float,
) -> None:
    events._trace_store.insert_event(
        TraceEventV1(
            ticket_id=ticket_id,
            action=ActionDescriptor(type=ActionType.STATE, target=target),
            lifecycle=LifecycleDescriptor(state=LifecycleState.STARTED),
            outcome=OperationOutcome.SUCCESS,
            occurred_at=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
        )
    )


def _ticket_response(ticket_id: str, status: str = "executing_benchmark") -> MagicMock:
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "id": ticket_id,
        "status": status,
        "custom_fields": {},
    }
    return response


def _run_detector(
    dispatcher: MagicMock,
    events: EventBus,
    *,
    stale_timeout: float = 900,
    ticket_id: str,
    status: str = "executing_benchmark",
) -> MagicMock:
    """Drive _check_stale_tasks against mocked HTTP responses."""
    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=_ticket_response(ticket_id, status))
    mock_client.patch = AsyncMock(return_value=MagicMock(status_code=200))
    mock_client.post = AsyncMock(return_value=MagicMock(status_code=200))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    return mock_client


@pytest.fixture
def stale_task() -> MagicMock:
    task = MagicMock()
    task.done.return_value = False
    return task


@pytest.mark.asyncio
async def test_recent_agent_event_prevents_cancellation(tmp_path, stale_task) -> None:
    from orchestrator.main import _check_stale_tasks

    events = EventBus(log_dir=str(tmp_path / "logs"))
    events.emit("SIG-1", "agent", "tool_called")
    dispatcher = MagicMock()
    dispatcher.active_tasks.return_value = {"SIG-1": stale_task}
    dispatcher._claim_ids = {"SIG-1": None}

    with patch("orchestrator.main.AuditedAsyncHTTPClient") as client_class:
        client_class.return_value = _run_detector(dispatcher, events, ticket_id="SIG-1")
        await _check_stale_tasks(
            dispatcher, events, stale_timeout=900, store_url="http://store"
        )

    stale_task.cancel.assert_not_called()


@pytest.mark.asyncio
async def test_claim_renewal_does_not_count_as_activity(tmp_path, stale_task) -> None:
    """Renewal runs on a timer; it must not hide a stuck agent."""
    from orchestrator.main import _check_stale_tasks

    events = EventBus(log_dir=str(tmp_path / "logs"))
    events.emit("SIG-2", "agent", "tool_called")
    events._trace_store._open_connection().execute(
        "UPDATE trace_events SET occurred_at = ? WHERE ticket_id = ?",
        ("2020-01-01T00:00:00+00:00", "SIG-2"),
    )
    events._trace_store._open_connection().commit()
    _insert_state_event(events, "SIG-2", "renew_claim", age_seconds=5)
    _insert_state_event(events, "SIG-2", "claim_fence", age_seconds=3)

    dispatcher = MagicMock()
    dispatcher.active_tasks.return_value = {"SIG-2": stale_task}
    dispatcher._claim_ids = {"SIG-2": None}

    with patch("orchestrator.main.AuditedAsyncHTTPClient") as client_class:
        client_class.return_value = _run_detector(dispatcher, events, ticket_id="SIG-2")
        await _check_stale_tasks(
            dispatcher, events, stale_timeout=900, store_url="http://store"
        )

    stale_task.cancel.assert_called_once()


@pytest.mark.asyncio
async def test_dispatch_start_is_used_when_no_events_exist(
    tmp_path, stale_task
) -> None:
    """A dispatch that never reports is still evaluated."""
    from orchestrator.main import _check_stale_tasks

    events = EventBus(log_dir=str(tmp_path / "logs"))
    dispatcher = MagicMock()
    dispatcher.active_tasks.return_value = {"SIG-3": stale_task}
    dispatcher._claim_ids = {"SIG-3": None}
    dispatcher.task_started_at.return_value = 1.0

    with patch("orchestrator.main.AuditedAsyncHTTPClient") as client_class:
        client_class.return_value = _run_detector(dispatcher, events, ticket_id="SIG-3")
        await _check_stale_tasks(
            dispatcher, events, stale_timeout=900, store_url="http://store"
        )

    stale_task.cancel.assert_called_once()


@pytest.mark.asyncio
async def test_recent_dispatch_start_prevents_cancellation(
    tmp_path, stale_task
) -> None:
    import time

    from orchestrator.main import _check_stale_tasks

    events = EventBus(log_dir=str(tmp_path / "logs"))
    dispatcher = MagicMock()
    dispatcher.active_tasks.return_value = {"SIG-4": stale_task}
    dispatcher._claim_ids = {"SIG-4": None}
    dispatcher.task_started_at.return_value = time.time()

    with patch("orchestrator.main.AuditedAsyncHTTPClient") as client_class:
        client_class.return_value = _run_detector(dispatcher, events, ticket_id="SIG-4")
        await _check_stale_tasks(
            dispatcher, events, stale_timeout=900, store_url="http://store"
        )

    stale_task.cancel.assert_not_called()


@pytest.mark.asyncio
async def test_closed_ticket_is_cleaned_up_without_events(tmp_path, stale_task) -> None:
    """Closed-ticket cleanup no longer depends on having trace events."""
    from orchestrator.main import _check_stale_tasks

    events = EventBus(log_dir=str(tmp_path / "logs"))
    dispatcher = MagicMock()
    dispatcher.active_tasks.return_value = {"SIG-5": stale_task}
    dispatcher._claim_ids = {"SIG-5": None}
    dispatcher.task_started_at.return_value = 1.0
    dispatcher.mark_done = AsyncMock()

    with patch("orchestrator.main.AuditedAsyncHTTPClient") as client_class:
        client_class.return_value = _run_detector(
            dispatcher, events, ticket_id="SIG-5", status="closed"
        )
        await _check_stale_tasks(
            dispatcher, events, stale_timeout=900, store_url="http://store"
        )

    stale_task.cancel.assert_called_once()
