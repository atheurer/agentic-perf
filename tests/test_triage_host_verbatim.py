"""Tests for triage required_hosts verbatim-host validation (issue #1363).

Triage can drop a DNS label while copying long, near-identical sibling FQDNs.
The mismatch was previously caught only at the awaiting_provision handoff —
after the resource and platform agents had already run — with an error that
blamed the resource agent. ``_validate_submit_call`` now rejects the submit
with the offending value and suggested corrections so the model self-corrects
in the same loop.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from agents.triage.agent import (
    TriageAgent,
    _collect_custom_field_text,
    _verbatim_host_mismatches,
)
from providers.llm.base import ToolCall

DESCRIPTION = (
    "Please run an uperf throughput comparison.\n\n"
    "Client:     u37-h12-000-r7725.rdu3.labs.perfscale.redhat.com\n"
    "Server:     u37-h14-000-r7725.rdu3.labs.perfscale.redhat.com\n"
)


def _agent(
    description: str = DESCRIPTION, custom_fields: dict | None = None
) -> TriageAgent:
    ticket = {
        "id": "PERF-TRIAGE-HOST",
        "status": "triage_pending",
        "summary": "Benchmark request",
        "description": description,
        "custom_fields": custom_fields or {},
    }
    agent = TriageAgent(
        llm_provider=AsyncMock(),
        state_store_url="http://localhost:8090",
        skill_provider=_FakeSkillProvider(),
    )
    agent._get_ticket = AsyncMock(return_value=ticket)
    return agent


class _FakeSkillProvider:
    def list_harnesses(self) -> list[str]:
        return ["crucible"]


def _submit_call(required_hosts: list[dict]) -> ToolCall:
    return ToolCall(
        id="submit-1",
        name="submit_triage_result",
        input={
            "parsed_specs": {},
            "hypothesis": "throughput",
            "required_hosts": required_hosts,
        },
    )


def test_submit_call_construction_matches_tool_contract():
    """The submit payload shape used by these tests mirrors the tool schema."""
    call = _submit_call([{"roles": ["client"]}])
    assert call.name == "submit_triage_result"
    assert isinstance(call.input["required_hosts"], list)


@pytest.mark.asyncio
async def test_submit_with_matching_hosts_is_allowed():
    agent = _agent()
    call = _submit_call(
        [
            {
                "roles": ["client"],
                "host": "u37-h12-000-r7725.rdu3.labs.perfscale.redhat.com",
            },
            {
                "roles": ["server"],
                "host": "u37-h14-000-r7725.rdu3.labs.perfscale.redhat.com",
            },
        ]
    )
    assert await agent._validate_submit_call("PERF-TRIAGE-HOST", call) is None


@pytest.mark.asyncio
async def test_submit_with_dropped_dns_label_is_rejected_with_suggestion():
    """The exact PERF-51EC4BDA shape: one dropped label on a sibling FQDN."""
    agent = _agent()
    call = _submit_call(
        [
            {
                "roles": ["client"],
                "host": "u37-h12-000-r7725.rdu3.labs.perfscale.redhat.com",
            },
            {
                "roles": ["server"],
                # rdu3 label dropped — never named by the user
                "host": "u37-h14-000-r7725.labs.perfscale.redhat.com",
            },
        ]
    )
    rejection = await agent._validate_submit_call("PERF-TRIAGE-HOST", call)
    assert rejection is not None
    assert rejection.startswith("REJECTED")
    assert "u37-h14-000-r7725.labs.perfscale.redhat.com" in rejection
    # The correct sibling must be suggested so the model can self-correct.
    assert "u37-h14-000-r7725.rdu3.labs.perfscale.redhat.com" in rejection


@pytest.mark.asyncio
async def test_host_from_custom_fields_is_accepted():
    agent = _agent(
        custom_fields={
            "known_hosts": {"server": "10.0.0.42"},
        }
    )
    call = _submit_call([{"roles": ["server"], "host": "10.0.0.42"}])
    assert await agent._validate_submit_call("PERF-TRIAGE-HOST", call) is None


@pytest.mark.asyncio
async def test_allocated_hosts_without_host_field_are_ignored():
    agent = _agent()
    call = _submit_call([{"roles": ["client"]}, {"roles": ["server"], "nic_speed": 25}])
    assert await agent._validate_submit_call("PERF-TRIAGE-HOST", call) is None


@pytest.mark.asyncio
async def test_store_failure_fails_open():
    agent = _agent()
    agent._get_ticket = AsyncMock(side_effect=ConnectionError("store down"))
    call = _submit_call([{"roles": ["client"], "host": "not-in-description.example"}])
    assert await agent._validate_submit_call("PERF-TRIAGE-HOST", call) is None


@pytest.mark.asyncio
async def test_only_triage_submit_tool_is_validated():
    agent = _agent()
    call = ToolCall(
        id="other-1",
        name="submit_review_result",
        input={"required_hosts": [{"roles": ["client"], "host": "bogus.example"}]},
    )
    assert await agent._validate_submit_call("PERF-TRIAGE-HOST", call) is None


def test_verbatim_host_mismatches_suggestions_rank_closest_first():
    ticket = {"description": "Use host-a.rdu3.labs.example.com and 10.1.2.3"}
    mismatches = _verbatim_host_mismatches(
        [{"roles": ["client"], "host": "host-a.rdu3.example.com"}], ticket
    )
    assert len(mismatches) == 1
    _i, submitted, suggestions = mismatches[0]
    assert submitted == "host-a.rdu3.example.com"
    assert "host-a.rdu3.labs.example.com" in suggestions


def test_verbatim_host_mismatches_empty_description_fails_open():
    assert (
        _verbatim_host_mismatches(
            [{"roles": ["client"], "host": "bogus.example"}], {"description": ""}
        )
        == []
    )
    assert _verbatim_host_mismatches([{"roles": ["client"]}], None) == []


def test_collect_custom_field_text_flattens_nested_values():
    text = _collect_custom_field_text(
        {"a": {"b": ["10.0.0.1", 5, None]}, "c": "s.example.com", "d": 7}
    )
    assert "10.0.0.1" in text
    assert "s.example.com" in text
