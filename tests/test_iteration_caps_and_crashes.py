"""Tests for cumulative agent iterations, global iteration caps, override semantics, and EventBus budget state recovery."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agents.base import AgentBase
from providers.events import EventBus
from providers.llm.base import LLMProvider, LLMResponse, ToolDefinition


class _StubAgent(AgentBase):
    def _system_prompt(self, ticket: dict[str, Any]) -> str:
        return "test"

    def _build_messages(self, ticket: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"role": "user", "content": "test"}]

    async def _handle_completion(self, ticket_id: str, response: LLMResponse) -> None:
        pass


class _InfiniteToolLLM(LLMProvider):
    def __init__(self) -> None:
        self.call_count = 0

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
                    name="some_tool",
                    input={},
                ),
            ],
            stop_reason="tool_use",
            raw_content=[
                {
                    "type": "tool_use",
                    "id": f"tc_{self.call_count}",
                    "name": "some_tool",
                    "input": {},
                },
            ],
        )


@pytest.mark.asyncio
async def test_cumulative_iteration_limit_respected(tmp_path):
    """Verifies that past agent iterations are counted and subtracted from the agent's phase budget."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    ticket_id = "PERF-TEST"

    # Pre-write 3 llm_request events for this agent
    jsonl_path = log_dir / f"{ticket_id}.jsonl"
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for i in range(3):
            f.write(
                json.dumps(
                    {
                        "seq": i + 1,
                        "agent": "test-agent",
                        "event_type": "llm_request",
                        "data": {"iteration": i},
                    }
                )
                + "\n"
            )

    event_bus = EventBus(log_dir=log_dir)
    llm = _InfiniteToolLLM()
    agent = _StubAgent(
        agent_name="test-agent",
        llm_provider=llm,
        state_store_url="http://localhost:8090",
        event_bus=event_bus,
        max_iterations=5,  # 3 already consumed, only 2 remaining!
    )

    agent._client = AsyncMock()
    agent._client.get = AsyncMock(
        return_value=AsyncMock(
            status_code=200,
            json=lambda: {
                "id": ticket_id,
                "status": "triage_pending",
                "summary": "test",
                "custom_fields": {},
            },
            raise_for_status=lambda: None,
        ),
    )
    agent._client.post = AsyncMock(
        return_value=AsyncMock(
            status_code=200,
            json=lambda: {},
            raise_for_status=lambda: None,
        ),
    )

    await agent.run(ticket_id)

    # Respects budget: should run 2 iterations + 1 grace iteration = 3 calls
    assert llm.call_count == 3


@pytest.mark.asyncio
async def test_global_iteration_limit_respected(tmp_path):
    """Verifies that the global iteration limit halts execution across all agents."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    ticket_id = "PERF-TEST"

    # Pre-write 98 llm_request events across other agents
    jsonl_path = log_dir / f"{ticket_id}.jsonl"
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for i in range(98):
            f.write(
                json.dumps(
                    {
                        "seq": i + 1,
                        "agent": "some-other-agent",
                        "event_type": "llm_request",
                        "data": {"iteration": i},
                    }
                )
                + "\n"
            )

    event_bus = EventBus(log_dir=log_dir)
    llm = _InfiniteToolLLM()
    agent = _StubAgent(
        agent_name="test-agent",
        llm_provider=llm,
        state_store_url="http://localhost:8090",
        event_bus=event_bus,
        max_iterations=10,  # 98 consumed globally, global limit 100 -> only 2 remaining!
    )

    # Mock client and ticket fields
    agent._client = AsyncMock()
    ticket_data = {
        "id": ticket_id,
        "status": "triage_pending",
        "summary": "test",
        "custom_fields": {
            "global_max_iterations_override": 100,
        },
    }
    agent._client.get = AsyncMock(
        return_value=AsyncMock(
            status_code=200,
            json=lambda: ticket_data,
            raise_for_status=lambda: None,
        ),
    )

    posted_comments = []
    transitions = []

    async def mock_post(url, **kwargs):
        data = kwargs.get("json", {})
        if "comments" in url:
            posted_comments.append(data.get("body", ""))
        elif "transition" in url:
            transitions.append(data.get("status", ""))
        return AsyncMock(
            status_code=200, json=lambda: {}, raise_for_status=lambda: None
        )

    agent._client.post = mock_post
    agent._client.patch = AsyncMock(
        return_value=AsyncMock(
            status_code=200, json=lambda: {}, raise_for_status=lambda: None
        )
    )

    await agent.run(ticket_id)

    # 2 remaining global iterations -> 2 calls before halting
    assert llm.call_count == 2

    # Verify transitions & comments
    assert any("reached global maximum iteration limit" in c for c in posted_comments)
    assert "awaiting_customer_guidance" in transitions


def test_event_bus_loads_cumulative_from_file(tmp_path):
    """Verifies that EventBus reconstructs LLM usage statistics from previous log entries."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    ticket_id = "PERF-BUDGET-RECOVERY"

    # Pre-write llm_usage events from previous runs
    jsonl_path = log_dir / f"{ticket_id}.jsonl"
    with open(jsonl_path, "w", encoding="utf-8") as f:
        # First event: test-agent usage
        f.write(
            json.dumps(
                {
                    "seq": 1,
                    "agent": "test-agent",
                    "event_type": "llm_usage",
                    "data": {
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "duration_ms": 1000,
                        "model": "model-a",
                    },
                }
            )
            + "\n"
        )
        # Second event: other-agent usage
        f.write(
            json.dumps(
                {
                    "seq": 2,
                    "agent": "other-agent",
                    "event_type": "llm_usage",
                    "data": {
                        "input_tokens": 200,
                        "output_tokens": 80,
                        "duration_ms": 2000,
                        "model": "model-b",
                    },
                }
            )
            + "\n"
        )

    event_bus = EventBus(log_dir=log_dir)

    # Verify cumulative overall usage
    overall = event_bus.get_cumulative_usage(ticket_id)
    assert overall["input_tokens"] == 300
    assert overall["output_tokens"] == 130
    assert overall["llm_calls"] == 2

    # Verify per-agent usage breakdown
    agent_usage = event_bus.get_agent_usage(ticket_id)
    assert "test-agent" in agent_usage
    assert agent_usage["test-agent"]["input_tokens"] == 100
    assert agent_usage["test-agent"]["output_tokens"] == 50

    assert "other-agent" in agent_usage
    assert agent_usage["other-agent"]["input_tokens"] == 200
    assert agent_usage["other-agent"]["output_tokens"] == 80


# --- Override additive semantics ---


def _prewrite_events(path, agent_name, count):
    """Write count llm_request events to a JSONL log file."""
    with open(path, "w", encoding="utf-8") as f:
        for i in range(count):
            f.write(
                json.dumps(
                    {
                        "seq": i + 1,
                        "agent": agent_name,
                        "event_type": "llm_request",
                        "data": {"iteration": i},
                    }
                )
                + "\n"
            )


def _make_agent(llm, event_bus, max_iterations=5):
    agent = _StubAgent(
        agent_name="test-agent",
        llm_provider=llm,
        state_store_url="http://localhost:8090",
        event_bus=event_bus,
        max_iterations=max_iterations,
    )
    agent._iteration_agent_type = "test"
    return agent


def _mock_client(ticket_id, custom_fields=None):
    """Return a mock httpx client wired to a ticket."""
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
async def test_durable_grant_is_not_recharged_on_redispatch(tmp_path):
    """A cumulative cap grants only its unused allowance on every dispatch."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    ticket_id = "PERF-OVERRIDE"
    _prewrite_events(log_dir / f"{ticket_id}.jsonl", "test-agent", 20)

    event_bus = EventBus(log_dir=log_dir)
    llm = _InfiniteToolLLM()
    agent = _make_agent(llm, event_bus, max_iterations=20)
    agent._client = _mock_client(
        ticket_id,
        {"iteration_budget": {"agent_limits": {"test": 30}}},
    )

    await agent.run(ticket_id)

    # 10 new + 1 grace = 11 LLM calls
    assert llm.call_count == 11
    # A pause/resume without another explicit grant cannot renew these calls.
    await agent.run(ticket_id)
    assert llm.call_count == 11
    assert agent.max_iterations == 20


@pytest.mark.asyncio
async def test_durable_cap_without_prior_iterations(tmp_path):
    """An absolute per-agent cap is respected on a fresh ticket."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    ticket_id = "PERF-FRESH"

    event_bus = EventBus(log_dir=log_dir)
    llm = _InfiniteToolLLM()
    agent = _make_agent(llm, event_bus, max_iterations=2)
    agent._client = _mock_client(
        ticket_id,
        {"iteration_budget": {"agent_limits": {"test": 5}}},
    )

    await agent.run(ticket_id)

    # No prior iterations, so 5 + 1 grace = 6 calls
    assert llm.call_count == 6


@pytest.mark.asyncio
async def test_non_override_retains_lifetime_semantics(tmp_path):
    """Without the override flag, max_iterations is still a lifetime total."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    ticket_id = "PERF-LIFETIME"
    _prewrite_events(log_dir / f"{ticket_id}.jsonl", "test-agent", 3)

    event_bus = EventBus(log_dir=log_dir)
    llm = _InfiniteToolLLM()
    agent = _make_agent(llm, event_bus, max_iterations=5)
    agent._client = _mock_client(ticket_id)

    await agent.run(ticket_id)

    # Lifetime: 5 total - 3 prior = 2 remaining + 1 grace = 3 calls
    assert llm.call_count == 3


@pytest.mark.asyncio
async def test_exhausted_global_budget_has_no_phantom_iterations(tmp_path):
    """Granting three more calls after exhaustion permits exactly three calls."""
    from providers.tracing import ActionType, LifecycleState, TraceRecorder

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    ticket_id = "PERF-GLOBAL-GRANT"
    _prewrite_events(log_dir / f"{ticket_id}.jsonl", "test-agent", 20)
    event_bus = EventBus(log_dir=log_dir)
    llm = _InfiniteToolLLM()
    agent = _make_agent(llm, event_bus, max_iterations=0)
    fields = {"global_max_iterations_override": 20}
    agent._client = _mock_client(ticket_id, fields)
    recorded = []

    class Sink:
        def record(self, event):
            recorded.append(event)

    agent._trace = TraceRecorder(client=Sink())
    try:
        await agent.run(ticket_id)
        assert llm.call_count == 0
        assert agent._get_previous_iteration_counts(ticket_id) == (20, 20)
        assert not [event for event in recorded if event.action.type == ActionType.LLM]

        fields["global_max_iterations_override"] = 23
        await agent.run(ticket_id)
        assert llm.call_count == 3
        assert agent._get_previous_iteration_counts(ticket_id) == (23, 23)
        llm_events = [
            event for event in recorded if event.action.type == ActionType.LLM
        ]
        started = [
            event
            for event in llm_events
            if event.lifecycle.state == LifecycleState.STARTED
        ]
        terminal = [
            event
            for event in llm_events
            if event.lifecycle.state != LifecycleState.STARTED
        ]
        assert len(started) == len(terminal) == 3
        assert {event.action_id for event in started} == {
            event.action_id for event in terminal
        }
    finally:
        event_bus.close()
        await agent.close()


@pytest.mark.asyncio
async def test_inline_human_reply_observes_new_agent_and_global_allowances(tmp_path):
    """A grant at the old limit applies while this same invocation is waiting."""
    from providers.llm.base import ToolResult

    event_bus = EventBus(log_dir=tmp_path / "logs")
    llm = _InfiniteToolLLM()
    agent = _make_agent(llm, event_bus, max_iterations=2)
    fields = {"global_max_iterations_override": 3}
    agent._client = _mock_client("PERF-INLINE", fields)
    ticket = agent._client.get.return_value.json()
    agent._HITL_POLL_INTERVAL = 0.001
    granted = False

    async def user_replies(ticket_id, status, comment=None):
        nonlocal granted
        if not granted:
            granted = True
            fields["iteration_budget"] = {"agent_limits": {"test": 5}}
            fields["global_max_iterations_override"] = 6
            ticket["comments"] = [
                {"author": "user", "body": "Continue with three more iterations."}
            ]

    async def tool_call(call, **kwargs):
        if llm.call_count == 3:
            assert agent._wrapup_reason == "iteration"
            await agent._request_human_input("PERF-INLINE", "Continue?")
            assert agent.max_iterations == 5
            assert agent._global_iteration_limit == 6
            assert agent._wrapup_reason is None
        return ToolResult(tool_use_id=call.id, content="ok")

    agent._transition_ticket = AsyncMock(side_effect=user_replies)
    agent._add_comment = AsyncMock()
    agent._execute_tool = AsyncMock(side_effect=tool_call)
    try:
        await agent.run("PERF-INLINE")
        assert granted
        assert llm.call_count == 6
        assert agent._get_previous_iteration_counts("PERF-INLINE") == (6, 6)
    finally:
        event_bus.close()
        await agent.close()


@pytest.mark.asyncio
async def test_resource_teardown_has_independent_allowance_and_shared_global_count(
    tmp_path,
):
    """Creation work cannot spend a later teardown phase's explicit allowance."""
    bus = EventBus(log_dir=tmp_path / "logs")
    ticket_id = "PERF-RESOURCE-PHASES"
    for _ in range(12):
        bus.emit(
            ticket_id,
            "resource-agent",
            "llm_request",
            {"agent_type": "resource_create"},
        )
    llm = _InfiniteToolLLM()
    agent = _StubAgent(
        agent_name="resource-agent",
        llm_provider=llm,
        state_store_url="http://localhost:8090",
        event_bus=bus,
        max_iterations=20,
    )
    agent._iteration_agent_type = "resource_teardown"
    agent._client = _mock_client(
        ticket_id,
        {
            "iteration_budget": {"agent_limits": {"resource_teardown": 30}},
            # The configured global allowance was32; +10 grants a cap42. It
            # permits exactly30 teardown calls after the12 creation calls.
            "global_max_iterations_override": 42,
        },
    )
    try:
        assert agent._get_previous_iteration_counts(ticket_id) == (0, 12)
        await agent.run(ticket_id)
        assert llm.call_count == 30
        assert agent._get_previous_iteration_counts(ticket_id) == (30, 42)
        requests = [
            event
            for event in bus.get_events(ticket_id, limit=100_000)
            if event["event_type"] == "llm_request"
        ]
        assert (
            len(
                [
                    event
                    for event in requests
                    if event["data"].get("agent_type") == "resource_teardown"
                ]
            )
            == 30
        )
    finally:
        bus.close()
        await agent.close()


@pytest.mark.parametrize("with_status_trail,expected", [(True, 0), (False, 12)])
@pytest.mark.asyncio
async def test_legacy_shared_agent_name_uses_phase_evidence_or_conservative_fallback(
    tmp_path, with_status_trail, expected
):
    bus = EventBus(log_dir=tmp_path / "logs")
    tid = "PERF-LEGACY-PHASE"
    if with_status_trail:
        bus.emit(
            tid,
            "user",
            "status_change",
            {"from": "triage_pending", "to": "awaiting_hardware"},
        )
    for _ in range(12):
        bus.emit(tid, "resource-agent", "llm_request", {})
    if with_status_trail:
        bus.emit(
            tid,
            "user",
            "status_change",
            {"from": "awaiting_hardware", "to": "awaiting_teardown"},
        )
    agent = _StubAgent(
        agent_name="resource-agent",
        llm_provider=_InfiniteToolLLM(),
        state_store_url="http://localhost:8090",
        event_bus=bus,
    )
    agent._iteration_agent_type = "resource_teardown"
    try:
        assert agent._get_previous_iteration_counts(tid) == (expected, 12)
    finally:
        bus.close()
        await agent.close()


@pytest.mark.parametrize("mode,expected", [("create", 12), ("teardown", 4)])
@pytest.mark.asyncio
async def test_direct_resource_agent_keeps_correct_budget_identity(
    tmp_path, mode, expected
):
    from agents.resource.agent import ResourceAgent

    bus = EventBus(log_dir=tmp_path / "logs")
    tid = "PERF-DIRECT-RESOURCE"
    for phase, count in (("resource_create", 12), ("resource_teardown", 4)):
        for _ in range(count):
            bus.emit(tid, "resource-agent", "llm_request", {"agent_type": phase})
    agent = ResourceAgent(
        llm_provider=_InfiniteToolLLM(),
        state_store_url="http://localhost:8090",
        mode=mode,
        event_bus=bus,
    )
    try:
        assert agent._iteration_agent_type == f"resource_{mode}"
        assert agent._get_previous_iteration_counts(tid) == (expected, 16)
    finally:
        bus.close()
        await agent.close()
