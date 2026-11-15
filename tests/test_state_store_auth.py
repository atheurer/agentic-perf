"""Tests for state store API authentication."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from state_store.auth import make_auth_dependency
from state_store.identity import UserStore
from state_store.main import create_app
from state_store.models import (
    AcquireOrchestratorLeaseRequest,
    CreateTicketRequest,
    TicketStatus,
)


@pytest.fixture
def app(tmp_path):
    application = create_app(initialize_immediately=True)
    return application


@pytest.fixture
def authed_client(app):
    c = TestClient(app)
    c.headers["Authorization"] = f"Bearer {app.state.api_token}"
    return c


@pytest.fixture
def unauthed_client(app):
    return TestClient(app)


class TestAuthEnforcement:
    def test_api_without_token_returns_401(self, unauthed_client):
        r = unauthed_client.get("/api/v1/tickets")
        assert r.status_code == 401

    def test_api_with_wrong_token_returns_401(self, app):
        c = TestClient(app)
        c.headers["Authorization"] = "Bearer wrong-token"
        r = c.get("/api/v1/tickets")
        assert r.status_code == 401

    def test_api_with_correct_token_returns_200(self, authed_client):
        r = authed_client.get("/api/v1/tickets")
        assert r.status_code == 200

    def test_health_without_token_returns_200(self, unauthed_client):
        r = unauthed_client.get("/api/v1/health")
        assert r.status_code == 200

    def test_post_ticket_without_token_returns_401(self, unauthed_client):
        r = unauthed_client.post(
            "/api/v1/tickets",
            json={"summary": "test", "description": "test"},
        )
        assert r.status_code == 401

    def test_post_ticket_with_token_returns_200(self, authed_client):
        r = authed_client.post(
            "/api/v1/tickets",
            json={"summary": "test", "description": "test"},
        )
        assert r.status_code == 200

    def test_dashboard_without_token_returns_200(self, unauthed_client):
        r = unauthed_client.get("/")
        assert r.status_code in (200, 404)

    def test_near_miss_token_returns_401(self, app):
        """Token differing only in the last character is rejected."""
        real = app.state.api_token
        near_miss = real[:-1] + ("a" if real[-1] != "a" else "b")
        c = TestClient(app)
        c.headers["Authorization"] = f"Bearer {near_miss}"
        r = c.get("/api/v1/tickets")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_non_ascii_bearer_returns_401_not_500(self, app):
        """Non-ASCII bytes in the bearer must not crash compare_digest."""
        dep = make_auth_dependency(app.state.api_token)
        mock_request = MagicMock()
        mock_request.headers = {"authorization": "Bearer tést-tokën"}
        with pytest.raises(HTTPException) as exc_info:
            await dep(mock_request)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_empty_bearer_rejected(self):
        """Empty bearer token is rejected before any comparison."""
        dep = make_auth_dependency("")
        mock_request = MagicMock()
        mock_request.headers = {"authorization": "Bearer "}
        with pytest.raises(HTTPException) as exc_info:
            await dep(mock_request)
        assert exc_info.value.status_code == 401
        assert "empty" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_empty_token_file_rejects_nonempty_bearer(self):
        """If the deployment token is empty, a nonempty bearer must not
        match — the empty-token guard on compare_digest prevents it."""
        dep = make_auth_dependency("")
        mock_request = MagicMock()
        mock_request.headers = {"authorization": "Bearer some-token"}
        with pytest.raises(HTTPException) as exc_info:
            await dep(mock_request)
        assert exc_info.value.status_code == 401


class TestTokenTTL:
    """Token TTL enforcement and last-used tracking."""

    @pytest.fixture()
    def user_store(self, tmp_path):
        return UserStore(persist_path=tmp_path / "users.json")

    @pytest.mark.asyncio
    async def test_expired_token_returns_401(self, user_store):
        """Token older than TTL is rejected."""
        user, raw_token = user_store.create_user("alice")
        with user_store._lock:
            u = user_store._users["alice"]
            u.token_issued_at = datetime.now(timezone.utc) - timedelta(
                days=100,
            )

        dep = make_auth_dependency(
            "deploy-token",
            multi_user=True,
            user_store=user_store,
            token_ttl_days=90,
        )
        mock_request = MagicMock()
        mock_request.headers = {
            "authorization": f"Bearer {raw_token}",
        }
        with pytest.raises(HTTPException) as exc_info:
            await dep(mock_request)
        assert exc_info.value.status_code == 401
        assert "expired" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_unexpired_token_succeeds(self, user_store):
        """Token within TTL window is accepted."""
        _, raw_token = user_store.create_user("alice")
        dep = make_auth_dependency(
            "deploy-token",
            multi_user=True,
            user_store=user_store,
            token_ttl_days=200,
        )
        mock_request = MagicMock()
        mock_request.headers = {
            "authorization": f"Bearer {raw_token}",
        }
        principal = await dep(mock_request)
        assert principal.username == "alice"

    @pytest.mark.asyncio
    async def test_ttl_zero_disables_expiry(self, user_store):
        """TTL=0 means no expiry — ancient tokens still work."""
        _, raw_token = user_store.create_user("alice")
        with user_store._lock:
            u = user_store._users["alice"]
            u.token_issued_at = datetime.now(timezone.utc) - timedelta(
                days=9999,
            )

        dep = make_auth_dependency(
            "deploy-token",
            multi_user=True,
            user_store=user_store,
            token_ttl_days=0,
        )
        mock_request = MagicMock()
        mock_request.headers = {
            "authorization": f"Bearer {raw_token}",
        }
        principal = await dep(mock_request)
        assert principal.username == "alice"

    @pytest.mark.asyncio
    async def test_deployment_token_exempt_from_ttl(self, user_store):
        """Deployment/service token must never be subject to TTL."""
        deploy_token = "my-deploy-token"
        dep = make_auth_dependency(
            deploy_token,
            multi_user=True,
            user_store=user_store,
            token_ttl_days=1,
        )
        mock_request = MagicMock()
        mock_request.headers = {
            "authorization": f"Bearer {deploy_token}",
        }
        principal = await dep(mock_request)
        assert principal.kind == "service"
        assert principal.username == "deployment"

    @pytest.mark.asyncio
    async def test_single_user_mode_unaffected(self):
        """TTL parameter has no effect in single-user mode."""
        deploy_token = "my-deploy-token"
        dep = make_auth_dependency(
            deploy_token,
            multi_user=False,
            token_ttl_days=1,
        )
        mock_request = MagicMock()
        mock_request.headers = {
            "authorization": f"Bearer {deploy_token}",
        }
        principal = await dep(mock_request)
        assert principal.kind == "service"

    @pytest.mark.asyncio
    async def test_ttl_falls_back_to_created_at(self, user_store):
        """Pre-existing users without token_issued_at use created_at."""
        _, raw_token = user_store.create_user("alice")
        with user_store._lock:
            u = user_store._users["alice"]
            u.token_issued_at = None
            u.created_at = datetime.now(timezone.utc) - timedelta(days=5)

        dep = make_auth_dependency(
            "deploy-token",
            multi_user=True,
            user_store=user_store,
            token_ttl_days=3,
        )
        mock_request = MagicMock()
        mock_request.headers = {
            "authorization": f"Bearer {raw_token}",
        }
        with pytest.raises(HTTPException) as exc_info:
            await dep(mock_request)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_auth_updates_last_used(self, user_store):
        """Successful auth updates last_used_at on the user."""
        _, raw_token = user_store.create_user("alice")
        dep = make_auth_dependency(
            "deploy-token",
            multi_user=True,
            user_store=user_store,
        )
        mock_request = MagicMock()
        mock_request.headers = {
            "authorization": f"Bearer {raw_token}",
        }
        await dep(mock_request)
        user = user_store.get_user("alice")
        assert user.last_used_at is not None

    @pytest.mark.asyncio
    async def test_ttl_boundary_exact_day(self, user_store):
        """Token at exactly TTL days is expired (>= comparison)."""
        _, raw_token = user_store.create_user("alice")
        with user_store._lock:
            u = user_store._users["alice"]
            u.token_issued_at = datetime.now(timezone.utc) - timedelta(
                days=30,
            )

        dep = make_auth_dependency(
            "deploy-token",
            multi_user=True,
            user_store=user_store,
            token_ttl_days=30,
        )
        mock_request = MagicMock()
        mock_request.headers = {
            "authorization": f"Bearer {raw_token}",
        }
        with pytest.raises(HTTPException) as exc_info:
            await dep(mock_request)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_ttl_boundary_one_day_before(self, user_store):
        """Token one day before TTL is still valid."""
        _, raw_token = user_store.create_user("alice")
        with user_store._lock:
            u = user_store._users["alice"]
            u.token_issued_at = datetime.now(timezone.utc) - timedelta(
                days=29,
            )

        dep = make_auth_dependency(
            "deploy-token",
            multi_user=True,
            user_store=user_store,
            token_ttl_days=30,
        )
        mock_request = MagicMock()
        mock_request.headers = {
            "authorization": f"Bearer {raw_token}",
        }
        principal = await dep(mock_request)
        assert principal.username == "alice"

    @pytest.mark.asyncio
    async def test_expired_auth_does_not_update_last_used(self, user_store):
        """Expired token must not update last_used_at."""
        _, raw_token = user_store.create_user("alice")
        with user_store._lock:
            u = user_store._users["alice"]
            u.token_issued_at = datetime.now(timezone.utc) - timedelta(
                days=100,
            )

        dep = make_auth_dependency(
            "deploy-token",
            multi_user=True,
            user_store=user_store,
            token_ttl_days=90,
        )
        mock_request = MagicMock()
        mock_request.headers = {
            "authorization": f"Bearer {raw_token}",
        }
        with pytest.raises(HTTPException):
            await dep(mock_request)
        user = user_store.get_user("alice")
        assert user.last_used_at is None


def test_claim_reconciliation_is_bounded_and_requires_auth(
    app, authed_client, unauthed_client
) -> None:
    store = app.state.store
    ticket = store.create_ticket(
        CreateTicketRequest(summary="closed with claim", description="d")
    )
    session_id = uuid4()
    lease = store.acquire_orchestrator_lease(
        AcquireOrchestratorLeaseRequest(
            session_id=session_id,
            instance_name="test-orchestrator",
            host="host-a",
            pid=123,
            process_start_id="boot:123",
            ttl_seconds=30,
        )
    )
    store.claim_ticket(
        ticket.id,
        "owner",
        session_id=session_id,
        epoch=lease.epoch,
        claim_id="claim-1",
    )
    with store._lock:
        stored = store._tickets[ticket.id]
        stored.status = TicketStatus.CLOSED
        store._global_seq += 1
        stored.transition_seq = store._global_seq
        store._persist_ticket(stored)

    assert (
        unauthed_client.get("/api/v1/tickets/claim-reconciliation").status_code == 401
    )
    response = authed_client.get(
        "/api/v1/tickets/claim-reconciliation",
        params={"limit": 1},
    )

    claim = store.get_ticket(ticket.id).custom_fields["claim"]
    projected_claim = {
        field: claim[field]
        for field in ("claim_id", "owner", "session_id", "epoch", "expires")
        if field in claim
    }
    assert response.status_code == 200
    body = response.json()
    assert body["tickets"] == [
        {
            "id": ticket.id,
            "status": "closed",
            "custom_fields": {"claim": projected_claim},
        }
    ]
    assert body["read_errors"] is False
    assert body["next_cursor"] in {None, ticket.id}
    assert {key: value for key, value in body.items() if key != "next_cursor"} == {
        "tickets": [
            {
                "id": ticket.id,
                "status": "closed",
                "custom_fields": {"claim": projected_claim},
            }
        ],
        "read_errors": False,
    }
