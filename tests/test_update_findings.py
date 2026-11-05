"""Tests for update_findings provider method and synthesis reinvestigation path.

Covers:
- FileRecordProvider.update_findings on inconclusive/empty records
- FileRecordProvider.update_findings rejection on conclusive records
- Synthesis agent update path when matched_investigation_id is set
- Synthesis agent create path when no matched_investigation_id
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from providers.investigation.file import FileRecordProvider
from providers.investigation.models import (
    AnomalyContext,
    InvestigationRecord,
)

# --- Provider: update_findings ---


class TestUpdateFindings:
    @pytest.fixture()
    def provider(self, tmp_path):
        return FileRecordProvider(persist_dir=tmp_path)

    @pytest.mark.asyncio
    async def test_update_empty_outcome_succeeds(self, provider):
        """update_findings on a record with empty outcome succeeds."""
        record = InvestigationRecord(
            anomaly_context=AnomalyContext(subsystem="cpu", metric="throughput"),
            root_cause_summary="",
            confidence=0.0,
        )
        # convergence_outcome defaults to ""
        rid = await provider.create(record)

        await provider.update_findings(
            investigation_id=rid,
            root_cause_summary="fixed root cause",
            confidence=0.85,
            convergence_outcome="ISOLATION",
        )

        updated = await provider.get(rid)
        assert updated is not None
        assert updated.root_cause_summary == "fixed root cause"
        assert updated.confidence == 0.85
        assert updated.operational_metrics.convergence_outcome == "ISOLATION"

    @pytest.mark.asyncio
    async def test_update_inconclusive_outcome_succeeds(self, provider):
        """update_findings on a record with ENTROPY_STALL outcome succeeds."""
        record = InvestigationRecord(
            anomaly_context=AnomalyContext(subsystem="cpu", metric="throughput"),
        )
        record.operational_metrics.convergence_outcome = "ENTROPY_STALL"
        rid = await provider.create(record)

        await provider.update_findings(
            investigation_id=rid,
            root_cause_summary="now resolved",
            confidence=0.9,
            convergence_outcome="ISOLATION",
        )

        updated = await provider.get(rid)
        assert updated is not None
        assert updated.root_cause_summary == "now resolved"
        assert updated.operational_metrics.convergence_outcome == "ISOLATION"

    @pytest.mark.asyncio
    async def test_update_entropy_stall_lowercase_succeeds(self, provider):
        """update_findings on a record with lowercase entropy_stall outcome succeeds."""
        record = InvestigationRecord(
            anomaly_context=AnomalyContext(subsystem="cpu", metric="throughput"),
        )
        record.operational_metrics.convergence_outcome = "entropy_stall"
        rid = await provider.create(record)

        await provider.update_findings(
            investigation_id=rid,
            root_cause_summary="now resolved",
            confidence=0.9,
            convergence_outcome="ISOLATION",
        )

        updated = await provider.get(rid)
        assert updated is not None
        assert updated.root_cause_summary == "now resolved"
        assert updated.operational_metrics.convergence_outcome == "ISOLATION"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "outcome",
        [
            "inconclusive",
            "entropy_stall",
            "max_iterations",
            "manual_interruption",
            "undetermined",
        ],
    )
    async def test_update_all_inconclusive_outcomes_succeed(self, provider, outcome):
        """update_findings on any inconclusive outcome succeeds."""
        record = InvestigationRecord(
            anomaly_context=AnomalyContext(subsystem="cpu", metric="throughput"),
        )
        record.operational_metrics.convergence_outcome = outcome
        rid = await provider.create(record)

        await provider.update_findings(
            investigation_id=rid,
            root_cause_summary="resolved",
            confidence=0.9,
            convergence_outcome="ISOLATION",
        )

        updated = await provider.get(rid)
        assert updated is not None
        assert updated.operational_metrics.convergence_outcome == "ISOLATION"

    @pytest.mark.asyncio
    async def test_update_conclusive_outcome_raises(self, provider):
        """update_findings on a record with conclusive outcome raises ValueError."""
        record = InvestigationRecord(
            anomaly_context=AnomalyContext(subsystem="cpu", metric="throughput"),
            root_cause_summary="already found",
            confidence=0.95,
        )
        record.operational_metrics.convergence_outcome = "ISOLATION"
        rid = await provider.create(record)

        with pytest.raises(ValueError, match="conclusive outcome"):
            await provider.update_findings(
                investigation_id=rid,
                root_cause_summary="different cause",
                confidence=0.8,
                convergence_outcome="EXPECTED_BEHAVIOR",
            )

    @pytest.mark.asyncio
    async def test_update_missing_record_raises(self, provider):
        """update_findings on a non-existent record raises KeyError."""
        with pytest.raises(KeyError):
            await provider.update_findings(
                investigation_id="RCA-NONEXISTENT",
                root_cause_summary="test",
                confidence=0.5,
                convergence_outcome="ISOLATION",
            )


# --- Synthesis agent: update vs create ---


class TestSynthesisReinvestigation:
    def _make_agent(self, custom_fields):
        """Build a SynthesisAgent with mocked HTTP and MCP."""
        from agents.synthesis.agent import SynthesisAgent
        from providers.llm.mock import MockLLMProvider

        agent = SynthesisAgent(
            llm_provider=MockLLMProvider(),
            state_store_url="http://localhost:8090",
        )
        agent._client = AsyncMock()
        agent._client.get = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: {
                    "id": "PERF-REINV",
                    "custom_fields": custom_fields,
                },
                raise_for_status=lambda: None,
            ),
        )
        agent._client.patch = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: {},
                raise_for_status=lambda: None,
            ),
        )
        agent._client.post = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: {},
                raise_for_status=lambda: None,
            ),
        )
        return agent

    def _make_response(self):
        from providers.llm.base import LLMResponse, ToolCall

        return LLMResponse(
            text=None,
            tool_calls=[
                ToolCall(
                    id="tc_1",
                    name="submit_synthesis_result",
                    input={
                        "root_cause_summary": "reinvestigated cause",
                        "confidence": 0.88,
                        "convergence_outcome": "ISOLATION",
                        "build_id": "build-2026-07-01",
                    },
                ),
            ],
            stop_reason="tool_use",
        )

    @pytest.mark.asyncio
    async def test_calls_update_when_matched_investigation_id(self):
        """Synthesis calls update_investigation_record when
        matched_investigation_id is set."""
        agent = self._make_agent(
            {
                "anomaly_context": {
                    "subsystem": "network",
                    "metric": "latency",
                    "direction": "degrading",
                },
                "evaluation_result": {
                    "convergence_gate": "isolation",
                },
                "dedup_result": {
                    "matched_investigation_id": "RCA-EXISTING",
                },
                "investigation_ledger": [],
                "execution_plan": {"steps": []},
            }
        )

        mock_mcp = AsyncMock()
        mock_mcp.call_tool = AsyncMock(
            return_value='{"status": "updated", "investigation_id": "RCA-EXISTING"}'
        )
        agent._mcp = mock_mcp

        await agent._handle_completion("PERF-REINV", self._make_response())

        # Should have called update_investigation_record first
        tool_calls = [c[0][0] for c in mock_mcp.call_tool.call_args_list]
        assert "update_investigation_record" in tool_calls
        assert "create_investigation_record" not in tool_calls

        # Check the update call args
        update_call = [
            c
            for c in mock_mcp.call_tool.call_args_list
            if c[0][0] == "update_investigation_record"
        ][0]
        assert update_call[0][1]["investigation_id"] == "RCA-EXISTING"
        assert update_call[0][1]["root_cause_summary"] == "reinvestigated cause"

        # Should also have appended build history
        history_calls = [
            c
            for c in mock_mcp.call_tool.call_args_list
            if c[0][0] == "append_build_history"
        ]
        assert len(history_calls) == 1
        assert history_calls[0][0][1]["action"] == "REINVESTIGATION_COMPLETE"

    @pytest.mark.asyncio
    async def test_calls_create_when_no_matched_investigation_id(self):
        """Synthesis calls create_investigation_record when no
        matched_investigation_id is set."""
        agent = self._make_agent(
            {
                "anomaly_context": {
                    "subsystem": "network",
                    "metric": "latency",
                    "direction": "degrading",
                },
                "evaluation_result": {
                    "convergence_gate": "isolation",
                },
                "investigation_ledger": [],
                "execution_plan": {"steps": []},
            }
        )

        mock_mcp = AsyncMock()
        mock_mcp.call_tool = AsyncMock(
            return_value='{"status": "created", "investigation_id": "RCA-NEW"}'
        )
        agent._mcp = mock_mcp

        await agent._handle_completion("PERF-REINV", self._make_response())

        tool_calls = [c[0][0] for c in mock_mcp.call_tool.call_args_list]
        assert "create_investigation_record" in tool_calls
        assert "update_investigation_record" not in tool_calls

    @pytest.mark.asyncio
    async def test_falls_back_to_create_when_update_returns_not_found(self):
        """Synthesis falls back to create_investigation_record when
        update_investigation_record returns {"status": "not_found"}."""
        agent = self._make_agent(
            {
                "anomaly_context": {
                    "subsystem": "network",
                    "metric": "latency",
                    "direction": "degrading",
                },
                "evaluation_result": {
                    "convergence_gate": "isolation",
                },
                "dedup_result": {
                    "matched_investigation_id": "RCA-GONE",
                },
                "investigation_ledger": [],
                "execution_plan": {"steps": []},
            }
        )

        call_count = 0

        async def _side_effect(tool_name, args):
            nonlocal call_count
            call_count += 1
            if tool_name == "update_investigation_record":
                return '{"status": "not_found", "message": "Record not found"}'
            return '{"status": "created", "investigation_id": "RCA-NEW"}'

        mock_mcp = AsyncMock()
        mock_mcp.call_tool = AsyncMock(side_effect=_side_effect)
        agent._mcp = mock_mcp

        await agent._handle_completion("PERF-REINV", self._make_response())

        tool_calls = [c[0][0] for c in mock_mcp.call_tool.call_args_list]
        assert "update_investigation_record" in tool_calls
        assert "create_investigation_record" in tool_calls
