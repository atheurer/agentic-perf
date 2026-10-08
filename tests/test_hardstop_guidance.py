"""Tests for guidance_summary population after task cancellation.

Covers issue #980: when an open ticket's agent is cancelled, the
guidance_summary should identify the cancellation cause and agent activity.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.main import _build_cancellation_guidance_summary


class TestBuildCancellationGuidanceSummary:
    """Unit tests for _build_cancellation_guidance_summary."""

    def test_basic_fields(self):
        ticket: dict[str, Any] = {"comments": [], "custom_fields": {}}
        summary = _build_cancellation_guidance_summary(
            ticket, "executing_benchmark", "hard_stop"
        )
        assert summary["agent"] == "benchmark"
        assert summary["reason"] == "hard_stop"
        assert "interrupted" in summary["details"]
        assert len(summary["suggested_actions"]) == 3

    def test_includes_last_agent_message(self):
        ticket: dict[str, Any] = {
            "comments": [
                {"author": "benchmark-agent", "body": "Running fio on board-3"},
            ],
            "custom_fields": {},
        }
        summary = _build_cancellation_guidance_summary(
            ticket,
            "executing_benchmark",
            "hard_stop",
            agent_identity="benchmark-agent",
        )
        assert "Running fio on board-3" in summary["details"]

    def test_only_uses_comments_from_running_agent(self):
        ticket: dict[str, Any] = {
            "comments": [
                {"author": "fleet-coordinator", "body": "Provisioning boards"},
                {"author": "system", "body": "Status changed"},
                {"author": "alice", "body": "Please use another board"},
            ],
            "custom_fields": {},
        }
        summary = _build_cancellation_guidance_summary(
            ticket,
            "coordinating_fleet",
            "hard_stop",
            agent_identity="fleet-coordinator",
        )
        assert summary["agent"] == "fleet_coordinator"
        assert "Provisioning boards" in summary["details"]
        assert "Please use another board" not in summary["details"]

    def test_omits_comment_context_without_agent_identity(self):
        ticket: dict[str, Any] = {
            "comments": [{"author": "alice", "body": "Please use another board"}],
            "custom_fields": {},
        }
        summary = _build_cancellation_guidance_summary(
            ticket, "coordinating_fleet", "claim_lost"
        )
        assert summary["reason"] == "claim_lost"
        assert "Please use another board" not in summary["details"]

    def test_includes_benchmark_results_note(self):
        ticket: dict[str, Any] = {
            "comments": [],
            "custom_fields": {"benchmark_results": {"fio": {"iops": 1234}}},
        }
        summary = _build_cancellation_guidance_summary(
            ticket, "executing_benchmark", "hard_stop"
        )
        assert "benchmark results" in summary["details"].lower()

    def test_empty_status_defaults_to_unknown(self):
        ticket: dict[str, Any] = {"comments": [], "custom_fields": {}}
        summary = _build_cancellation_guidance_summary(ticket, "", "hard_stop")
        assert summary["agent"] == "unknown"

    def test_long_message_truncated(self):
        long_body = "x" * 3000
        ticket: dict[str, Any] = {
            "comments": [{"author": "analyze-agent", "body": long_body}],
            "custom_fields": {},
        }
        summary = _build_cancellation_guidance_summary(
            ticket, "analyzing", "hard_stop", agent_identity="analyze-agent"
        )
        # details should not include the full 3000-char message
        assert len(summary["details"]) < 1000

    def test_no_comments(self):
        ticket: dict[str, Any] = {"custom_fields": {}}
        summary = _build_cancellation_guidance_summary(
            ticket, "triage_pending", "hard_stop"
        )
        assert summary["agent"] == "triage"
        assert summary["reason"] == "hard_stop"
        assert "suggested_actions" in summary

    def test_suggested_actions_content(self):
        ticket: dict[str, Any] = {"comments": [], "custom_fields": {}}
        summary = _build_cancellation_guidance_summary(
            ticket, "executing_benchmark", "hard_stop"
        )
        actions = summary["suggested_actions"]
        # Should suggest retry, review, and abort
        assert any("retry" in a.lower() or "re-dispatch" in a.lower() for a in actions)
        assert any("abort" in a.lower() for a in actions)


class TestHardstopGuidanceIntegration:
    """Integration tests for cancellation guidance in run_agent_task."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("user_stop", "claim_lost", "expected_reason", "expected_comment"),
        [
            (True, False, "hard_stop", "Agent stopped by user request"),
            (False, False, "task_cancelled", "Agent stopped: task cancelled"),
            (
                False,
                True,
                "claim_lost",
                "Agent stopped: orchestrator claim lost",
            ),
        ],
        ids=["user-hard-stop", "generic-cancellation", "claim-loss"],
    )
    async def test_open_ticket_gets_cause_accurate_guidance(
        self,
        user_stop: bool,
        claim_lost: bool,
        expected_reason: str,
        expected_comment: str,
    ):
        """Open tickets get guidance for every cancellation cause."""
        from orchestrator.main import run_agent_task

        async def hanging_run(tid):
            await asyncio.sleep(100)

        agent = MagicMock()
        agent.agent_name = "benchmark-agent"
        agent.run = hanging_run
        agent.close = AsyncMock()

        dispatcher = MagicMock()
        dispatcher.create_agent.return_value = agent
        dispatcher.store_url = "http://localhost:9999"
        dispatcher.events = None
        dispatcher._trace_contexts = {}
        dispatcher.clear_agent = MagicMock()
        dispatcher.mark_done = AsyncMock()
        dispatcher._claim_ids = {}
        dispatcher.was_stopped_by_user.return_value = user_stop
        dispatcher.has_lost_claim.return_value = claim_lost

        # Mock HTTP client
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        # GET returns a ticket that is not closed
        ticket_response = MagicMock()
        ticket_response.status_code = 200
        ticket_response.json.return_value = {
            "status": "executing_benchmark",
            "comments": [
                {"author": "benchmark-agent", "body": "Running fio on board-3"},
                {"author": "alice", "body": "Please switch to a different board"},
            ],
            "custom_fields": {},
        }
        mock_client.get = AsyncMock(return_value=ticket_response)
        mock_client.patch = AsyncMock(return_value=MagicMock())
        mock_client.post = AsyncMock(return_value=MagicMock())

        with patch(
            "orchestrator.main.AuditedAsyncHTTPClient",
            return_value=mock_client,
        ):
            task = asyncio.create_task(
                run_agent_task(dispatcher, "executing_benchmark", "PERF-TEST1")
            )
            # Let the agent start
            await asyncio.sleep(0.05)
            task.cancel()
            # Should not raise — CancelledError is handled internally
            await task

        # Open tickets receive guidance whose reason matches the cancellation.
        mock_client.patch.assert_awaited_once()
        patch_call = mock_client.patch.await_args
        fields = patch_call.kwargs.get(
            "json", patch_call.args[1] if len(patch_call.args) > 1 else {}
        ).get("fields", {})
        assert fields["interrupted"] is True
        gs = fields["guidance_summary"]
        assert gs["agent"] == "benchmark"
        assert gs["reason"] == expected_reason
        assert "Running fio" in gs["details"]
        assert "Please switch to a different board" not in gs["details"]
        assert len(gs["suggested_actions"]) >= 1

        mock_client.post.assert_awaited_once()
        transition = mock_client.post.await_args.kwargs["json"]
        assert transition["status"] == "awaiting_customer_guidance"
        assert transition["comment"] == expected_comment

    @pytest.mark.asyncio
    async def test_cancelled_error_skips_closed_ticket(self):
        """Force-closed user hard stops do not get a guidance summary."""
        from orchestrator.main import run_agent_task

        async def hanging_run(tid):
            await asyncio.sleep(100)

        agent = MagicMock()
        agent.run = hanging_run
        agent.close = AsyncMock()

        dispatcher = MagicMock()
        dispatcher.create_agent.return_value = agent
        dispatcher.store_url = "http://localhost:9999"
        dispatcher.events = None
        dispatcher._trace_contexts = {}
        dispatcher.clear_agent = MagicMock()
        dispatcher.mark_done = AsyncMock()
        dispatcher._claim_ids = {}
        dispatcher.was_stopped_by_user.return_value = True
        dispatcher.has_lost_claim.return_value = False

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        # GET returns a closed ticket
        ticket_response = MagicMock()
        ticket_response.status_code = 200
        ticket_response.json.return_value = {
            "status": "closed",
            "comments": [],
            "custom_fields": {},
        }
        mock_client.get = AsyncMock(return_value=ticket_response)
        mock_client.patch = AsyncMock()
        mock_client.post = AsyncMock()

        with patch(
            "orchestrator.main.AuditedAsyncHTTPClient",
            return_value=mock_client,
        ):
            task = asyncio.create_task(
                run_agent_task(dispatcher, "executing_benchmark", "PERF-CLOSED")
            )
            await asyncio.sleep(0.05)
            task.cancel()
            await task

        # No PATCH or POST should have been made
        mock_client.patch.assert_not_awaited()
        mock_client.post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_active_hard_stop_keeps_ticket_force_closed(self, tmp_path):
        """The real stop path still closes the ticket after cancelling its agent."""
        import httpx

        from orchestrator.dispatcher import Dispatcher
        from orchestrator.main import _process_stop_requests, run_agent_task
        from state_store.main import create_app
        from state_store.models import CreateTicketRequest, TransitionRequest
        from state_store.store import TicketStore

        store = TicketStore(persist_dir=tmp_path)
        app = create_app(initialize_immediately=True)
        app.state.store = store
        ticket = store.create_ticket(
            CreateTicketRequest(summary="active hard stop", description="test")
        )
        for status in (
            "triage_pending",
            "awaiting_hardware",
            "awaiting_provision",
            "executing_benchmark",
        ):
            store.transition_ticket(ticket.id, TransitionRequest(status=status))
        store.update_fields(
            ticket.id,
            {
                "stop_requested": {
                    "mode": "hard",
                    "requested_at": "2026-01-01T00:00:00Z",
                },
            },
        )

        agent_started = asyncio.Event()

        class BlockingAgent:
            agent_name = "benchmark-agent"

            async def run(self, ticket_id: str) -> None:
                agent_started.set()
                await asyncio.Future()

            async def close(self) -> None:
                return None

        dispatcher = Dispatcher(
            state_store_url="http://testserver",
            llm_provider=MagicMock(),
            skill_provider=MagicMock(),
        )
        dispatcher.events = None
        dispatcher.create_agent = MagicMock(return_value=BlockingAgent())
        dispatcher.release_claim = AsyncMock()

        def make_client(**kwargs):
            return httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
                headers=kwargs.get("headers", {}),
            )

        with (
            patch(
                "orchestrator.main._auth_headers",
                return_value={"Authorization": f"Bearer {app.state.api_token}"},
            ),
            patch("orchestrator.main.AuditedAsyncHTTPClient", make_client),
        ):
            task = asyncio.create_task(
                run_agent_task(dispatcher, "executing_benchmark", ticket.id)
            )
            dispatcher.set_task(ticket.id, task, status="executing_benchmark")
            await agent_started.wait()
            await _process_stop_requests(dispatcher, "http://testserver")
            await asyncio.wait_for(task, timeout=5)

        assert store.get_ticket(ticket.id).status.value == "closed"

    @pytest.mark.asyncio
    async def test_claim_loss_mutations_keep_audited_trace_and_leader_fence(
        self, monkeypatch
    ):
        """Claim-loss recovery uses the real audited wrapper with ticket context."""
        import httpx

        from orchestrator.main import run_agent_task
        from providers.execution import (
            AuditedAsyncHTTPClient as RealAuditedAsyncHTTPClient,
        )
        from providers.tracing import new_trace_context

        ticket_id = "PERF-TRACE-LOSS"
        monkeypatch.setenv("AGENTIC_PERF_API_TOKEN", "test-token")
        monkeypatch.setenv(
            "AGENTIC_PERF_ORCHESTRATOR_SESSION_ID",
            "00000000-0000-0000-0000-000000000001",
        )
        monkeypatch.setenv("AGENTIC_PERF_ORCHESTRATOR_EPOCH", "1")

        requests = []
        ticket = {
            "status": "executing_benchmark",
            "comments": [
                {"author": "benchmark-agent", "body": "Running fio"},
            ],
            "custom_fields": {},
        }

        def handle_request(request):
            requests.append(request)
            if request.method == "GET":
                return httpx.Response(200, json=ticket)
            return httpx.Response(200, json={})

        transport = httpx.MockTransport(handle_request)
        trace_events = []

        async def record_trace(event):
            trace_events.append(event)

        clients = []

        def audited_client_factory(*args, **kwargs):
            wrapper = RealAuditedAsyncHTTPClient(
                client=httpx.AsyncClient(
                    *args,
                    transport=transport,
                    **kwargs,
                ),
                emit=record_trace,
            )
            clients.append(wrapper)
            return wrapper

        started = asyncio.Event()

        async def hanging_run(_ticket_id):
            started.set()
            await asyncio.Future()

        agent = MagicMock()
        agent.agent_name = "benchmark-agent"
        agent.trace_context = new_trace_context(
            ticket_id=ticket_id,
            agent_id="benchmark-agent",
        )
        agent.run = hanging_run
        agent.close = AsyncMock()

        dispatcher = MagicMock()
        dispatcher.create_agent.return_value = agent
        dispatcher.store_url = "http://ticket-store"
        dispatcher.events = None
        dispatcher._trace_contexts = {}
        dispatcher._claim_ids = {ticket_id: "stale-claim-id"}
        dispatcher.clear_agent = MagicMock()
        dispatcher.mark_done = AsyncMock()
        dispatcher.was_stopped_by_user.return_value = False
        dispatcher.has_lost_claim.return_value = True
        dispatcher.is_deposed.return_value = True

        with patch(
            "orchestrator.main.AuditedAsyncHTTPClient",
            side_effect=audited_client_factory,
        ):
            task = asyncio.create_task(
                run_agent_task(dispatcher, "executing_benchmark", ticket_id)
            )
            await started.wait()
            task.cancel()
            await task

        mutating_requests = [
            request for request in requests if request.method in ("PATCH", "POST")
        ]
        assert {request.method for request in mutating_requests} == {"PATCH", "POST"}
        for request in mutating_requests:
            assert request.headers["X-Agentic-Perf-Causal-Context"] == "v1"
            assert request.headers["X-Agentic-Perf-Ticket-Id"] == ticket_id
            assert request.headers["X-Agentic-Perf-Mutation-Scope"] == "leader"
            assert "X-Agentic-Perf-Claim-Id" not in request.headers
            assert request.headers["traceparent"].startswith("00-")
        assert trace_events
        assert {event.ticket_id for event in trace_events} == {ticket_id}
        assert clients
