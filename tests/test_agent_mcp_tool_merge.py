"""Tests for merging MCP tools with agent-local workspace tools."""

from __future__ import annotations

import pytest

from providers.llm.base import ToolDefinition
from providers.llm.mock import MockLLMProvider
from tests.conftest import MockSkillProvider


@pytest.mark.parametrize(
    "agent_class",
    [
        "agents.analyze.agent.AnalyzeAgent",
        "agents.evaluate.agent.EvaluateAgent",
        "agents.gathering_context.agent.GatheringContextAgent",
        "agents.synthesis.agent.SynthesisAgent",
        "agents.platform.agent.PlatformAgent",
        "agents.triage.agent.TriageAgent",
        "agents.review.agent.ReviewAgent",
        "agents.retrospective.agent.RetrospectiveAgent",
        "agents.resource.agent.ResourceAgent",
        "agents.provisioning.agent.ProvisioningAgent",
    ],
)
def test_mcp_merge_preserves_workspace_tools(agent_class):
    module_name, class_name = agent_class.rsplit(".", 1)
    module = __import__(module_name, fromlist=[class_name])
    constructor_kwargs = (
        {"skill_provider": MockSkillProvider()} if class_name == "TriageAgent" else {}
    )
    agent = getattr(module, class_name)(
        llm_provider=MockLLMProvider(),
        state_store_url="http://localhost:8090",
        **constructor_kwargs,
    )
    local_tool = agent.tools[0]
    mcp_tool = ToolDefinition(name="mcp_tool", description="", input_schema={})

    agent._install_mcp_tools([mcp_tool])

    assert agent.tools == [mcp_tool, *agent._local_tools_snapshot]
    assert local_tool in agent.tools


@pytest.mark.parametrize(
    "agent_class",
    [
        "agents.analyze.agent.AnalyzeAgent",
        "agents.evaluate.agent.EvaluateAgent",
        "agents.gathering_context.agent.GatheringContextAgent",
    ],
)
def test_external_tool_filter_preserves_workspace_tools(agent_class):
    from agents.mcp_client import filter_external_tools

    module_name, class_name = agent_class.rsplit(".", 1)
    module = __import__(module_name, fromlist=[class_name])
    agent = getattr(module, class_name)(
        llm_provider=MockLLMProvider(),
        state_store_url="http://localhost:8090",
    )
    external_tool = ToolDefinition(
        name="external_tool", description="", input_schema={}
    )

    filtered = filter_external_tools(
        [*agent.tools, external_tool],
        {"external_tool": "external"},
        ["external"],
        {"external": {"external_tool"}},
    )

    assert all(tool in filtered for tool in agent.tools)


def test_mcp_merge_is_idempotent():
    from agents.analyze.agent import AnalyzeAgent

    agent = AnalyzeAgent(
        llm_provider=MockLLMProvider(),
        state_store_url="http://localhost:8090",
    )
    mcp_tool = ToolDefinition(name="mcp_tool", description="", input_schema={})

    agent._install_mcp_tools([mcp_tool])
    first_tools = agent.tools
    agent._install_mcp_tools([mcp_tool])

    assert agent.tools == first_tools


def test_workspace_tool_precedence_on_name_collision(caplog):
    from agents.synthesis.agent import SynthesisAgent

    agent = SynthesisAgent(
        llm_provider=MockLLMProvider(),
        state_store_url="http://localhost:8090",
    )
    workspace_tool = agent.tools[0]
    colliding_mcp_tool = ToolDefinition(
        name=workspace_tool.name, description="MCP duplicate", input_schema={}
    )
    distinct_mcp_tool = ToolDefinition(name="mcp_tool", description="", input_schema={})

    with caplog.at_level("WARNING", logger="agents.base"):
        agent._install_mcp_tools([colliding_mcp_tool, distinct_mcp_tool])

    assert workspace_tool in agent.tools
    assert colliding_mcp_tool not in agent.tools
    assert distinct_mcp_tool in agent.tools
    assert workspace_tool.name in caplog.text
