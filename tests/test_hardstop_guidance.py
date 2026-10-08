"""Tests for guidance_summary population on hard-stop (CancelledError).

Covers issue #980: when a ticket is hard-stopped, the guidance_summary
custom field should be populated so users see context about what happened.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.main import _build_hardstop_guidance_summary


class TestBuildHardstopGuidanceSummary:
    """Unit tests for _build_hardstop_guidance_summary."""

    def test_basic_fields(self):
        ticket: dict[str, Any] = {"comments": [], "custom_fields": {}}
        summary = _build_hardstop_guidance_summary(ticket, "executing_benchmark")
        assert summary["agent"] == "executing_benchmark"
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
        summary = _build_hardstop_guidance_summary(ticket, "executing_benchmark")
        assert "Running fio on board-3" in summary["details"]

    def test_skips_system_and_user_comments(self):
        ticket: dict[str, Any] = {
            "comments": [
                {"author": "fleet-agent", "body": "Provisioning boards"},
                {"author": "system", "body": "Status changed"},
                {"author": "user-alice", "body": "Please hurry"},
            ],
            "custom_fields": {},
        }
        summary = _build_hardstop_guidance_summary(ticket, "fleet_provisioning")
        # Should pick fleet-agent, not system or user
        assert "Provisioning boards" in summary["details"]

    def test_includes_benchmark_results_note(self):
        ticket: dict[str, Any] = {
            "comments": [],
            "custom_fields": {"benchmark_results": {"fio": {"iops": 1234}}},
        }
        summary = _build_hardstop_guidance_summary(ticket, "executing_benchmark")
        assert "benchmark results" in summary["details"].lower()

    def test_empty_status_defaults_to_unknown(self):
        ticket: dict[str, Any] = {"comments": [], "custom_fields": {}}
        summary = _build_hardstop_guidance_summary(ticket, "")
        assert summary["agent"] == "unknown"

    def test_long_message_truncated(self):
        long_body = "x" * 3000
        ticket: dict[str, Any] = {
            "comments": [{"author": "analyze-agent", "body": long_body}],
            "custom_fields": {},
        }
        summary = _build_hardstop_guidance_summary(ticket, "analyzing")
        # details should not include the full 3000-char message
        assert len(summary["details"]) < 1000

    def test_no_comments(self):
        ticket: dict[str, Any] = {"custom_fields": {}}
        summary = _build_hardstop_guidance_summary(ticket, "triaging")
        assert summary["agent"] == "triaging"
        assert summary["reason"] == "hard_stop"
        assert "suggested_actions" in summary

    def test_suggested_actions_content(self):
        ticket: dict[str, Any] = {"comments": [], "custom_fields": {}}
        summary = _build_hardstop_guidance_summary(ticket, "executing_benchmark")
        actions = summary["suggested_actions"]
        # Should suggest retry, review, and abort
        assert any("retry" in a.lower() or "re-dispatch" in a.lower() for a in actions)
        assert any("abort" in a.lower() for a in actions)


class TestHardstopGuidanceIntegration:
    """Integration test: CancelledError handler writes guidance_summary."""

    @pytest.mark.asyncio
    async def test_cancelled_error_writes_guidance_summary(self):
        """When run_agent_task is cancelled, guidance_summary is populated."""
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
            ],
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
                run_agent_task(dispatcher, "executing_benchmark", "PERF-TEST1")
            )
            # Let the agent start
            await asyncio.sleep(0.05)
            task.cancel()
            # Should not raise — CancelledError is handled internally
            await task

        # Verify the PATCH call included guidance_summary
        mock_client.patch.assert_awaited_once()
        patch_call = mock_client.patch.await_args
        fields = patch_call.kwargs.get(
            "json", patch_call.args[1] if len(patch_call.args) > 1 else {}
        ).get("fields", {})
        assert "guidance_summary" in fields
        gs = fields["guidance_summary"]
        assert gs["agent"] == "executing_benchmark"
        assert gs["reason"] == "hard_stop"
        assert "Running fio" in gs["details"]
        assert len(gs["suggested_actions"]) >= 1

    @pytest.mark.asyncio
    async def test_cancelled_error_skips_closed_ticket(self):
        """When ticket is already closed, no guidance_summary is written."""
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
