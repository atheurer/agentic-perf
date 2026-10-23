"""Tests for the PATCH /users/{username} endpoint."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from state_store.identity import UserStore
from state_store.main import create_app


@pytest.fixture()
def multi_user_env(tmp_path):
    """App, user_store, and deployment token for multi-user tests."""
    user_store = UserStore(persist_path=tmp_path / "users.json")

    from state_store.auth import make_auth_dependency
    from state_store.main import mount_routers

    app = (
        create_app.__wrapped__(initialize_immediately=True)
        if hasattr(create_app, "__wrapped__")
        else create_app(initialize_immediately=True)
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

    app.router.routes.clear()
    mount_routers(app, auth)

    return app, user_store, token


@pytest.fixture()
def admin_client(multi_user_env):
    """TestClient authenticated with the deployment token (admin)."""
    app, _store, deploy_token = multi_user_env
    c = TestClient(app)
    c.headers["Authorization"] = f"Bearer {deploy_token}"
    return c


@pytest.fixture()
def app(multi_user_env):
    app, _, _ = multi_user_env
    return app


class TestPatchUserValidation:
    def test_negative_max_requests_per_hour_returns_422(self, admin_client):
        admin_client.post(
            "/api/v1/users",
            json={
                "username": "alice",
                "service_account": True,
                "allowed_sources": ["test"],
            },
        )
        r = admin_client.patch(
            "/api/v1/users/alice",
            json={"max_requests_per_hour": -5},
        )
        assert r.status_code == 422
        assert "non-negative" in r.json()["detail"].lower()

    def test_explicit_null_allowed_sources_returns_422(self, admin_client):
        admin_client.post("/api/v1/users", json={"username": "alice"})
        r = admin_client.patch(
            "/api/v1/users/alice",
            json={"allowed_sources": None},
        )
        assert r.status_code == 422
        assert "allowed_sources" in r.json()["detail"]

    def test_omitted_fields_are_unchanged(self, admin_client):
        create_r = admin_client.post(
            "/api/v1/users",
            json={
                "username": "alice-omit",
                "service_account": True,
                "allowed_sources": ["src1"],
                "max_requests_per_hour": 100,
            },
        )
        assert create_r.status_code == 200, f"create failed: {create_r.text}"
        # Only update allowed_sources; max_requests_per_hour should stay 100
        r = admin_client.patch(
            "/api/v1/users/alice-omit",
            json={"allowed_sources": ["src1", "src2"]},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["allowed_sources"] == ["src1", "src2"]
        assert data["max_requests_per_hour"] == 100

    def test_successful_update_returns_updated_values(self, admin_client):
        create_r = admin_client.post(
            "/api/v1/users",
            json={
                "username": "alice-update",
                "service_account": True,
                "allowed_sources": ["test"],
            },
        )
        assert create_r.status_code == 200, f"create failed: {create_r.text}"
        r = admin_client.patch(
            "/api/v1/users/alice-update",
            json={
                "allowed_sources": ["new-source"],
                "max_requests_per_hour": 50,
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["allowed_sources"] == ["new-source"]
        assert data["max_requests_per_hour"] == 50
