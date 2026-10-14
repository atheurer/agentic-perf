"""Provider-specific reservation contracts and scoped resource guidance."""

from __future__ import annotations

import ast
import importlib
import inspect
import json
import re
import textwrap
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


def _schema_property_names(schema):
    """Collect property names recursively, including nested model definitions."""
    names = set()
    if isinstance(schema, dict):
        properties = schema.get("properties")
        if isinstance(properties, dict):
            names.update(properties)
        for value in schema.values():
            names.update(_schema_property_names(value))
    elif isinstance(schema, list):
        for value in schema:
            names.update(_schema_property_names(value))
    return names


def test_every_registered_provider_has_a_validated_selection_contract():
    assert set(RESERVATION_SELECTION_CONTRACTS) == set(PROVIDER_REGISTRY)

    for provider in PROVIDER_REGISTRY:
        contract = RESERVATION_SELECTION_CONTRACTS[provider]
        provider_class = _provider_class(provider)

        assert provider_class.reservation_selection_schema() == contract.json_schema()
        assert set(contract.prompt_fields) == _schema_property_names(
            contract.json_schema()
        )
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
        assert not re.search(rf"\b{re.escape(field)}\b", prompt)


def test_shared_prompt_and_tool_description_have_no_provider_selection_shape():
    from agents.resource.server import check_available_resources, reserve_resources

    prompts_dir = Path(__file__).resolve().parents[1] / "agents/resource/prompts"
    shared_text = "\n".join(
        [
            RESOURCE_BASE_PROMPT,
            check_available_resources.__doc__ or "",
            reserve_resources.__doc__ or "",
        ]
    )
    for provider in PROVIDER_REGISTRY:
        fragment = AgentBase._load_prompt_fragment(prompts_dir, provider)
        assert fragment
        assert fragment not in shared_text

    provider_fields = {
        field
        for contract in RESERVATION_SELECTION_CONTRACTS.values()
        for field in _schema_property_names(contract.json_schema())
    }
    for field in provider_fields:
        assert not re.search(rf"\b{re.escape(field)}\b", shared_text)


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
    line_owners: dict[str, set[str]] = {}
    for harness, fragment in registered_fragments.items():
        for line in fragment.splitlines():
            marker = line.strip()
            if len(marker) >= 16:
                line_owners.setdefault(marker, set()).add(harness)
    scope_markers = {
        harness: {
            marker for marker, owners in line_owners.items() if owners == {harness}
        }
        for harness in registered_fragments
    }

    assert all(scope_markers.values()), "registered fragments need unique scope markers"
    for markers in scope_markers.values():
        for marker in markers:
            assert marker not in BENCHMARK_BASE_PROMPT

    for harness, expected_fragment in registered_fragments.items():
        agent = BenchmarkAgent.__new__(BenchmarkAgent)
        agent._skill_provider = skill_provider
        prompt = agent._system_prompt(
            {"custom_fields": {"directives": {"harness": harness}}}
        )
        assert expected_fragment in prompt
        for marker in scope_markers[harness]:
            assert marker in {line.strip() for line in prompt.splitlines()}
        for other_harness, other_markers in scope_markers.items():
            if other_harness != harness:
                for marker in other_markers:
                    assert marker not in {line.strip() for line in prompt.splitlines()}


def _provider_name_branches(function) -> set[str]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
            continue
        comparison = node.test
        if not isinstance(comparison.left, ast.Name):
            continue
        if comparison.left.id not in {"provider", "rp"}:
            continue
        names.update(
            comparator.value
            for comparator in comparison.comparators
            if isinstance(comparator, ast.Constant)
            and isinstance(comparator.value, str)
        )
    return names


def test_shared_resource_selection_flow_has_no_quads_name_branch():
    from agents.resource.agent import _prepare_selection_via_resource_tool
    from agents.resource.server import check_available_resources, reserve_resources

    shared_selection_flow = [
        check_available_resources,
        reserve_resources,
        _prepare_selection_via_resource_tool,
    ]
    for function in shared_selection_flow:
        assert "quads" not in _provider_name_branches(function)


def _configure_quads_server(monkeypatch, ticket):
    import agents.resource.server as srv
    from providers.resource.quads import QuadsResourceProvider

    monkeypatch.delenv("TICKET_ID", raising=False)
    provider = QuadsResourceProvider(MagicMock())
    provider.reserve = AsyncMock()
    registry = MagicMock()
    registry.get_provider = AsyncMock(return_value=provider)
    marker = AsyncMock()

    monkeypatch.setattr(srv, "_ensure_init", AsyncMock())
    monkeypatch.setattr(srv, "_initialized", True)
    monkeypatch.setattr(srv, "_registry", registry)
    monkeypatch.setattr(srv, "_ticket", ticket)
    monkeypatch.setattr(srv, "_resources_allocated", False)
    monkeypatch.setattr(srv, "_reservation_uncertain", False)
    monkeypatch.setattr(srv, "_reservation_failures", 0)
    monkeypatch.setattr(srv, "_last_reservation", {})
    monkeypatch.setattr(srv, "_persist_unknown_reservation_marker", marker)
    monkeypatch.setattr(srv, "_persist_known_reservation_outcome", AsyncMock())
    return srv, provider, marker


@pytest.mark.asyncio
async def test_quads_aws_shaped_selection_is_rejected_before_marker_or_provider_call(
    monkeypatch,
):
    ticket = {
        "id": "PERF-EAF95C59",
        "custom_fields": {
            "resource_provider": "quads",
            "required_hosts": [
                {"roles": ["controller"]},
                {"roles": ["client"]},
                {"roles": ["server"]},
            ],
        },
    }
    srv, provider, marker = _configure_quads_server(monkeypatch, ticket)

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


@pytest.mark.asyncio
async def test_quads_requires_discovery_even_without_ticket_required_hosts(monkeypatch):
    ticket = {"custom_fields": {"resource_provider": "quads"}}
    srv, provider, marker = _configure_quads_server(monkeypatch, ticket)

    raw = await srv.reserve_resources(
        provider="quads",
        selection={"hostnames": ["invented.example.com"]},
        description="must be discovered first",
        ticket_id="PERF-TEST",
    )
    result = json.loads(raw)

    assert result["status"] == "invalid_selection"
    assert result["provider_call_started"] is False
    assert "eligible host options are unavailable" in result["error"]
    provider.reserve.assert_not_awaited()
    marker.assert_not_awaited()


@pytest.mark.asyncio
async def test_quads_rejects_eligibility_cached_for_a_different_ticket_context(
    monkeypatch,
):
    ticket = {
        "id": "PERF-TEST",
        "custom_fields": {
            "resource_provider": "quads",
            "required_hosts": [{"roles": ["client"]}],
        },
    }
    srv, provider, marker = _configure_quads_server(monkeypatch, ticket)
    provider.record_availability_context(
        [{"options": [{"hostname": "host-01.example.com"}]}],
        srv._reservation_ticket_context("PERF-TEST", ticket["custom_fields"]),
    )
    ticket["custom_fields"]["required_hosts"] = [{"roles": ["server"]}]

    raw = await srv.reserve_resources(
        provider="quads",
        selection={"hostnames": ["host-01.example.com"]},
        description="stale discovery must not authorize reserve",
        ticket_id="PERF-TEST",
    )
    result = json.loads(raw)

    assert result["status"] == "invalid_selection"
    assert result["provider_call_started"] is False
    assert "eligible host options are unavailable" in result["error"]
    provider.reserve.assert_not_awaited()
    marker.assert_not_awaited()


@pytest.mark.asyncio
async def test_quads_per_host_discovery_exposes_options_for_distinct_selection(
    monkeypatch,
):
    required_hosts = [{"roles": ["target"], "min_cores": 32} for _ in range(3)]
    ticket = {
        "id": "PERF-TEST",
        "custom_fields": {
            "resource_provider": "quads",
            "required_hosts": required_hosts,
        },
    }
    srv, provider, marker = _configure_quads_server(monkeypatch, ticket)
    options = [
        {"hostname": "host-01.example.com", "cores": 64},
        {"hostname": "host-02.example.com", "cores": 64},
        {"hostname": "host-03.example.com", "cores": 64},
    ]
    provider.check_available = AsyncMock(
        return_value={"available_count": 3, "options": options}
    )
    provider.reserve.return_value = {
        "status": "active",
        "reservation_id": "73",
        "provider_metadata": {"assignment_id": 73},
        "hosts": [option["hostname"] for option in options],
    }

    raw = await srv.check_available_resources(
        provider="quads", required_hosts=required_hosts
    )
    availability = json.loads(raw)
    recommendations = availability["per_host_recommendations"]

    assert len(recommendations) == 3
    assert all(item["recommended"] == options[0] for item in recommendations)
    assert all(item["options"] == options for item in recommendations)
    selection = {
        "hostnames": [
            item["options"][index]["hostname"]
            for index, item in enumerate(recommendations)
        ]
    }

    raw = await srv.reserve_resources(
        provider="quads",
        selection=selection,
        description="three identical requirements",
        ticket_id="PERF-TEST",
    )
    result = json.loads(raw)

    assert result["status"] == "active"
    assert selection["hostnames"] == [option["hostname"] for option in options]
    provider.reserve.assert_awaited_once()
    marker.assert_awaited_once()


@pytest.mark.asyncio
async def test_quads_reorders_reversed_controller_target_selection_to_ticket_order(
    monkeypatch,
):
    required_hosts = [
        {"roles": ["controller"], "min_cores": 32},
        {"roles": ["target"], "min_cores": 16},
    ]
    ticket = {
        "id": "PERF-TEST",
        "custom_fields": {
            "resource_provider": "quads",
            "required_hosts": required_hosts,
        },
    }
    srv, provider, marker = _configure_quads_server(monkeypatch, ticket)
    options_by_role = {
        "controller": [{"hostname": "controller.example.com"}],
        "target": [{"hostname": "target.example.com"}],
    }
    provider.check_available = AsyncMock(
        side_effect=lambda requirements: {
            "available_count": 1,
            "options": options_by_role[requirements["roles"][0]],
        }
    )
    provider.reserve.return_value = {
        "status": "active",
        "reservation_id": "74",
        "provider_metadata": {"assignment_id": 74},
        "hosts": ["controller.example.com", "target.example.com"],
    }

    await srv.check_available_resources(provider="quads", required_hosts=required_hosts)
    raw = await srv.reserve_resources(
        provider="quads",
        selection={"hostnames": ["target.example.com", "controller.example.com"]},
        description="reversed host response order",
        ticket_id="PERF-TEST",
    )

    assert json.loads(raw)["status"] == "active", raw
    assert provider.reserve.await_args.args[0]["hostnames"] == [
        "controller.example.com",
        "target.example.com",
    ]
    marker.assert_awaited_once()


@pytest.mark.asyncio
async def test_auto_preparation_dispatches_to_provider_hook_without_reserving(
    monkeypatch,
):
    required_hosts = [
        {"roles": ["controller"]},
        {"roles": ["target"]},
    ]
    ticket = {
        "id": "PERF-TEST",
        "custom_fields": {
            "resource_provider": "quads",
            "required_hosts": required_hosts,
        },
    }
    srv, provider, marker = _configure_quads_server(monkeypatch, ticket)
    options_by_role = {
        "controller": [{"hostname": "controller.example.com"}],
        "target": [{"hostname": "target.example.com"}],
    }
    provider.check_available = AsyncMock(
        side_effect=lambda requirements: {
            "available_count": 1,
            "options": options_by_role[requirements["roles"][0]],
        }
    )

    raw = await srv.prepare_reservation_selection(
        provider="quads",
        selection={"hostnames": ["target.example.com", "controller.example.com"]},
        required_hosts=required_hosts,
        ticket_id="PERF-TEST",
    )
    result = json.loads(raw)

    assert result["status"] == "ready"
    assert result["selection"]["hostnames"] == [
        "controller.example.com",
        "target.example.com",
    ]
    assert provider.check_available.await_count == 2
    provider.reserve.assert_not_awaited()
    marker.assert_not_awaited()


@pytest.mark.asyncio
async def test_quads_adapter_structurally_validates_before_assignment_creation():
    from providers.resource.quads import QuadsResourceProvider

    client = MagicMock()
    client.create_assignment = AsyncMock()
    provider = QuadsResourceProvider(client)

    with pytest.raises(ReservationSelectionError):
        await provider.reserve(
            {"hostnames": ["host-01.example.com"], "instance_type": "m5.xlarge"},
            "invalid direct adapter call",
        )

    client.create_assignment.assert_not_awaited()


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
            assert not re.search(rf"\b{re.escape(field)}\b", auto_select_prompt)


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
