"""Tests for webhook investigation context injection (#1309).

Tests are source-agnostic — they use generic field names, not
Horreum-specific or boot-time-specific values.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from agents.base import AgentBase


class TestBuildInvestigationContext:
    """Verify _build_investigation_context is generic and correct."""

    def _make_agent(self, agent_name: str = "analyze-agent"):
        """Create a minimal stub with _build_investigation_context."""
        agent = MagicMock(spec=AgentBase)
        agent.agent_name = agent_name
        agent._build_investigation_context = (
            AgentBase._build_investigation_context.__get__(agent)
        )
        agent._INVESTIGATION_AGENTS = AgentBase._INVESTIGATION_AGENTS
        return agent

    def _make_ticket(
        self,
        anomaly_context: dict[str, Any] | None = None,
        run_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        cf: dict[str, Any] = {}
        if anomaly_context is not None:
            cf["anomaly_context"] = anomaly_context
        if run_metadata is not None:
            cf["run_metadata"] = run_metadata
        return {"custom_fields": cf}

    def test_no_context_for_regular_ticket(self):
        agent = self._make_agent()
        ticket = self._make_ticket()
        assert agent._build_investigation_context(ticket) == ""

    def test_empty_dicts_return_empty(self):
        agent = self._make_agent()
        ticket = self._make_ticket(anomaly_context={}, run_metadata={})
        assert agent._build_investigation_context(ticket) == ""

    def test_preserves_all_run_metadata_fields(self):
        """Any key in run_metadata should appear — not just known fields."""
        agent = self._make_agent()
        ticket = self._make_ticket(
            run_metadata={
                "target": "my-server-01",
                "cluster": "prod-us-east",
                "custom_tag": "experiment-42",
            }
        )
        ctx = agent._build_investigation_context(ticket)
        assert "my-server-01" in ctx
        assert "prod-us-east" in ctx
        assert "experiment-42" in ctx

    def test_preserves_all_anomaly_context_fields(self):
        """Any key in anomaly_context should appear."""
        agent = self._make_agent()
        ticket = self._make_ticket(
            anomaly_context={
                "source": "prometheus",
                "severity": "critical",
                "instance": "db-replica-3",
                "summary": "Latency p99 > 500ms",
            }
        )
        ctx = agent._build_investigation_context(ticket)
        assert "prometheus" in ctx
        assert "critical" in ctx
        assert "db-replica-3" in ctx
        assert "Latency p99" in ctx

    def test_includes_both_contexts(self):
        agent = self._make_agent()
        ticket = self._make_ticket(
            anomaly_context={"source": "monitoring", "metric": "cpu_usage"},
            run_metadata={"target": "worker-node-7", "region": "eu-west"},
        )
        ctx = agent._build_investigation_context(ticket)
        assert "System Context" in ctx
        assert "Alert Details" in ctx
        assert "worker-node-7" in ctx
        assert "cpu_usage" in ctx

    def test_contains_grounding_instruction(self):
        agent = self._make_agent()
        ticket = self._make_ticket(anomaly_context={"source": "test"})
        ctx = agent._build_investigation_context(ticket)
        assert "triggered by an external alert" in ctx
        assert "alert context and system identifiers" in ctx

    def test_skips_none_and_empty_values(self):
        agent = self._make_agent()
        ticket = self._make_ticket(
            anomaly_context={"source": "ci", "notes": None, "tags": [], "detail": ""}
        )
        ctx = agent._build_investigation_context(ticket)
        assert "ci" in ctx
        assert "notes" not in ctx.lower()
        assert "tags" not in ctx.lower()
        # 'detail' appears in section heading 'Alert Details' — check the value isn't present
        assert '""' not in ctx  # empty string value not shown

    def test_nested_dicts_shown_as_json(self):
        agent = self._make_agent()
        ticket = self._make_ticket(
            anomaly_context={"labels": {"severity": "warning", "team": "sre"}}
        )
        ctx = agent._build_investigation_context(ticket)
        assert "severity" in ctx
        assert "warning" in ctx

    def test_skipped_for_introspection_agent(self):
        agent = self._make_agent(agent_name="introspection-agent")
        ticket = self._make_ticket(anomaly_context={"source": "test", "metric": "x"})
        assert agent._build_investigation_context(ticket) == ""

    def test_skipped_for_retrospective_agent(self):
        agent = self._make_agent(agent_name="retrospective-agent")
        ticket = self._make_ticket(anomaly_context={"source": "test", "metric": "x"})
        assert agent._build_investigation_context(ticket) == ""

    def test_included_for_review_agent(self):
        agent = self._make_agent(agent_name="review-agent")
        ticket = self._make_ticket(anomaly_context={"source": "test", "metric": "x"})
        ctx = agent._build_investigation_context(ticket)
        assert "Investigation Context" in ctx
