"""Tests for graceful stop, hard stop, and abort functionality.

Covers: stop API endpoints, abort endpoint, agent stop flag,
abort drift guard, dispatcher stop_agent.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

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
    c = TestClient(app)
    c.headers["Authorization"] = f"Bearer {app.state.api_token}"
    return c


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


class TestForceCloseEndpoint:
    def test_force_close_from_new(self, client, store):
        ticket = store.create_ticket(
            CreateTicketRequest(summary="test", description="test"),
        )
        r = client.post(f"/api/v1/tickets/{ticket.id}/force-close")
        assert r.status_code == 200
        assert r.json()["status"] == "closed"
        assert store.get_ticket(ticket.id).status.value == "closed"

    def test_force_close_from_triage_pending(self, client, store):
        ticket = store.create_ticket(
            CreateTicketRequest(summary="test", description="test"),
        )
        store.transition_ticket(
            ticket.id,
            TransitionRequest(status="triage_pending"),
        )
        r = client.post(f"/api/v1/tickets/{ticket.id}/force-close")
        assert r.status_code == 200
        assert r.json()["status"] == "closed"

    def test_force_close_already_closed(self, client, closed_ticket):
        r = client.post(
            f"/api/v1/tickets/{closed_ticket.id}/force-close",
        )
        assert r.status_code == 200
        assert r.json()["status"] == "closed"

    def test_force_close_nonexistent_returns_404(self, client):
        r = client.post("/api/v1/tickets/PERF-nonexist/force-close")
        assert r.status_code == 404


class TestForceCloseStore:
    def test_force_close_persists_file(self, store, tmp_path):
        ticket = store.create_ticket(
            CreateTicketRequest(summary="test", description="test"),
        )
        store.force_close(ticket.id, comment="admin close")
        import json

        data = json.loads(
            (tmp_path / f"{ticket.id}.json").read_text(),
        )
        assert data["status"] == "closed"
        assert any("admin close" in c["body"] for c in data["comments"])

    def test_force_close_records_previous_status(self, store):
        ticket = store.create_ticket(
            CreateTicketRequest(summary="test", description="test"),
        )
        store.transition_ticket(
            ticket.id,
            TransitionRequest(status="triage_pending"),
        )
        result = store.force_close(ticket.id)
        assert result.previous_status.value == "triage_pending"

    def test_force_close_clears_claim_and_stop(self, store):
        ticket = store.create_ticket(
            CreateTicketRequest(summary="test", description="test"),
        )
        store.update_fields(
            ticket.id,
            {
                "claim": {"owner": "orch-1", "ts": "2026-01-01"},
                "stop_requested": {"mode": "hard"},
            },
        )
        result = store.force_close(ticket.id)
        assert "claim" not in result.custom_fields
        assert "stop_requested" not in result.custom_fields


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
        agent.DEFAULT_GLOBAL_MAX_ITERATIONS = 100
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


# ── Process Stop Requests Integration ────────────────────


class TestProcessStopRequests:
    """Verify _process_stop_requests handles non-active tickets."""

    @pytest.mark.asyncio
    async def test_hard_stop_closes_non_active(self, app, store, client):
        """Non-active ticket with hard stop_requested gets force-closed."""
        ticket = store.create_ticket(
            CreateTicketRequest(summary="test", description="test"),
        )
        store.transition_ticket(
            ticket.id,
            TransitionRequest(status="triage_pending"),
        )
        store.update_fields(
            ticket.id,
            {
                "stop_requested": {
                    "mode": "hard",
                    "requested_at": "2026-01-01T00:00:00Z",
                },
            },
        )

        from orchestrator.dispatcher import Dispatcher
        from orchestrator.main import _process_stop_requests

        dispatcher = Dispatcher(
            state_store_url="http://testserver",
            llm_provider=MagicMock(),
            skill_provider=MagicMock(),
        )
        dispatcher._auth_headers = lambda: {
            "Authorization": f"Bearer {app.state.api_token}",
        }

        from unittest.mock import patch

        import httpx

        # Patch _auth_headers at module level so the orchestrator
        # function picks up the test token.
        with patch(
            "orchestrator.main._auth_headers",
            return_value={
                "Authorization": f"Bearer {app.state.api_token}",
            },
        ):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as _:
                # _process_stop_requests creates its own client,
                # so we need to monkeypatch httpx.AsyncClient
                original_init = httpx.AsyncClient.__init__

                def patched_init(self_client, **kwargs):
                    kwargs.pop("timeout", None)
                    kwargs.pop("headers", None)
                    original_init(
                        self_client,
                        transport=transport,
                        base_url="http://testserver",
                        headers={
                            "Authorization": (f"Bearer {app.state.api_token}"),
                        },
                        timeout=10.0,
                    )

                with patch.object(
                    httpx.AsyncClient,
                    "__init__",
                    patched_init,
                ):
                    await _process_stop_requests(
                        dispatcher,
                        "http://testserver",
                    )

        result = store.get_ticket(ticket.id)
        assert result.status.value == "closed"

    @pytest.mark.asyncio
    async def test_graceful_stop_pauses_non_active(
        self,
        app,
        store,
        client,
    ):
        """Non-active ticket with graceful stop gets paused."""
        ticket = store.create_ticket(
            CreateTicketRequest(summary="test", description="test"),
        )
        store.transition_ticket(
            ticket.id,
            TransitionRequest(status="triage_pending"),
        )
        store.update_fields(
            ticket.id,
            {
                "stop_requested": {
                    "mode": "graceful",
                    "requested_at": "2026-01-01T00:00:00Z",
                },
            },
        )

        from orchestrator.dispatcher import Dispatcher
        from orchestrator.main import _process_stop_requests

        dispatcher = Dispatcher(
            state_store_url="http://testserver",
            llm_provider=MagicMock(),
            skill_provider=MagicMock(),
        )

        from unittest.mock import patch

        import httpx

        with patch(
            "orchestrator.main._auth_headers",
            return_value={
                "Authorization": f"Bearer {app.state.api_token}",
            },
        ):
            transport = httpx.ASGITransport(app=app)
            original_init = httpx.AsyncClient.__init__

            def patched_init(self_client, **kwargs):
                kwargs.pop("timeout", None)
                kwargs.pop("headers", None)
                original_init(
                    self_client,
                    transport=transport,
                    base_url="http://testserver",
                    headers={
                        "Authorization": (f"Bearer {app.state.api_token}"),
                    },
                    timeout=10.0,
                )

            with patch.object(
                httpx.AsyncClient,
                "__init__",
                patched_init,
            ):
                await _process_stop_requests(
                    dispatcher,
                    "http://testserver",
                )

        result = store.get_ticket(ticket.id)
        assert result.status.value == "awaiting_customer_guidance"


# ── Abort Endpoint Tests ─────────────────────────────────


@pytest.fixture
def guidance_ticket(store):
    """A ticket parked in awaiting_customer_guidance."""
    ticket = store.create_ticket(
        CreateTicketRequest(summary="test", description="test"),
    )
    for status in [
        "triage_pending",
        "awaiting_hardware",
    ]:
        store.transition_ticket(
            ticket.id,
            TransitionRequest(status=status),
        )
    store.transition_ticket(
        ticket.id,
        TransitionRequest(status="awaiting_customer_guidance"),
    )
    return store.get_ticket(ticket.id)


class TestAbortEndpoint:
    def test_abort_sets_marker(self, client, guidance_ticket):
        r = client.post(f"/api/v1/tickets/{guidance_ticket.id}/abort")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "awaiting_teardown"
        marker = data["custom_fields"]["abort_requested"]
        assert "requested_at" in marker

    def test_abort_retires_plan_step(self, client, store, guidance_ticket):
        plan = {
            "current_step": 1,
            "steps": [
                {
                    "id": 0,
                    "agent_type": "benchmark",
                    "status": "completed",
                    "params": {},
                },
                {
                    "id": 1,
                    "agent_type": "review",
                    "status": "in_progress",
                    "params": {},
                },
            ],
        }
        store.update_fields(
            guidance_ticket.id,
            {"execution_plan": plan},
        )
        r = client.post(f"/api/v1/tickets/{guidance_ticket.id}/abort")
        assert r.status_code == 200
        updated_plan = r.json()["custom_fields"]["execution_plan"]
        assert updated_plan["steps"][1]["status"] == "aborted"

    def test_abort_non_guidance_returns_409(self, client, active_ticket):
        r = client.post(f"/api/v1/tickets/{active_ticket.id}/abort")
        assert r.status_code == 409

    def test_abort_with_reason(self, client, store, guidance_ticket):
        r = client.post(
            f"/api/v1/tickets/{guidance_ticket.id}/abort",
            json={"reason": "Aborting via TUI"},
        )
        assert r.status_code == 200
        ticket = store.get_ticket(guidance_ticket.id)
        comments = [c.body for c in ticket.comments]
        assert any("Aborting via TUI" in c for c in comments)

    def test_abort_nonexistent_returns_404(self, client):
        r = client.post("/api/v1/tickets/PERF-nonexist/abort")
        assert r.status_code == 404


# ── Store-level Abort Marker Tests ───────────────────────


class TestStoreAbortMarker:
    def test_guidance_to_teardown_sets_marker(self, store):
        ticket = store.create_ticket(
            CreateTicketRequest(summary="test", description="test"),
        )
        for status in ["triage_pending", "awaiting_hardware"]:
            store.transition_ticket(
                ticket.id,
                TransitionRequest(status=status),
            )
        store.transition_ticket(
            ticket.id,
            TransitionRequest(status="awaiting_customer_guidance"),
        )
        result = store.transition_ticket(
            ticket.id,
            TransitionRequest(status="awaiting_teardown"),
        )
        assert "abort_requested" in result.custom_fields
        assert "requested_at" in result.custom_fields["abort_requested"]

    def test_normal_teardown_does_not_set_marker(self, store):
        ticket = store.create_ticket(
            CreateTicketRequest(summary="test", description="test"),
        )
        for status in [
            "triage_pending",
            "awaiting_hardware",
            "awaiting_provision",
            "executing_benchmark",
            "awaiting_review",
            "awaiting_teardown",
        ]:
            store.transition_ticket(
                ticket.id,
                TransitionRequest(status=status),
            )
        result = store.get_ticket(ticket.id)
        assert "abort_requested" not in result.custom_fields


# ── HITL Drift Guard Tests ───────────────────────────────


class TestHITLDriftGuard:
    @pytest.mark.asyncio
    async def test_drift_guard_raises_on_abort(self):
        from agents.base import AgentBase, HITLDriftError

        agent = MagicMock(spec=AgentBase)
        agent.agent_name = "review-agent"
        agent._events = None
        agent._client = AsyncMock()
        agent.store_url = "http://localhost:8090"
        agent._emit = MagicMock()
        agent._add_comment = AsyncMock()
        agent._transition_ticket = AsyncMock()
        agent._HITL_POLL_INTERVAL = 0.01
        agent._HITL_TIMEOUT = 0.1
        agent._HITL_NO_RESUME_STATUSES = AgentBase._HITL_NO_RESUME_STATUSES

        call_count = 0

        async def fake_get_ticket(ticket_id):
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return {
                    "id": ticket_id,
                    "status": "awaiting_customer_guidance",
                    "comments": [],
                    "custom_fields": {},
                }
            return {
                "id": ticket_id,
                "status": "awaiting_teardown",
                "comments": [],
                "custom_fields": {
                    "abort_requested": {
                        "requested_at": "2026-08-06T00:00:00Z",
                    },
                },
            }

        agent._get_ticket = AsyncMock(side_effect=fake_get_ticket)

        with pytest.raises(HITLDriftError, match="awaiting_teardown"):
            await AgentBase._request_human_input(
                agent,
                "PERF-TEST",
                "Need input",
            )

        agent._emit.assert_any_call(
            "PERF-TEST",
            "agent_aborted",
            {
                "reason": "ticket_drifted",
                "new_status": "awaiting_teardown",
                "abort_requested": True,
            },
        )

    @pytest.mark.asyncio
    async def test_drift_guard_on_closed_ticket(self):
        from agents.base import AgentBase, HITLDriftError

        agent = MagicMock(spec=AgentBase)
        agent.agent_name = "review-agent"
        agent._events = None
        agent._client = AsyncMock()
        agent.store_url = "http://localhost:8090"
        agent._emit = MagicMock()
        agent._add_comment = AsyncMock()
        agent._transition_ticket = AsyncMock()
        agent._HITL_POLL_INTERVAL = 0.01
        agent._HITL_TIMEOUT = 0.1
        agent._HITL_NO_RESUME_STATUSES = AgentBase._HITL_NO_RESUME_STATUSES

        async def fake_get_ticket(ticket_id):
            return {
                "id": ticket_id,
                "status": "closed",
                "comments": [],
                "custom_fields": {},
            }

        agent._get_ticket = AsyncMock(side_effect=fake_get_ticket)

        with pytest.raises(HITLDriftError, match="closed"):
            await AgentBase._request_human_input(
                agent,
                "PERF-TEST",
                "Need input",
            )


# ── _advance_plan Abort Guard Test ───────────────────────


class TestAdvancePlanAbortGuard:
    def test_advance_plan_noop_when_aborted(self):
        from orchestrator.main import _advance_plan

        plan = {
            "current_step": 0,
            "steps": [
                {
                    "id": 0,
                    "agent_type": "benchmark",
                    "status": "in_progress",
                    "params": {},
                },
                {
                    "id": 1,
                    "agent_type": "review",
                    "status": "pending",
                    "params": {},
                },
            ],
        }
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "status": "awaiting_teardown",
            "custom_fields": {
                "execution_plan": plan,
                "abort_requested": {
                    "requested_at": "2026-08-06T00:00:00Z",
                },
            },
        }
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)

        with patch("httpx.Client", return_value=mock_client):
            _advance_plan(
                "http://localhost:8090",
                "PERF-TEST",
                "executing_benchmark",
            )

        mock_client.patch.assert_not_called()
        mock_client.post.assert_not_called()
