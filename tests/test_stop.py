"""Tests for graceful and hard stop functionality.

Covers: stop API endpoints, agent stop flag, dispatcher stop_agent.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from state_store.main import create_app
from state_store.models import (
    CreateTicketRequest,
    TransitionRequest,
)
from state_store.store import TicketStore

# ── Fixtures ──────────────────────────────────────────────


@pytest.fixture
def store(tmp_path):
    return TicketStore(persist_dir=tmp_path)


@pytest.fixture
def app(store):
    application = create_app()
    application.state.store = store
    return application


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def active_ticket(store):
    ticket = store.create_ticket(
        CreateTicketRequest(summary="test", description="test"),
    )
    for status in [
        "triage_pending",
        "awaiting_hardware",
        "awaiting_provision",
        "executing_benchmark",
    ]:
        store.transition_ticket(
            ticket.id,
            TransitionRequest(status=status),
        )
    return store.get_ticket(ticket.id)


@pytest.fixture
def closed_ticket(store):
    ticket = store.create_ticket(
        CreateTicketRequest(summary="closed", description="closed"),
    )
    for status in [
        "triage_pending",
        "awaiting_hardware",
        "awaiting_provision",
        "executing_benchmark",
        "awaiting_review",
        "awaiting_teardown",
        "closed",
    ]:
        store.transition_ticket(
            ticket.id,
            TransitionRequest(status=status),
        )
    return store.get_ticket(ticket.id)


# ── State Store API Tests ─────────────────────────────────


class TestStopEndpoint:
    def test_stop_sets_custom_field(self, client, active_ticket):
        r = client.post(
            f"/api/v1/tickets/{active_ticket.id}/stop",
            json={"mode": "graceful"},
        )
        assert r.status_code == 200
        data = r.json()
        stop_req = data["custom_fields"]["stop_requested"]
        assert stop_req["mode"] == "graceful"
        assert "requested_at" in stop_req

    def test_stop_hard_mode(self, client, active_ticket):
        r = client.post(
            f"/api/v1/tickets/{active_ticket.id}/stop",
            json={"mode": "hard"},
        )
        assert r.status_code == 200
        assert r.json()["custom_fields"]["stop_requested"]["mode"] == "hard"

    def test_stop_default_mode_is_graceful(self, client, active_ticket):
        r = client.post(
            f"/api/v1/tickets/{active_ticket.id}/stop",
            json={},
        )
        assert r.status_code == 200
        assert r.json()["custom_fields"]["stop_requested"]["mode"] == "graceful"

    def test_stop_terminal_ticket_returns_409(self, client, closed_ticket):
        r = client.post(
            f"/api/v1/tickets/{closed_ticket.id}/stop",
            json={"mode": "graceful"},
        )
        assert r.status_code == 409
        assert "terminal" in r.json()["detail"].lower()

    def test_stop_guidance_ticket_returns_409(self, client, active_ticket, store):
        store.transition_ticket(
            active_ticket.id,
            TransitionRequest(status="awaiting_customer_guidance"),
        )
        r = client.post(
            f"/api/v1/tickets/{active_ticket.id}/stop",
            json={"mode": "graceful"},
        )
        assert r.status_code == 409

    def test_stop_nonexistent_ticket_returns_404(self, client):
        r = client.post(
            "/api/v1/tickets/PERF-nonexist/stop",
            json={"mode": "graceful"},
        )
        assert r.status_code == 404


class TestStopAllEndpoint:
    def test_stop_all_affects_active_tickets(
        self,
        client,
        store,
        active_ticket,
    ):
        r = client.post("/api/v1/stop-all", json={"mode": "graceful"})
        assert r.status_code == 200
        data = r.json()
        assert data["count"] >= 1
        ids = [t["id"] for t in data["affected"]]
        assert active_ticket.id in ids

    def test_stop_all_skips_terminal(self, client, store, closed_ticket):
        r = client.post("/api/v1/stop-all", json={"mode": "graceful"})
        assert r.status_code == 200
        ids = [t["id"] for t in r.json()["affected"]]
        assert closed_ticket.id not in ids

    def test_stop_all_empty_when_no_active(self, client, store, closed_ticket):
        r = client.post("/api/v1/stop-all", json={"mode": "graceful"})
        assert r.status_code == 200
        assert r.json()["count"] == 0


# ── Agent Stop Flag Tests ─────────────────────────────────


class TestAgentStopFlag:
    def test_request_stop_sets_flag(self):
        from agents.base import AgentBase

        agent = MagicMock(spec=AgentBase)
        agent._stop_requested = False
        AgentBase.request_stop(agent)
        assert agent._stop_requested is True

    @pytest.mark.asyncio
    async def test_graceful_stop_breaks_loop(self):
        from agents.base import AgentBase

        mock_llm = MagicMock()
        mock_llm.complete = AsyncMock()

        agent = MagicMock(spec=AgentBase)
        agent._stop_requested = True
        agent.agent_name = "test-agent"
        agent._events = None
        agent._client = AsyncMock()
        agent.store_url = "http://localhost:8090"
        agent.max_iterations = 10
        agent._budget_grace = False

        agent._emit = MagicMock()
        agent._transition_ticket = AsyncMock()
        agent._get_ticket = AsyncMock(
            return_value={"id": "PERF-test", "custom_fields": {}},
        )
        agent._system_prompt = MagicMock(return_value="prompt")
        agent._build_messages = MagicMock(return_value=[])

        await AgentBase.run(agent, "PERF-test")

        agent._emit.assert_any_call(
            "PERF-test",
            "agent_stopped",
            {"mode": "graceful"},
        )
        agent._transition_ticket.assert_called_once_with(
            "PERF-test",
            "awaiting_customer_guidance",
            comment="Agent stopped (graceful) by user request",
        )
        mock_llm.complete.assert_not_called()


# ── Dispatcher Stop Tests ─────────────────────────────────


class TestDispatcherStop:
    def test_stop_agent_graceful(self):
        from orchestrator.dispatcher import Dispatcher

        dispatcher = Dispatcher(
            state_store_url="http://localhost:8090",
            llm_provider=MagicMock(),
            skill_provider=MagicMock(),
        )
        agent = MagicMock()
        agent.request_stop = MagicMock()
        dispatcher._agents["PERF-test"] = agent

        result = dispatcher.stop_agent("PERF-test", "graceful")
        assert result is True
        agent.request_stop.assert_called_once()

    def test_stop_agent_hard(self):
        from orchestrator.dispatcher import Dispatcher

        dispatcher = Dispatcher(
            state_store_url="http://localhost:8090",
            llm_provider=MagicMock(),
            skill_provider=MagicMock(),
        )
        task = MagicMock()
        task.done.return_value = False
        task.cancel = MagicMock()
        dispatcher._tasks["PERF-test"] = task

        result = dispatcher.stop_agent("PERF-test", "hard")
        assert result is True
        task.cancel.assert_called_once()

    def test_stop_agent_not_active(self):
        from orchestrator.dispatcher import Dispatcher

        dispatcher = Dispatcher(
            state_store_url="http://localhost:8090",
            llm_provider=MagicMock(),
            skill_provider=MagicMock(),
        )
        result = dispatcher.stop_agent("PERF-nonexist", "graceful")
        assert result is False

    def test_mark_done_clears_agent(self):
        from orchestrator.dispatcher import Dispatcher

        dispatcher = Dispatcher(
            state_store_url="http://localhost:8090",
            llm_provider=MagicMock(),
            skill_provider=MagicMock(),
        )
        dispatcher._agents["PERF-test"] = MagicMock()
        dispatcher._tasks["PERF-test"] = MagicMock()
        dispatcher.mark_done("PERF-test")
        assert "PERF-test" not in dispatcher._agents
        assert "PERF-test" not in dispatcher._tasks
