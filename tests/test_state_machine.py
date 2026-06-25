"""Tests for the state machine transitions, including the recursive
investigation loop statuses added for RHIVOS 03A.
"""

from __future__ import annotations

from state_store.models import VALID_TRANSITIONS, TicketStatus

# --- New statuses exist ---


def test_new_statuses_in_enum():
    """All recursive loop statuses are defined."""
    assert TicketStatus.GATHERING_CONTEXT == "gathering_context"
    assert TicketStatus.PLANNING_INVESTIGATION == "planning_investigation"
    assert TicketStatus.EVALUATING_CONVERGENCE == "evaluating_convergence"
    assert TicketStatus.SYNTHESIZING_RESULTS == "synthesizing_results"


def test_new_statuses_in_transitions():
    """All new statuses have transition entries."""
    for status in (
        TicketStatus.GATHERING_CONTEXT,
        TicketStatus.PLANNING_INVESTIGATION,
        TicketStatus.EVALUATING_CONVERGENCE,
        TicketStatus.SYNTHESIZING_RESULTS,
    ):
        assert status in VALID_TRANSITIONS, (
            f"{status.value} missing from VALID_TRANSITIONS"
        )


# --- Investigation loop entry ---


def test_triage_can_enter_grounding():
    """Triage can route to grounding for investigation tickets."""
    allowed = VALID_TRANSITIONS[TicketStatus.TRIAGE_PENDING]
    assert TicketStatus.GATHERING_CONTEXT in allowed


def test_triage_can_still_enter_hardware():
    """Triage can still route to awaiting_hardware for ad-hoc tickets."""
    allowed = VALID_TRANSITIONS[TicketStatus.TRIAGE_PENDING]
    assert TicketStatus.AWAITING_HARDWARE in allowed


# --- Grounding transitions ---


def test_grounding_to_planning():
    """Grounding advances to planning when no existing record matches."""
    allowed = VALID_TRANSITIONS[TicketStatus.GATHERING_CONTEXT]
    assert TicketStatus.PLANNING_INVESTIGATION in allowed


def test_grounding_to_closed():
    """Grounding can close the ticket when a matching record is found."""
    allowed = VALID_TRANSITIONS[TicketStatus.GATHERING_CONTEXT]
    assert TicketStatus.CLOSED in allowed


def test_grounding_to_hitl():
    """Grounding can pause for human input."""
    allowed = VALID_TRANSITIONS[TicketStatus.GATHERING_CONTEXT]
    assert TicketStatus.AWAITING_CUSTOMER_GUIDANCE in allowed


# --- Planning transitions ---


def test_planning_to_provision():
    """Planning advances to provision when plan is agreed."""
    allowed = VALID_TRANSITIONS[TicketStatus.PLANNING_INVESTIGATION]
    assert TicketStatus.AWAITING_PROVISION in allowed


def test_planning_to_hardware():
    """Planning can request new resources."""
    allowed = VALID_TRANSITIONS[TicketStatus.PLANNING_INVESTIGATION]
    assert TicketStatus.AWAITING_HARDWARE in allowed


def test_planning_to_hitl():
    """Planning can pause for human input."""
    allowed = VALID_TRANSITIONS[TicketStatus.PLANNING_INVESTIGATION]
    assert TicketStatus.AWAITING_CUSTOMER_GUIDANCE in allowed


# --- Evaluate loop-back transitions ---


def test_evaluate_to_planning():
    """Evaluate can loop back to planning (refine params)."""
    allowed = VALID_TRANSITIONS[TicketStatus.EVALUATING_CONVERGENCE]
    assert TicketStatus.PLANNING_INVESTIGATION in allowed


def test_evaluate_to_provision():
    """Evaluate can loop back to provision (re-flash hardware)."""
    allowed = VALID_TRANSITIONS[TicketStatus.EVALUATING_CONVERGENCE]
    assert TicketStatus.AWAITING_PROVISION in allowed


def test_evaluate_to_synthesize():
    """Evaluate advances to synthesize on convergence."""
    allowed = VALID_TRANSITIONS[TicketStatus.EVALUATING_CONVERGENCE]
    assert TicketStatus.SYNTHESIZING_RESULTS in allowed


def test_evaluate_to_hitl():
    """Evaluate can pause for manual interruption."""
    allowed = VALID_TRANSITIONS[TicketStatus.EVALUATING_CONVERGENCE]
    assert TicketStatus.AWAITING_CUSTOMER_GUIDANCE in allowed


# --- Benchmark can enter evaluate ---


def test_benchmark_to_evaluate():
    """Benchmark can transition to evaluate for investigation tickets."""
    allowed = VALID_TRANSITIONS[TicketStatus.EXECUTING_BENCHMARK]
    assert TicketStatus.EVALUATING_CONVERGENCE in allowed


def test_benchmark_still_goes_to_review():
    """Benchmark can still go to review for ad-hoc tickets."""
    allowed = VALID_TRANSITIONS[TicketStatus.EXECUTING_BENCHMARK]
    assert TicketStatus.AWAITING_REVIEW in allowed


# --- Synthesize transitions ---


def test_synthesize_to_teardown():
    """Synthesize advances to teardown."""
    allowed = VALID_TRANSITIONS[TicketStatus.SYNTHESIZING_RESULTS]
    assert TicketStatus.AWAITING_TEARDOWN in allowed


def test_synthesize_to_hitl():
    """Synthesize can pause for human input."""
    allowed = VALID_TRANSITIONS[TicketStatus.SYNTHESIZING_RESULTS]
    assert TicketStatus.AWAITING_CUSTOMER_GUIDANCE in allowed


# --- Original pipeline unchanged ---


def test_original_linear_pipeline_intact():
    """The original linear pipeline transitions still work."""
    linear_path = [
        (TicketStatus.NEW, TicketStatus.TRIAGE_PENDING),
        (TicketStatus.TRIAGE_PENDING, TicketStatus.AWAITING_HARDWARE),
        (TicketStatus.AWAITING_HARDWARE, TicketStatus.AWAITING_PROVISION),
        (TicketStatus.AWAITING_PROVISION, TicketStatus.EXECUTING_BENCHMARK),
        (TicketStatus.EXECUTING_BENCHMARK, TicketStatus.AWAITING_REVIEW),
        (TicketStatus.AWAITING_REVIEW, TicketStatus.AWAITING_TEARDOWN),
        (TicketStatus.AWAITING_TEARDOWN, TicketStatus.CLOSED),
    ]
    for from_status, to_status in linear_path:
        allowed = VALID_TRANSITIONS[from_status]
        assert to_status in allowed, (
            f"Linear transition {from_status.value} → {to_status.value} is broken"
        )


def test_rerun_loop_intact():
    """The existing rerun loop still works."""
    allowed = VALID_TRANSITIONS[TicketStatus.AWAITING_REVIEW]
    assert TicketStatus.TRIAGE_PENDING in allowed


# --- Full investigation loop path ---


def test_full_investigation_loop_path():
    """The complete investigation path is traversable."""
    path = [
        (TicketStatus.TRIAGE_PENDING, TicketStatus.GATHERING_CONTEXT),
        (TicketStatus.GATHERING_CONTEXT, TicketStatus.PLANNING_INVESTIGATION),
        (TicketStatus.PLANNING_INVESTIGATION, TicketStatus.AWAITING_PROVISION),
        (TicketStatus.AWAITING_PROVISION, TicketStatus.EXECUTING_BENCHMARK),
        (TicketStatus.EXECUTING_BENCHMARK, TicketStatus.EVALUATING_CONVERGENCE),
        (TicketStatus.EVALUATING_CONVERGENCE, TicketStatus.SYNTHESIZING_RESULTS),
        (TicketStatus.SYNTHESIZING_RESULTS, TicketStatus.AWAITING_TEARDOWN),
        (TicketStatus.AWAITING_TEARDOWN, TicketStatus.CLOSED),
    ]
    for from_status, to_status in path:
        allowed = VALID_TRANSITIONS[from_status]
        assert to_status in allowed, (
            f"Investigation path {from_status.value} → {to_status.value} is broken"
        )


def test_evaluate_loop_back_path():
    """Evaluate can loop back through planning and re-execute."""
    # evaluate → planning → provision → benchmark → evaluate
    path = [
        (TicketStatus.EVALUATING_CONVERGENCE, TicketStatus.PLANNING_INVESTIGATION),
        (TicketStatus.PLANNING_INVESTIGATION, TicketStatus.AWAITING_PROVISION),
        (TicketStatus.AWAITING_PROVISION, TicketStatus.EXECUTING_BENCHMARK),
        (TicketStatus.EXECUTING_BENCHMARK, TicketStatus.EVALUATING_CONVERGENCE),
    ]
    for from_status, to_status in path:
        allowed = VALID_TRANSITIONS[from_status]
        assert to_status in allowed, (
            f"Loop-back path {from_status.value} → {to_status.value} is broken"
        )


# --- Dispatcher mapping ---


def test_dispatcher_maps_new_statuses():
    """All new statuses are mapped in STATUS_AGENT_MAP."""
    from orchestrator.dispatcher import STATUS_AGENT_MAP

    for status in (
        "gathering_context",
        "planning_investigation",
        "evaluating_convergence",
        "synthesizing_results",
    ):
        assert status in STATUS_AGENT_MAP, f"{status} missing from STATUS_AGENT_MAP"
