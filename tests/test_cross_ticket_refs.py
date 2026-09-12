"""Tests for cross-ticket artifact resolution."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from agents.analyze.agent import AnalyzeAgent
from agents.base import AgentBase
from agents.review.agent import ReviewAgent
from agents.server_utils import extract_ticket_references
from providers.llm.base import LLMProvider


@pytest.fixture(params=[AnalyzeAgent, ReviewAgent], ids=["analyze", "review"])
async def artifact_agent(request: pytest.FixtureRequest) -> AsyncIterator[AgentBase]:
    agent = request.param(
        llm_provider=MagicMock(spec=LLMProvider),
        state_store_url="http://state-store.test",
    )
    try:
        yield agent
    finally:
        await agent.close()


async def test_structured_references_reach_run_context(
    artifact_agent: AgentBase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise each agent's run prefetch and real initial-message builder."""
    ticket = {
        "id": "PERF-00000000",
        "summary": "Compare prior results",
        "description": "Compare the selected baseline.",
        "custom_fields": {"reference_tickets": ["PERF-11111111"]},
    }
    artifact_fields = {"output_dir": "/artifacts/baseline", "run_id": "baseline-run"}
    get_ticket = AsyncMock(
        side_effect=[ticket, {"custom_fields": artifact_fields}],
    )
    monkeypatch.setattr(artifact_agent, "_get_ticket", get_ticket)
    mcp = AsyncMock()
    mcp.list_tools.return_value = []
    monkeypatch.setattr(
        f"{type(artifact_agent).__module__}.AgentMCPClient", lambda: mcp
    )
    monkeypatch.setattr(
        "agents.mcp_client.connect_external_servers",
        AsyncMock(return_value=([], None)),
    )
    messages: list[dict[str, Any]] = []

    async def capture_messages(agent: AgentBase, ticket_id: str) -> None:
        assert ticket_id == ticket["id"]
        messages.extend(agent._build_messages(ticket))

    monkeypatch.setattr(AgentBase, "run", capture_messages)

    await artifact_agent.run(ticket["id"])

    assert get_ticket.await_args_list == [call("PERF-00000000"), call("PERF-11111111")]
    content = messages[0]["content"]
    assert "## Referenced Ticket Artifacts" in content
    assert "**PERF-11111111:**" in content
    assert "output_dir: `/artifacts/baseline`" in content
    assert "run_id: `baseline-run`" in content
    mcp.disconnect.assert_awaited_once()


async def test_reference_sources_are_combined_and_deduplicated(
    artifact_agent: AgentBase, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket = {
        "id": "PERF-00000000",
        "description": "Compare PERF-11111111 with PERF-22222222 and PERF-00000000",
        "custom_fields": {
            "hypothesis": "PERF-22222222 should match PERF-33333333",
            "reference_tickets": ["PERF-00000000", "PERF-11111111", "PERF-44444444"],
        },
    }
    artifact_fields = {"output_dir": "/artifacts/baseline", "run_id": "baseline-run"}
    get_ticket = AsyncMock(return_value={"custom_fields": artifact_fields})
    monkeypatch.setattr(artifact_agent, "_get_ticket", get_ticket)

    await artifact_agent._resolve_referenced_artifacts(ticket)

    expected_ids = ["PERF-11111111", "PERF-22222222", "PERF-33333333", "PERF-44444444"]
    assert get_ticket.await_args_list == [call(ticket_id) for ticket_id in expected_ids]
    assert artifact_agent._referenced_artifacts == {
        ticket_id: artifact_fields for ticket_id in expected_ids
    }


async def test_missing_artifacts_do_not_block_or_leak_context(
    artifact_agent: AgentBase, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket = {
        "id": "PERF-00000000",
        "summary": "Compare prior results",
        "description": "Compare the selected baselines.",
        "custom_fields": {
            "reference_tickets": ["PERF-11111111", "PERF-22222222", "PERF-33333333"],
        },
    }
    get_ticket = AsyncMock(
        side_effect=[
            RuntimeError("Referenced ticket unavailable"),
            {"custom_fields": {"run_id": "no-artifacts"}},
            {"custom_fields": {"output_dir": "/artifacts/available"}},
        ],
    )
    monkeypatch.setattr(artifact_agent, "_get_ticket", get_ticket)

    await artifact_agent._resolve_referenced_artifacts(ticket)

    assert get_ticket.await_count == 3
    assert artifact_agent._referenced_artifacts == {
        "PERF-33333333": {"output_dir": "/artifacts/available", "run_id": ""},
    }
    content = artifact_agent._build_messages(ticket)[0]["content"]
    assert "**PERF-33333333:**" in content
    assert "**PERF-11111111:**" not in content
    assert "**PERF-22222222:**" not in content

    ticket["custom_fields"] = {}
    get_ticket.reset_mock()
    await artifact_agent._resolve_referenced_artifacts(ticket)

    get_ticket.assert_not_awaited()
    assert artifact_agent._referenced_artifacts == {}
    content = artifact_agent._build_messages(ticket)[0]["content"]
    assert "## Referenced Ticket Artifacts" not in content


class TestExtractTicketReferences:
    """Test PERF-XXXXXXXX extraction from text."""

    def test_single_reference(self):
        text = "Compare with PERF-0D71DDDE"
        assert extract_ticket_references(text) == ["PERF-0D71DDDE"]

    def test_multiple_references(self):
        text = "Compare PERF-0D71DDDE and PERF-04E41F3F"
        refs = extract_ticket_references(text)
        assert refs == ["PERF-0D71DDDE", "PERF-04E41F3F"]

    def test_deduplication(self):
        text = "PERF-AABBCCDD mentioned twice: PERF-AABBCCDD"
        assert extract_ticket_references(text) == ["PERF-AABBCCDD"]

    def test_preserves_order(self):
        text = "First PERF-11111111 then PERF-22222222"
        refs = extract_ticket_references(text)
        assert refs == ["PERF-11111111", "PERF-22222222"]

    def test_no_references(self):
        text = "Run boot-time on qc8775"
        assert extract_ticket_references(text) == []

    def test_partial_id_ignored(self):
        text = "PERF-ABC is too short"
        assert extract_ticket_references(text) == []

    def test_lowercase_ignored(self):
        text = "perf-0d71ddde lowercase"
        assert extract_ticket_references(text) == []

    def test_embedded_in_url(self):
        text = "See https://example.com/tickets/PERF-0D71DDDE for details"
        assert extract_ticket_references(text) == ["PERF-0D71DDDE"]
