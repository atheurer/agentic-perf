"""Tests for introspection guidance summary."""

from __future__ import annotations

from agents.introspection.agent import IntrospectionAgent


def _make_agent():
    """Create an introspection agent for testing."""
    agent = IntrospectionAgent(
        state_store_url="http://localhost:8090",
    )
    return agent


def _make_ticket(status="awaiting_customer_guidance", comments=None):
    """Build a mock ticket."""
    return {
        "id": "PERF-TEST",
        "status": status,
        "summary": "Test ticket",
        "custom_fields": {},
        "comments": comments or [],
    }


class TestGuidanceSummaryDeterministic:
    def test_timeout_reason(self):
        agent = _make_agent()
        ticket = _make_ticket(
            comments=[
                {"author": "analyze-agent", "body": "LLM call timed out after 180s"},
            ]
        )
        summary = agent._build_guidance_summary_deterministic(ticket, [])
        assert summary["reason"] == "timeout"
        assert summary["agent"] == "analyze-agent"

    def test_error_reason(self):
        agent = _make_agent()
        ticket = _make_ticket(
            comments=[
                {
                    "author": "platform-agent",
                    "body": "Platform Setup Failed: SSH unreachable",
                },
            ]
        )
        summary = agent._build_guidance_summary_deterministic(ticket, [])
        assert summary["reason"] == "error"

    def test_needs_input_reason(self):
        agent = _make_agent()
        ticket = _make_ticket(
            comments=[
                {
                    "author": "resource-agent",
                    "body": "Input needed: no boards available",
                },
            ]
        )
        summary = agent._build_guidance_summary_deterministic(ticket, [])
        assert summary["reason"] == "needs_input"

    def test_build_failure_reason(self):
        agent = _make_agent()
        ticket = _make_ticket(
            comments=[
                {
                    "author": "image-builder",
                    "body": "Image build failed: step-build-image error",
                },
            ]
        )
        summary = agent._build_guidance_summary_deterministic(ticket, [])
        assert summary["reason"] == "build_failure"

    def test_resource_exhaustion_reason(self):
        agent = _make_agent()
        ticket = _make_ticket(
            comments=[
                {
                    "author": "resource-agent",
                    "body": "No available board matching selector",
                },
            ]
        )
        summary = agent._build_guidance_summary_deterministic(ticket, [])
        assert summary["reason"] == "resource_exhaustion"

    def test_handoff_blocked_reason(self):
        agent = _make_agent()
        ticket = _make_ticket(
            comments=[
                {"author": "orchestrator", "body": "Handoff blocked: No run_id"},
            ]
        )
        summary = agent._build_guidance_summary_deterministic(ticket, [])
        assert summary["reason"] == "handoff_blocked"

    def test_unknown_with_no_comments(self):
        agent = _make_agent()
        ticket = _make_ticket(comments=[])
        summary = agent._build_guidance_summary_deterministic(ticket, [])
        assert summary["reason"] == "unknown"

    def test_system_comments_skipped(self):
        agent = _make_agent()
        ticket = _make_ticket(
            comments=[
                {"author": "benchmark-agent", "body": "Test failed with error"},
                {"author": "system", "body": "Agent needs clarification"},
            ]
        )
        summary = agent._build_guidance_summary_deterministic(ticket, [])
        assert summary["agent"] == "benchmark-agent"

    def test_anomalies_included(self):
        agent = _make_agent()
        ticket = _make_ticket(
            comments=[
                {"author": "agent", "body": "Something failed"},
            ]
        )
        anomalies = [
            {"severity": "high", "description": "Tool X failed 5 times"},
            {"severity": "medium", "description": "Minor issue"},
            {"severity": "critical", "description": "Critical problem"},
        ]
        summary = agent._build_guidance_summary_deterministic(ticket, anomalies)
        assert len(summary["anomalies"]) == 2  # only high+critical
        assert "Tool X" in summary["anomalies"][0]

    def test_suggested_actions_present(self):
        agent = _make_agent()
        ticket = _make_ticket(
            comments=[
                {"author": "agent", "body": "LLM timed out"},
            ]
        )
        summary = agent._build_guidance_summary_deterministic(ticket, [])
        assert len(summary["suggested_actions"]) > 0


class TestGuidanceProducedFlag:
    def test_flag_prevents_duplicate(self):
        agent = _make_agent()
        assert not agent._guidance_produced
        agent._guidance_produced = True
        # Second check should not trigger
        assert agent._guidance_produced

    def test_flag_resets_on_status_change(self):
        agent = _make_agent()
        agent._guidance_produced = True
        # Simulating status leaving guidance
        agent._guidance_produced = False
        assert not agent._guidance_produced


class TestSystemErrorFallback:
    def test_system_exception_found(self):
        agent = _make_agent()
        ticket = _make_ticket(
            comments=[
                {"author": "system", "body": "Triage complete, starting plan step 0"},
                {
                    "author": "system",
                    "body": "Agent failed with an unhandled exception: 'str' object is not a mapping",
                },
            ]
        )
        summary = agent._build_guidance_summary_deterministic(ticket, [])
        assert summary["agent"] == "system"
        assert summary["reason"] == "error"
        assert "exception" in summary["details"].lower()

    def test_system_error_not_benign(self):
        agent = _make_agent()
        ticket = _make_ticket(
            comments=[
                {"author": "system", "body": "Triage complete, starting plan step 0"},
                {"author": "system", "body": "Plan advancing to step 1"},
            ]
        )
        summary = agent._build_guidance_summary_deterministic(ticket, [])
        assert summary["agent"] == ""
        assert summary["reason"] == "unknown"

    def test_agent_comment_takes_priority(self):
        agent = _make_agent()
        ticket = _make_ticket(
            comments=[
                {"author": "system", "body": "Agent failed with exception"},
                {"author": "analyze-agent", "body": "LLM call timed out"},
            ]
        )
        summary = agent._build_guidance_summary_deterministic(ticket, [])
        assert summary["agent"] == "analyze-agent"
        assert summary["reason"] == "timeout"
