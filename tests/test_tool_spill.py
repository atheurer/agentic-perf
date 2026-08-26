from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import paths
from agents.base import AgentBase
from providers.llm.base import ToolCall
from providers.workspace.manager import WorkspaceManager


class DummyAgent(AgentBase):
    def _system_prompt(self, ticket):
        return "You are a test agent."

    def _build_messages(self, ticket):
        return [{"role": "user", "content": "Run tests"}]

    async def _handle_completion(self, ticket_id, response):
        return None


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "TICKET_DIR", tmp_path / "tickets")
    mock_llm = MagicMock()
    mock_events = MagicMock()

    test_agent = DummyAgent(
        agent_name="test_agent",
        llm_provider=mock_llm,
        state_store_url="http://fake-store",
        event_bus=mock_events,
    )
    test_agent._current_ticket_id = "PERF-SPILL-1"
    return test_agent


async def test_tool_output_under_threshold_not_spilled(agent):
    small_payload = json.dumps({"status": "ok", "count": 5})
    agent._tool_handlers = {"fetch_status": AsyncMock(return_value=small_payload)}

    call = ToolCall(id="call_1", name="fetch_status", input={})
    res = await agent._execute_tool(call)

    assert not res.is_error
    assert res.content == small_payload


async def test_tool_output_over_threshold_spilled(agent):
    # Large 50KB payload
    large_data = {"series": [i * 1.5 for i in range(5000)]}
    large_payload = json.dumps(large_data)
    agent._tool_handlers = {"get_metrics": AsyncMock(return_value=large_payload)}

    call = ToolCall(id="call_2", name="get_metrics", input={})
    res = await agent._execute_tool(call)

    assert not res.is_error
    parsed_res = json.loads(res.content)
    assert parsed_res["status"] == "spilled_to_workspace"
    assert parsed_res["tool_name"] == "get_metrics"
    assert parsed_res["file_ref"].startswith("workspace://get_metrics_")
    assert parsed_res["format"] == "json"
    assert parsed_res["size_bytes"] == len(large_payload.encode("utf-8"))
    assert "preview" in parsed_res

    # Verify we can query the spilled file directly via WorkspaceManager
    manager = WorkspaceManager(ticket_id=agent._current_ticket_id)
    jq_res = manager.jq_query(parsed_res["file_ref"], ".series[0:3]")
    assert jq_res["status"] == "ok"
    assert jq_res["result"] == [0.0, 1.5, 3.0]


async def test_exempt_tools_not_spilled(agent, monkeypatch):
    monkeypatch.setenv("TOOL_SPILL_THRESHOLD", "10")
    agent._spill_threshold = 10

    # jq_query and submit_* should be exempt
    exempt_payload = json.dumps({"rows": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]})
    agent._tool_handlers = {
        "jq_query": AsyncMock(return_value=exempt_payload),
        "submit_review_result": AsyncMock(return_value=exempt_payload),
    }

    call1 = ToolCall(id="call_jq", name="jq_query", input={})
    res1 = await agent._execute_tool(call1)
    assert res1.content == exempt_payload

    call2 = ToolCall(id="call_sub", name="submit_review_result", input={})
    res2 = await agent._execute_tool(call2)
    assert res2.content == exempt_payload


async def test_caller_configured_threshold(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "TICKET_DIR", tmp_path / "tickets")
    mock_llm = MagicMock()
    mock_events = MagicMock()

    # Explicit threshold 100 bytes via constructor
    custom_agent = DummyAgent(
        agent_name="custom_agent",
        llm_provider=mock_llm,
        state_store_url="http://fake-store",
        event_bus=mock_events,
        tool_spill_threshold=100,
    )
    custom_agent._current_ticket_id = "PERF-CONFIG-1"

    payload_150_bytes = json.dumps({"data": "x" * 150})
    custom_agent._tool_handlers = {"get_data": AsyncMock(return_value=payload_150_bytes)}

    call = ToolCall(id="call_custom", name="get_data", input={})
    res = await custom_agent._execute_tool(call)

    assert not res.is_error
    parsed = json.loads(res.content)
    assert parsed["status"] == "spilled_to_workspace"
    assert parsed["file_ref"].startswith("workspace://get_data_")
