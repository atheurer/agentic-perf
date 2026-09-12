"""Dispatcher-level trace outcomes for claims and lease loss."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from orchestrator.dispatcher import Dispatcher
from providers.tracing import LifecycleState, TraceRecorder


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


def test_claim_rejection_is_a_durable_dispatch_outcome() -> None:
    dispatcher, sink = _dispatcher()
    client = MagicMock()
    client.__enter__.return_value.post.return_value.status_code = 409
    with patch("orchestrator.dispatcher.httpx.Client", return_value=client):
        assert not dispatcher.try_claim("PERF-1", "triage_pending")
    assert sink.events[-1].lifecycle.state == LifecycleState.REJECTED


async def test_renewal_loss_records_claim_failure() -> None:
    dispatcher, sink = _dispatcher()
    client = MagicMock()
    client.__enter__.return_value.post.return_value.status_code = 200
    with patch("orchestrator.dispatcher.httpx.Client", return_value=client):
        assert dispatcher.try_claim("PERF-1", "triage_pending")
    dispatcher.renew_claim = MagicMock(return_value=False)
    dispatcher.lease_seconds = 0
    await dispatcher._renewal_loop("PERF-1")
    assert sink.events[-1].lifecycle.state == LifecycleState.FAILED
    assert sink.events[-1].action.phase == "claim_renewal"


def test_resume_creates_a_new_invocation_linked_to_prior_dispatch() -> None:
    dispatcher, sink = _dispatcher()
    client = MagicMock()
    client.__enter__.return_value.post.return_value.status_code = 200
    with patch("orchestrator.dispatcher.httpx.Client", return_value=client):
        assert dispatcher.try_claim("PERF-1", "triage_pending")
    first = dispatcher._trace_contexts["PERF-1"]
    dispatcher.mark_done("PERF-1")
    with patch("orchestrator.dispatcher.httpx.Client", return_value=client):
        assert dispatcher.try_claim("PERF-1", "triage_pending")
    second = dispatcher._trace_contexts["PERF-1"]
    assert second.invocation_id != first.invocation_id
    assert second.parent_action_id == first.action_id
    assert sink.events[-1].attributes["prior_invocation_id"] == str(first.invocation_id)


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
