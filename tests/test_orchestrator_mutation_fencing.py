from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
from fastapi import Depends, FastAPI

from orchestrator.main import _mutation_headers
from state_store.api.router import api_router
from state_store.auth import make_auth_dependency
from state_store.identity import UserStore
from state_store.models import AcquireOrchestratorLeaseRequest, CreateTicketRequest
from state_store.store import ClaimFenceError, TicketStore


def _lease(session_id):
    return AcquireOrchestratorLeaseRequest(
        session_id=session_id,
        instance_name="recovery-test",
        host="host-a",
        pid=123,
        process_start_id="boot:123",
        ttl_seconds=30,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "suffix", "body"),
    [
        ("patch", "fields", {"fields": {"orchestrator_retry_state": None}}),
        ("post", "comments", {"author": "orchestrator", "body": "recovery"}),
        ("post", "transition", {"status": "triage_pending"}),
    ],
)
async def test_unclaimed_recovery_mutations_are_fenced_after_takeover(
    tmp_path, monkeypatch, method, suffix, body
):
    now = [datetime.now(timezone.utc)]
    store = TicketStore(persist_dir=tmp_path, clock=lambda: now[0])
    ticket = store.create_ticket(
        CreateTicketRequest(summary="recover", description="x")
    )
    old_session = uuid4()
    old_lease = store.acquire_orchestrator_lease(_lease(old_session))
    monkeypatch.setenv("AGENTIC_PERF_API_TOKEN", "service")
    monkeypatch.setenv("AGENTIC_PERF_ORCHESTRATOR_SESSION_ID", str(old_session))
    monkeypatch.setenv("AGENTIC_PERF_ORCHESTRATOR_EPOCH", str(old_lease.epoch))
    old_headers = _mutation_headers(None)
    assert old_headers["X-Agentic-Perf-Mutation-Scope"] == "leader"

    app = FastAPI()
    app.state.store = store
    app.include_router(
        api_router, dependencies=[Depends(make_auth_dependency("service"))]
    )
    path = f"/api/v1/tickets/{ticket.id}/{suffix}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://store"
    ) as client:
        # Leader recovery works without a dispatch claim.
        response = await getattr(client, method)(path, headers=old_headers, json=body)
        assert response.status_code == 200, response.text
        now[0] += timedelta(seconds=31)
        new_session = uuid4()
        new_lease = store.acquire_orchestrator_lease(_lease(new_session))
        before = store.get_ticket(ticket.id).model_dump(mode="json")
        response = await getattr(client, method)(path, headers=old_headers, json=body)
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["reason"] == "stale_epoch"
        assert store.get_ticket(ticket.id).model_dump(mode="json") == before
        monkeypatch.setenv("AGENTIC_PERF_ORCHESTRATOR_SESSION_ID", str(new_session))
        monkeypatch.setenv("AGENTIC_PERF_ORCHESTRATOR_EPOCH", str(new_lease.epoch))
        # The new leader may write fields/comments on the same unclaimed ticket.
        response = await client.patch(
            f"/api/v1/tickets/{ticket.id}/fields",
            headers=_mutation_headers(None),
            json={"fields": {"note": "new leader"}},
        )
        assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_leader_recovery_preserves_claim_fencing_and_user_writes(
    tmp_path, monkeypatch
):
    now = [datetime.now(timezone.utc)]
    store = TicketStore(persist_dir=tmp_path / "tickets", clock=lambda: now[0])
    users = UserStore(tmp_path / "users.json")
    _, user_token = users.create_user("alice")
    ticket = store.create_ticket(CreateTicketRequest(summary="claim", description="x"))
    session = uuid4()
    lease = store.acquire_orchestrator_lease(_lease(session))
    claim = store.claim_ticket(
        ticket.id, "agent", session_id=session, epoch=lease.epoch
    )
    monkeypatch.setenv("AGENTIC_PERF_API_TOKEN", "service")
    monkeypatch.setenv("AGENTIC_PERF_ORCHESTRATOR_SESSION_ID", str(session))
    monkeypatch.setenv("AGENTIC_PERF_ORCHESTRATOR_EPOCH", str(lease.epoch))
    headers = _mutation_headers(None)
    app = FastAPI()
    app.state.store = store
    app.include_router(
        api_router,
        dependencies=[
            Depends(make_auth_dependency("service", multi_user=True, user_store=users))
        ],
    )
    path = f"/api/v1/tickets/{ticket.id}/fields"
    body = {"fields": {"note": "updated"}}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://store"
    ) as client:
        response = await client.patch(path, headers=headers, json=body)
        assert response.status_code == 409
        assert response.json()["detail"]["reason"] == "claim_active"
        assert "note" not in store.get_ticket(ticket.id).custom_fields
        response = await client.patch(
            path, headers=_mutation_headers(claim["claim_id"]), json=body
        )
        assert response.status_code == 200, response.text
        response = await client.patch(
            path, headers=_mutation_headers("wrong-claim"), json=body
        )
        assert response.status_code == 409
        assert response.json()["detail"]["reason"] == "claim_owned_by_other_session"
        partial = dict(headers)
        del partial["X-Agentic-Perf-Mutation-Scope"]
        assert (await client.patch(path, headers=partial, json=body)).status_code == 409
        claim_only = {
            "Authorization": "Bearer service",
            "X-Agentic-Perf-Claim-Id": claim["claim_id"],
        }
        assert (
            await client.patch(path, headers=claim_only, json=body)
        ).status_code == 409
        user_headers = dict(headers, Authorization=f"Bearer {user_token}")
        assert (
            await client.patch(path, headers=user_headers, json=body)
        ).status_code == 403
        response = await client.patch(
            path, headers={"Authorization": f"Bearer {user_token}"}, json=body
        )
        assert response.status_code == 200, response.text
        store.release_claim(
            ticket.id,
            "agent",
            session_id=session,
            epoch=lease.epoch,
            claim_id=claim["claim_id"],
        )
        assert (await client.patch(path, headers=headers, json=body)).status_code == 200


@pytest.mark.parametrize(
    "claim_kind", ["expired", "deposed", "bad_expiry", "bad_epoch"]
)
def test_leader_recovery_handles_stale_and_malformed_claims(tmp_path, claim_kind):
    now = [datetime.now(timezone.utc)]
    store = TicketStore(persist_dir=tmp_path, clock=lambda: now[0])
    ticket = store.create_ticket(CreateTicketRequest(summary="claim", description="x"))
    session = uuid4()
    lease = store.acquire_orchestrator_lease(_lease(session))
    store.claim_ticket(
        ticket.id, "agent", duration_seconds=300, session_id=session, epoch=lease.epoch
    )
    claim = store._tickets[ticket.id].custom_fields["claim"]
    if claim_kind == "expired":
        claim["expires"] = (now[0] - timedelta(seconds=1)).isoformat()
    elif claim_kind == "deposed":
        now[0] += timedelta(seconds=31)
        session = uuid4()
        lease = store.acquire_orchestrator_lease(_lease(session))
    elif claim_kind == "bad_expiry":
        claim["expires"] = "unknown"
    else:
        claim["epoch"] = "bad"
    if claim_kind in ("bad_expiry", "bad_epoch"):
        with pytest.raises(ClaimFenceError) as error:
            store.update_fields(
                ticket.id,
                {"note": "recovery"},
                session_id=session,
                epoch=lease.epoch,
                leader_only=True,
            )
        assert error.value.reason == "claim_malformed"
        assert "note" not in store.get_ticket(ticket.id).custom_fields
    else:
        updated = store.update_fields(
            ticket.id,
            {"note": "recovery"},
            session_id=session,
            epoch=lease.epoch,
            leader_only=True,
        )
        assert updated.custom_fields["note"] == "recovery"
