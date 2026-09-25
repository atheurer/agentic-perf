"""Tests for _block_handoff_failed recovery when rewind is invalid.

Covers #991: tickets stuck in evaluating_convergence when no benchmark ran
because the rewind transition to executing_benchmark is not valid from
evaluating_convergence.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.dispatcher import Dispatcher
from orchestrator.handoff import check_handoff


class TestEvaluatingConvergenceHandoffBlock:
    """Handoff check blocks evaluating_convergence with no benchmark."""

    def test_no_run_id_no_benchmark_status_blocks(self) -> None:
        """Ticket with no run_id and no benchmark_status is blocked."""
        ticket: dict = {"custom_fields": {}}
        ok, reason = check_handoff("evaluating_convergence", ticket)
        assert not ok
        assert "No run_id" in reason

    def test_no_run_id_benchmark_status_none_blocks(self) -> None:
        """Ticket with run_id absent and benchmark_status=None blocks."""
        ticket: dict = {"custom_fields": {"benchmark_status": None}}
        ok, reason = check_handoff("evaluating_convergence", ticket)
        assert not ok
        assert "benchmark_status=None" in reason

    def test_completed_benchmark_passes(self) -> None:
        """Ticket with benchmark_status='completed' passes even without run_id."""
        ticket: dict = {
            "custom_fields": {"benchmark_status": "completed"},
        }
        ok, _ = check_handoff("evaluating_convergence", ticket)
        assert ok

    def test_has_run_id_passes(self) -> None:
        """Ticket with a run_id passes regardless of benchmark_status."""
        ticket: dict = {
            "custom_fields": {"run_id": "run-123"},
        }
        ok, _ = check_handoff("evaluating_convergence", ticket)
        assert ok


class TestBlockHandoffFailedRewindFallback:
    """_block_handoff_failed recovers when rewind transition fails (#991)."""

    @pytest.fixture()
    def mock_client(self) -> MagicMock:
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        return client

    async def test_direct_hitl_when_rewind_fails(
        self,
        mock_client: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When rewind 422s, ticket transitions directly to HITL."""
        import orchestrator.main as mod

        monkeypatch.setenv("AGENTIC_PERF_API_TOKEN", "test-token")

        # Track all POST calls
        post_calls: list[tuple[str, dict]] = []

        def make_response(status_code: int = 200, json_data: dict | None = None):
            resp = MagicMock()
            resp.status_code = status_code
            resp.json.return_value = json_data or {}
            return resp

        async def fake_post(url: str, json: dict | None = None, **kw):
            post_calls.append((url, json or {}))
            # First POST = rewind transition → 422 (invalid)
            if (
                "transition" in url
                and len([c for c in post_calls if "transition" in c[0]]) == 1
            ):
                return make_response(422)
            # Second POST = comment → 200
            # Third POST = HITL transition → 200
            return make_response(200)

        async def fake_get(url: str, **kw):
            # Re-fetch returns current status (still evaluating_convergence)
            return make_response(
                200,
                {"status": "evaluating_convergence"},
            )

        mock_client.post = fake_post
        mock_client.get = fake_get

        with patch.object(
            mod,
            "AuditedAsyncHTTPClient",
            lambda **kwargs: mock_client,
        ):
            await mod._block_handoff_failed(
                store_url="http://store:8090",
                ticket_id="PERF-TEST1",
                reason="No run_id and benchmark_status=None",
                current_status="evaluating_convergence",
            )

        # Should have 3 POSTs: rewind (failed), comment, HITL transition
        assert len(post_calls) == 3
        # The final transition should target awaiting_customer_guidance
        final_url, final_json = post_calls[-1]
        assert "transition" in final_url
        assert final_json["status"] == "awaiting_customer_guidance"

    async def test_rewind_succeeds_then_hitl(
        self,
        mock_client: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When rewind succeeds, HITL transition uses rewound status."""
        import orchestrator.main as mod

        monkeypatch.setenv("AGENTIC_PERF_API_TOKEN", "test-token")

        post_calls: list[tuple[str, dict]] = []

        def make_response(status_code: int = 200, json_data: dict | None = None):
            resp = MagicMock()
            resp.status_code = status_code
            resp.json.return_value = json_data or {}
            return resp

        async def fake_post(url: str, json: dict | None = None, **kw):
            post_calls.append((url, json or {}))
            return make_response(200)

        async def fake_get(url: str, **kw):
            # After successful rewind, status is executing_benchmark
            return make_response(
                200,
                {"status": "executing_benchmark"},
            )

        mock_client.post = fake_post
        mock_client.get = fake_get

        with patch.object(
            mod,
            "AuditedAsyncHTTPClient",
            lambda **kwargs: mock_client,
        ):
            await mod._block_handoff_failed(
                store_url="http://store:8090",
                ticket_id="PERF-TEST2",
                reason="test reason",
                current_status="evaluating_convergence",
            )

        # 3 POSTs: rewind, comment, HITL transition
        assert len(post_calls) == 3
        final_url, final_json = post_calls[-1]
        assert final_json["status"] == "awaiting_customer_guidance"

    async def test_returns_false_when_hitl_transition_fails(
        self,
        mock_client: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Returns False when HITL transition fails so caller retries."""
        import orchestrator.main as mod

        monkeypatch.setenv("AGENTIC_PERF_API_TOKEN", "test-token")

        def make_response(
            status_code: int = 200,
            json_data: dict | None = None,
        ):
            resp = MagicMock()
            resp.status_code = status_code
            resp.json.return_value = json_data or {}
            return resp

        call_count = 0

        async def fake_post(url: str, json: dict | None = None, **kw):
            nonlocal call_count
            call_count += 1
            # Rewind fails, comment succeeds, HITL transition fails
            if "transition" in url:
                return make_response(422)
            return make_response(200)

        async def fake_get(url: str, **kw):
            return make_response(
                200,
                {"status": "evaluating_convergence"},
            )

        mock_client.post = fake_post
        mock_client.get = fake_get

        with patch.object(
            mod,
            "AuditedAsyncHTTPClient",
            lambda **kwargs: mock_client,
        ):
            result = await mod._block_handoff_failed(
                store_url="http://store:8090",
                ticket_id="PERF-TEST3",
                reason="test",
                current_status="evaluating_convergence",
            )

        assert result is False

    async def test_status_lookup_failure_does_not_stop_hitl_recovery(
        self,
        mock_client: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failed diagnostic read still allows the HITL transition attempt."""
        import orchestrator.main as mod

        monkeypatch.setenv("AGENTIC_PERF_API_TOKEN", "test-token")

        transition_calls = 0

        def make_response(status_code: int = 200):
            resp = MagicMock()
            resp.status_code = status_code
            return resp

        async def fake_post(url: str, json: dict | None = None, **kw):
            nonlocal transition_calls
            if "transition" in url:
                transition_calls += 1
                return make_response(422)
            return make_response(200)

        async def fake_get(url: str, **kw):
            raise OSError("state store temporarily unavailable")

        mock_client.post = fake_post
        mock_client.get = fake_get

        with patch.object(
            mod,
            "AuditedAsyncHTTPClient",
            lambda **kwargs: mock_client,
        ):
            result = await mod._block_handoff_failed(
                store_url="http://store:8090",
                ticket_id="PERF-TEST-LOOKUP-ERROR",
                reason="test",
                current_status="evaluating_convergence",
            )

        assert result is False
        assert transition_calls == 2

    async def test_returns_true_when_hitl_succeeds(
        self,
        mock_client: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Returns True when HITL transition succeeds."""
        import orchestrator.main as mod

        monkeypatch.setenv("AGENTIC_PERF_API_TOKEN", "test-token")

        def make_response(
            status_code: int = 200,
            json_data: dict | None = None,
        ):
            resp = MagicMock()
            resp.status_code = status_code
            resp.json.return_value = json_data or {}
            return resp

        async def fake_post(url: str, json: dict | None = None, **kw):
            return make_response(200)

        async def fake_get(url: str, **kw):
            return make_response(
                200,
                {"status": "executing_benchmark"},
            )

        mock_client.post = fake_post
        mock_client.get = fake_get

        with patch.object(
            mod,
            "AuditedAsyncHTTPClient",
            lambda **kwargs: mock_client,
        ):
            result = await mod._block_handoff_failed(
                store_url="http://store:8090",
                ticket_id="PERF-TEST4",
                reason="test",
                current_status="evaluating_convergence",
            )

        assert result is True


def test_handoff_block_suppression_expires_after_hitl_resume() -> None:
    """A resumed ticket can retry the same stage after its HITL pause."""
    dispatcher = Dispatcher("http://store", MagicMock(), MagicMock())
    ticket_id = "PERF-RESUME"
    source_status = "evaluating_convergence"
    hitl_status = "awaiting_customer_guidance"

    dispatcher.mark_handoff_blocked(ticket_id, source_status)

    # A stale snapshot at the failed stage must keep suppressing another block.
    dispatcher.reconcile_handoff_blocked({ticket_id: source_status})
    assert dispatcher.is_handoff_blocked(ticket_id, source_status)

    # The poll loop observes all fetched statuses before filtering dispatchable
    # stages, so the non-dispatched HITL status clears the stale marker.
    dispatcher.reconcile_handoff_blocked({ticket_id: hitl_status})
    assert not dispatcher.is_handoff_blocked(ticket_id, source_status)

    # When the user resumes to the original stage, it is eligible for retry.
    dispatcher.reconcile_handoff_blocked({ticket_id: source_status})
    assert not dispatcher.is_handoff_blocked(ticket_id, source_status)
