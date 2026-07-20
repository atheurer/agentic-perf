"""API-level tests for user, group, and whoami endpoints."""

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
def multi_user_env(tmp_path):
    """App, user_store, and deployment token for multi-user tests."""
    app, user_store, deploy_token = _make_multi_user_app(tmp_path)
    return app, user_store, deploy_token


@pytest.fixture()
def admin_client(multi_user_env):
    """TestClient authenticated with the deployment token (admin)."""
    app, _store, deploy_token = multi_user_env
    c = TestClient(app)
    c.headers["Authorization"] = f"Bearer {deploy_token}"
    return c


@pytest.fixture()
def user_store(multi_user_env):
    _, store, _ = multi_user_env
    return store


@pytest.fixture()
def deploy_token(multi_user_env):
    _, _, token = multi_user_env
    return token


@pytest.fixture()
def app(multi_user_env):
    app, _, _ = multi_user_env
    return app


class TestUserCRUD:
    def test_create_user(self, admin_client):
        r = admin_client.post(
            "/api/v1/users",
            json={"username": "alice"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["user"]["username"] == "alice"
        assert "token" in data
        assert len(data["token"]) == 64
        assert "token_hash" not in data["user"]

    def test_create_admin_user(self, admin_client):
        r = admin_client.post(
            "/api/v1/users",
            json={"username": "superadmin", "is_admin": True},
        )
        assert r.status_code == 200
        assert r.json()["user"]["is_admin"] is True

    def test_create_duplicate_returns_409(self, admin_client):
        admin_client.post("/api/v1/users", json={"username": "alice"})
        r = admin_client.post("/api/v1/users", json={"username": "alice"})
        assert r.status_code == 409

    def test_create_invalid_username_returns_422(self, admin_client):
        r = admin_client.post(
            "/api/v1/users",
            json={"username": "bad name!"},
        )
        assert r.status_code == 422

    def test_list_users(self, admin_client):
        admin_client.post("/api/v1/users", json={"username": "alice"})
        admin_client.post("/api/v1/users", json={"username": "bob"})
        r = admin_client.get("/api/v1/users")
        assert r.status_code == 200
        names = {u["username"] for u in r.json()}
        assert names == {"alice", "bob"}
        for u in r.json():
            assert "token_hash" not in u

    def test_disable_enable_user(self, admin_client):
        admin_client.post("/api/v1/users", json={"username": "alice"})
        r = admin_client.post("/api/v1/users/alice/disable")
        assert r.status_code == 200
        assert r.json()["disabled"] is True

        r = admin_client.post("/api/v1/users/alice/enable")
        assert r.status_code == 200
        assert r.json()["disabled"] is False

    def test_grant_revoke_admin(self, admin_client):
        admin_client.post("/api/v1/users", json={"username": "alice"})
        r = admin_client.post("/api/v1/users/alice/admin")
        assert r.status_code == 200
        assert r.json()["is_admin"] is True

        r = admin_client.delete("/api/v1/users/alice/admin")
        assert r.status_code == 200
        assert r.json()["is_admin"] is False

    def test_rotate_token(self, admin_client):
        r = admin_client.post("/api/v1/users", json={"username": "alice"})
        old_token = r.json()["token"]

        r = admin_client.post("/api/v1/users/alice/rotate-token")
        assert r.status_code == 200
        new_token = r.json()["token"]
        assert new_token != old_token


class TestNonAdminAccess:
    def _make_user_client(self, admin_client, app, username="alice"):
        r = admin_client.post(
            "/api/v1/users",
            json={"username": username},
        )
        token = r.json()["token"]
        c = TestClient(app)
        c.headers["Authorization"] = f"Bearer {token}"
        return c

    def test_non_admin_can_list_users(self, admin_client, app):
        c = self._make_user_client(admin_client, app)
        r = c.get("/api/v1/users")
        assert r.status_code == 200

    def test_non_admin_can_list_groups(self, admin_client, app):
        c = self._make_user_client(admin_client, app)
        r = c.get("/api/v1/groups")
        assert r.status_code == 200

    def test_non_admin_cannot_create_user(self, admin_client, app):
        c = self._make_user_client(admin_client, app)
        r = c.post("/api/v1/users", json={"username": "bob"})
        assert r.status_code == 403

    def test_non_admin_cannot_disable_user(self, admin_client, app):
        c = self._make_user_client(admin_client, app)
        r = c.post("/api/v1/users/alice/disable")
        assert r.status_code == 403

    def test_non_admin_cannot_enable_user(self, admin_client, app):
        c = self._make_user_client(admin_client, app)
        r = c.post("/api/v1/users/alice/enable")
        assert r.status_code == 403

    def test_non_admin_cannot_grant_admin(self, admin_client, app):
        c = self._make_user_client(admin_client, app)
        r = c.post("/api/v1/users/alice/admin")
        assert r.status_code == 403

    def test_non_admin_can_rotate_own_token(self, admin_client, app):
        c = self._make_user_client(admin_client, app)
        r = c.post("/api/v1/users/alice/rotate-token")
        assert r.status_code == 200

    def test_non_admin_cannot_rotate_others_token(
        self,
        admin_client,
        app,
    ):
        admin_client.post("/api/v1/users", json={"username": "bob"})
        c = self._make_user_client(admin_client, app)
        r = c.post("/api/v1/users/bob/rotate-token")
        assert r.status_code == 403

    def test_non_admin_cannot_create_group(self, admin_client, app):
        c = self._make_user_client(admin_client, app)
        r = c.post("/api/v1/groups", json={"name": "devs"})
        assert r.status_code == 403

    def test_non_admin_cannot_delete_group(self, admin_client, app):
        admin_client.post("/api/v1/groups", json={"name": "devs"})
        c = self._make_user_client(admin_client, app)
        r = c.delete("/api/v1/groups/devs")
        assert r.status_code == 403

    def test_non_admin_cannot_add_member(self, admin_client, app):
        admin_client.post("/api/v1/groups", json={"name": "devs"})
        c = self._make_user_client(admin_client, app)
        r = c.put("/api/v1/groups/devs/members/alice")
        assert r.status_code == 403


class TestGroupAPI:
    def test_create_list_delete_group(self, admin_client):
        r = admin_client.post(
            "/api/v1/groups",
            json={"name": "devs", "description": "developers"},
        )
        assert r.status_code == 200
        assert r.json()["name"] == "devs"

        r = admin_client.get("/api/v1/groups")
        assert r.status_code == 200
        assert len(r.json()) == 1

        r = admin_client.delete("/api/v1/groups/devs")
        assert r.status_code == 200

        r = admin_client.get("/api/v1/groups")
        assert r.json() == []

    def test_add_remove_member(self, admin_client):
        admin_client.post("/api/v1/users", json={"username": "alice"})
        admin_client.post("/api/v1/groups", json={"name": "devs"})

        r = admin_client.put("/api/v1/groups/devs/members/alice")
        assert r.status_code == 200

        r = admin_client.get("/api/v1/users")
        alice = [u for u in r.json() if u["username"] == "alice"][0]
        assert "devs" in alice["groups"]

        r = admin_client.delete("/api/v1/groups/devs/members/alice")
        assert r.status_code == 200


class TestWhoami:
    def test_whoami_with_deployment_token(self, admin_client):
        r = admin_client.get("/api/v1/whoami")
        assert r.status_code == 200
        data = r.json()
        assert data["kind"] == "service"
        assert data["username"] == "deployment"
        assert data["is_admin"] is True

    def test_whoami_with_user_token(self, admin_client, app):
        r = admin_client.post(
            "/api/v1/users",
            json={"username": "alice"},
        )
        token = r.json()["token"]
        c = TestClient(app)
        c.headers["Authorization"] = f"Bearer {token}"
        r = c.get("/api/v1/whoami")
        assert r.status_code == 200
        data = r.json()
        assert data["kind"] == "user"
        assert data["username"] == "alice"
        assert data["is_admin"] is False


class TestMultiUserAuth:
    def test_disabled_user_rejected(self, admin_client, app):
        r = admin_client.post(
            "/api/v1/users",
            json={"username": "alice"},
        )
        token = r.json()["token"]
        admin_client.post("/api/v1/users/alice/disable")

        c = TestClient(app)
        c.headers["Authorization"] = f"Bearer {token}"
        r = c.get("/api/v1/whoami")
        assert r.status_code == 401

    def test_re_enabled_user_works(self, admin_client, app):
        r = admin_client.post(
            "/api/v1/users",
            json={"username": "alice"},
        )
        token = r.json()["token"]
        admin_client.post("/api/v1/users/alice/disable")
        admin_client.post("/api/v1/users/alice/enable")

        c = TestClient(app)
        c.headers["Authorization"] = f"Bearer {token}"
        r = c.get("/api/v1/whoami")
        assert r.status_code == 200
        assert r.json()["username"] == "alice"

    def test_wrong_token_rejected(self, app):
        c = TestClient(app)
        c.headers["Authorization"] = "Bearer wrongtoken"
        r = c.get("/api/v1/whoami")
        assert r.status_code == 401

    def test_deployment_token_still_works(self, admin_client):
        r = admin_client.get("/api/v1/whoami")
        assert r.status_code == 200
        assert r.json()["kind"] == "service"

    def test_no_token_hash_in_create_response(self, admin_client):
        r = admin_client.post(
            "/api/v1/users",
            json={"username": "alice"},
        )
        assert "token_hash" not in r.json()["user"]

    def test_no_token_hash_in_list_response(self, admin_client):
        admin_client.post("/api/v1/users", json={"username": "alice"})
        r = admin_client.get("/api/v1/users")
        for user in r.json():
            assert "token_hash" not in user

    def test_bootstrap_with_deployment_token(self, admin_client):
        r = admin_client.post(
            "/api/v1/users",
            json={"username": "first-admin", "is_admin": True},
        )
        assert r.status_code == 200
        assert r.json()["user"]["is_admin"] is True
