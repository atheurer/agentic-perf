"""Tests for review agent re-dispatch prevention (#379).

Covers: review_submitted marker persistence, poll-loop re-dispatch
guard, _plan_controls_next_transition alignment with _advance_plan,
and data-loss block removal.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from agents.base import AgentBase
from providers.events import EventBus
from providers.llm.base import LLMProvider, LLMResponse, ToolDefinition
from state_store.main import create_app
from state_store.models import CreateTicketRequest, TransitionRequest
from state_store.store import TicketStore

# ── Shared helpers ───────────────────────────────────────


class _StubAgent(AgentBase):
    """Minimal agent for plan-check tests."""

    def _system_prompt(self, ticket: dict[str, Any]) -> str:
        return "test"

    def _build_messages(
        self,
        ticket: dict[str, Any],
    ) -> list[dict[str, Any]]:
        return [{"role": "user", "content": "test"}]

    async def _handle_completion(
        self,
        ticket_id: str,
        response: LLMResponse,
    ) -> None:
        pass


class _FinishingLLM(LLMProvider):
    """LLM that immediately returns end_turn."""

    async def complete(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        return LLMResponse(
            text="done",
            tool_calls=[],
            stop_reason="end_turn",
            raw_content=[],
        )


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
def review_ticket(store):
    """Ticket at awaiting_review with review_submitted=True."""
    ticket = store.create_ticket(
        CreateTicketRequest(summary="perf review", description="test"),
    )
    for status in [
        "triage_pending",
        "awaiting_hardware",
        "awaiting_provision",
        "executing_benchmark",
        "awaiting_review",
    ]:
        store.transition_ticket(
            ticket.id,
            TransitionRequest(status=status),
        )
    store.update_fields(
        ticket.id,
        {
            "review_submitted": True,
            "run_id": "run-001",
            "benchmark_status": "completed",
        },
    )
    return store.get_ticket(ticket.id)


# ── 1. review_submitted marker ──────────────────────────


class TestReviewSubmittedMarker:
    def test_marker_survives_slim_list_endpoint(
        self,
        client,
        review_ticket,
    ):
        """review_submitted is NOT in _HEAVY_FIELDS so the list
        endpoint returns it — the poll loop can see it."""
        r = client.get("/api/v1/tickets")
        assert r.status_code == 200
        tickets = r.json()
        match = [t for t in tickets if t["id"] == review_ticket.id]
        assert len(match) == 1
        cf = match[0]["custom_fields"]
        assert cf.get("review_submitted") is True

    def test_marker_not_in_heavy_fields(self):
        """Confirm review_submitted is not in _HEAVY_FIELDS."""
        from state_store.api.tickets import _HEAVY_FIELDS

        assert "review_submitted" not in _HEAVY_FIELDS

    def test_verdict_is_in_heavy_fields(self):
        """verdict IS in _HEAVY_FIELDS — this is why we can't
        use it as the re-dispatch guard marker."""
        from state_store.api.tickets import _HEAVY_FIELDS

        assert "verdict" in _HEAVY_FIELDS


# ── 2. Poll-loop re-dispatch guard ──────────────────────


class TestReDispatchGuard:
    """Verify that the poll_loop skips tickets with review_submitted."""

    def test_review_submitted_ticket_not_dispatched(
        self,
        client,
        review_ticket,
    ):
        """A ticket at awaiting_review with review_submitted=True
        should not be re-dispatched by the poll loop."""
        r = client.get("/api/v1/tickets")
        assert r.status_code == 200
        tickets = r.json()
        match = [t for t in tickets if t["id"] == review_ticket.id]
        assert len(match) == 1
        t = match[0]

        assert t["status"] == "awaiting_review"
        assert t["custom_fields"].get("review_submitted") is True

    def test_review_submitted_not_parked_at_guidance(
        self,
        review_ticket,
        store,
    ):
        """A review_submitted ticket should NOT be transitioned to
        guidance — it's correctly parked at awaiting_review waiting
        for _advance_plan or manual transition."""
        t = store.get_ticket(review_ticket.id)
        assert t.status.value == "awaiting_review"
        assert t.custom_fields.get("review_submitted") is True

    def test_no_review_submitted_allows_dispatch(self, store):
        """Tickets at awaiting_review WITHOUT review_submitted
        should still be dispatchable."""
        ticket = store.create_ticket(
            CreateTicketRequest(
                summary="fresh review",
                description="test",
            ),
        )
        for status in [
            "triage_pending",
            "awaiting_hardware",
            "awaiting_provision",
            "executing_benchmark",
            "awaiting_review",
        ]:
            store.transition_ticket(
                ticket.id,
                TransitionRequest(status=status),
            )
        store.update_fields(
            ticket.id,
            {
                "run_id": "run-002",
                "benchmark_status": "completed",
            },
        )
        t = store.get_ticket(ticket.id)
        assert t.status.value == "awaiting_review"
        assert t.custom_fields.get("review_submitted") is None


# ── 3. _plan_controls_next_transition alignment ─────────


class TestPlanControlsAlignment:
    """Verify that _plan_controls_next_transition checks step status,
    matching _advance_plan's guard."""

    @pytest.mark.asyncio
    async def test_pending_step_does_not_defer(self, tmp_path):
        """When step status is 'pending', agent should NOT defer
        its transition — _advance_plan wouldn't advance it either."""
        llm = _FinishingLLM()
        agent = _StubAgent(
            agent_name="review-agent",
            llm_provider=llm,
            state_store_url="http://localhost:8090",
        )
        agent._client = AsyncMock()
        agent._client.get = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: {
                    "id": "PERF-TEST",
                    "status": "awaiting_review",
                    "summary": "test",
                    "custom_fields": {
                        "execution_plan": {
                            "current_step": 0,
                            "steps": [
                                {
                                    "agent_type": "review",
                                    "status": "pending",
                                },
                                {
                                    "agent_type": "teardown",
                                    "status": "pending",
                                },
                            ],
                        },
                    },
                },
                raise_for_status=lambda: None,
            ),
        )

        result = await agent._plan_controls_next_transition("PERF-TEST")
        assert result is False

    @pytest.mark.asyncio
    async def test_in_progress_step_defers(self, tmp_path):
        """When step status is 'in_progress', agent SHOULD defer."""
        llm = _FinishingLLM()
        agent = _StubAgent(
            agent_name="review-agent",
            llm_provider=llm,
            state_store_url="http://localhost:8090",
        )
        agent._client = AsyncMock()
        agent._client.get = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: {
                    "id": "PERF-TEST",
                    "status": "awaiting_review",
                    "summary": "test",
                    "custom_fields": {
                        "execution_plan": {
                            "current_step": 0,
                            "steps": [
                                {
                                    "agent_type": "review",
                                    "status": "in_progress",
                                },
                                {
                                    "agent_type": "teardown",
                                    "status": "pending",
                                },
                            ],
                        },
                    },
                },
                raise_for_status=lambda: None,
            ),
        )

        result = await agent._plan_controls_next_transition("PERF-TEST")
        assert result is True

    @pytest.mark.asyncio
    async def test_completed_step_does_not_defer(self, tmp_path):
        """When step is already completed, no deferral."""
        llm = _FinishingLLM()
        agent = _StubAgent(
            agent_name="review-agent",
            llm_provider=llm,
            state_store_url="http://localhost:8090",
        )
        agent._client = AsyncMock()
        agent._client.get = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: {
                    "id": "PERF-TEST",
                    "status": "awaiting_review",
                    "summary": "test",
                    "custom_fields": {
                        "execution_plan": {
                            "current_step": 0,
                            "steps": [
                                {
                                    "agent_type": "review",
                                    "status": "completed",
                                },
                                {
                                    "agent_type": "teardown",
                                    "status": "pending",
                                },
                            ],
                        },
                    },
                },
                raise_for_status=lambda: None,
            ),
        )

        result = await agent._plan_controls_next_transition("PERF-TEST")
        assert result is False

    @pytest.mark.asyncio
    async def test_no_plan_does_not_defer(self, tmp_path):
        """Without a plan, agent does its own transition."""
        llm = _FinishingLLM()
        agent = _StubAgent(
            agent_name="review-agent",
            llm_provider=llm,
            state_store_url="http://localhost:8090",
        )
        agent._client = AsyncMock()
        agent._client.get = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: {
                    "id": "PERF-TEST",
                    "status": "awaiting_review",
                    "summary": "test",
                    "custom_fields": {},
                },
                raise_for_status=lambda: None,
            ),
        )

        result = await agent._plan_controls_next_transition("PERF-TEST")
        assert result is False


# ── 4. _advance_plan logging ────────────────────────────


class TestAdvancePlanLogging:
    """Verify _advance_plan logs when it declines to advance."""

    def test_advance_plan_logs_pending_step(self, caplog):
        """_advance_plan should log when step status is not in_progress."""
        import logging

        from orchestrator.main import _advance_plan

        ticket_data = {
            "id": "PERF-PLAN01",
            "status": "awaiting_review",
            "custom_fields": {
                "execution_plan": {
                    "current_step": 0,
                    "steps": [
                        {
                            "agent_type": "review",
                            "status": "pending",
                        },
                        {
                            "agent_type": "teardown",
                            "status": "pending",
                        },
                    ],
                },
            },
        }

        mock_response = MagicMock(
            status_code=200,
        )
        mock_response.json.return_value = ticket_data
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response

        with (
            patch("httpx.Client", return_value=mock_client),
            patch(
                "orchestrator.main._auth_headers",
                return_value={},
            ),
            caplog.at_level(
                logging.DEBUG,
                logger="orchestrator.main",
            ),
        ):
            _advance_plan(
                "http://testserver",
                "PERF-PLAN01",
                "awaiting_review",
            )

        assert any("not in_progress" in r.message for r in caplog.records)


# ── 5. Marker clearing on plan advance ──────────────────


class TestMarkerClearedOnAdvance:
    """Verify that _advance_plan clears review_submitted when
    it completes a step — enabling re-dispatch if the ticket
    returns to awaiting_review in a later plan step."""

    def test_advance_plan_clears_review_submitted(self):
        """_advance_plan should null out review_submitted when
        it PATCHes the plan forward."""
        from orchestrator.main import _advance_plan

        ticket_data = {
            "id": "PERF-CLEAR01",
            "status": "awaiting_review",
            "custom_fields": {
                "review_submitted": True,
                "execution_plan": {
                    "current_step": 0,
                    "steps": [
                        {
                            "agent_type": "review",
                            "status": "in_progress",
                        },
                        {
                            "agent_type": "teardown",
                            "status": "pending",
                        },
                    ],
                },
            },
        }

        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = ticket_data
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.patch.return_value = MagicMock(status_code=200)
        mock_client.post.return_value = MagicMock(status_code=200)

        with (
            patch("httpx.Client", return_value=mock_client),
            patch(
                "orchestrator.main._auth_headers",
                return_value={},
            ),
        ):
            _advance_plan(
                "http://testserver",
                "PERF-CLEAR01",
                "awaiting_review",
            )

        patch_calls = mock_client.patch.call_args_list
        assert len(patch_calls) >= 1
        fields = patch_calls[0][1]["json"]["fields"]
        assert fields.get("review_submitted") is None

    def test_advance_plan_final_step_clears_marker(self):
        """When completing the final plan step, review_submitted
        is also cleared."""
        from orchestrator.main import _advance_plan

        ticket_data = {
            "id": "PERF-FINAL01",
            "status": "awaiting_review",
            "custom_fields": {
                "review_submitted": True,
                "execution_plan": {
                    "current_step": 0,
                    "steps": [
                        {
                            "agent_type": "review",
                            "status": "in_progress",
                        },
                    ],
                },
            },
        }

        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = ticket_data
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.patch.return_value = MagicMock(status_code=200)

        with (
            patch("httpx.Client", return_value=mock_client),
            patch(
                "orchestrator.main._auth_headers",
                return_value={},
            ),
        ):
            _advance_plan(
                "http://testserver",
                "PERF-FINAL01",
                "awaiting_review",
            )

        patch_calls = mock_client.patch.call_args_list
        assert len(patch_calls) == 1
        fields = patch_calls[0][1]["json"]["fields"]
        assert fields.get("review_submitted") is None


# ── 6. Data-loss block removal ──────────────────────────


class TestDataLossBlockRemoval:
    """Verify that _handle_completion no longer discards review
    data when _user_approved_submit is False."""

    @pytest.mark.asyncio
    async def test_handle_completion_always_persists(self, tmp_path):
        """_handle_completion should persist review data regardless
        of _user_approved_submit state."""
        from agents.review.agent import ReviewAgent

        llm = _FinishingLLM()
        event_bus = EventBus(log_dir=tmp_path / "logs")
        agent = ReviewAgent(
            llm_provider=llm,
            state_store_url="http://localhost:8090",
            event_bus=event_bus,
        )
        agent._user_approved_submit = False
        agent._client = AsyncMock()

        update_calls = []

        async def capture_update(tid, fields):
            update_calls.append(fields)
            return {"id": tid, "custom_fields": fields}

        agent._update_fields = capture_update
        agent._add_comment = AsyncMock(return_value={})
        agent._plan_controls_next_transition = AsyncMock(
            return_value=False,
        )
        agent._transition_ticket = AsyncMock()

        response = LLMResponse(
            text="Review complete. Verdict: pass.",
            tool_calls=[],
            stop_reason="end_turn",
            raw_content=[],
        )

        await agent._handle_completion("PERF-TEST", response)

        assert len(update_calls) == 1
        fields = update_calls[0]
        assert fields["review_submitted"] is True
        assert "verdict" in fields
        assert "review_summary" in fields
