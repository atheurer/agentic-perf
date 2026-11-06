"""Tests for orchestrator kill reason recording and agent handling.

Covers:
- Stale task detector records kill reason before cancellation
- Benchmark agent formats kill reason on failure
- Kill reason persists through ticket lifecycle
- Fallback message when kill reason is absent
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from providers.events import EventBus


class TestKillReasonRecording:
    """Test that orchestrator records kill reason before SIGKILL."""

    @pytest.mark.asyncio
    async def test_stale_task_detector_records_kill_reason(self, tmp_path):
        """Stale task detector should write kill reason to custom_fields."""
        from orchestrator.main import _check_stale_tasks

        events = EventBus(log_dir=str(tmp_path / "logs"))
        dispatcher = MagicMock()

        stale_task = MagicMock()
        stale_task.done.return_value = False
        dispatcher.active_tasks.return_value = {"KILL-001": stale_task}
        dispatcher._claim_ids = {"KILL-001": None}

        # Emit an old event to trigger stale detection
        events.emit("KILL-001", "agent", "tool_called")
        old_ts = "2020-01-01T00:00:00+00:00"
        events._trace_store._open_connection().execute(
            "UPDATE trace_events SET occurred_at = ? WHERE ticket_id = ?",
            (old_ts, "KILL-001"),
        )
        events._trace_store._open_connection().commit()

        # Mock the HTTP PATCH call to verify kill reason is written
        patched_fields = None

        async def mock_patch(url: str, json: dict) -> MagicMock:
            nonlocal patched_fields
            if "fields" in json:
                patched_fields = json["fields"]
            response = MagicMock()
            response.status_code = 200
            return response

        response_mock = MagicMock()
        response_mock.status_code = 200
        response_mock.json.return_value = {
            "id": "KILL-001",
            "status": "executing_benchmark",
            "custom_fields": {},
        }

        with patch("orchestrator.main.AuditedAsyncHTTPClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.patch = AsyncMock(side_effect=mock_patch)
            mock_client.get = AsyncMock(return_value=response_mock)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            await _check_stale_tasks(
                dispatcher,
                events,
                stale_timeout=900,
                store_url="http://localhost:9999",
            )

        # Verify kill reason was patched
        assert patched_fields is not None
        assert "last_subprocess_kill_reason" in patched_fields
        kill_reason = patched_fields["last_subprocess_kill_reason"]
        assert kill_reason["code"] == "stale_task_timeout"
        assert "idle_seconds" in kill_reason
        assert "threshold_seconds" in kill_reason
        assert kill_reason["mechanism"] == "orchestrator_stale_task_detector"
        assert kill_reason["idle_seconds"] > 900

    @pytest.mark.asyncio
    async def test_stale_task_detector_continues_on_patch_failure(self, tmp_path):
        """Stale task detector should cancel task even if PATCH fails."""
        from orchestrator.main import _check_stale_tasks

        events = EventBus(log_dir=str(tmp_path / "logs"))
        dispatcher = MagicMock()

        stale_task = MagicMock()
        stale_task.done.return_value = False
        dispatcher.active_tasks.return_value = {"FAIL-001": stale_task}
        dispatcher._claim_ids = {"FAIL-001": None}

        events.emit("FAIL-001", "agent", "tool_called")
        old_ts = "2020-01-01T00:00:00+00:00"
        events._trace_store._open_connection().execute(
            "UPDATE trace_events SET occurred_at = ? WHERE ticket_id = ?",
            (old_ts, "FAIL-001"),
        )
        events._trace_store._open_connection().commit()

        response_mock = MagicMock()
        response_mock.status_code = 200
        response_mock.json.return_value = {
            "id": "FAIL-001",
            "status": "executing_benchmark",
            "custom_fields": {},
        }

        with patch("orchestrator.main.AuditedAsyncHTTPClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.patch = AsyncMock(side_effect=RuntimeError("Network error"))
            mock_client.get = AsyncMock(return_value=response_mock)
            mock_client.post = AsyncMock(return_value=MagicMock(status_code=200))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            await _check_stale_tasks(
                dispatcher,
                events,
                stale_timeout=900,
                store_url="http://localhost:9999",
            )

        # Task should be cancelled despite the PATCH failure
        stale_task.cancel.assert_called_once()


class TestKillReasonFormatting:
    """Test benchmark agent kill reason formatting."""

    def test_format_kill_reason_stale_task_timeout(self):
        """Stale task timeout should format with idle and threshold."""
        from providers.kill_reason import format_kill_reason

        kill_reason = {
            "code": "stale_task_timeout",
            "idle_seconds": 1234,
            "threshold_seconds": 900,
            "mechanism": "orchestrator_stale_task_detector",
        }

        result = format_kill_reason(kill_reason)

        assert result is not None
        assert "1234s" in result
        assert "900s" in result
        assert "orchestrator" in result
        assert "heartbeat" in result
        assert "hung" in result

    def test_format_kill_reason_unknown_code(self):
        """Unknown code should produce fallback message."""
        from providers.kill_reason import format_kill_reason

        kill_reason = {
            "code": "unknown_mechanism",
            "idle_seconds": 100,
        }

        result = format_kill_reason(kill_reason)

        assert result is not None
        assert "unknown_mechanism" in result
        assert "orchestrator" in result
        assert "logs" in result

    def test_format_kill_reason_missing_code(self):
        """Missing code should return None."""
        from providers.kill_reason import format_kill_reason

        kill_reason = {"idle_seconds": 100}

        result = format_kill_reason(kill_reason)

        assert result is None

    def test_format_kill_reason_none(self):
        """None input should return None."""
        from providers.kill_reason import format_kill_reason

        result = format_kill_reason(None)

        assert result is None

    def test_format_kill_reason_not_dict(self):
        """Non-dict input should return None."""
        from providers.kill_reason import format_kill_reason

        result = format_kill_reason("not a dict")

        assert result is None


class TestBenchmarkAgentKillReasonHandling:
    """Test benchmark agent uses kill reason in failure messages."""

    @pytest.mark.asyncio
    async def test_benchmark_adds_kill_reason_to_notes(self, tmp_path):
        """Failed benchmark with kill reason should include it in notes."""
        from agents.benchmark.agent import BenchmarkAgent

        agent = BenchmarkAgent(
            llm_provider=MagicMock(),
            state_store_url="http://localhost:9999",
        )

        # Mock the ticket with kill reason
        ticket_with_kill = {
            "id": "BENCH-001",
            "status": "executing_benchmark",
            "custom_fields": {
                "last_subprocess_kill_reason": {
                    "code": "stale_task_timeout",
                    "idle_seconds": 600,
                    "threshold_seconds": 900,
                    "mechanism": "orchestrator_stale_task_detector",
                }
            },
        }

        agent._get_ticket = AsyncMock(return_value=ticket_with_kill)
        agent._update_fields = AsyncMock()
        agent._add_comment = AsyncMock()
        agent._transition_ticket = AsyncMock()

        # Create response that indicates benchmark failed
        response = MagicMock()
        response.text = json.dumps(
            {
                "run_id": "run-123",
                "benchmark_status": "failed",
                "notes": "Subprocess exited with code -9; output pipes remained open.",
            }
        )

        await agent._handle_completion("BENCH-001", response)

        # _update_fields is called twice: once to clear last_subprocess_kill_reason
        # (preventing state leakage across retries), and once for the final fields.
        assert agent._update_fields.call_count == 2
        clear_call = agent._update_fields.call_args_list[0]
        assert clear_call[0][1] == {"last_subprocess_kill_reason": None}
        final_call = agent._update_fields.call_args_list[1]
        fields = final_call[0][1]

        assert fields["benchmark_status"] == "failed"
        assert fields["benchmark_notes"] is not None
        assert "Kill Reason" in fields["benchmark_notes"]
        assert "heartbeat" in fields["benchmark_notes"].lower()
        assert "600s" in fields["benchmark_notes"]

    @pytest.mark.asyncio
    async def test_benchmark_omits_kill_reason_when_absent(self, tmp_path):
        """Failed benchmark without kill reason should have plain notes."""
        from agents.benchmark.agent import BenchmarkAgent

        agent = BenchmarkAgent(
            llm_provider=MagicMock(),
            state_store_url="http://localhost:9999",
        )

        # Mock the ticket without kill reason
        ticket_without_kill = {
            "id": "BENCH-002",
            "status": "executing_benchmark",
            "custom_fields": {},
        }

        agent._get_ticket = AsyncMock(return_value=ticket_without_kill)
        agent._update_fields = AsyncMock()
        agent._add_comment = AsyncMock()
        agent._transition_ticket = AsyncMock()

        response = MagicMock()
        response.text = json.dumps(
            {
                "run_id": "run-456",
                "benchmark_status": "failed",
                "notes": "Process exited with code -9",
            }
        )

        await agent._handle_completion("BENCH-002", response)

        agent._update_fields.assert_called_once()
        call_args = agent._update_fields.call_args
        fields = call_args[0][1]

        assert fields["benchmark_status"] == "failed"
        assert fields["benchmark_notes"] == "Process exited with code -9"

    @pytest.mark.asyncio
    async def test_benchmark_completed_ignores_kill_reason(self, tmp_path):
        """Completed benchmark should not add kill reason even if present."""
        from agents.benchmark.agent import BenchmarkAgent

        agent = BenchmarkAgent(
            llm_provider=MagicMock(),
            state_store_url="http://localhost:9999",
        )

        ticket = {
            "id": "BENCH-003",
            "status": "executing_benchmark",
            "custom_fields": {
                "last_subprocess_kill_reason": {
                    "code": "stale_task_timeout",
                    "idle_seconds": 600,
                    "threshold_seconds": 900,
                }
            },
        }

        agent._get_ticket = AsyncMock(return_value=ticket)
        agent._update_fields = AsyncMock()
        agent._add_comment = AsyncMock()
        agent._transition_ticket = AsyncMock()

        response = MagicMock()
        response.text = json.dumps(
            {
                "run_id": "run-789",
                "benchmark_status": "completed",
                "notes": "Run completed successfully",
            }
        )

        await agent._handle_completion("BENCH-003", response)

        agent._update_fields.assert_called_once()
        call_args = agent._update_fields.call_args
        fields = call_args[0][1]

        assert fields["benchmark_status"] == "completed"
        assert fields["benchmark_notes"] == "Run completed successfully"
        assert "Kill Reason" not in (fields["benchmark_notes"] or "")

    @pytest.mark.asyncio
    async def test_benchmark_preserves_existing_notes(self, tmp_path):
        """Kill reason should be appended to existing notes."""
        from agents.benchmark.agent import BenchmarkAgent

        agent = BenchmarkAgent(
            llm_provider=MagicMock(),
            state_store_url="http://localhost:9999",
        )

        ticket = {
            "id": "BENCH-004",
            "status": "executing_benchmark",
            "custom_fields": {
                "last_subprocess_kill_reason": {
                    "code": "stale_task_timeout",
                    "idle_seconds": 1500,
                    "threshold_seconds": 900,
                }
            },
        }

        agent._get_ticket = AsyncMock(return_value=ticket)
        agent._update_fields = AsyncMock()
        agent._add_comment = AsyncMock()
        agent._transition_ticket = AsyncMock()

        response = MagicMock()
        response.text = json.dumps(
            {
                "run_id": "run-999",
                "benchmark_status": "failed",
                "notes": "Existing failure details: exit code -9, connection timeout",
            }
        )

        await agent._handle_completion("BENCH-004", response)

        assert agent._update_fields.call_count == 2
        clear_call = agent._update_fields.call_args_list[0]
        assert clear_call[0][1] == {"last_subprocess_kill_reason": None}
        final_call = agent._update_fields.call_args_list[1]
        fields = final_call[0][1]

        notes = fields["benchmark_notes"]
        assert notes is not None
        assert "Existing failure details" in notes
        assert "connection timeout" in notes
        assert "Kill Reason" in notes
        assert notes.index("Existing") < notes.index("Kill Reason")


def _persisted_notes(agent: MagicMock) -> str:
    """Return benchmark_notes from the first _update_fields payload that has it."""
    for call in agent._update_fields.call_args_list:
        payload = call[0][1]
        if "benchmark_notes" in payload:
            return payload["benchmark_notes"]
    return ""


def _make_agent(ticket: dict) -> MagicMock:
    from agents.benchmark.agent import BenchmarkAgent

    agent = BenchmarkAgent(
        llm_provider=MagicMock(),
        state_store_url="http://localhost:9999",
    )
    agent._get_ticket = AsyncMock(return_value=ticket)
    agent._update_fields = AsyncMock()
    agent._add_comment = AsyncMock()
    agent._transition_ticket = AsyncMock()
    return agent


def _failed_response(notes: str) -> MagicMock:
    response = MagicMock()
    response.text = json.dumps(
        {"run_id": "run-kill", "benchmark_status": "failed", "notes": notes}
    )
    return response


class TestKillReasonSurfacing:
    """Reasons recorded on the ticket are surfaced on a failed benchmark."""

    @pytest.mark.asyncio
    async def test_harness_timeout_surfaced_from_ticket_field(self):
        """The harness records its own deadline; no LLM echo is required."""
        agent = _make_agent(
            {
                "id": "BENCH-010",
                "status": "executing_benchmark",
                "custom_fields": {
                    "last_subprocess_kill_reason": {
                        "code": "harness_timeout",
                        "timeout_seconds": 5400,
                        "mechanism": "benchmark_harness",
                    }
                },
            }
        )

        await agent._handle_completion(
            "BENCH-010", _failed_response("Exited with code -9.")
        )

        notes = _persisted_notes(agent)
        assert "Kill Reason" in notes
        assert "harness timeout" in notes
        assert "not an orchestrator stale-task kill" in notes
        # The reason is consumed so a later attempt cannot inherit it.
        assert {"last_subprocess_kill_reason": None} in [
            call[0][1] for call in agent._update_fields.call_args_list
        ]

    @pytest.mark.asyncio
    async def test_harness_stall_surfaced_from_ticket_field(self):
        agent = _make_agent(
            {
                "id": "BENCH-011",
                "status": "executing_benchmark",
                "custom_fields": {
                    "last_subprocess_kill_reason": {
                        "code": "harness_stall",
                        "stall_seconds": 360,
                        "mechanism": "benchmark_harness",
                    }
                },
            }
        )

        await agent._handle_completion(
            "BENCH-011", _failed_response("Exited with code -9.")
        )

        notes = _persisted_notes(agent)
        assert "stall detected" in notes
        assert "360s" in notes

    @pytest.mark.asyncio
    async def test_kill_reason_surfaced_without_minus_nine_in_notes(self):
        """The reason no longer depends on the LLM writing '-9' in its notes."""
        agent = _make_agent(
            {
                "id": "BENCH-012",
                "status": "executing_benchmark",
                "custom_fields": {
                    "last_subprocess_kill_reason": {
                        "code": "stale_task_timeout",
                        "idle_seconds": 3700,
                        "threshold_seconds": 3600,
                    }
                },
            }
        )

        await agent._handle_completion(
            "BENCH-012", _failed_response("Timed out waiting for samples.")
        )

        notes = _persisted_notes(agent)
        assert "no heartbeat activity for 3700s" in notes

    @pytest.mark.asyncio
    async def test_no_kill_reason_when_ticket_has_none(self):
        """A plain failure with no recorded reason stays unannotated."""
        agent = _make_agent(
            {
                "id": "BENCH-013",
                "status": "executing_benchmark",
                "custom_fields": {},
            }
        )

        await agent._handle_completion(
            "BENCH-013", _failed_response("Board went unresponsive, exit code -9.")
        )

        assert "Kill Reason" not in _persisted_notes(agent)
