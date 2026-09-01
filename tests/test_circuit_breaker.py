"""Tests for the repeated-failure circuit breaker.

Covers:
- classify_result: empty, error, healthy, JSON shapes, spill descriptors
- CircuitBreakerState: streaks, resets, interleaving, trips, caps, exemptions
- Config merging: circuit_breaker_from_config, circuit_breaker_from_custom_fields
- Integration: [SYSTEM] injection in agent loop, event emission, max_trips cap
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from providers.circuit_breaker import (
    CircuitBreakerState,
    circuit_breaker_from_config,
    circuit_breaker_from_custom_fields,
    classify_result,
)

# ── classify_result ────────────────────────────────────────────


class TestClassifyResult:
    def test_is_error_true(self):
        assert classify_result("t", "some output", is_error=True) is True

    def test_empty_string(self):
        assert classify_result("t", "", is_error=False) is True

    def test_whitespace_only(self):
        assert classify_result("t", "   ", is_error=False) is True

    def test_empty_list_string(self):
        assert classify_result("t", "[]", is_error=False) is True

    def test_empty_dict_string(self):
        assert classify_result("t", "{}", is_error=False) is True

    def test_null_string(self):
        assert classify_result("t", "null", is_error=False) is True

    def test_healthy_text(self):
        assert classify_result("t", "All good", is_error=False) is False

    def test_healthy_json(self):
        content = json.dumps({"status": "ok", "results": [1, 2, 3]})
        assert classify_result("t", content, is_error=False) is False

    def test_json_exit_code_nonzero(self):
        content = json.dumps({"exit_code": 1, "stderr": "fail"})
        assert classify_result("t", content, is_error=False) is True

    def test_json_exit_code_zero(self):
        content = json.dumps({"exit_code": 0, "stdout": "ok"})
        assert classify_result("t", content, is_error=False) is False

    def test_json_success_false(self):
        content = json.dumps({"success": False})
        assert classify_result("t", content, is_error=False) is True

    def test_json_success_true(self):
        content = json.dumps({"success": True, "data": "x"})
        assert classify_result("t", content, is_error=False) is False

    def test_json_status_failed(self):
        content = json.dumps({"status": "failed"})
        assert classify_result("t", content, is_error=False) is True

    def test_json_status_error(self):
        content = json.dumps({"status": "Error"})
        assert classify_result("t", content, is_error=False) is True

    def test_json_status_no_files_found(self):
        content = json.dumps({"status": "no_files_found", "dir": ["/a", "/b"]})
        assert classify_result("t", content, is_error=False) is True

    def test_json_status_no_results(self):
        content = json.dumps({"status": "no_results"})
        assert classify_result("t", content, is_error=False) is True

    def test_json_status_not_found(self):
        content = json.dumps({"status": "not_found"})
        assert classify_result("t", content, is_error=False) is True

    def test_json_error_field(self):
        content = json.dumps({"error": "Something went wrong"})
        assert classify_result("t", content, is_error=False) is True

    def test_json_error_field_none(self):
        content = json.dumps({"error": "none", "data": "ok"})
        assert classify_result("t", content, is_error=False) is False

    def test_json_error_field_null(self):
        content = json.dumps({"error": "null"})
        assert classify_result("t", content, is_error=False) is False

    def test_json_empty_results_list(self):
        content = json.dumps({"results": []})
        assert classify_result("t", content, is_error=False) is True

    def test_json_empty_matches_list(self):
        content = json.dumps({"matches": []})
        assert classify_result("t", content, is_error=False) is True

    def test_json_empty_files_list(self):
        content = json.dumps({"files": []})
        assert classify_result("t", content, is_error=False) is True

    def test_json_nonempty_results(self):
        content = json.dumps({"results": [{"id": 1}]})
        assert classify_result("t", content, is_error=False) is False

    def test_spill_descriptor(self):
        content = json.dumps(
            {
                "file_ref": "workspace://tool_1.json",
                "preview": "some data...",
                "size_bytes": 50000,
            }
        )
        assert classify_result("t", content, is_error=False) is False

    def test_spill_descriptor_with_error(self):
        content = json.dumps(
            {
                "file_ref": "workspace://error.json",
                "preview": "error details...",
                "success": False,
            }
        )
        assert classify_result("t", content, is_error=False) is True

    def test_spill_descriptor_with_exit_code(self):
        content = json.dumps(
            {
                "file_ref": "workspace://output.json",
                "exit_code": 1,
            }
        )
        assert classify_result("t", content, is_error=False) is True

    def test_exit_code_string_zero(self):
        content = json.dumps({"exit_code": "0", "stdout": "ok"})
        assert classify_result("t", content, is_error=False) is False

    def test_exit_code_string_nonzero(self):
        content = json.dumps({"exit_code": "1"})
        assert classify_result("t", content, is_error=False) is True

    def test_json_list_not_empty(self):
        content = json.dumps([1, 2, 3])
        assert classify_result("t", content, is_error=False) is False

    def test_non_json_text(self):
        assert classify_result("t", "just a string", is_error=False) is False

    def test_none_content(self):
        assert classify_result("t", None, is_error=False) is True

    def test_json_status_ok(self):
        content = json.dumps({"status": "ok"})
        assert classify_result("t", content, is_error=False) is False


# ── CircuitBreakerState ────────────────────────────────────────


class TestCircuitBreakerState:
    def test_streak_increments(self):
        s = CircuitBreakerState()
        s.record("tool_a", True)
        s.record("tool_a", True)
        s.record("tool_a", True)
        assert s.get_consecutive("tool_a") == 3

    def test_streak_resets_on_success(self):
        s = CircuitBreakerState()
        s.record("tool_a", True)
        s.record("tool_a", True)
        s.record("tool_a", False)
        assert s.get_consecutive("tool_a") == 0

    def test_interleave_independent(self):
        s = CircuitBreakerState()
        s.record("tool_a", True)
        s.record("tool_b", False)
        s.record("tool_a", True)
        s.record("tool_b", True)
        s.record("tool_a", True)
        assert s.get_consecutive("tool_a") == 3
        assert s.get_consecutive("tool_b") == 1

    def test_trip_at_threshold(self):
        s = CircuitBreakerState()
        for _ in range(3):
            s.record("t", True)
        tripped, count = s.check("t", threshold=3, max_trips=2, exempt_tools=[])
        assert tripped is True
        assert count == 3

    def test_no_trip_below_threshold(self):
        s = CircuitBreakerState()
        s.record("t", True)
        s.record("t", True)
        tripped, _ = s.check("t", threshold=3, max_trips=2, exempt_tools=[])
        assert tripped is False

    def test_trip_at_multiples(self):
        s = CircuitBreakerState()
        for _ in range(6):
            s.record("t", True)
        tripped, count = s.check("t", threshold=3, max_trips=5, exempt_tools=[])
        assert tripped is True
        assert count == 6

    def test_no_trip_between_multiples(self):
        s = CircuitBreakerState()
        # Trip at 3
        for _ in range(3):
            s.record("t", True)
        s.check("t", threshold=3, max_trips=5, exempt_tools=[])
        # At 4 — not a multiple of 3
        s.record("t", True)
        tripped, _ = s.check("t", threshold=3, max_trips=5, exempt_tools=[])
        assert tripped is False

    def test_max_trips_cap(self):
        s = CircuitBreakerState()
        for _ in range(3):
            s.record("t", True)
        s.check("t", threshold=3, max_trips=1, exempt_tools=[])
        # Second multiple — should be capped
        for _ in range(3):
            s.record("t", True)
        tripped, _ = s.check("t", threshold=3, max_trips=1, exempt_tools=[])
        assert tripped is False

    def test_exempt_tool(self):
        s = CircuitBreakerState()
        for _ in range(5):
            s.record("poll_status", True)
        tripped, _ = s.check(
            "poll_status",
            threshold=3,
            max_trips=2,
            exempt_tools=["poll_status"],
        )
        assert tripped is False

    def test_threshold_zero(self):
        s = CircuitBreakerState()
        s.record("t", True)
        tripped, _ = s.check("t", threshold=0, max_trips=2, exempt_tools=[])
        assert tripped is False

    def test_unknown_tool_returns_zero(self):
        s = CircuitBreakerState()
        assert s.get_consecutive("unknown") == 0

    def test_trips_property(self):
        s = CircuitBreakerState()
        for _ in range(3):
            s.record("t", True)
        s.check("t", threshold=3, max_trips=2, exempt_tools=[])
        assert s.trips.get("t") == 1


# ── Config merging ─────────────────────────────────────────────


class TestConfigMerging:
    def test_from_config_defaults(self):
        result = circuit_breaker_from_config({})
        assert result["enabled"] is True
        assert result["threshold"] == 3
        assert result["max_trips_per_tool"] == 2
        assert result["exempt_tools"] == []

    def test_from_config_partial(self):
        result = circuit_breaker_from_config(
            {
                "circuit_breaker": {"threshold": 5},
            }
        )
        assert result["threshold"] == 5
        assert result["enabled"] is True

    def test_from_config_full(self):
        result = circuit_breaker_from_config(
            {
                "circuit_breaker": {
                    "enabled": False,
                    "threshold": 10,
                    "max_trips_per_tool": 5,
                    "exempt_tools": ["poll"],
                },
            }
        )
        assert result["enabled"] is False
        assert result["threshold"] == 10
        assert result["max_trips_per_tool"] == 5
        assert result["exempt_tools"] == ["poll"]

    def test_from_custom_fields_override(self):
        config_cb = circuit_breaker_from_config({})
        result = circuit_breaker_from_custom_fields(
            {"circuit_breaker": {"threshold": 7, "exempt_tools": ["x"]}},
            config_cb,
        )
        assert result["threshold"] == 7
        assert result["exempt_tools"] == ["x"]
        assert result["enabled"] is True

    def test_from_custom_fields_empty(self):
        config_cb = circuit_breaker_from_config(
            {
                "circuit_breaker": {"threshold": 4},
            }
        )
        result = circuit_breaker_from_custom_fields({}, config_cb)
        assert result["threshold"] == 4

    def test_from_custom_fields_disable(self):
        config_cb = circuit_breaker_from_config({})
        result = circuit_breaker_from_custom_fields(
            {"circuit_breaker": {"enabled": False}},
            config_cb,
        )
        assert result["enabled"] is False

    def test_string_threshold_coerced(self):
        result = circuit_breaker_from_config(
            {"circuit_breaker": {"threshold": "5"}},
        )
        assert result["threshold"] == 5
        assert isinstance(result["threshold"], int)

    def test_null_threshold_defaults(self):
        result = circuit_breaker_from_config(
            {"circuit_breaker": {"threshold": None}},
        )
        assert result["threshold"] == 3

    def test_null_exempt_tools_defaults(self):
        result = circuit_breaker_from_config(
            {"circuit_breaker": {"exempt_tools": None}},
        )
        assert result["exempt_tools"] == []

    def test_string_max_trips_coerced(self):
        result = circuit_breaker_from_config(
            {"circuit_breaker": {"max_trips_per_tool": "4"}},
        )
        assert result["max_trips_per_tool"] == 4

    def test_custom_fields_string_threshold(self):
        config_cb = circuit_breaker_from_config({})
        result = circuit_breaker_from_custom_fields(
            {"circuit_breaker": {"threshold": "7"}},
            config_cb,
        )
        assert result["threshold"] == 7


# ── Integration with agent loop ────────────────────────────────

from agents.base import AgentBase
from providers.events import EventBus
from providers.llm.base import LLMProvider, LLMResponse, ToolDefinition


class _StubAgent(AgentBase):
    def _system_prompt(self, ticket: dict[str, Any]) -> str:
        return "test"

    def _build_messages(self, ticket: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"role": "user", "content": "test"}]

    async def _handle_completion(
        self,
        ticket_id: str,
        response: LLMResponse,
    ) -> None:
        pass


class _FailingToolLLM(LLMProvider):
    """LLM that calls a named tool every iteration."""

    def __init__(self, tool_name: str = "retrieve_results") -> None:
        self.call_count = 0
        self.tool_name = tool_name

    async def complete(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[ToolDefinition] | None = None,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        from providers.llm.base import ToolCall

        self.call_count += 1
        return LLMResponse(
            text=None,
            tool_calls=[
                ToolCall(
                    id=f"tc_{self.call_count}",
                    name=self.tool_name,
                    input={"pattern": "*.csv"},
                ),
            ],
            stop_reason="tool_use",
            raw_content=[
                {
                    "type": "tool_use",
                    "id": f"tc_{self.call_count}",
                    "name": self.tool_name,
                    "input": {"pattern": "*.csv"},
                },
            ],
        )


def _mock_client(ticket_id, custom_fields=None):
    ticket_data = {
        "id": ticket_id,
        "status": "triage_pending",
        "summary": "test",
        "custom_fields": custom_fields or {},
    }
    client = AsyncMock()
    client.get = AsyncMock(
        return_value=AsyncMock(
            status_code=200,
            json=lambda: ticket_data,
            raise_for_status=lambda: None,
        ),
    )
    client.post = AsyncMock(
        return_value=AsyncMock(
            status_code=200,
            json=lambda: {},
            raise_for_status=lambda: None,
        ),
    )
    client.patch = AsyncMock(
        return_value=AsyncMock(
            status_code=200,
            json=lambda: {},
            raise_for_status=lambda: None,
        ),
    )
    return client


@pytest.mark.asyncio
async def test_system_message_injected_after_threshold(tmp_path):
    """After 3 consecutive failures, a [SYSTEM] message is appended."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    ticket_id = "PERF-CB"

    event_bus = EventBus(log_dir=log_dir)
    llm = _FailingToolLLM()
    agent = _StubAgent(
        agent_name="test-agent",
        llm_provider=llm,
        state_store_url="http://localhost:8090",
        event_bus=event_bus,
        max_iterations=5,
    )
    agent._client = _mock_client(ticket_id)

    await agent.run(ticket_id)

    jsonl = (log_dir / f"{ticket_id}.jsonl").read_text()
    cb_events = [
        json.loads(line)
        for line in jsonl.strip().split("\n")
        if json.loads(line).get("event_type") == "circuit_breaker"
    ]
    assert len(cb_events) >= 1
    assert cb_events[0]["data"]["tool"] == "retrieve_results"
    assert cb_events[0]["data"]["consecutive"] == 3


@pytest.mark.asyncio
async def test_system_message_content_in_conversation(tmp_path):
    """Verify the [SYSTEM] message actually reaches the LLM conversation."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    ticket_id = "PERF-CB-MSG"

    captured_messages: list[list[dict]] = []

    class _CapturingLLM(_FailingToolLLM):
        async def complete(self, system_prompt, messages, tools=None, max_tokens=4096):
            captured_messages.append(list(messages))
            return await super().complete(system_prompt, messages, tools, max_tokens)

    event_bus = EventBus(log_dir=log_dir)
    llm = _CapturingLLM()
    agent = _StubAgent(
        agent_name="test-agent",
        llm_provider=llm,
        state_store_url="http://localhost:8090",
        event_bus=event_bus,
        max_iterations=5,
    )
    agent._client = _mock_client(ticket_id)

    await agent.run(ticket_id)

    # After 3 failures (calls 1-3), the 4th LLM call should see the
    # circuit breaker message. captured_messages[0] is the initial call.
    assert len(captured_messages) >= 4
    fourth_call_msgs = captured_messages[3]
    cb_msgs = [
        m
        for m in fourth_call_msgs
        if isinstance(m.get("content"), str)
        and "[SYSTEM] Circuit breaker:" in m["content"]
    ]
    assert len(cb_msgs) == 1
    assert "retrieve_results" in cb_msgs[0]["content"]
    assert "<tool_output>" in cb_msgs[0]["content"]

    # First 3 calls should NOT have the circuit breaker message
    for i in range(3):
        for m in captured_messages[i]:
            if isinstance(m.get("content"), str):
                assert "[SYSTEM] Circuit breaker:" not in m["content"]


@pytest.mark.asyncio
async def test_max_trips_caps_injections(tmp_path):
    """max_trips_per_tool=1 limits to one injection per streak."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    ticket_id = "PERF-CB-CAP"

    event_bus = EventBus(log_dir=log_dir)
    llm = _FailingToolLLM()
    agent = _StubAgent(
        agent_name="test-agent",
        llm_provider=llm,
        state_store_url="http://localhost:8090",
        event_bus=event_bus,
        max_iterations=10,
    )
    agent._client = _mock_client(
        ticket_id,
        custom_fields={
            "circuit_breaker": {"max_trips_per_tool": 1},
        },
    )

    await agent.run(ticket_id)

    jsonl = (log_dir / f"{ticket_id}.jsonl").read_text()
    cb_events = [
        json.loads(line)
        for line in jsonl.strip().split("\n")
        if json.loads(line).get("event_type") == "circuit_breaker"
    ]
    assert len(cb_events) == 1


@pytest.mark.asyncio
async def test_disabled_no_injection(tmp_path):
    """enabled=false produces no circuit breaker events or messages."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    ticket_id = "PERF-CB-OFF"

    event_bus = EventBus(log_dir=log_dir)
    llm = _FailingToolLLM()
    agent = _StubAgent(
        agent_name="test-agent",
        llm_provider=llm,
        state_store_url="http://localhost:8090",
        event_bus=event_bus,
        max_iterations=5,
    )
    agent._client = _mock_client(
        ticket_id,
        custom_fields={
            "circuit_breaker": {"enabled": False},
        },
    )

    await agent.run(ticket_id)

    jsonl = (log_dir / f"{ticket_id}.jsonl").read_text()
    cb_events = [
        json.loads(line)
        for line in jsonl.strip().split("\n")
        if json.loads(line).get("event_type") == "circuit_breaker"
    ]
    assert len(cb_events) == 0


@pytest.mark.asyncio
async def test_exempt_tool_not_tripped(tmp_path):
    """Exempt tools never trigger circuit breaker events."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    ticket_id = "PERF-CB-EXEMPT"

    event_bus = EventBus(log_dir=log_dir)
    llm = _FailingToolLLM(tool_name="poll_status")
    agent = _StubAgent(
        agent_name="test-agent",
        llm_provider=llm,
        state_store_url="http://localhost:8090",
        event_bus=event_bus,
        max_iterations=5,
    )
    agent._client = _mock_client(
        ticket_id,
        custom_fields={
            "circuit_breaker": {"exempt_tools": ["poll_status"]},
        },
    )

    await agent.run(ticket_id)

    jsonl = (log_dir / f"{ticket_id}.jsonl").read_text()
    cb_events = [
        json.loads(line)
        for line in jsonl.strip().split("\n")
        if json.loads(line).get("event_type") == "circuit_breaker"
    ]
    assert len(cb_events) == 0


@pytest.mark.asyncio
async def test_no_files_found_payload_triggers(tmp_path):
    """The exact payload from issue #292 triggers the circuit breaker."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    ticket_id = "PERF-CB-292"

    no_files_payload = json.dumps(
        {
            "status": "no_files_found",
            "directory_listing": ["/data/results/run1", "/data/results/run2"],
        }
    )

    assert classify_result("retrieve_results", no_files_payload, False) is True

    event_bus = EventBus(log_dir=log_dir)

    class _NoFilesLLM(LLMProvider):
        def __init__(self):
            self.call_count = 0

        async def complete(self, system_prompt, messages, tools=None, max_tokens=4096):
            from providers.llm.base import ToolCall

            self.call_count += 1
            return LLMResponse(
                text=None,
                tool_calls=[
                    ToolCall(
                        id=f"tc_{self.call_count}",
                        name="retrieve_results",
                        input={},
                    ),
                ],
                stop_reason="tool_use",
                raw_content=[
                    {
                        "type": "tool_use",
                        "id": f"tc_{self.call_count}",
                        "name": "retrieve_results",
                        "input": {},
                    },
                ],
            )

    from agents.base import ToolResult
    from providers.llm.base import ToolDefinition as _TD

    class _NoFilesAgent(_StubAgent):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.tools = [
                _TD(
                    name="retrieve_results",
                    description="Retrieve results",
                    input_schema={"type": "object", "properties": {}},
                ),
            ]

        async def _execute_tool(self, tool_call):
            return ToolResult(
                tool_use_id=tool_call.id,
                content=no_files_payload,
                is_error=False,
            )

    llm = _NoFilesLLM()
    agent = _NoFilesAgent(
        agent_name="test-agent",
        llm_provider=llm,
        state_store_url="http://localhost:8090",
        event_bus=event_bus,
        max_iterations=5,
    )
    agent._client = _mock_client(ticket_id)

    await agent.run(ticket_id)

    jsonl = (log_dir / f"{ticket_id}.jsonl").read_text()
    cb_events = [
        json.loads(line)
        for line in jsonl.strip().split("\n")
        if json.loads(line).get("event_type") == "circuit_breaker"
    ]
    assert len(cb_events) >= 1
    assert cb_events[0]["data"]["tool"] == "retrieve_results"
