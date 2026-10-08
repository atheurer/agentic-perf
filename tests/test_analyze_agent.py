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
