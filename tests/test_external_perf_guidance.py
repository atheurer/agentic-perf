"""Tests for conditional external perf data prompt guidance."""

from __future__ import annotations

from unittest.mock import AsyncMock

from providers.llm.base import ToolDefinition


class TestGatheringContextGuidance:
    """Test gathering_context prompt injection."""

    def _make_agent(self):
        from agents.gathering_context.agent import GatheringContextAgent

        return GatheringContextAgent(
            llm_provider=AsyncMock(),
            state_store_url="http://localhost:8090",
        )

    def test_guidance_injected_when_tools_present(self):
        agent = self._make_agent()
        agent.tools = [
            ToolDefinition(name="get_baseline_stats", description="", input_schema={}),
            ToolDefinition(
                name="query_investigation_records", description="", input_schema={}
            ),
        ]
        ticket = {"custom_fields": {}}
        prompt = agent._system_prompt(ticket)
        assert "Historical Performance Data" in prompt
        assert "get_baseline_stats" in prompt

    def test_guidance_absent_when_no_tools(self):
        agent = self._make_agent()
        agent.tools = [
            ToolDefinition(
                name="query_investigation_records", description="", input_schema={}
            ),
        ]
        ticket = {"custom_fields": {}}
        prompt = agent._system_prompt(ticket)
        assert "Historical Performance Data" not in prompt


class TestEvaluateGuidance:
    """Test evaluate prompt injection."""

    def _make_agent(self):
        from agents.evaluate.agent import EvaluateAgent

        return EvaluateAgent(
            llm_provider=AsyncMock(),
            state_store_url="http://localhost:8090",
        )

    def test_guidance_injected_with_baseline_tools(self):
        agent = self._make_agent()
        agent.tools = [
            ToolDefinition(name="get_baseline_stats", description="", input_schema={}),
        ]
        ticket = {"custom_fields": {}}
        prompt = agent._system_prompt(ticket)
        assert "Historical Performance Data" in prompt
        assert "Baseline-informed convergence" in prompt

    def test_guidance_includes_phase2_when_present(self):
        agent = self._make_agent()
        agent.tools = [
            ToolDefinition(name="get_baseline_stats", description="", input_schema={}),
            ToolDefinition(
                name="find_similar_anomalies", description="", input_schema={}
            ),
            ToolDefinition(name="get_distribution", description="", input_schema={}),
        ]
        ticket = {"custom_fields": {}}
        prompt = agent._system_prompt(ticket)
        assert "find_similar_anomalies" in prompt
        assert "get_distribution" in prompt
        assert "bimodal" in prompt

    def test_guidance_absent_when_no_tools(self):
        agent = self._make_agent()
        agent.tools = [
            ToolDefinition(
                name="submit_evaluation_result", description="", input_schema={}
            ),
        ]
        ticket = {"custom_fields": {}}
        prompt = agent._system_prompt(ticket)
        assert "Historical Performance Data" not in prompt



