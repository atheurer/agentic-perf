"""Provider-specific reservation contracts and scoped resource guidance."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.base import AgentBase
from agents.resource.prompts import RESOURCE_BASE_PROMPT
from providers.resource.registry import PROVIDER_REGISTRY
from providers.resource.selection import (
    RESERVATION_SELECTION_CONTRACTS,
    ReservationSelectionError,
    validate_reservation_selection,
)


def _provider_class(name: str):
    module_name, class_name = PROVIDER_REGISTRY[name]["class"].rsplit(".", 1)
    return getattr(importlib.import_module(module_name), class_name)


def test_every_registered_provider_has_a_validated_selection_contract():
    assert set(RESERVATION_SELECTION_CONTRACTS) == set(PROVIDER_REGISTRY)

    for provider in PROVIDER_REGISTRY:
        contract = RESERVATION_SELECTION_CONTRACTS[provider]
        provider_class = _provider_class(provider)

        assert provider_class.reservation_selection_schema() == contract.json_schema()
        assert (
            validate_reservation_selection(provider, contract.valid_example)
            == contract.valid_example
        )
        for selection in contract.invalid_examples:
            with pytest.raises(ReservationSelectionError):
                validate_reservation_selection(provider, selection)


def test_quads_selection_must_cover_ticket_hosts_and_match_eligible_options():
    required_hosts = [
        {"roles": ["controller"]},
        {"roles": ["client"]},
        {"roles": ["server"]},
    ]
    selection = {"hostnames": ["host-01.example.com", "host-02.example.com"]}

    with pytest.raises(ReservationSelectionError, match="requires 3 managed host"):
        validate_reservation_selection(
            "quads", selection, required_hosts=required_hosts
        )

    with pytest.raises(ReservationSelectionError, match="eligible host options"):
        validate_reservation_selection(
            "quads",
            {
                "hostnames": [
                    "host-01.example.com",
                    "host-02.example.com",
                    "host-03.example.com",
                ]
            },
            required_hosts=required_hosts,
            require_eligible_hostnames=True,
        )

    valid_selection = {
        "hostnames": [
            "host-01.example.com",
            "host-02.example.com",
            "host-03.example.com",
        ]
    }
    with pytest.raises(ReservationSelectionError, match="eligible options"):
        validate_reservation_selection(
            "quads",
            valid_selection,
            required_hosts=required_hosts,
            eligible_hostnames_by_requirement=[
                {"host-01.example.com"},
                {"host-01.example.com"},
                {"host-03.example.com"},
            ],
        )

    assert (
        validate_reservation_selection(
            "quads",
            valid_selection,
            required_hosts=required_hosts,
            eligible_hostnames_by_requirement=[
                {"host-01.example.com"},
                {"host-02.example.com"},
                {"host-03.example.com"},
            ],
        )
        == valid_selection
    )


@pytest.mark.asyncio
async def test_quads_discovery_filters_ticket_cpu_and_memory_requirements():
    from providers.resource.quads import QuadsResourceProvider

    client = MagicMock()
    client.get_available = AsyncMock(
        return_value=[
            {"hostname": "small.example.com", "cores": 32, "memory_gb": 64},
            {"hostname": "large.example.com", "cores": 64, "memory_gb": 256},
        ]
    )
    provider = QuadsResourceProvider(client)

    result = await provider.check_available({"min_cores": 48, "min_memory_gb": 128})

    assert [host["hostname"] for host in result["options"]] == ["large.example.com"]


@pytest.mark.parametrize("provider", PROVIDER_REGISTRY)
def test_only_matching_provider_selection_guidance_is_assembled(provider):
    from agents.resource.agent import ResourceAgent

    ticket = {
        "id": "PERF-TEST",
        "summary": "test",
        "description": "test",
        "custom_fields": {"resource_provider": provider},
        "comments": [],
    }
    prompt = ResourceAgent.__new__(ResourceAgent)._system_prompt(ticket)
    own_fields = RESERVATION_SELECTION_CONTRACTS[provider].prompt_fields
    for field in own_fields:
        assert field in prompt

    other_fields = {
        field
        for other_provider, contract in RESERVATION_SELECTION_CONTRACTS.items()
        if other_provider != provider
        for field in contract.prompt_fields
    }
    unique_other_fields = other_fields - set(own_fields)
    # Hostnames are also referenced in AWS IP mapping guidance, unrelated to
    # the AWS reservation selection shape.
    unique_other_fields.discard("hostnames")
    for field in unique_other_fields:
        assert field not in prompt


def test_shared_prompt_and_tool_description_have_no_provider_selection_shape():
    from agents.resource.server import reserve_resources

    prompts_dir = Path(__file__).resolve().parents[1] / "agents/resource/prompts"
    provider_fields = {
        field
        for contract in RESERVATION_SELECTION_CONTRACTS.values()
        for field in contract.prompt_fields
    }
    tool_description = reserve_resources.__doc__ or ""
    for provider in PROVIDER_REGISTRY:
        fragment = AgentBase._load_prompt_fragment(prompts_dir, provider)
        assert fragment
        assert fragment not in RESOURCE_BASE_PROMPT
        assert fragment not in tool_description
    for field in provider_fields:
        assert field not in RESOURCE_BASE_PROMPT
        assert field not in tool_description


def test_registered_harness_guidance_is_scoped_to_its_assembled_prompt(monkeypatch):
    from agents.benchmark.agent import BenchmarkAgent
    from agents.benchmark.prompts import BENCHMARK_BASE_PROMPT
    from agents.server_utils import build_skill_provider
    from providers.skills.private import PrivateSkillProvider

    monkeypatch.delenv("ZATHRAS_HOME", raising=False)
    monkeypatch.setattr(
        PrivateSkillProvider, "_load_config", lambda self, suite_name: {}
    )
    skill_provider = build_skill_provider(
        catalog_only=True, resolve_source=False, zathras_home=""
    )
    prompts_dir = Path(__file__).resolve().parents[1] / "agents/benchmark/prompts"
    registered_fragments = {
        name: (prompts_dir / f"{name}.md").read_text().strip()
        for name in skill_provider.list_harnesses()
        if (prompts_dir / f"{name}.md").is_file()
    }

    assert registered_fragments
    for fragment in registered_fragments.values():
        assert fragment not in BENCHMARK_BASE_PROMPT

    for harness, expected_fragment in registered_fragments.items():
        agent = BenchmarkAgent.__new__(BenchmarkAgent)
        agent._skill_provider = skill_provider
        prompt = agent._system_prompt(
            {"custom_fields": {"directives": {"harness": harness}}}
        )
        assert expected_fragment in prompt
        for other_harness, other_fragment in registered_fragments.items():
            if other_harness != harness:
                assert other_fragment not in prompt


@pytest.mark.asyncio
async def test_quads_aws_shaped_selection_is_rejected_before_marker_or_provider_call(
    monkeypatch,
):
    import agents.resource.server as srv
    from providers.resource.quads import QuadsResourceProvider

    provider = QuadsResourceProvider(MagicMock())
    provider.reserve = AsyncMock()
    registry = MagicMock()
    registry.get_provider = AsyncMock(return_value=provider)
    marker = AsyncMock()

    monkeypatch.setattr(srv, "_initialized", True)
    monkeypatch.setattr(srv, "_registry", registry)
    monkeypatch.setattr(
        srv,
        "_ticket",
        {
            "id": "PERF-EAF95C59",
            "custom_fields": {
                "resource_provider": "quads",
                "required_hosts": [
                    {"roles": ["controller"]},
                    {"roles": ["client"]},
                    {"roles": ["server"]},
                ],
            },
        },
    )
    monkeypatch.setattr(srv, "_resources_allocated", False)
    monkeypatch.setattr(srv, "_reservation_uncertain", False)
    monkeypatch.setattr(srv, "_reservation_failures", 0)
    monkeypatch.setattr(srv, "_persist_unknown_reservation_marker", marker)

    raw = await srv.reserve_resources(
        provider="quads",
        selection={
            "instance_specs": [
                {"instance_type": "m5.4xlarge", "count": 1, "role": "controller"},
                {"instance_type": "m5n.4xlarge", "count": 2, "role": "client"},
            ],
            "os": "RHEL9",
            "nic_speed_gbps": 100,
        },
        description="PERF-EAF95C59 regression",
        ticket_id="PERF-EAF95C59",
    )
    result = json.loads(raw)

    assert result["status"] == "invalid_selection"
    assert result["invalid_selection"] is True
    assert result["provider_call_started"] is False
    assert result["allocation_unknown"] is False
    assert "Invalid quads selection" in result["error"]
    provider.reserve.assert_not_awaited()
    marker.assert_not_awaited()


def test_contract_metadata_covers_all_provider_prompt_fragments():
    prompts_dir = Path("agents/resource/prompts")
    for provider, contract in RESERVATION_SELECTION_CONTRACTS.items():
        fragments = AgentBase._load_prompt_fragments(
            prompts_dir.parent, resource_provider=provider
        )
        assert fragments
        for field in contract.prompt_fields:
            assert field in fragments

    from agents.resource.agent import ResourceAgent

    auto_select_prompt = ResourceAgent.__new__(ResourceAgent)._system_prompt(
        {"custom_fields": {}}
    )
    assert "reservation_selection_schema" in auto_select_prompt
    for contract in RESERVATION_SELECTION_CONTRACTS.values():
        for field in contract.prompt_fields:
            assert field not in auto_select_prompt


@pytest.mark.asyncio
async def test_availability_result_exposes_selected_provider_contract(monkeypatch):
    import agents.resource.server as srv
    from providers.resource.quads import QuadsResourceProvider

    client = MagicMock()
    client.get_available = AsyncMock(
        return_value=[{"hostname": "host-01.example.com", "cores": 32}]
    )
    provider = QuadsResourceProvider(client)
    registry = MagicMock()
    registry.get_provider = AsyncMock(return_value=provider)
    monkeypatch.setattr(srv, "_initialized", True)
    monkeypatch.setattr(srv, "_registry", registry)
    monkeypatch.setattr(srv, "_ticket", {})
    monkeypatch.setattr(srv, "_resources_allocated", False)
    monkeypatch.setattr(srv, "_reservation_uncertain", False)

    raw = await srv.check_available_resources(provider="quads")
    result = json.loads(raw)

    assert (
        result["reservation_selection_schema"]
        == provider.reservation_selection_schema()
    )
    assert (
        result["reservation_selection_example"]
        == RESERVATION_SELECTION_CONTRACTS["quads"].valid_example
    )
