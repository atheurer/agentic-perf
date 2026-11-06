from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from fastapi import Depends, FastAPI

from state_store.api.router import api_router
from state_store.auth import make_auth_dependency
from state_store.identity import UserStore
from state_store.models import (
    AcquireOrchestratorLeaseRequest,
    CreateTicketRequest,
    TicketStatus,
)
from state_store.store import TicketStore


@pytest.mark.asyncio
async def test_checkpoint_requires_service_and_live_platform_claim(tmp_path):
    store = TicketStore(persist_dir=tmp_path / "tickets")
    users = UserStore(tmp_path / "users.json")
    _, user_token = users.create_user("alice")
    ticket = store.create_ticket(
        CreateTicketRequest(summary="test", description="test"), owners=["alice"]
    )
    store._tickets[ticket.id].status = TicketStatus.PREPARING_PLATFORM
    session = uuid4()
    lease = store.acquire_orchestrator_lease(
        AcquireOrchestratorLeaseRequest(
            session_id=session,
            instance_name="test",
            host="host",
            pid=123,
            process_start_id="test:123",
            ttl_seconds=30,
        )
    )
    claim = store.claim_ticket(
        ticket.id, "platform", session_id=session, epoch=lease.epoch
    )
    app = FastAPI()
    app.state.store = store
    app.state.multi_user = True
    app.include_router(
        api_router,
        dependencies=[
            Depends(make_auth_dependency("service", multi_user=True, user_store=users))
        ],
    )
    fence = {
        "X-Agentic-Perf-Orchestrator-Session": str(session),
        "X-Agentic-Perf-Orchestrator-Epoch": str(lease.epoch),
        "X-Agentic-Perf-Claim-Id": claim["claim_id"],
    }
    body = {"fields": {"foreman_reinstall_state": {"status": "verified"}}}
    path = f"/api/v1/tickets/{ticket.id}/fields"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://store"
    ) as client:
        assert (
            await client.patch(
                path, headers={"Authorization": f"Bearer {user_token}"}, json=body
            )
        ).status_code == 403
        assert (
            await client.patch(
                path,
                headers={"Authorization": f"Bearer {user_token}", **fence},
                json=body,
            )
        ).status_code == 403
        assert (
            await client.patch(
                path, headers={"Authorization": "Bearer service"}, json=body
            )
        ).status_code == 422
        wrong = {**fence, "X-Agentic-Perf-Claim-Id": "wrong"}
        assert (
            await client.patch(
                path, headers={"Authorization": "Bearer service", **wrong}, json=body
            )
        ).status_code == 409
        assert (
            await client.patch(
                path, headers={"Authorization": "Bearer service", **fence}, json=body
            )
        ).status_code == 200
        expires = store._tickets[ticket.id].custom_fields["claim"]["expires"]
        store._tickets[ticket.id].custom_fields["claim"]["expires"] = (
            "2000-01-01T00:00:00+00:00"
        )
        assert (
            await client.patch(
                path, headers={"Authorization": "Bearer service", **fence}, json=body
            )
        ).status_code == 409
        store._tickets[ticket.id].custom_fields["claim"]["expires"] = expires
        store._tickets[ticket.id].status = TicketStatus.AWAITING_PROVISION
        assert (
            await client.patch(
                path, headers={"Authorization": "Bearer service", **fence}, json=body
            )
        ).status_code == 422


def test_ticket_creation_rejects_preloaded_checkpoint():
    with pytest.raises(ValueError, match="checkpoints are server-owned"):
        CreateTicketRequest(
            summary="test",
            description="test",
            custom_fields={"foreman_reinstall_state": {"status": "verified"}},
        )
