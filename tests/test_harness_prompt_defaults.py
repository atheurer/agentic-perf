"""Regression tests for default and explicit harness prompt behavior."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agents.benchmark.agent import BenchmarkAgent
from agents.provisioning.agent import ProvisioningAgent


@pytest.mark.parametrize("directives", [{}, {"harness": "crucible"}])
def test_benchmark_crucible_prompt_and_tool_scope_with_default_or_explicit_harness(
    directives: dict[str, str],
):
    agent = BenchmarkAgent.__new__(BenchmarkAgent)
    agent._skill_provider = SimpleNamespace(default_harness="crucible")
    agent.tools = [
        SimpleNamespace(name="read_skills"),
        SimpleNamespace(name="list_harness_docs"),
        SimpleNamespace(name="read_harness_doc"),
        SimpleNamespace(name="get_execution_config"),
        SimpleNamespace(name="validate_benchmark"),
    ]
    ticket = {"custom_fields": {"directives": directives}}

    prompt = agent._system_prompt(ticket)
    agent._apply_tool_scoping(ticket)

    assert "## Crucible Benchmark Execution" in prompt
    # Crucible's configured tool policy is applied in both cases.
    assert [tool.name for tool in agent.tools] == ["validate_benchmark"]


@pytest.mark.parametrize("directives", [{}, {"harness": "crucible"}])
def test_provisioning_crucible_prompt_with_default_or_explicit_harness(
    directives: dict[str, str],
):
    agent = ProvisioningAgent.__new__(ProvisioningAgent)
    agent._skill_provider = SimpleNamespace(default_harness="crucible")
    ticket = {"custom_fields": {"directives": directives}}

    prompt = agent._system_prompt(ticket)

    assert "## Crucible Provisioning Notes" in prompt
