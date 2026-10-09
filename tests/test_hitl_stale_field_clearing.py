"""Tests for clearing stale pipeline fields on HITL resume.

When a ticket resumes from awaiting_customer_guidance back into the
pipeline, cached provisioning/resource fields must be cleared so that
deterministic resolution steps re-run with updated directives.

Covers fix for #1162.
"""

from __future__ import annotations

import pytest

from state_store.audit import AuditLog
from state_store.models import CreateTicketRequest, TicketStatus, TransitionRequest
from state_store.store import TicketStore


@pytest.fixture
def store(tmp_path):
    return TicketStore(persist_dir=tmp_path)


def _advance_to(store: TicketStore, ticket_id: str, statuses: list[str]) -> None:
    """Transition a ticket through a list of statuses."""
    for status in statuses:
        store.transition_ticket(
            ticket_id,
            TransitionRequest(status=TicketStatus(status)),
        )


def _make_ticket_at_guidance(
    store: TicketStore, prior_status: str = "awaiting_hardware"
):
    """Create a ticket in awaiting_customer_guidance with stale fields set."""
    ticket = store.create_ticket(
        CreateTicketRequest(summary="test", description="test")
    )
    tid = ticket.id

    # Advance to prior_status
    path_to_status = {
        "triage_pending": ["triage_pending"],
        "awaiting_hardware": ["triage_pending", "awaiting_hardware"],
        "awaiting_provision": [
            "triage_pending",
            "awaiting_hardware",
            "awaiting_provision",
        ],
        "executing_benchmark": [
            "triage_pending",
            "awaiting_hardware",
            "awaiting_provision",
            "executing_benchmark",
        ],
        "awaiting_review": [
            "triage_pending",
            "awaiting_hardware",
            "awaiting_provision",
            "executing_benchmark",
            "awaiting_review",
        ],
    }
    _advance_to(store, tid, path_to_status[prior_status])

    # Set stale pipeline fields
    store.update_fields(
        tid,
        {
            "jumpstarter_flash": {"image_url": "http://old-image/bad.raw"},
            "resource_provider": "jumpstarter",
            "resource_reservation_id": "old-lease",
            "resource_provider_metadata": {
                "lease_id": "old-lease",
                "board": "old-board-123",
            },
            "platform_ready": True,
        },
    )

    # Transition to guidance
    store.transition_ticket(
        tid,
        TransitionRequest(
            status=TicketStatus.AWAITING_CUSTOMER_GUIDANCE,
            comment="Need user correction",
        ),
    )

    return tid


class TestStalePipelineFieldClearing:
    """Stale fields are cleared when resuming from HITL guidance."""

    def test_resume_clears_jumpstarter_flash(self, store):
        tid = _make_ticket_at_guidance(store, "awaiting_hardware")
        store.transition_ticket(
            tid,
            TransitionRequest(status=TicketStatus.AWAITING_HARDWARE),
        )
        ticket = store.get_ticket(tid)
        assert "jumpstarter_flash" not in ticket.custom_fields

    def test_resume_preserves_resource_allocation_for_teardown(self, store):
        tid = _make_ticket_at_guidance(store, "awaiting_hardware")
        store.transition_ticket(
            tid,
            TransitionRequest(status=TicketStatus.AWAITING_HARDWARE),
        )
        ticket = store.get_ticket(tid)
        assert ticket.custom_fields["resource_provider"] == "jumpstarter"
        assert ticket.custom_fields["resource_reservation_id"] == "old-lease"
        assert ticket.custom_fields["resource_provider_metadata"]["lease_id"] == (
            "old-lease"
        )

    def test_resume_clears_platform_ready(self, store):
        tid = _make_ticket_at_guidance(store, "awaiting_hardware")
        store.transition_ticket(
            tid,
            TransitionRequest(status=TicketStatus.AWAITING_HARDWARE),
        )
        ticket = store.get_ticket(tid)
        assert "platform_ready" not in ticket.custom_fields

    def test_resume_to_preparing_platform_clears_old_successful_jumpstarter_image(
        self, store
    ):
        tid = _make_ticket_at_guidance(store, "awaiting_hardware")
        store.update_fields(
            tid,
            {
                "jumpstarter_flash": {
                    "status": "success",
                    "image_version": "9.4",
                    "image_url": "http://images.example/9.4.raw",
                },
                "directives": {"image_version": "9.5"},
            },
        )

        store.transition_ticket(
            tid,
            TransitionRequest(status=TicketStatus.PREPARING_PLATFORM),
        )

        ticket = store.get_ticket(tid)
        assert ticket.custom_fields["directives"]["image_version"] == "9.5"
        assert "jumpstarter_flash" not in ticket.custom_fields
        assert "platform_ready" not in ticket.custom_fields

    def test_resume_clears_all_stale_fields(self, store):
        tid = _make_ticket_at_guidance(store, "awaiting_hardware")
        store.transition_ticket(
            tid,
            TransitionRequest(status=TicketStatus.AWAITING_HARDWARE),
        )
        ticket = store.get_ticket(tid)
        for field in (
            "jumpstarter_flash",
            "platform_ready",
        ):
            assert field not in ticket.custom_fields, (
                f"{field} should have been cleared"
            )

    def test_resume_preserves_unrelated_fields(self, store):
        tid = _make_ticket_at_guidance(store, "awaiting_hardware")
        store.update_fields(tid, {"directives": {"release": "rhel-9.5"}})
        store.transition_ticket(
            tid,
            TransitionRequest(status=TicketStatus.AWAITING_HARDWARE),
        )
        ticket = store.get_ticket(tid)
        assert ticket.custom_fields.get("directives") == {"release": "rhel-9.5"}

    def test_resume_to_different_stage(self, store):
        """Resume to a different pipeline stage still clears stale fields."""
        tid = _make_ticket_at_guidance(store, "awaiting_provision")
        store.transition_ticket(
            tid,
            TransitionRequest(status=TicketStatus.AWAITING_HARDWARE),
        )
        ticket = store.get_ticket(tid)
        assert "jumpstarter_flash" not in ticket.custom_fields
        assert "platform_ready" not in ticket.custom_fields
        assert ticket.custom_fields["resource_reservation_id"] == "old-lease"

    def test_abort_does_not_clear_fields(self, store):
        """Transitioning to teardown should not clear stale fields."""
        tid = _make_ticket_at_guidance(store, "awaiting_hardware")
        store.transition_ticket(
            tid,
            TransitionRequest(status=TicketStatus.AWAITING_TEARDOWN),
        )
        ticket = store.get_ticket(tid)
        # Fields stay — teardown may need them for cleanup
        assert "jumpstarter_flash" in ticket.custom_fields

    def test_fields_absent_is_noop(self, store):
        """Resuming when stale fields are already absent does not error."""
        ticket = store.create_ticket(
            CreateTicketRequest(summary="test", description="test")
        )
        tid = ticket.id
        _advance_to(store, tid, ["triage_pending", "awaiting_hardware"])
        # No stale fields set
        store.transition_ticket(
            tid,
            TransitionRequest(status=TicketStatus.AWAITING_CUSTOMER_GUIDANCE),
        )
        # Should not raise
        store.transition_ticket(
            tid,
            TransitionRequest(status=TicketStatus.AWAITING_HARDWARE),
        )
        ticket = store.get_ticket(tid)
        assert "jumpstarter_flash" not in ticket.custom_fields

    def test_only_set_fields_are_cleared(self, store):
        """Only fields that actually exist get cleared."""
        ticket = store.create_ticket(
            CreateTicketRequest(summary="test", description="test")
        )
        tid = ticket.id
        _advance_to(store, tid, ["triage_pending", "awaiting_hardware"])
        # Set only one stale field
        store.update_fields(tid, {"jumpstarter_flash": {"image": "old"}})
        store.transition_ticket(
            tid,
            TransitionRequest(status=TicketStatus.AWAITING_CUSTOMER_GUIDANCE),
        )
        store.transition_ticket(
            tid,
            TransitionRequest(status=TicketStatus.AWAITING_HARDWARE),
        )
        ticket = store.get_ticket(tid)
        assert "jumpstarter_flash" not in ticket.custom_fields

    def test_double_guidance_still_clears(self, store):
        """Double-pausing then resuming still clears stale fields."""
        tid = _make_ticket_at_guidance(store, "awaiting_hardware")
        # Double-pause (re-enter guidance)
        store.transition_ticket(
            tid,
            TransitionRequest(status=TicketStatus.AWAITING_CUSTOMER_GUIDANCE),
        )
        store.transition_ticket(
            tid,
            TransitionRequest(status=TicketStatus.AWAITING_HARDWARE),
        )
        ticket = store.get_ticket(tid)
        assert "jumpstarter_flash" not in ticket.custom_fields

    def test_resume_to_review_preserves_pipeline_cache(self, store):
        tid = _make_ticket_at_guidance(store, "awaiting_review")
        store.transition_ticket(
            tid,
            TransitionRequest(status=TicketStatus.AWAITING_REVIEW),
        )
        ticket = store.get_ticket(tid)
        assert "jumpstarter_flash" in ticket.custom_fields
        assert ticket.custom_fields["platform_ready"] is True

    def test_cleared_field_names_are_in_transition_audit_without_trace_context(
        self, tmp_path
    ):
        audit = AuditLog(path=tmp_path / "audit.jsonl")
        store = TicketStore(persist_dir=tmp_path / "tickets", audit_log=audit)
        tid = _make_ticket_at_guidance(store, "awaiting_hardware")

        store.transition_ticket(
            tid,
            TransitionRequest(status=TicketStatus.AWAITING_HARDWARE),
        )

        transition = [
            entry
            for entry in audit.read(ticket_id=tid)
            if entry["mutation"] == "transition_ticket"
        ][-1]
        assert transition["data"]["cleared_stale_fields"] == [
            "jumpstarter_flash",
            "platform_ready",
        ]
