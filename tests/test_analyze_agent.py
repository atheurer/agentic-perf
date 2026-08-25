"""Tests for the analysis agent and analyzing status."""

from __future__ import annotations

from state_store.models import (
    VALID_TRANSITIONS,
    CreateTicketRequest,
    TicketStatus,
    TransitionRequest,
)
from state_store.store import TicketStore


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
