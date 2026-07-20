"""Tests for ticket ownership and write gating (PR 2)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from state_store.identity import UserStore
from state_store.main import create_app


def _make_multi_user_app(tmp_path):
    """Create an app with multi-user mode enabled."""
    user_store = UserStore(persist_path=tmp_path / "users.json")

    from state_store.auth import make_auth_dependency

    app = (
        create_app.__wrapped__() if hasattr(create_app, "__wrapped__") else create_app()
    )

    token = app.state.api_token

    app.state.multi_user = True
    app.state.user_store = user_store

    auth = make_auth_dependency(
        token,
        multi_user=True,
        user_store=user_store,
    )
    app.state.auth_dependency = auth

    from fastapi import Depends

    from state_store.api.router import api_router, health_router

    app.router.routes.clear()
    app.include_router(api_router, dependencies=[Depends(auth)])
    app.include_router(health_router)

    return app, user_store, token


@pytest.fixture()
def env(tmp_path):
    app, user_store, deploy_token = _make_multi_user_app(tmp_path)
    return app, user_store, deploy_token


@pytest.fixture()
def admin_client(env):
    app, _, deploy_token = env
    c = TestClient(app)
    c.headers["Authorization"] = f"Bearer {deploy_token}"
    return c


@pytest.fixture()
def app(env):
    app, _, _ = env
    return app


@pytest.fixture()
def user_store(env):
    _, store, _ = env
    return store


@pytest.fixture()
def deploy_token(env):
    _, _, token = env
    return token


def _create_user(admin_client, username, is_admin=False):
    r = admin_client.post(
        "/api/v1/users",
        json={"username": username, "is_admin": is_admin},
    )
    r.raise_for_status()
    return r.json()["token"]


def _user_client(app, token):
    c = TestClient(app)
    c.headers["Authorization"] = f"Bearer {token}"
    return c


def _create_ticket(client, owners=None):
    body = {
        "summary": "test ticket",
        "description": "test description",
    }
    if owners is not None:
        body["owners"] = owners
    r = client.post("/api/v1/tickets", json=body)
    r.raise_for_status()
    tid = r.json()["id"]
    client.post(
        f"/api/v1/tickets/{tid}/transition",
        json={"status": "triage_pending"},
    )
    return tid


class TestTicketCreation:
    def test_user_creates_ticket_is_owner(self, admin_client, app):
        token = _create_user(admin_client, "alice")
        c = _user_client(app, token)
        r = c.post(
            "/api/v1/tickets",
            json={"summary": "test", "description": "test"},
        )
        assert r.status_code == 200
        ticket = r.json()
        assert ticket["created_by"] == "alice"
        assert "alice" in ticket["owners"]

    def test_user_creates_ticket_with_extra_owners(self, admin_client, app):
        _create_user(admin_client, "alice")
        token_bob = _create_user(admin_client, "bob")
        c = _user_client(app, token_bob)
        r = c.post(
            "/api/v1/tickets",
            json={
                "summary": "test",
                "description": "test",
                "owners": ["alice"],
            },
        )
        assert r.status_code == 200
        ticket = r.json()
        assert ticket["created_by"] == "bob"
        assert "alice" in ticket["owners"]
        assert "bob" in ticket["owners"]

    def test_service_creates_ticket_unclaimed(self, admin_client):
        r = admin_client.post(
            "/api/v1/tickets",
            json={"summary": "test", "description": "test"},
        )
        assert r.status_code == 200
        ticket = r.json()
        assert ticket["created_by"] == ""
        assert ticket["owners"] == []

    def test_service_creates_ticket_with_owners(self, admin_client):
        _create_user(admin_client, "alice")
        r = admin_client.post(
            "/api/v1/tickets",
            json={
                "summary": "test",
                "description": "test",
                "owners": ["alice"],
            },
        )
        assert r.status_code == 200
        assert r.json()["owners"] == ["alice"]


class TestWriteGating:
    def test_owner_can_update_fields(self, admin_client, app):
        token = _create_user(admin_client, "alice")
        c = _user_client(app, token)
        tid = _create_ticket(c)

        r = c.patch(
            f"/api/v1/tickets/{tid}/fields",
            json={"fields": {"note": "hello"}},
        )
        assert r.status_code == 200

    def test_non_owner_cannot_update_fields(self, admin_client, app):
        token_alice = _create_user(admin_client, "alice")
        token_bob = _create_user(admin_client, "bob")
        c_alice = _user_client(app, token_alice)
        c_bob = _user_client(app, token_bob)
        tid = _create_ticket(c_alice)

        r = c_bob.patch(
            f"/api/v1/tickets/{tid}/fields",
            json={"fields": {"note": "hello"}},
        )
        assert r.status_code == 403

    def test_admin_can_update_any_ticket(self, admin_client, app):
        token = _create_user(admin_client, "alice")
        c = _user_client(app, token)
        tid = _create_ticket(c)

        r = admin_client.patch(
            f"/api/v1/tickets/{tid}/fields",
            json={"fields": {"note": "admin edit"}},
        )
        assert r.status_code == 200

    def test_service_can_update_any_ticket(self, admin_client, app):
        token = _create_user(admin_client, "alice")
        c = _user_client(app, token)
        tid = _create_ticket(c)

        r = admin_client.patch(
            f"/api/v1/tickets/{tid}/fields",
            json={"fields": {"note": "service edit"}},
        )
        assert r.status_code == 200

    def test_unclaimed_ticket_writable_by_anyone(self, admin_client, app):
        tid = _create_ticket(admin_client)
        token = _create_user(admin_client, "bob")
        c = _user_client(app, token)
        r = c.patch(
            f"/api/v1/tickets/{tid}/fields",
            json={"fields": {"note": "bob edit"}},
        )
        assert r.status_code == 200


class TestTransitionGating:
    def test_owner_can_transition(self, admin_client, app):
        token = _create_user(admin_client, "alice")
        c = _user_client(app, token)
        tid = _create_ticket(c)

        r = c.post(
            f"/api/v1/tickets/{tid}/transition",
            json={"status": "awaiting_hardware"},
        )
        assert r.status_code == 200

    def test_non_owner_cannot_transition(self, admin_client, app):
        token_alice = _create_user(admin_client, "alice")
        token_bob = _create_user(admin_client, "bob")
        c_alice = _user_client(app, token_alice)
        c_bob = _user_client(app, token_bob)
        tid = _create_ticket(c_alice)

        r = c_bob.post(
            f"/api/v1/tickets/{tid}/transition",
            json={"status": "awaiting_hardware"},
        )
        assert r.status_code == 403


class TestCommentGating:
    def test_owner_can_comment(self, admin_client, app):
        token = _create_user(admin_client, "alice")
        c = _user_client(app, token)
        tid = _create_ticket(c)

        r = c.post(
            f"/api/v1/tickets/{tid}/comments",
            json={"author": "alice", "body": "hello"},
        )
        assert r.status_code == 200

    def test_non_owner_cannot_comment(self, admin_client, app):
        token_alice = _create_user(admin_client, "alice")
        token_bob = _create_user(admin_client, "bob")
        c_alice = _user_client(app, token_alice)
        c_bob = _user_client(app, token_bob)
        tid = _create_ticket(c_alice)

        r = c_bob.post(
            f"/api/v1/tickets/{tid}/comments",
            json={"author": "bob", "body": "hello"},
        )
        assert r.status_code == 403

    def test_user_comment_author_overridden(self, admin_client, app):
        token = _create_user(admin_client, "alice")
        c = _user_client(app, token)
        tid = _create_ticket(c)

        r = c.post(
            f"/api/v1/tickets/{tid}/comments",
            json={"author": "spoofed", "body": "hello"},
        )
        assert r.status_code == 200
        comment = r.json()
        assert comment["author"] == "alice"

    def test_service_keeps_client_author(self, admin_client, app):
        tid = _create_ticket(admin_client)
        r = admin_client.post(
            f"/api/v1/tickets/{tid}/comments",
            json={"author": "triage-agent", "body": "agent comment"},
        )
        assert r.status_code == 200
        assert r.json()["author"] == "triage-agent"


class TestStopGating:
    def test_owner_can_stop(self, admin_client, app):
        token = _create_user(admin_client, "alice")
        c = _user_client(app, token)
        tid = _create_ticket(c)

        r = c.post(
            f"/api/v1/tickets/{tid}/stop",
            json={"mode": "graceful"},
        )
        assert r.status_code == 200

    def test_non_owner_cannot_stop(self, admin_client, app):
        token_alice = _create_user(admin_client, "alice")
        token_bob = _create_user(admin_client, "bob")
        c_alice = _user_client(app, token_alice)
        c_bob = _user_client(app, token_bob)
        tid = _create_ticket(c_alice)

        r = c_bob.post(
            f"/api/v1/tickets/{tid}/stop",
            json={"mode": "graceful"},
        )
        assert r.status_code == 403

    def test_stop_all_requires_admin(self, admin_client, app):
        token = _create_user(admin_client, "alice")
        c = _user_client(app, token)

        r = c.post("/api/v1/stop-all", json={"mode": "graceful"})
        assert r.status_code == 403

    def test_stop_all_admin_succeeds(self, admin_client):
        r = admin_client.post("/api/v1/stop-all", json={"mode": "graceful"})
        assert r.status_code == 200


class TestInterjectGating:
    def test_owner_can_interject(self, admin_client, app):
        token = _create_user(admin_client, "alice")
        c = _user_client(app, token)
        tid = _create_ticket(c)

        r = c.post(
            f"/api/v1/tickets/{tid}/interject",
            json={"message": "hello"},
        )
        assert r.status_code == 200

    def test_non_owner_cannot_interject(self, admin_client, app):
        token_alice = _create_user(admin_client, "alice")
        token_bob = _create_user(admin_client, "bob")
        c_alice = _user_client(app, token_alice)
        c_bob = _user_client(app, token_bob)
        tid = _create_ticket(c_alice)

        r = c_bob.post(
            f"/api/v1/tickets/{tid}/interject",
            json={"message": "hello"},
        )
        assert r.status_code == 403


class TestOwnerManagement:
    def test_add_owner(self, admin_client, app):
        token_alice = _create_user(admin_client, "alice")
        _create_user(admin_client, "bob")
        c_alice = _user_client(app, token_alice)
        tid = _create_ticket(c_alice)

        r = c_alice.put(f"/api/v1/tickets/{tid}/owners/bob")
        assert r.status_code == 200
        assert "bob" in r.json()["owners"]

    def test_remove_owner(self, admin_client, app):
        token_alice = _create_user(admin_client, "alice")
        _create_user(admin_client, "bob")
        c_alice = _user_client(app, token_alice)
        tid = _create_ticket(c_alice)

        c_alice.put(f"/api/v1/tickets/{tid}/owners/bob")

        r = c_alice.delete(f"/api/v1/tickets/{tid}/owners/bob")
        assert r.status_code == 200
        assert "bob" not in r.json()["owners"]

    def test_last_owner_protected(self, admin_client, app):
        token = _create_user(admin_client, "alice")
        c = _user_client(app, token)
        tid = _create_ticket(c)

        r = c.delete(f"/api/v1/tickets/{tid}/owners/alice")
        assert r.status_code == 409

    def test_non_owner_cannot_add_owner(self, admin_client, app):
        token_alice = _create_user(admin_client, "alice")
        token_bob = _create_user(admin_client, "bob")
        c_alice = _user_client(app, token_alice)
        c_bob = _user_client(app, token_bob)
        tid = _create_ticket(c_alice)

        r = c_bob.put(f"/api/v1/tickets/{tid}/owners/bob")
        assert r.status_code == 403

    def test_list_owners(self, admin_client, app):
        token = _create_user(admin_client, "alice")
        c = _user_client(app, token)
        tid = _create_ticket(c)

        r = c.get(f"/api/v1/tickets/{tid}/owners")
        assert r.status_code == 200
        assert "alice" in r.json()["owners"]


class TestClaimCarveout:
    def test_claim_unclaimed_self(self, admin_client, app):
        """Any user can add themselves to an unclaimed ticket."""
        _create_user(admin_client, "alice")
        tid = _create_ticket(admin_client)

        token = _create_user(admin_client, "bob")
        c = _user_client(app, token)

        r = c.put(f"/api/v1/tickets/{tid}/owners/bob")
        assert r.status_code == 200
        assert "bob" in r.json()["owners"]

    def test_cannot_add_others_to_unclaimed(self, admin_client, app):
        """User cannot add someone else to an unclaimed ticket."""
        _create_user(admin_client, "alice")
        token_bob = _create_user(admin_client, "bob")
        tid = _create_ticket(admin_client)

        c = _user_client(app, token_bob)
        r = c.put(f"/api/v1/tickets/{tid}/owners/alice")
        assert r.status_code == 403

    def test_cannot_claim_owned_ticket(self, admin_client, app):
        """User cannot add themselves to a ticket owned by someone else."""
        token_alice = _create_user(admin_client, "alice")
        token_bob = _create_user(admin_client, "bob")
        c_alice = _user_client(app, token_alice)
        tid = _create_ticket(c_alice)

        c_bob = _user_client(app, token_bob)
        r = c_bob.put(f"/api/v1/tickets/{tid}/owners/bob")
        assert r.status_code == 403

    def test_admin_can_add_anyone_to_unclaimed(self, admin_client, app):
        _create_user(admin_client, "alice")
        tid = _create_ticket(admin_client)

        r = admin_client.put(f"/api/v1/tickets/{tid}/owners/alice")
        assert r.status_code == 200
        assert "alice" in r.json()["owners"]


class TestLegacyMode:
    """When multi_user=False, all write gating is disabled."""

    @pytest.fixture()
    def legacy_client(self):
        app = (
            create_app.__wrapped__()
            if hasattr(create_app, "__wrapped__")
            else create_app()
        )
        token = app.state.api_token
        c = TestClient(app)
        c.headers["Authorization"] = f"Bearer {token}"
        return c

    def test_legacy_no_gating(self, legacy_client):
        r = legacy_client.post(
            "/api/v1/tickets",
            json={"summary": "test", "description": "test"},
        )
        r.raise_for_status()
        tid = r.json()["id"]

        legacy_client.post(
            f"/api/v1/tickets/{tid}/transition",
            json={"status": "triage_pending"},
        )

        r = legacy_client.patch(
            f"/api/v1/tickets/{tid}/fields",
            json={"fields": {"note": "hello"}},
        )
        assert r.status_code == 200

    def test_legacy_stop_all_no_admin_required(self, legacy_client):
        r = legacy_client.post(
            "/api/v1/stop-all",
            json={"mode": "graceful"},
        )
        assert r.status_code == 200


class TestOwnersPersistence:
    def test_owners_survive_serialization(self, admin_client, app):
        token = _create_user(admin_client, "alice")
        c = _user_client(app, token)
        tid = _create_ticket(c)

        r = c.get(f"/api/v1/tickets/{tid}")
        assert r.status_code == 200
        ticket = r.json()
        assert "alice" in ticket["owners"]
        assert ticket["created_by"] == "alice"

    def test_add_owner_idempotent(self, admin_client, app):
        token = _create_user(admin_client, "alice")
        c = _user_client(app, token)
        tid = _create_ticket(c)

        r = c.put(f"/api/v1/tickets/{tid}/owners/alice")
        assert r.status_code == 200
        assert r.json()["owners"].count("alice") == 1

    def test_add_nonexistent_user_rejected(self, admin_client, app):
        token = _create_user(admin_client, "alice")
        c = _user_client(app, token)
        tid = _create_ticket(c)

        r = c.put(f"/api/v1/tickets/{tid}/owners/ghost")
        assert r.status_code == 404

    def test_remove_nonowner_rejected(self, admin_client, app):
        token = _create_user(admin_client, "alice")
        c = _user_client(app, token)
        tid = _create_ticket(c)

        r = c.delete(f"/api/v1/tickets/{tid}/owners/bob")
        assert r.status_code == 404


class TestAbortGating:
    def test_non_owner_cannot_abort(self, admin_client, app):
        token_alice = _create_user(admin_client, "alice")
        token_bob = _create_user(admin_client, "bob")
        c_alice = _user_client(app, token_alice)
        c_bob = _user_client(app, token_bob)

        r = c_alice.post(
            "/api/v1/tickets",
            json={"summary": "t", "description": "t"},
        )
        tid = r.json()["id"]
        c_alice.post(
            f"/api/v1/tickets/{tid}/transition",
            json={"status": "triage_pending"},
        )
        admin_client.post(
            f"/api/v1/tickets/{tid}/transition",
            json={"status": "awaiting_customer_guidance"},
        )

        r = c_bob.post(f"/api/v1/tickets/{tid}/abort")
        assert r.status_code == 403
