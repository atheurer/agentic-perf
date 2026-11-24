"""Tests for state store transition logic.

Covers: double request_clarification, previous_status preservation.
"""

from __future__ import annotations

import pytest

from state_store.models import TicketStatus, TransitionRequest
from state_store.store import TicketStore


@pytest.fixture
def store(tmp_path):
    return TicketStore(persist_dir=tmp_path)


@pytest.fixture
def ticket_in_benchmark(store):
    """Create a ticket and advance it to executing_benchmark."""
    from state_store.models import CreateTicketRequest

    ticket = store.create_ticket(
        CreateTicketRequest(summary="test", description="test")
    )
    tid = ticket.id
    for status in [
        "triage_pending",
        "awaiting_hardware",
        "awaiting_provision",
        "executing_benchmark",
    ]:
        store.transition_ticket(tid, TransitionRequest(status=status))
    return store.get_ticket(tid)


class TestDoubleRequestClarification:
    def test_preparing_platform_can_return_to_resource_reconciliation(self, store):
        from state_store.models import CreateTicketRequest

        ticket = store.create_ticket(
            CreateTicketRequest(summary="test", description="test")
        )
        for status in (
            "triage_pending",
            "awaiting_hardware",
            "preparing_platform",
            "awaiting_customer_guidance",
            "preparing_platform",
            "awaiting_hardware",
        ):
            store.transition_ticket(ticket.id, TransitionRequest(status=status))

        assert store.get_ticket(ticket.id).status == TicketStatus.AWAITING_HARDWARE

    def test_single_pause_and_resume(self, store, ticket_in_benchmark):
        """Normal case: pause once, resume back to executing_benchmark."""
        tid = ticket_in_benchmark.id
        store.transition_ticket(
            tid, TransitionRequest(status="awaiting_customer_guidance")
        )
        t = store.get_ticket(tid)
        assert t.status == TicketStatus.AWAITING_CUSTOMER_GUIDANCE
        assert t.previous_status == TicketStatus.EXECUTING_BENCHMARK

        store.transition_ticket(tid, TransitionRequest(status="executing_benchmark"))
        t = store.get_ticket(tid)
        assert t.status == TicketStatus.EXECUTING_BENCHMARK
        assert t.previous_status is None

    def test_double_pause_preserves_original_status(self, store, ticket_in_benchmark):
        """Double request_clarification must not clobber previous_status."""
        tid = ticket_in_benchmark.id

        store.transition_ticket(
            tid, TransitionRequest(status="awaiting_customer_guidance")
        )
        t = store.get_ticket(tid)
        assert t.previous_status == TicketStatus.EXECUTING_BENCHMARK

        store.transition_ticket(
            tid, TransitionRequest(status="awaiting_customer_guidance")
        )
        t = store.get_ticket(tid)
        assert t.status == TicketStatus.AWAITING_CUSTOMER_GUIDANCE
        assert t.previous_status == TicketStatus.EXECUTING_BENCHMARK

        store.transition_ticket(tid, TransitionRequest(status="executing_benchmark"))
        t = store.get_ticket(tid)
        assert t.status == TicketStatus.EXECUTING_BENCHMARK

    def test_triple_pause_preserves_original_status(self, store, ticket_in_benchmark):
        """Even three consecutive pauses preserve the original status."""
        tid = ticket_in_benchmark.id

        store.transition_ticket(
            tid, TransitionRequest(status="awaiting_customer_guidance")
        )
        store.transition_ticket(
            tid, TransitionRequest(status="awaiting_customer_guidance")
        )
        store.transition_ticket(
            tid, TransitionRequest(status="awaiting_customer_guidance")
        )
        t = store.get_ticket(tid)
        assert t.previous_status == TicketStatus.EXECUTING_BENCHMARK

        store.transition_ticket(tid, TransitionRequest(status="executing_benchmark"))
        t = store.get_ticket(tid)
        assert t.status == TicketStatus.EXECUTING_BENCHMARK

    def test_abort_from_double_pause(self, store, ticket_in_benchmark):
        """Abort (awaiting_teardown) works even after double pause."""
        tid = ticket_in_benchmark.id

        store.transition_ticket(
            tid, TransitionRequest(status="awaiting_customer_guidance")
        )
        store.transition_ticket(
            tid, TransitionRequest(status="awaiting_customer_guidance")
        )

        store.transition_ticket(tid, TransitionRequest(status="awaiting_teardown"))
        t = store.get_ticket(tid)
        assert t.status == TicketStatus.AWAITING_TEARDOWN


class TestHarnessInstallProvenance:
    """#1382: server-owned install provenance authorizes teardown removal."""

    def test_record_and_merge(self, store, tmp_path):
        from state_store.models import CreateTicketRequest

        ticket = store.create_ticket(CreateTicketRequest(summary="s", description="d"))
        store.record_harness_installs(
            ticket.id,
            [{"host": "10.0.0.1", "harness": "uperf", "install_path": ""}],
        )
        # Re-recording (idempotent re-run of provisioning) merges per host.
        store.record_harness_installs(
            ticket.id,
            [
                {"host": "10.0.0.1", "harness": "uperf"},
                {"host": "10.0.0.2", "harness": "uperf"},
            ],
        )
        fields = store.get_ticket(ticket.id).custom_fields
        provenance = fields["harness_install_provenance"]
        assert sorted(provenance) == ["10.0.0.1", "10.0.0.2"]
        assert provenance["10.0.0.1"]["installed_by_ticket"] == ticket.id
        assert provenance["10.0.0.1"]["harness"] == "uperf"

        restarted = TicketStore(persist_dir=tmp_path)
        assert (
            restarted.get_ticket(ticket.id).custom_fields["harness_install_provenance"][
                "10.0.0.2"
            ]["installed_by_ticket"]
            == ticket.id
        )

    def test_generic_fields_patch_cannot_write_provenance(self, store):
        from state_store.models import CreateTicketRequest

        ticket = store.create_ticket(CreateTicketRequest(summary="s", description="d"))
        with pytest.raises(ValueError, match="harness install provenance"):
            store.update_fields(ticket.id, {"harness_install_provenance": {"h": "x"}})

    def test_records_require_host_and_harness(self, store):
        from state_store.models import CreateTicketRequest

        ticket = store.create_ticket(CreateTicketRequest(summary="s", description="d"))
        with pytest.raises(ValueError, match="host and harness"):
            store.record_harness_installs(ticket.id, [{"host": "", "harness": "x"}])


def test_harness_installs_api_service_only_and_records(tmp_path):
    """#1386-adjacent API surface: the provenance endpoint is service-only."""
    from fastapi.testclient import TestClient

    from state_store.auth import make_auth_dependency
    from state_store.identity import UserStore
    from state_store.main import create_app, mount_routers
    from state_store.models import CreateTicketRequest
    from state_store.store import TicketStore

    application = create_app(initialize_immediately=True)
    application.state.store = TicketStore(persist_dir=tmp_path)
    token = application.state.api_token

    users = UserStore(persist_path=tmp_path / "users.json")
    _, user_token = users.create_user("alice")
    application.state.multi_user = True
    application.state.user_store = users
    auth = make_auth_dependency(
        token, multi_user=True, user_store=users, token_ttl_days=0
    )
    application.state.auth_dependency = auth
    application.router.routes.clear()
    mount_routers(application, auth)

    client = TestClient(application)
    store = application.state.store
    ticket = store.create_ticket(CreateTicketRequest(summary="s", description="d"))
    path = f"/api/v1/tickets/{ticket.id}/harness-installs"

    unauthenticated = client.post(path, json={"records": []})
    assert unauthenticated.status_code == 401

    # A user principal must be rejected: harness provenance is
    # service-owned.
    forbidden = client.post(
        path,
        json={"records": [{"host": "10.0.0.1", "harness": "uperf"}]},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert forbidden.status_code == 403

    ok = client.post(
        path,
        json={"records": [{"host": "10.0.0.1", "harness": "uperf"}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["recorded_hosts"] == ["10.0.0.1"]
    assert (
        store.get_ticket(ticket.id).custom_fields["harness_install_provenance"][
            "10.0.0.1"
        ]["harness"]
        == "uperf"
    )
