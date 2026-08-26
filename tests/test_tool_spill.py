from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import paths
from agents.base import AgentBase
from providers.llm.base import ToolCall


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


async def test_agent_has_native_workspace_tools(agent):
    tool_names = [t.name for t in agent.tools]
    assert "jq_query" in tool_names
    assert "grep_file" in tool_names
    assert "read_file_slice" in tool_names
    assert "list_workspace_files" in tool_names


async def test_tool_output_under_threshold_not_spilled(agent):
    small_payload = json.dumps({"status": "ok", "count": 5})
    agent._tool_handlers["fetch_status"] = AsyncMock(return_value=small_payload)

    call = ToolCall(id="call_1", name="fetch_status", input={})
    res = await agent._execute_tool(call)

    assert not res.is_error
    assert res.content == small_payload


async def test_tool_output_over_threshold_spilled_and_queryable(agent):
    # Large 50KB payload
    large_data = {"series": [i * 1.5 for i in range(5000)]}
    large_payload = json.dumps(large_data)
    agent._tool_handlers["get_metrics"] = AsyncMock(return_value=large_payload)

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

    # Query via agent's native jq_query tool handler
    jq_call = ToolCall(
        id="call_jq",
        name="jq_query",
        input={"file_ref": parsed_res["file_ref"], "filter": ".series[0:3]"},
    )
    jq_res = await agent._execute_tool(jq_call)
    assert not jq_res.is_error
    parsed_jq = json.loads(jq_res.content)
    assert parsed_jq["status"] == "ok"
    assert parsed_jq["result"] == [0.0, 1.5, 3.0]


async def test_exempt_tools_not_spilled(agent, monkeypatch):
    monkeypatch.setenv("TOOL_SPILL_THRESHOLD", "10")
    agent._spill_threshold = 10

    exempt_tools = [
        "jq_query",
        "grep_file",
        "read_file_slice",
        "list_workspace_files",
        "read_skill",
        "read_skills",
        "read_harness_doc",
        "get_review_config",
        "get_execution_config",
        "get_example_runfile",
        "get_tool_params",
        "read_remote_file",
        "request_clarification",
        "present_runfile_for_approval",
        "submit_review_result",
    ]

    large_payload = json.dumps({"rows": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]})
    for tool_name in exempt_tools:
        agent._tool_handlers[tool_name] = AsyncMock(return_value=large_payload)
        call = ToolCall(id=f"call_{tool_name}", name=tool_name, input={})
        res = await agent._execute_tool(call)
        assert res.content == large_payload, f"{tool_name} was unexpectedly spilled"


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
    custom_agent._tool_handlers["get_data"] = AsyncMock(return_value=payload_150_bytes)

    call = ToolCall(id="call_custom", name="get_data", input={})
    res = await custom_agent._execute_tool(call)

    assert not res.is_error
    parsed = json.loads(res.content)
    assert parsed["status"] == "spilled_to_workspace"
    assert parsed["file_ref"].startswith("workspace://get_data_")
