"""Tests for the analysis agent and analyzing status."""

from __future__ import annotations

import json

import pytest

from agents.analyze.agent import AnalyzeAgent
from state_store.models import (
    VALID_TRANSITIONS,
    CreateTicketRequest,
    TicketStatus,
    TransitionRequest,
)
from state_store.store import TicketStore


@pytest.mark.asyncio
async def test_analyze_harness_alias_search_uses_shared_aliases(monkeypatch):
    tickets = [
        {
            "id": "PERF-ALIAS",
            "status": "closed",
            "custom_fields": {"directives": {"harness": "Arcaflow"}},
        },
        {
            "id": "PERF-CANONICAL",
            "status": "closed",
            "custom_fields": {
                "directives": {"harness": "arcaflow-plugins"},
            },
        },
        {
            "id": "PERF-OTHER",
            "status": "closed",
            "custom_fields": {"directives": {"harness": "uperf"}},
        },
    ]

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return tickets

    class _HTTPClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return _Response()

    monkeypatch.setattr("providers.execution.AuditedAsyncHTTPClient", _HTTPClient)
    from agents.analyze.server import search_tickets

    alias_filter = json.loads(await search_tickets(harness="arcaflow"))
    canonical_filter = json.loads(await search_tickets(harness="arcaflow-plugins"))

    assert alias_filter["count"] == 2
    assert [t["ticket_id"] for t in alias_filter["tickets"]] == [
        "PERF-ALIAS",
        "PERF-CANONICAL",
    ]
    assert canonical_filter == alias_filter


def test_analyzing_status_exists():
    """The analyzing status is a valid ticket status."""
    assert hasattr(TicketStatus, "ANALYZING")
    assert TicketStatus.ANALYZING.value == "analyzing"


def test_analyzing_transitions():
    """Analyzing can transition to review, hardware, or guidance."""
    allowed = VALID_TRANSITIONS[TicketStatus.ANALYZING]
    assert TicketStatus.AWAITING_REVIEW in allowed
    assert TicketStatus.AWAITING_HARDWARE in allowed
    assert TicketStatus.AWAITING_CUSTOMER_GUIDANCE in allowed


@pytest.mark.asyncio
async def test_analyze_request_clarification_pauses_until_user_reply(monkeypatch):
    agent = AnalyzeAgent(llm_provider=None, state_store_url="http://unused")
    agent._ticket_id = "PERF-ANALYZE-HITL"
    agent._HITL_POLL_INTERVAL = 0
    assert "request_clarification" in {tool.name for tool in agent.tools}

    tickets = iter(
        [
            {"status": "analyzing", "comments": [], "custom_fields": {}},
            {
                "status": "analyzing",
                "comments": [
                    {"author": "user", "body": "Use the installed controller docs."}
                ],
                "custom_fields": {},
            },
        ]
    )
    transitions = []

    async def get_ticket(_ticket_id):
        return next(tickets)

    async def add_comment(_ticket_id, body):
        assert "conflict" in body

    async def transition_ticket(_ticket_id, status, **_kwargs):
        transitions.append(status)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(agent, "_get_ticket", get_ticket)
    monkeypatch.setattr(agent, "_add_comment", add_comment)
    monkeypatch.setattr(agent, "_transition_ticket", transition_ticket)
    monkeypatch.setattr("agents.base.asyncio.sleep", no_sleep)

    reply = await agent._tool_handlers["request_clarification"](
        question="The context sources conflict. Which source should guide this analysis?"
    )
    assert reply == "Use the installed controller docs."
    assert transitions == ["awaiting_customer_guidance"]
    await agent.close()


@pytest.mark.asyncio
async def test_analyze_mcp_tool_merge_reuses_stable_native_tools():
    from providers.llm.base import ToolDefinition

    agent = AnalyzeAgent(llm_provider=None, state_store_url="http://unused")
    remote_tool = ToolDefinition(
        name="get_skill_context",
        description="Retrieve context.",
        input_schema={"type": "object"},
    )

    agent._set_mcp_tools([remote_tool])
    first_names = [tool.name for tool in agent.tools]
    agent._set_mcp_tools([remote_tool])
    second_names = [tool.name for tool in agent.tools]

    assert first_names == second_names
    assert first_names.count("request_clarification") == 1
    assert first_names.count("get_skill_context") == 1
    await agent.close()


@pytest.mark.asyncio
async def test_analyze_clarification_precedes_mixed_submit_and_tools(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from agents.base import AgentBase
    from providers.llm.base import LLMResponse, ToolCall, ToolDefinition
    from providers.tracing import ActionType, LifecycleState, TraceRecorder

    ticket_id = "PERF-ANALYZE-MIXED-HITL"
    agent = AnalyzeAgent(llm_provider=None, state_store_url="http://unused")
    # Exercise AgentBase's dispatch loop directly while preserving the ticket
    # context normally set by AnalyzeAgent.run before it connects to MCP.
    agent._ticket_id = ticket_id
    agent.max_iterations = 3
    agent._tool_min_interval = 0
    agent._client = SimpleNamespace(headers={}, aclose=AsyncMock())
    agent.tools.extend(
        [
            ToolDefinition(
                name="other_probe",
                description="Probe that must wait for user guidance.",
                input_schema={"type": "object"},
            ),
            ToolDefinition(
                name="submit_analysis_result",
                description="Submit analysis.",
                input_schema={"type": "object"},
            ),
        ]
    )
    ticket = {
        "id": ticket_id,
        "summary": "Context conflict test",
        "description": "Investigate the result.",
        "status": "analyzing",
        "custom_fields": {"global_max_iterations_override": 3},
    }
    hitl_started = asyncio.Event()
    user_replied = asyncio.Event()
    completion_calls = []
    trace_events = []
    transitions = []
    other_calls = []
    llm_messages = []

    first_turn = LLMResponse(
        text=None,
        tool_calls=[
            ToolCall(
                id="submit-same-turn",
                name="submit_analysis_result",
                input={"conclusive": False, "finding": "premature"},
            ),
            ToolCall(
                id="other-same-turn",
                name="other_probe",
                input={},
            ),
            ToolCall(
                id="clarify-same-turn",
                name="request_clarification",
                input={"question": "Which conflicting source should guide this?"},
            ),
        ],
        stop_reason="tool_use",
        raw_content=[],
    )
    after_guidance = LLMResponse(
        text=None,
        tool_calls=[
            ToolCall(
                id="submit-after-reply",
                name="submit_analysis_result",
                input={"conclusive": False, "finding": "guided"},
            )
        ],
        stop_reason="tool_use",
        raw_content=[],
    )
    responses = [first_turn, after_guidance]

    class _LLM:
        async def complete(self, **kwargs):
            llm_messages.append(kwargs["messages"])
            return responses.pop(0)

    agent.llm = _LLM()
    agent._trace = TraceRecorder(client=SimpleNamespace(record=trace_events.append))
    agent._get_ticket = AsyncMock(return_value=ticket)
    agent._check_interject = AsyncMock(return_value=None)
    agent._check_drift = lambda: None
    agent._get_previous_iteration_counts = lambda _ticket_id: (0, 0)
    agent._tool_handlers["other_probe"] = AsyncMock(side_effect=other_calls.append)

    async def request_human_input(_ticket_id, _question):
        hitl_started.set()
        await user_replied.wait()
        return "Use the installed controller evidence."

    async def update_fields(_ticket_id, _fields):
        assert user_replied.is_set()
        completion_calls.append("analysis_result")

    async def add_comment(_ticket_id, _comment):
        assert user_replied.is_set()

    async def plan_controls_next_transition(_ticket_id):
        return False

    async def transition(_ticket_id, status, **_kwargs):
        assert user_replied.is_set()
        transitions.append(status)

    agent._request_human_input = request_human_input
    agent._update_fields = update_fields
    agent._add_comment = add_comment
    agent._plan_controls_next_transition = plan_controls_next_transition
    agent._transition_ticket = transition

    class _Workspace:
        def __init__(self, *, ticket_id, agent_name):
            assert ticket_id == ticket_id_arg
            assert agent_name == "analyze-agent"

        def list_effective_files(self):
            return []

        def read_effective_context(self):
            return ""

    ticket_id_arg = ticket_id
    monkeypatch.setattr("providers.workspace.manager.WorkspaceManager", _Workspace)

    run_task = asyncio.create_task(AgentBase.run(agent, ticket_id))
    await asyncio.wait_for(hitl_started.wait(), timeout=5)
    assert completion_calls == []
    assert transitions == []
    assert other_calls == []

    user_replied.set()
    await asyncio.wait_for(run_task, timeout=5)
    await agent.close()

    assert completion_calls == ["analysis_result"]
    assert transitions == ["awaiting_hardware"]
    assert other_calls == []
    result_messages = [
        item
        for message in llm_messages[1]
        if message["role"] == "user" and isinstance(message["content"], list)
        for item in message["content"]
    ]
    results_by_call_id = {item["tool_use_id"]: item for item in result_messages}
    assert (
        "request_clarification takes precedence"
        in results_by_call_id["submit-same-turn"]["content"]
    )
    assert (
        "request_clarification takes precedence"
        in results_by_call_id["other-same-turn"]["content"]
    )

    tool_events = [
        event for event in trace_events if event.action.type == ActionType.TOOL
    ]
    states_by_call_id = {}
    for event in tool_events:
        states_by_call_id.setdefault(event.tool_call_id, []).append(
            event.lifecycle.state
        )
    assert states_by_call_id["clarify-same-turn"] == [
        LifecycleState.PROPOSED,
        LifecycleState.STARTED,
        LifecycleState.COMPLETED,
    ]
    assert states_by_call_id["submit-same-turn"] == [
        LifecycleState.PROPOSED,
        LifecycleState.SHORT_CIRCUITED,
    ]
    assert states_by_call_id["other-same-turn"] == [
        LifecycleState.PROPOSED,
        LifecycleState.SHORT_CIRCUITED,
    ]


def test_triage_can_transition_to_analyzing():
    """Triage can route directly to analyzing."""
    allowed = VALID_TRANSITIONS[TicketStatus.TRIAGE_PENDING]
    assert TicketStatus.ANALYZING in allowed


def test_gathering_context_can_transition_to_analyzing():
    """Gathering context (webhook path) can route to analyzing."""
    allowed = VALID_TRANSITIONS[TicketStatus.GATHERING_CONTEXT]
    assert TicketStatus.ANALYZING in allowed


def test_store_transition_to_analyzing(tmp_path):
    """Ticket can transition from triage_pending to analyzing."""
    store = TicketStore(persist_dir=tmp_path)
    ticket = store.create_ticket(
        CreateTicketRequest(summary="Test analysis", description="test"),
    )
    tid = ticket.id

    store.transition_ticket(
        tid,
        TransitionRequest(status=TicketStatus.TRIAGE_PENDING),
    )
    ticket = store.transition_ticket(
        tid,
        TransitionRequest(status=TicketStatus.ANALYZING),
    )
    assert ticket.status == TicketStatus.ANALYZING


def test_analyzing_to_review_conclusive(tmp_path):
    """Conclusive analysis transitions to awaiting_review."""
    store = TicketStore(persist_dir=tmp_path)
    ticket = store.create_ticket(
        CreateTicketRequest(summary="Test analysis", description="test"),
    )
    tid = ticket.id

    store.transition_ticket(
        tid,
        TransitionRequest(status=TicketStatus.TRIAGE_PENDING),
    )
    store.transition_ticket(
        tid,
        TransitionRequest(status=TicketStatus.ANALYZING),
    )
    ticket = store.transition_ticket(
        tid,
        TransitionRequest(
            status=TicketStatus.AWAITING_REVIEW,
            comment="Analysis conclusive",
        ),
    )
    assert ticket.status == TicketStatus.AWAITING_REVIEW


def test_analyzing_to_hardware_inconclusive(tmp_path):
    """Inconclusive analysis transitions to awaiting_hardware."""
    store = TicketStore(persist_dir=tmp_path)
    ticket = store.create_ticket(
        CreateTicketRequest(summary="Test analysis", description="test"),
    )
    tid = ticket.id

    store.transition_ticket(
        tid,
        TransitionRequest(status=TicketStatus.TRIAGE_PENDING),
    )
    store.transition_ticket(
        tid,
        TransitionRequest(status=TicketStatus.ANALYZING),
    )
    ticket = store.transition_ticket(
        tid,
        TransitionRequest(
            status=TicketStatus.AWAITING_HARDWARE,
            comment="Analysis inconclusive, need benchmark",
        ),
    )
    assert ticket.status == TicketStatus.AWAITING_HARDWARE


def test_plan_agent_status_includes_analyze():
    """The PLAN_AGENT_STATUS map includes analyze → analyzing."""
    pytest = __import__("pytest")
    try:
        from orchestrator.main import PLAN_AGENT_STATUS
    except ImportError:
        pytest.skip("orchestrator imports unavailable")

    assert PLAN_AGENT_STATUS["analyze"] == "analyzing"


def test_dispatcher_maps_analyzing():
    """STATUS_AGENT_MAP maps analyzing to the analyze agent type."""
    pytest = __import__("pytest")
    try:
        from orchestrator.dispatcher import STATUS_AGENT_MAP
    except ImportError:
        pytest.skip("orchestrator imports unavailable")

    assert STATUS_AGENT_MAP["analyzing"] == "analyze"


def test_loop_analyze_blocked_without_prior_analysis():
    """loop_analyze guard blocks when ticket has no analysis_result."""
    allowed = VALID_TRANSITIONS[TicketStatus.EVALUATING_CONVERGENCE]
    # The state machine allows the transition
    assert TicketStatus.ANALYZING in allowed

    # But the evaluate agent's code guard should block it
    # when there's no analysis_result. We test the state machine
    # allows it (the guard is in agent code, not the state machine).
    # The agent-level guard is tested via the evaluate agent tests.


def test_analyzing_not_in_non_dispatchable():
    """analyzing is a dispatchable status (not terminal or paused)."""
    from state_store.models import (
        NON_DISPATCHABLE_STATUSES,
    )

    assert TicketStatus.ANALYZING not in NON_DISPATCHABLE_STATUSES
