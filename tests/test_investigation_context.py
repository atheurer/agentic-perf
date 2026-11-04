"""Tests for webhook investigation context injection (#1309)."""

from typing import Any

from agents.base import AgentBase


class _StubAgent(AgentBase):
    """Minimal concrete subclass for testing base-class methods."""

    def _system_prompt(self, ticket: dict[str, Any]) -> str:
        return ""

    def _build_messages(self, ticket: dict[str, Any]) -> list[dict[str, Any]]:
        return []

    async def _handle_completion(self, *args: Any, **kwargs: Any) -> None:
        pass


class TestBuildInvestigationContext:
    def _make_ticket(self, anomaly_context=None, run_metadata=None):
        cf = {}
        if anomaly_context is not None:
            cf["anomaly_context"] = anomaly_context
        if run_metadata is not None:
            cf["run_metadata"] = run_metadata
        return {"custom_fields": cf}

    def _get_context(self, ticket):
        # Call the method directly — it's a pure function on ticket data
        agent = _StubAgent.__new__(_StubAgent)
        return agent._build_investigation_context(ticket)

    def test_no_context_for_regular_ticket(self):
        ticket = self._make_ticket()
        assert self._get_context(ticket) == ""

    def test_includes_target_system(self):
        ticket = self._make_ticket(
            run_metadata={"target": "nxp-s32g-vnp-rdb3", "os_id": "autosd"}
        )
        ctx = self._get_context(ticket)
        assert "nxp-s32g-vnp-rdb3" in ctx
        assert "autosd" in ctx

    def test_includes_alert_metric(self):
        ticket = self._make_ticket(
            anomaly_context={
                "source": "horreum",
                "variable_name": "BOOT2 - Kernel Duration",
                "change_description": "mean changed from 1020 to 941",
            }
        )
        ctx = self._get_context(ticket)
        assert "BOOT2 - Kernel Duration" in ctx
        assert "horreum" in ctx
        assert "mean changed" in ctx

    def test_includes_both_contexts(self):
        ticket = self._make_ticket(
            anomaly_context={"source": "monitoring", "variable_name": "latency_p99"},
            run_metadata={"target": "qc8775", "os_id": "rhivos", "build": "abc123"},
        )
        ctx = self._get_context(ticket)
        assert "qc8775" in ctx
        assert "rhivos" in ctx
        assert "abc123" in ctx
        assert "latency_p99" in ctx

    def test_handles_missing_fields_gracefully(self):
        ticket = self._make_ticket(anomaly_context={"source": "unknown"})
        ctx = self._get_context(ticket)
        assert "unknown" in ctx

    def test_contains_grounding_instruction(self):
        ticket = self._make_ticket(
            anomaly_context={"source": "test", "variable_name": "metric_x"}
        )
        ctx = self._get_context(ticket)
        assert "Reference the system identifiers" in ctx

    def test_empty_dicts_return_empty(self):
        ticket = self._make_ticket(anomaly_context={}, run_metadata={})
        assert self._get_context(ticket) == ""
