"""State-store contract for approvals of kind ``kernel_change``.

A kernel change approval binds to the intent record the provisioning server
persisted under ``custom_fields.kernel_change_intents``; the shared
resolve/consume paths then work unchanged, and a benchmark approval can
never be spent as a kernel approval or vice versa.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from agents.provisioning.kernel import new_intent
from providers.rhel_kernel import KernelSpec
from state_store.models import (
    AcquireOrchestratorLeaseRequest,
    ConsumeApprovalRequest,
    CreateApprovalRequest,
    CreateTicketRequest,
    ResolveApprovalRequest,
    TicketStatus,
)
from state_store.store import TicketStore
from tests.test_provisioning_kernel_tools import kernel_step_ticket

INTENT_ID = "kci-" + "a" * 32
DIGEST = hashlib.sha256(b"kernel-intent").hexdigest()


def _claimed_store(tmp_path, clock=None):
    store = (
        TicketStore(persist_dir=tmp_path, clock=clock)
        if clock
        else TicketStore(persist_dir=tmp_path)
    )
    ticket = store.create_ticket(
        CreateTicketRequest(
            summary="s",
            description="d",
            custom_fields=kernel_step_ticket()["custom_fields"],
        )
    )
    ticket.status = TicketStatus.AWAITING_PROVISION
    ticket.status_trail = ["new", "awaiting_provision"]
    store._tickets[ticket.id] = ticket
    store._persist_ticket(ticket)
    session_id = uuid4()
    store.acquire_orchestrator_lease(
        AcquireOrchestratorLeaseRequest(
            session_id=session_id,
            instance_name="test",
            host="localhost",
            pid=1,
            process_start_id="test",
            ttl_seconds=60,
        )
    )
    claim = store.claim_ticket(
        ticket.id, "provisioning", session_id=session_id, epoch=1
    )
    return store, ticket, str(session_id), claim["claim_id"]


def _seed_intent(store, ticket, digest=None, *, actions=("reboot",)):
    intent = new_intent(
        ticket.id,
        2,
        ["10.0.0.1", "10.0.0.2"],
        KernelSpec("5.14.0-503.16.1.el9_5.x86_64"),
        list(actions),
        {},
    )
    intent["intent_id"] = INTENT_ID
    if digest is not None:
        intent["intent_digest"] = digest
    ticket.custom_fields["kernel_change_intents"] = {"records": {INTENT_ID: intent}}
    store._persist_ticket(ticket)
    return intent["intent_digest"]


def _request(session_id, claim_id, **overrides) -> CreateApprovalRequest:
    body = dict(
        kind="kernel_change",
        validation_id=INTENT_ID,
        presented_run_file_digest=DIGEST,
        execution_intent_digest=DIGEST,
        summary="kernel change",
        invocation_id="invocation-1",
        tool_call_id="tool-call-1",
        session_id=session_id,
        session_epoch="1",
        waiter_owner="invocation-1",
        ticket_attempt=claim_id,
        claim_id=claim_id,
    )
    body.update(overrides)
    return CreateApprovalRequest(**body)


def _consume(session_id, claim_id, **overrides) -> ConsumeApprovalRequest:
    body = dict(
        kind="kernel_change",
        kernel_action="reboot",
        validation_id=INTENT_ID,
        presented_run_file_digest=DIGEST,
        execution_intent_digest=DIGEST,
        session_id=session_id,
        session_epoch="1",
        claim_id=claim_id,
        ticket_attempt=claim_id,
    )
    body.update(overrides)
    if body["kind"] != "kernel_change":
        body["kernel_action"] = None
    return ConsumeApprovalRequest(**body)


def test_kernel_change_binds_to_persisted_intent(tmp_path):
    store, ticket, session_id, claim_id = _claimed_store(tmp_path)
    digest = _seed_intent(store, ticket)
    approval = store.create_approval_request(
        ticket.id,
        _request(
            session_id,
            claim_id,
            presented_run_file_digest=digest,
            execution_intent_digest=digest,
        ),
        created_by="provisioning",
    )
    assert approval.kind == "kernel_change"
    assert approval.validation_id == INTENT_ID
    resolved = store.resolve_approval_request(
        ticket.id,
        approval.approval_request_id,
        ResolveApprovalRequest(decision="approved", comment_id="c-1"),
        resolved_by="alice",
    )
    assert resolved.status == "approved"
    consumed = store.consume_approval_request(
        ticket.id,
        approval.approval_request_id,
        _consume(
            session_id,
            claim_id,
            presented_run_file_digest=digest,
            execution_intent_digest=digest,
        ),
        consumed_by="provisioning",
    )
    assert consumed.consumed_at is not None
    with pytest.raises(ValueError, match="already consumed"):
        store.consume_approval_request(
            ticket.id,
            approval.approval_request_id,
            _consume(
                session_id,
                claim_id,
                presented_run_file_digest=digest,
                execution_intent_digest=digest,
            ),
            consumed_by="provisioning",
        )


def test_kernel_change_without_intent_is_rejected(tmp_path):
    store, ticket, session_id, claim_id = _claimed_store(tmp_path)
    digest = DIGEST
    with pytest.raises(ValueError, match="persisted kernel change intent"):
        store.create_approval_request(
            ticket.id,
            _request(
                session_id,
                claim_id,
                presented_run_file_digest=digest,
                execution_intent_digest=digest,
            ),
            created_by="provisioning",
        )


def test_kernel_change_digest_mismatch_is_rejected(tmp_path):
    store, ticket, session_id, claim_id = _claimed_store(tmp_path)
    _seed_intent(store, ticket, digest=hashlib.sha256(b"other").hexdigest())
    digest = DIGEST
    with pytest.raises(ValueError, match="persisted kernel change intent"):
        store.create_approval_request(
            ticket.id,
            _request(
                session_id,
                claim_id,
                presented_run_file_digest=digest,
                execution_intent_digest=digest,
            ),
            created_by="provisioning",
        )


def test_kernel_change_does_not_need_a_benchmark_validation(tmp_path):
    store, ticket, session_id, claim_id = _claimed_store(tmp_path)
    digest = _seed_intent(store, ticket)
    assert "benchmark_validations" not in ticket.custom_fields
    approval = store.create_approval_request(
        ticket.id,
        _request(
            session_id,
            claim_id,
            presented_run_file_digest=digest,
            execution_intent_digest=digest,
        ),
        created_by="provisioning",
    )
    assert approval.status == "pending"


def test_cross_kind_consumption_is_rejected(tmp_path):
    store, ticket, session_id, claim_id = _claimed_store(tmp_path)
    digest = _seed_intent(store, ticket)
    approval = store.create_approval_request(
        ticket.id,
        _request(
            session_id,
            claim_id,
            presented_run_file_digest=digest,
            execution_intent_digest=digest,
        ),
        created_by="provisioning",
    )
    store.resolve_approval_request(
        ticket.id,
        approval.approval_request_id,
        ResolveApprovalRequest(decision="approved", comment_id="c-1"),
        resolved_by="alice",
    )
    with pytest.raises(ValueError, match="kind mismatch"):
        store.consume_approval_request(
            ticket.id,
            approval.approval_request_id,
            _consume(
                session_id,
                claim_id,
                presented_run_file_digest=digest,
                execution_intent_digest=digest,
                kind="benchmark_run_file",
            ),
            consumed_by="benchmark",
        )


def test_approved_but_expired_cannot_be_consumed(tmp_path):
    now = [datetime.now(timezone.utc)]
    store, ticket, session_id, claim_id = _claimed_store(tmp_path, clock=lambda: now[0])
    digest = _seed_intent(store, ticket)
    approval = store.create_approval_request(
        ticket.id,
        _request(
            session_id,
            claim_id,
            presented_run_file_digest=digest,
            execution_intent_digest=digest,
            expires_at=now[0] + timedelta(minutes=5),
        ),
        created_by="provisioning",
    )
    store.resolve_approval_request(
        ticket.id,
        approval.approval_request_id,
        ResolveApprovalRequest(decision="approved", comment_id="c-1"),
        resolved_by="alice",
    )
    # Expiry is judged against wall-clock time at consumption, so set the
    # approval's expiry into the past directly.
    raw = store.get_ticket(ticket.id).custom_fields["approval_requests"]
    record = raw[approval.approval_request_id]
    record["expires_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    stored = store.get_ticket(ticket.id)
    stored.custom_fields["approval_requests"][approval.approval_request_id] = record
    store._persist_ticket(stored)
    with pytest.raises(ValueError, match="expired"):
        store.consume_approval_request(
            ticket.id,
            approval.approval_request_id,
            _consume(
                session_id,
                claim_id,
                presented_run_file_digest=digest,
                execution_intent_digest=digest,
            ),
            consumed_by="provisioning",
        )


def test_benchmark_kind_still_requires_validation_record(tmp_path):
    store, ticket, session_id, claim_id = _claimed_store(tmp_path)
    digest = _seed_intent(store, ticket)
    with pytest.raises(ValueError, match="executable validation record"):
        store.create_approval_request(
            ticket.id,
            _request(
                session_id,
                claim_id,
                presented_run_file_digest=digest,
                execution_intent_digest=digest,
                kind="benchmark_run_file",
            ),
            created_by="benchmark",
        )


@pytest.mark.parametrize("consumed_before", [False, True])
def test_reboot_reservation_is_atomic_and_cannot_be_reopened(tmp_path, consumed_before):
    from concurrent.futures import ThreadPoolExecutor

    store, ticket, session_id, claim_id = _claimed_store(tmp_path)
    digest = _seed_intent(
        store, ticket, actions=["install", "reboot"] if consumed_before else ["reboot"]
    )
    binding = dict(presented_run_file_digest=digest, execution_intent_digest=digest)
    approval = store.create_approval_request(
        ticket.id, _request(session_id, claim_id, **binding), created_by="provisioning"
    )
    store.resolve_approval_request(
        ticket.id,
        approval.approval_request_id,
        ResolveApprovalRequest(decision="approved", comment_id="c-1"),
        resolved_by="alice",
    )
    if consumed_before:
        store.consume_approval_request(
            ticket.id,
            approval.approval_request_id,
            _consume(session_id, claim_id, kernel_action="install", **binding),
            consumed_by="provisioning",
        )

        current = store.get_ticket(ticket.id)
        current.custom_fields["kernel_change_intents"]["records"][INTENT_ID][
            "progress"
        ]["install"] = "done"
        store._persist_ticket(current)

    def reserve():
        try:
            result = store.consume_approval_request(
                ticket.id,
                approval.approval_request_id,
                _consume(session_id, claim_id, kernel_action="reboot", **binding),
                consumed_by="provisioning",
            )
            return result.kernel_reboot_reserved
        except ValueError as exc:
            assert "already consumed" in str(exc)
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        reservations = list(executor.map(lambda _: reserve(), range(2)))
    assert sorted(reservations) == [False, True]
    current = store.get_ticket(ticket.id)
    current.custom_fields["kernel_change_intents"]["records"][INTENT_ID]["progress"][
        "reboot"
    ] = "pending"
    store._persist_ticket(current)
    assert reserve() is False


@pytest.mark.parametrize("field", ["addresses", "hosts", "actions_required", "kernel"])
def test_old_digest_cannot_approve_tampered_canonical_intent(tmp_path, field):
    store, ticket, session_id, claim_id = _claimed_store(tmp_path)
    digest = _seed_intent(store, ticket)
    intent = ticket.custom_fields["kernel_change_intents"]["records"][INTENT_ID]
    if field == "addresses":
        intent[field]["10.0.0.1"] = "10.0.0.99"
    elif field == "hosts":
        intent[field] = ["10.0.0.1"]
    elif field == "actions_required":
        intent[field] = ["install", "reboot"]
    else:
        intent[field]["companion_packages"] = ["5.14.0-503.14.1.el9_5.x86_64"]
    store._persist_ticket(ticket)
    with pytest.raises(ValueError):
        store.create_approval_request(
            ticket.id,
            _request(
                session_id,
                claim_id,
                presented_run_file_digest=digest,
                execution_intent_digest=digest,
            ),
            created_by="provisioning",
        )


@pytest.mark.parametrize(
    "failure", ["before_replace", "after_replace", "audit", "trace"]
)
def test_reboot_reservation_persistence_failures_are_fail_closed(
    tmp_path, monkeypatch, failure
):
    store, ticket, session_id, claim_id = _claimed_store(tmp_path)
    digest = _seed_intent(store, ticket)
    binding = dict(presented_run_file_digest=digest, execution_intent_digest=digest)
    approval = store.create_approval_request(
        ticket.id, _request(session_id, claim_id, **binding), created_by="provisioning"
    )
    store.resolve_approval_request(
        ticket.id,
        approval.approval_request_id,
        ResolveApprovalRequest(decision="approved", comment_id="c-1"),
        resolved_by="alice",
    )
    request = _consume(session_id, claim_id, kernel_action="reboot", **binding)
    persist = store._persist_ticket

    def fail_persist(candidate, *, require_durable=False):
        assert require_durable is True
        if failure == "after_replace":
            persist(candidate, require_durable=True)
        raise OSError("reservation persistence failure")

    def fail_event(*args, **kwargs):
        raise OSError("reservation event failure")

    with monkeypatch.context() as injected:
        if failure in ("before_replace", "after_replace"):
            injected.setattr(store, "_persist_ticket", fail_persist)
        else:
            injected.setattr(
                store,
                "_audit_log" if failure == "audit" else "_trace_mutation",
                fail_event,
            )
        with pytest.raises(OSError, match="reservation"):
            store.consume_approval_request(
                ticket.id,
                approval.approval_request_id,
                request,
                consumed_by="provisioning",
            )
    expected_reserved = failure != "before_replace"
    current = store.get_ticket(ticket.id)
    assert (
        current.custom_fields["approval_requests"][approval.approval_request_id][
            "kernel_reboot_reserved"
        ]
        is expected_reserved
    )
    restarted = TicketStore(persist_dir=tmp_path)
    recovered = restarted.get_ticket(ticket.id)
    assert (
        recovered.custom_fields["approval_requests"][approval.approval_request_id][
            "kernel_reboot_reserved"
        ]
        is expected_reserved
    )
    if expected_reserved:
        with pytest.raises(ValueError, match="already consumed"):
            store.consume_approval_request(
                ticket.id,
                approval.approval_request_id,
                request,
                consumed_by="provisioning",
            )
    else:
        reserved = store.consume_approval_request(
            ticket.id,
            approval.approval_request_id,
            request,
            consumed_by="provisioning",
        )
        assert reserved.kernel_reboot_reserved is True


@pytest.mark.parametrize(
    "field", ["kernel_change_intents", "kernel_transitions", "approval_requests"]
)
@pytest.mark.parametrize("nested", [False, True])
def test_generic_ticket_creation_cannot_seed_kernel_authority(tmp_path, field, nested):
    store = TicketStore(persist_dir=tmp_path)
    fields = {field: {"forged": {"state": "verified"}}}
    if nested:
        fields = {"nested": [fields]}
    with pytest.raises(ValueError):
        store.create_ticket(
            CreateTicketRequest(
                summary="forged",
                description="d",
                custom_fields=fields,
            )
        )


@pytest.mark.parametrize(
    "field", ["kernel_change_intents", "kernel_transitions", "approval_requests"]
)
@pytest.mark.parametrize("nested", [False, True])
def test_generic_field_patch_cannot_forge_or_erase_kernel_authority(
    tmp_path, field, nested
):
    store, ticket, session_id, claim_id = _claimed_store(tmp_path)
    digest = _seed_intent(store, ticket)
    approval = store.create_approval_request(
        ticket.id,
        _request(
            session_id,
            claim_id,
            presented_run_file_digest=digest,
            execution_intent_digest=digest,
        ),
        created_by="provisioning",
    )
    store.resolve_approval_request(
        ticket.id,
        approval.approval_request_id,
        ResolveApprovalRequest(decision="approved", comment_id="c-1"),
        resolved_by="alice",
    )
    store.consume_approval_request(
        ticket.id,
        approval.approval_request_id,
        _consume(
            session_id,
            claim_id,
            kernel_action="reboot",
            presented_run_file_digest=digest,
            execution_intent_digest=digest,
        ),
        consumed_by="provisioning",
    )
    before = store.get_ticket(ticket.id).model_dump(mode="json")
    fields = {field: {}}
    if nested:
        fields = {"nested": [fields]}
    with pytest.raises(ValueError):
        store.update_fields(
            ticket.id, fields, session_id=UUID(session_id), epoch=1, claim_id=claim_id
        )
    assert store.get_ticket(ticket.id).model_dump(mode="json") == before
    assert (
        TicketStore(persist_dir=tmp_path)
        .get_ticket(ticket.id)
        .custom_fields["approval_requests"][approval.approval_request_id][
            "kernel_reboot_reserved"
        ]
        is True
    )


@pytest.mark.parametrize(
    "action, consumed",
    [
        ("install", False),
        ("install", True),
        ("select_default", False),
        ("select_default", True),
        ("reboot", False),
    ],
)
@pytest.mark.parametrize(
    "changed",
    ["plan_step", "plan_spec", "plan_package", "assigned_hosts", "pins", "policy"],
)
def test_server_authorization_rechecks_fresh_plan_and_targets(
    tmp_path, action, consumed, changed
):
    store, ticket, session_id, claim_id = _claimed_store(tmp_path)
    digest = _seed_intent(store, ticket, actions=[action])
    binding = dict(presented_run_file_digest=digest, execution_intent_digest=digest)
    approval = store.create_approval_request(
        ticket.id, _request(session_id, claim_id, **binding), created_by="provisioning"
    )
    store.resolve_approval_request(
        ticket.id,
        approval.approval_request_id,
        ResolveApprovalRequest(decision="approved", comment_id="c-1"),
        resolved_by="alice",
    )
    request = _consume(session_id, claim_id, kernel_action=action, **binding)
    if consumed:
        store.consume_approval_request(
            ticket.id, approval.approval_request_id, request, consumed_by="provisioning"
        )
    # The client has already assembled its request from the approved snapshot.
    # Change durable authority before the server receives that same request.
    current = store.get_ticket(ticket.id)
    fields = current.custom_fields
    if changed == "plan_step":
        fields["execution_plan"]["current_step"] = 1
    elif changed == "plan_spec":
        fields["execution_plan"]["steps"][2]["params"]["kernel"][
            "companion_packages"
        ] = ["5.14.0-503.14.1.el9_5.x86_64"]
    elif changed == "plan_package":
        fields["execution_plan"]["steps"][2]["params"]["kernel"]["package"] = (
            "kernel-rt"
        )
    elif changed == "policy":
        fields["directives"] = {"kernel_reboot_policy": "if_needed"}
    elif changed == "assigned_hosts":
        fields["assigned_hardware_ips"]["targets"] = ["10.0.0.1", "10.0.0.3"]
    else:
        fields["kernel_change_intents"]["records"][INTENT_ID]["addresses"][
            "10.0.0.1"
        ] = "10.0.0.99"
    store._persist_ticket(current)
    before = (
        store.get_ticket(ticket.id)
        .custom_fields["approval_requests"][approval.approval_request_id]
        .copy()
    )
    with pytest.raises(ValueError):
        store.consume_approval_request(
            ticket.id, approval.approval_request_id, request, consumed_by="provisioning"
        )
    after = store.get_ticket(ticket.id).custom_fields["approval_requests"][
        approval.approval_request_id
    ]
    assert after == before


def _verified_checkpoint():
    hosts = ["10.0.0.1", "10.0.0.2"]
    return {
        "kernel": "5.14.0-503.16.1.el9_5.x86_64",
        "kernel_spec": {"release": "5.14.0-503.16.1.el9_5.x86_64", "package": "kernel"},
        "package": "kernel",
        "requested_hosts": hosts,
        "reboot_policy": "always",
        "addresses": {host: host for host in hosts},
        "state": "verified",
        "hosts": {
            host: {
                "state": "verified",
                "post_boot_id": f"boot-{host}",
                "pre_boot_id": "previous-boot",
                "reboot_issued": True,
                "reboot_pending": False,
                "observed_kernel": "5.14.0-503.16.1.el9_5.x86_64",
            }
            for host in hosts
        },
    }


def test_server_checkpoint_cas_rejects_delayed_success_after_failed_reconciliation(
    tmp_path,
):
    import copy

    store, ticket, session_id, claim_id = _claimed_store(tmp_path)
    fence = dict(session_id=UUID(session_id), epoch=1, claim_id=claim_id)
    success = store.kernel_control(
        ticket.id, "transition", "2", 0, _verified_checkpoint(), **fence
    )
    delayed_success = copy.deepcopy(success)
    failure = copy.deepcopy(success)
    failure["state"] = "indeterminate"
    for host in failure["hosts"].values():
        host.update(state="indeterminate", post_boot_id=None)
    invalidated = store.kernel_control(
        ticket.id, "transition", "2", success["record_version"], failure, **fence
    )
    with pytest.raises(ValueError, match="version conflict"):
        store.kernel_control(
            ticket.id,
            "transition",
            "2",
            delayed_success["record_version"],
            delayed_success,
            **fence,
        )
    restarted = TicketStore(persist_dir=tmp_path)
    assert (
        restarted.get_ticket(ticket.id).custom_fields["kernel_transitions"]["2"]
        == invalidated
    )
    assert invalidated["state"] == "indeterminate"


@pytest.mark.parametrize("kind", ["intent", "transition"])
@pytest.mark.parametrize("after_replace", [False, True])
def test_trusted_kernel_control_write_requires_durable_replacement(
    tmp_path, monkeypatch, kind, after_replace
):
    store, ticket, session_id, claim_id = _claimed_store(tmp_path)
    if kind == "intent":
        record = new_intent(
            ticket.id,
            2,
            ["10.0.0.1", "10.0.0.2"],
            KernelSpec("5.14.0-503.16.1.el9_5.x86_64"),
            ["reboot"],
            {},
        )
        record_id = record["intent_id"]
    else:
        record = _verified_checkpoint()
        record_id = "2"
    persist = store._persist_ticket

    def fail(candidate, *, require_durable=False):
        assert require_durable is True
        if after_replace:
            persist(candidate, require_durable=True)
        raise OSError("kernel control persistence failed")

    with monkeypatch.context() as injected:
        injected.setattr(store, "_persist_ticket", fail)
        with pytest.raises(OSError, match="kernel control"):
            store.kernel_control(
                ticket.id,
                kind,
                record_id,
                0,
                record,
                session_id=UUID(session_id),
                epoch=1,
                claim_id=claim_id,
            )

    def stored_record(view):
        fields = view.custom_fields
        return (
            fields.get("kernel_change_intents", {}).get("records", {})
            if kind == "intent"
            else fields.get("kernel_transitions", {})
        ).get(record_id)

    current = stored_record(store.get_ticket(ticket.id))
    recovered = stored_record(TicketStore(persist_dir=tmp_path).get_ticket(ticket.id))
    assert current == recovered
    assert (current is not None) is after_replace


@pytest.mark.asyncio
async def test_kernel_control_http_requires_trusted_fence_and_cas(tmp_path):
    import httpx
    from fastapi import Depends, FastAPI

    from state_store.api.router import api_router
    from state_store.auth import make_auth_dependency
    from state_store.identity import UserStore
    from state_store.main import _set_audit_actor

    store, ticket, session_id, claim_id = _claimed_store(tmp_path / "tickets")
    store._tickets[ticket.id].owners = ["alice"]
    users = UserStore(persist_path=tmp_path / "users.json")
    _, user_token = users.create_user("alice")
    app = FastAPI()
    app.state.store = store
    app.state.multi_user = True
    app.state.user_store = users
    app.include_router(
        api_router,
        dependencies=[
            Depends(make_auth_dependency("service", multi_user=True, user_store=users)),
            Depends(_set_audit_actor),
        ],
    )
    path = f"/api/v1/tickets/{ticket.id}/kernel-control"
    fence = {
        "X-Agentic-Perf-Orchestrator-Session": session_id,
        "X-Agentic-Perf-Orchestrator-Epoch": "1",
        "X-Agentic-Perf-Claim-Id": claim_id,
    }
    body = {
        "kind": "transition",
        "record_id": "2",
        "expected_version": 0,
        "record": _verified_checkpoint(),
    }
    service = {"Authorization": "Bearer service"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        denied = await client.post(
            path, json=body, headers={**fence, "Authorization": f"Bearer {user_token}"}
        )
        assert denied.status_code == 403
        missing = await client.post(path, json=body, headers=service)
        assert missing.status_code == 409
        stale = await client.post(
            path,
            json=body,
            headers={**service, **fence, "X-Agentic-Perf-Orchestrator-Epoch": "0"},
        )
        assert stale.status_code == 409
        accepted = await client.post(path, json=body, headers={**service, **fence})
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["record_version"] == 1
        replay = await client.post(path, json=body, headers={**service, **fence})
        assert replay.status_code == 409
        for field in (
            "kernel_transitions",
            "kernel_change_intents",
            "approval_requests",
        ):
            created = await client.post(
                "/api/v1/tickets",
                headers=service,
                json={
                    "summary": "forged",
                    "description": "d",
                    "custom_fields": {field: {}},
                },
            )
            assert created.status_code == 422, created.text
            patched = await client.patch(
                f"/api/v1/tickets/{ticket.id}/fields",
                headers={**service, **fence},
                json={"fields": {field: {}}},
            )
            assert patched.status_code == 422, patched.text
    assert (
        TicketStore(persist_dir=tmp_path / "tickets")
        .get_ticket(ticket.id)
        .custom_fields["kernel_transitions"]["2"]["record_version"]
        == 1
    )


@pytest.mark.parametrize("change", ["release", "companions"])
def test_new_approved_spec_replaces_only_completed_transition_at_observed_version(
    tmp_path, change
):
    import copy

    store, ticket, session_id, claim_id = _claimed_store(tmp_path)
    fence = dict(session_id=UUID(session_id), epoch=1, claim_id=claim_id)
    old = store.kernel_control(
        ticket.id, "transition", "2", 0, _verified_checkpoint(), **fence
    )
    current = store.get_ticket(ticket.id)
    spec_dict = current.custom_fields["execution_plan"]["steps"][2]["params"]["kernel"]
    if change == "release":
        spec_dict["release"] = "5.14.0-503.14.1.el9_5.x86_64"
    else:
        spec_dict["companion_packages"] = ["5.14.0-503.14.1.el9_5.x86_64"]
    store._persist_ticket(current)
    spec = KernelSpec.parse(spec_dict)
    intent = new_intent(ticket.id, 2, ["10.0.0.1", "10.0.0.2"], spec, ["reboot"], {})
    intent["intent_id"] = INTENT_ID
    store.kernel_control(ticket.id, "intent", INTENT_ID, 0, intent, **fence)
    binding = dict(
        presented_run_file_digest=intent["intent_digest"],
        execution_intent_digest=intent["intent_digest"],
    )
    approval = store.create_approval_request(
        ticket.id, _request(session_id, claim_id, **binding), created_by="provisioning"
    )
    store.resolve_approval_request(
        ticket.id,
        approval.approval_request_id,
        ResolveApprovalRequest(decision="approved", comment_id="c-1"),
        resolved_by="alice",
    )
    store.consume_approval_request(
        ticket.id,
        approval.approval_request_id,
        _consume(session_id, claim_id, **binding),
        consumed_by="provisioning",
    )
    replacement = copy.deepcopy(old)
    replacement.update(kernel=spec.release, kernel_spec=spec.to_dict())
    for host in replacement["hosts"].values():
        host.update(
            observed_kernel=spec.release,
            pre_boot_id="old-cycle",
            post_boot_id="new-cycle",
        )
    written = store.kernel_control(
        ticket.id, "transition", "2", old["record_version"], replacement, **fence
    )
    assert written["record_version"] == old["record_version"] + 1
    assert written["kernel_spec"] == spec.to_dict()
    assert written["previous_transitions"][-1]["kernel_spec"] == old["kernel_spec"]
    assert (
        TicketStore(persist_dir=tmp_path)
        .get_ticket(ticket.id)
        .custom_fields["kernel_transitions"]["2"]
        == written
    )


@pytest.mark.parametrize("action", ["install", "select_default", "reboot"])
def test_pending_old_transition_blocks_all_new_kernel_authorization(tmp_path, action):
    store, ticket, session_id, claim_id = _claimed_store(tmp_path)
    digest = _seed_intent(store, ticket, actions=[action])
    binding = dict(presented_run_file_digest=digest, execution_intent_digest=digest)
    approval = store.create_approval_request(
        ticket.id, _request(session_id, claim_id, **binding), created_by="provisioning"
    )
    store.resolve_approval_request(
        ticket.id,
        approval.approval_request_id,
        ResolveApprovalRequest(decision="approved", comment_id="c-1"),
        resolved_by="alice",
    )
    # The old operation becomes visibly ambiguous after this client snapshot.
    old = _verified_checkpoint()
    old["kernel"] = "5.14.0-503.14.1.el9_5.x86_64"
    old["kernel_spec"]["release"] = old["kernel"]
    old["state"] = "indeterminate"
    for host in old["hosts"].values():
        host.update(state="indeterminate", reboot_pending=True, post_boot_id=None)
    store._tickets[ticket.id].custom_fields["kernel_transitions"] = {"2": old}
    store._persist_ticket(store._tickets[ticket.id])
    with pytest.raises(
        ValueError, match="previous kernel reboot requires reconciliation"
    ):
        store.consume_approval_request(
            ticket.id,
            approval.approval_request_id,
            _consume(session_id, claim_id, kernel_action=action, **binding),
            consumed_by="provisioning",
        )
    restarted = TicketStore(persist_dir=tmp_path)
    persisted = restarted.get_ticket(ticket.id).custom_fields
    assert (
        persisted["kernel_transitions"]["2"]["hosts"]["10.0.0.1"]["reboot_pending"]
        is True
    )
    assert (
        persisted["approval_requests"][approval.approval_request_id]["consumed_at"]
        is None
    )


@pytest.mark.parametrize("after_replace", [False, True])
def test_pending_reboot_survives_failed_reconciliation_write_and_store_restart(
    tmp_path, monkeypatch, after_replace
):
    import copy

    from agents.provisioning.kernel import verification_from_transition

    store, ticket, session_id, claim_id = _claimed_store(tmp_path)
    fence = dict(session_id=UUID(session_id), epoch=1, claim_id=claim_id)
    pending = _verified_checkpoint()
    pending["state"] = "indeterminate"
    pending["hosts"]["10.0.0.1"].update(
        state="indeterminate",
        reboot_pending=True,
        pre_boot_id="old-still-running",
        post_boot_id=None,
        observed_kernel=None,
    )
    stored = store.kernel_control(ticket.id, "transition", "2", 0, pending, **fence)
    invalidated = copy.deepcopy(stored)
    invalidated["hosts"]["10.0.0.1"]["error"] = "unreachable during reconciliation"
    persist = store._persist_ticket

    def failed(candidate, *, require_durable=False):
        assert require_durable is True
        if after_replace:
            persist(candidate, require_durable=True)
        raise OSError("checkpoint failure")

    with monkeypatch.context() as injected:
        injected.setattr(store, "_persist_ticket", failed)
        with pytest.raises(OSError, match="checkpoint failure"):
            store.kernel_control(
                ticket.id,
                "transition",
                "2",
                stored["record_version"],
                invalidated,
                **fence,
            )
    restarted = TicketStore(persist_dir=tmp_path)
    recovered = restarted.get_ticket(ticket.id)
    evidence = recovered.custom_fields["kernel_transitions"]["2"]["hosts"]["10.0.0.1"]
    assert evidence["reboot_pending"] is True
    assert evidence["pre_boot_id"] == "old-still-running"
    assert not verification_from_transition(recovered.model_dump(mode="json"), 2)[0]
