from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.base import AgentBase
from agents.mcp_client import AgentMCPClient, MCPToolCallError, _ServerConnection
from providers.llm.base import ToolCall, ToolDefinition


class DummyAgent(AgentBase):
    def _system_prompt(self, ticket):
        return "You are a test agent."

    def _build_messages(self, ticket):
        return [{"role": "user", "content": "Run tests"}]

    async def _handle_completion(self, ticket_id, response):
        return None


@pytest.fixture
def agent():
    test_agent = DummyAgent(
        agent_name="test_agent",
        llm_provider=MagicMock(),
        state_store_url="http://fake-store",
        event_bus=MagicMock(),
    )
    test_agent._tool_min_interval = 0
    return test_agent


async def test_native_type_error_after_entry_is_not_replayed(agent):
    calls = 0

    async def handler() -> str:
        nonlocal calls
        calls += 1
        raise TypeError("handler failed after entry")

    agent._tool_handlers["mutate"] = handler
    result = await agent._execute_tool(ToolCall(id="call-1", name="mutate", input={}))

    assert calls == 1
    assert result.is_error
    assert json.loads(result.content)["retry_classification"] == "ambiguous_after_send"


async def test_mcp_failure_after_call_is_not_replayed(agent):
    mcp = MagicMock()
    mcp.call_tool = AsyncMock(side_effect=RuntimeError("connection lost"))
    agent._mcp = mcp

    result = await agent._execute_tool(ToolCall(id="call-1", name="mutate", input={}))

    assert mcp.call_tool.await_count == 1
    assert result.is_error
    assert json.loads(result.content)["retry_classification"] == "ambiguous_after_send"


async def test_mcp_client_marks_session_failure_as_ambiguous_after_send():
    session = MagicMock()
    session.call_tool = AsyncMock(side_effect=RuntimeError("connection lost"))
    client = AgentMCPClient()
    client._tool_routing["mutate"] = "test-server"
    client._servers["test-server"] = _ServerConnection(
        name="test-server", session=session
    )

    with pytest.raises(MCPToolCallError) as exc_info:
        await client.call_tool("mutate", {})

    assert exc_info.value.retry_classification == "ambiguous_after_send"
    assert session.call_tool.await_count == 1


async def test_mcp_client_missing_connection_fails_before_dispatch():
    session = MagicMock()
    session.call_tool = AsyncMock()
    hook = AsyncMock()
    client = AgentMCPClient()
    client._tool_routing["mutate"] = "missing-server"
    client.pre_call_hook = hook

    with pytest.raises(MCPToolCallError) as exc_info:
        await client.call_tool("mutate", {})

    assert exc_info.value.retry_classification == "transport_before_send"
    hook.assert_not_awaited()
    session.call_tool.assert_not_awaited()


async def test_mcp_hook_dispatch_failure_is_ambiguous_and_not_replayed():
    dispatched = AsyncMock()

    async def hook(name: str, arguments: dict[str, object]) -> None:
        await dispatched(name, arguments)
        raise RuntimeError("connection lost after hook dispatch")

    client = AgentMCPClient()
    client._tool_routing["mutate"] = "test-server"
    client._servers["test-server"] = _ServerConnection(
        name="test-server", session=MagicMock()
    )
    client.pre_call_hook = hook

    with pytest.raises(MCPToolCallError) as exc_info:
        await client.call_tool("mutate", {"value": 1})

    assert exc_info.value.retry_classification == "ambiguous_after_send"
    dispatched.assert_awaited_once_with("mutate", {"value": 1})


async def test_mcp_hook_preserves_typed_failure_classification():
    async def hook(name: str, arguments: dict[str, object]) -> None:
        raise MCPToolCallError("invalid request", "validation")

    client = AgentMCPClient()
    client._tool_routing["mutate"] = "test-server"
    client._servers["test-server"] = _ServerConnection(
        name="test-server", session=MagicMock()
    )
    client.pre_call_hook = hook

    with pytest.raises(MCPToolCallError) as exc_info:
        await client.call_tool("mutate", {})

    assert exc_info.value.retry_classification == "validation"


async def test_jq_filter_is_stripped_only_when_schema_omits_it(agent):
    without_filter = AsyncMock(return_value="without")
    with_filter = AsyncMock(return_value="with")
    agent.tools = [
        ToolDefinition(
            name="without_filter",
            description="",
            input_schema={"properties": {}},
        ),
        ToolDefinition(
            name="with_filter",
            description="",
            input_schema={"properties": {"jq_filter": {"type": "string"}}},
        ),
    ]
    agent._tool_handlers.update(
        {"without_filter": without_filter, "with_filter": with_filter}
    )

    await agent._execute_tool(
        ToolCall(
            id="call-1",
            name="without_filter",
            input={"jq_filter": ".items"},
        )
    )
    await agent._execute_tool(
        ToolCall(
            id="call-2",
            name="with_filter",
            input={"jq_filter": ".items"},
        )
    )

    without_filter.assert_awaited_once_with()
    with_filter.assert_awaited_once_with(jq_filter=".items")


async def test_signature_mismatch_is_rejected_before_handler_entry(agent):
    calls = 0

    async def handler(required: str) -> str:
        nonlocal calls
        calls += 1
        return required

    agent._tool_handlers["needs_argument"] = handler
    result = await agent._execute_tool(
        ToolCall(id="call-1", name="needs_argument", input={})
    )

    assert calls == 0
    assert result.is_error
    assert json.loads(result.content)["retry_classification"] == "validation"
