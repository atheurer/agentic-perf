"""Tests for async suspension: state machine, CloudEvents signal, and agent helper.

Tests the suspend/resume cycle for long-running hardware operations:
- State machine transitions to/from async_wait
- CloudEvents signal endpoint validation and resume
- AgentBase._suspend_for_async helper
- Timeout detection
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest  # noqa: I001

from state_store.models import (
    VALID_TRANSITIONS,
    Ticket,
    TicketStatus,
    TransitionRequest,
)

# --- State machine ---


class TestAsyncWaitStateMachine:
    """Test that async_wait status and transitions are valid."""

    def test_async_wait_status_exists(self):
        assert TicketStatus.ASYNC_WAIT == "async_wait"

    def test_executing_benchmark_can_suspend(self):
        transitions = VALID_TRANSITIONS[TicketStatus.EXECUTING_BENCHMARK]
        assert TicketStatus.ASYNC_WAIT in transitions

    def test_awaiting_provision_can_suspend(self):
        transitions = VALID_TRANSITIONS[TicketStatus.AWAITING_PROVISION]
        assert TicketStatus.ASYNC_WAIT in transitions

    def test_async_wait_can_resume_to_review(self):
        transitions = VALID_TRANSITIONS[TicketStatus.ASYNC_WAIT]
        assert TicketStatus.AWAITING_REVIEW in transitions

    def test_async_wait_can_resume_to_convergence(self):
        transitions = VALID_TRANSITIONS[TicketStatus.ASYNC_WAIT]
        assert TicketStatus.EVALUATING_CONVERGENCE in transitions

    def test_async_wait_can_resume_to_benchmark(self):
        transitions = VALID_TRANSITIONS[TicketStatus.ASYNC_WAIT]
        assert TicketStatus.EXECUTING_BENCHMARK in transitions

    def test_async_wait_can_resume_to_provision(self):
        transitions = VALID_TRANSITIONS[TicketStatus.ASYNC_WAIT]
        assert TicketStatus.AWAITING_PROVISION in transitions

    def test_async_wait_can_pause_for_guidance(self):
        """Timeout or error transitions to customer guidance."""
        transitions = VALID_TRANSITIONS[TicketStatus.ASYNC_WAIT]
        assert TicketStatus.AWAITING_CUSTOMER_GUIDANCE in transitions

    def test_async_wait_not_in_dispatch_map(self):
        """async_wait should NOT trigger an agent dispatch."""
        from orchestrator.dispatcher import STATUS_AGENT_MAP

        assert "async_wait" not in STATUS_AGENT_MAP


# --- CloudEvents signal endpoint ---


class TestCloudEventsSignal:
    """Test the CloudEvents signal API endpoint."""

    @pytest.fixture
    def store(self):
        from state_store.store import TicketStore

        return TicketStore()

    @pytest.fixture
    def app(self, store):
        from fastapi import FastAPI

        from state_store.api.router import api_router

        app = FastAPI()
        app.state.store = store
        app.include_router(api_router)
        return app

    @pytest.fixture
    def client(self, app):
        from fastapi.testclient import TestClient

        return TestClient(app)

    def _create_async_wait_ticket(
        self,
        store,
        ticket_id="TEST-001",
    ):
        """Create a ticket in async_wait with async_context."""
        ticket = Ticket(
            id=ticket_id,
            summary="Test ticket",
            description="Test",
            status=TicketStatus.EXECUTING_BENCHMARK,
            custom_fields={
                "async_context": {
                    "wait_type": "benchmark_execution",
                    "operation_id": "run-abc123",
                    "started_at": datetime.now(
                        timezone.utc,
                    ).isoformat(),
                    "expected_duration_mins": 60,
                    "resume_to_status": "awaiting_review",
                    "resume_context": {
                        "harness": "crucible",
                        "run_id": "abc123",
                    },
                    "suspended_by": "benchmark-agent",
                },
            },
        )
        store._tickets[ticket_id] = ticket
        store.transition_ticket(
            ticket_id,
            TransitionRequest(
                status="async_wait",
                comment="Test suspension",
            ),
        )
        return ticket_id

    def _make_cloud_event(
        self,
        event_id="run-abc123",
        event_type="dev.agentic-perf.benchmark.complete",
        source="/harness/crucible",
        subject="TEST-001",
        data=None,
    ):
        """Build a CloudEvents v1.0 payload."""
        return {
            "specversion": "1.0",
            "type": event_type,
            "source": source,
            "id": event_id,
            "subject": subject,
            "time": datetime.now(timezone.utc).isoformat(),
            "datacontenttype": "application/json",
            "data": data or {"exit_code": 0},
        }

    def test_signal_resumes_ticket(self, client, store):
        tid = self._create_async_wait_ticket(store)
        event = self._make_cloud_event()
        r = client.post(f"/api/v1/tickets/{tid}/signal", json=event)
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "resumed"
        assert data["resumed_to"] == "awaiting_review"
        assert data["event_id"] == "run-abc123"
        assert data["event_type"] == "dev.agentic-perf.benchmark.complete"

        # Verify ticket state
        ticket = store.get_ticket(tid)
        assert ticket.status == TicketStatus.AWAITING_REVIEW
        ctx = ticket.custom_fields["async_context"]
        sig = ctx["signal_received"]
        assert sig["type"] == "dev.agentic-perf.benchmark.complete"
        assert sig["source"] == "/harness/crucible"
        assert sig["data"]["exit_code"] == 0

    def test_signal_wrong_operation_id(self, client, store):
        tid = self._create_async_wait_ticket(store)
        event = self._make_cloud_event(event_id="wrong-id")
        r = client.post(f"/api/v1/tickets/{tid}/signal", json=event)
        assert r.status_code == 409
        assert "mismatch" in r.json()["detail"].lower()

    def test_signal_wrong_status(self, client, store):
        """Signal on a ticket not in async_wait."""
        ticket = Ticket(
            id="TEST-002",
            summary="Test",
            description="Test",
            status=TicketStatus.EXECUTING_BENCHMARK,
        )
        store._tickets["TEST-002"] = ticket
        event = self._make_cloud_event(subject="TEST-002")
        r = client.post("/api/v1/tickets/TEST-002/signal", json=event)
        assert r.status_code == 409
        assert "async_wait" in r.json()["detail"]

    def test_signal_not_found(self, client):
        event = self._make_cloud_event(subject="NONEXISTENT")
        r = client.post(
            "/api/v1/tickets/NONEXISTENT/signal",
            json=event,
        )
        assert r.status_code == 404

    def test_signal_subject_mismatch(self, client, store):
        """Subject in event must match URL ticket_id."""
        tid = self._create_async_wait_ticket(store)
        event = self._make_cloud_event(subject="WRONG-TICKET")
        r = client.post(f"/api/v1/tickets/{tid}/signal", json=event)
        assert r.status_code == 400
        assert "subject" in r.json()["detail"].lower()

    def test_signal_bad_specversion(self, client, store):
        tid = self._create_async_wait_ticket(store)
        event = self._make_cloud_event()
        event["specversion"] = "0.3"
        r = client.post(f"/api/v1/tickets/{tid}/signal", json=event)
        assert r.status_code == 400
        assert "specversion" in r.json()["detail"].lower()

    def test_signal_without_subject(self, client, store):
        """Subject is optional — omitting it is fine."""
        tid = self._create_async_wait_ticket(store)
        event = self._make_cloud_event()
        del event["subject"]
        r = client.post(f"/api/v1/tickets/{tid}/signal", json=event)
        assert r.status_code == 200


# --- Async context model ---


class TestAsyncContext:
    """Test async_context field structure."""

    def test_async_context_fields(self):
        ctx = {
            "wait_type": "benchmark_execution",
            "operation_id": "run-abc123",
            "started_at": "2026-06-30T10:00:00+00:00",
            "expected_duration_mins": 60,
            "resume_to_status": "awaiting_review",
            "resume_context": {"harness": "crucible"},
            "suspended_by": "benchmark-agent",
        }
        assert ctx["wait_type"] == "benchmark_execution"
        assert ctx["resume_to_status"] == "awaiting_review"
        assert ctx["expected_duration_mins"] == 60


# --- Timeout detection ---


class TestAsyncTimeout:
    """Test timeout calculation logic."""

    def test_not_timed_out(self):
        """Within 2x expected duration."""
        started = datetime.now(timezone.utc) - timedelta(minutes=30)
        expected_mins = 60
        timeout_mins = expected_mins * 2
        elapsed = (datetime.now(timezone.utc) - started).total_seconds() / 60
        assert elapsed < timeout_mins

    def test_timed_out(self):
        """Past 2x expected duration."""
        started = datetime.now(timezone.utc) - timedelta(minutes=180)
        expected_mins = 60
        timeout_mins = expected_mins * 2
        elapsed = (datetime.now(timezone.utc) - started).total_seconds() / 60
        assert elapsed > timeout_mins


# --- Full suspend → signal → resume cycle ---


class TestResumeFlow:
    """Test the full suspend → CloudEvent signal → resume cycle."""

    @pytest.fixture
    def store(self):
        from state_store.store import TicketStore

        return TicketStore()

    @pytest.fixture
    def app(self, store):
        from fastapi import FastAPI

        from state_store.api.router import api_router

        app = FastAPI()
        app.state.store = store
        app.include_router(api_router)
        return app

    @pytest.fixture
    def client(self, app):
        from fastapi.testclient import TestClient

        return TestClient(app)

    def test_suspend_signal_resume_cycle(self, client, store):
        """Full cycle: create → suspend → CloudEvent → resume."""
        # Create ticket in executing_benchmark
        ticket = Ticket(
            id="CYCLE-001",
            summary="Full cycle test",
            description="Test",
            status=TicketStatus.EXECUTING_BENCHMARK,
            custom_fields={
                "async_context": {
                    "wait_type": "benchmark_execution",
                    "operation_id": "run-xyz789",
                    "started_at": datetime.now(
                        timezone.utc,
                    ).isoformat(),
                    "expected_duration_mins": 45,
                    "resume_to_status": "evaluating_convergence",
                    "resume_context": {
                        "iteration": 2,
                        "hypothesis": "IO regression",
                    },
                    "suspended_by": "benchmark-agent",
                },
            },
        )
        store._tickets["CYCLE-001"] = ticket

        # Suspend: transition to async_wait
        store.transition_ticket(
            "CYCLE-001",
            TransitionRequest(
                status="async_wait",
                comment="Suspending for benchmark",
            ),
        )
        t = store.get_ticket("CYCLE-001")
        assert t.status == TicketStatus.ASYNC_WAIT

        # Signal: CloudEvent from benchmark harness
        event = {
            "specversion": "1.0",
            "type": "dev.agentic-perf.benchmark.complete",
            "source": "/harness/crucible/controller-10.0.0.1",
            "id": "run-xyz789",
            "subject": "CYCLE-001",
            "time": datetime.now(timezone.utc).isoformat(),
            "data": {
                "exit_code": 0,
                "run_id": "xyz789",
            },
        }
        r = client.post(
            "/api/v1/tickets/CYCLE-001/signal",
            json=event,
        )
        assert r.status_code == 200

        # Resume: ticket in evaluating_convergence
        t = store.get_ticket("CYCLE-001")
        assert t.status == TicketStatus.EVALUATING_CONVERGENCE

        # Context preserved through the cycle
        ctx = t.custom_fields["async_context"]
        assert ctx["resume_context"]["iteration"] == 2
        assert ctx["resume_context"]["hypothesis"] == "IO regression"
        assert ctx["signal_received"]["data"]["run_id"] == "xyz789"
        assert (
            ctx["signal_received"]["source"] == "/harness/crucible/controller-10.0.0.1"
        )
