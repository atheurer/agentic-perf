"""Compatibility coverage for tickets created before execution_model storage."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.benchmark.agent import BenchmarkAgent
from agents.platform.agent import PlatformAgent
from providers.skills.base import (
    EXECUTION_MODEL_CONTROLLER,
    EXECUTION_MODEL_DIRECT,
    BenchmarkSuite,
)
from providers.skills.catalog import resolve_ticket_execution_model
from providers.skills.multi import MultiHarnessSkillProvider
from tests.conftest import MockSkillProvider


def _skill_provider() -> MultiHarnessSkillProvider:
    return MultiHarnessSkillProvider(
        harnesses={
            "crucible": MockSkillProvider(
                benchmarks=[
                    BenchmarkSuite(
                        name="fio",
                        description="Crucible storage test",
                        harness="crucible",
                        execution_model=EXECUTION_MODEL_CONTROLLER,
                    )
                ]
            ),
            "arcaflow-plugins": MockSkillProvider(
                benchmarks=[
                    BenchmarkSuite(
                        name="stressng",
                        description="Arcaflow CPU stress test",
                        harness="arcaflow-plugins",
                        execution_model=EXECUTION_MODEL_DIRECT,
                    )
                ]
            ),
        },
        default_harness="crucible",
    )


def _completion_response(host: str) -> MagicMock:
    tool_call = MagicMock()
    tool_call.name = "submit_platform_result"
    tool_call.input = {
        "platform_ready": True,
        "hosts_provisioned": [host],
        "board_name": "board-1",
    }
    response = MagicMock()
    response.text = ""
    response.tool_calls = [tool_call]
    return response


async def _platform_fields(
    ticket: dict, skill_provider: MultiHarnessSkillProvider
):
    agent = PlatformAgent(
        llm_provider=AsyncMock(),
        state_store_url="http://localhost:8090",
        skill_provider=skill_provider,
    )
    with (
        patch.object(
            agent, "_get_ticket", new_callable=AsyncMock, return_value=ticket
        ),
        patch.object(agent, "_update_fields", new_callable=AsyncMock) as update_fields,
        patch.object(agent, "_add_comment", new_callable=AsyncMock),
        patch.object(
            agent,
            "_plan_controls_next_transition",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch.object(agent, "_transition_ticket", new_callable=AsyncMock),
    ):
        await agent._handle_completion(
            ticket.get("id", "PERF-OLD"), _completion_response("10.0.0.8")
        )
    return update_fields.call_args.args[1]


@pytest.mark.asyncio
async def test_old_arcaflow_jumpstarter_ticket_keeps_single_board_as_target():
    skill_provider = _skill_provider()
    ticket = {
        "id": "PERF-OLD",
        "summary": "Run stressng on a Jumpstarter board",
        "description": "Run the CPU benchmark",
        "custom_fields": {
            "resource_provider": "jumpstarter",
            "benchmark_suite": "stressng",
            "directives": {
                "workflow_source": "https://example.test/workflow.yaml"
            },
        },
        "comments": [],
    }

    platform_fields = await _platform_fields(ticket, skill_provider)
    assert platform_fields["assigned_hardware_ips"] == {
        "controller": "",
        "targets": ["10.0.0.8"],
    }

    ticket["custom_fields"].update(platform_fields)
    benchmark = BenchmarkAgent.__new__(BenchmarkAgent)
    benchmark._skill_provider = skill_provider
    benchmark._resolved_execution_models = {}
    benchmark._repo_cache = None
    assert (
        await benchmark._prepare_legacy_execution_model(ticket)
        == EXECUTION_MODEL_DIRECT
    )

    prompt = benchmark._system_prompt(ticket)
    messages = benchmark._build_messages(ticket)
    assert "## Direct Execution Model" in prompt
    assert "## Arcaflow Workflow Execution (mandatory)" in prompt
    assert "## Crucible Benchmark Execution" not in prompt
    assert "Target hosts for this benchmark" in messages[0]["content"]
    assert '"targets": [\n    "10.0.0.8"' in messages[0]["content"]


@pytest.mark.asyncio
async def test_legacy_ticket_without_harness_uses_crucible_controller_model():
    skill_provider = _skill_provider()
    ticket = {
        "id": "PERF-OLD-CRUCIBLE",
        "custom_fields": {
            "resource_provider": "jumpstarter",
            "benchmark_suite": "fio",
            "directives": {},
        },
    }

    execution_model = await resolve_ticket_execution_model(skill_provider, ticket)
    assert execution_model == EXECUTION_MODEL_CONTROLLER

    benchmark = BenchmarkAgent.__new__(BenchmarkAgent)
    benchmark._skill_provider = skill_provider
    benchmark._resolved_execution_models = {}
    benchmark._repo_cache = None
    await benchmark._prepare_legacy_execution_model(ticket)
    assert "## Direct Execution Model" not in benchmark._system_prompt(ticket)

    fields = await _platform_fields(ticket, skill_provider)
    assert fields["assigned_hardware_ips"] == {
        "controller": "10.0.0.8",
        "targets": [],
    }


@pytest.mark.asyncio
async def test_explicit_execution_model_overrides_harness_metadata():
    ticket = {
        "custom_fields": {
            "execution_model": EXECUTION_MODEL_CONTROLLER,
            "benchmark_suite": "stressng",
            "directives": {"harness": "arcaflow-plugins"},
        }
    }

    assert (
        await resolve_ticket_execution_model(_skill_provider(), ticket)
        == EXECUTION_MODEL_CONTROLLER
    )
