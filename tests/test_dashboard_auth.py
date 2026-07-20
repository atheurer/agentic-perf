"""Tests for dashboard token injection in multi-user vs legacy mode."""

from __future__ import annotations

import pytest
from fastapi import Depends
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from state_store.identity import UserStore
from state_store.main import STATIC_DIR, create_app


def _make_multi_user_app(tmp_path):
    from state_store.api.router import api_router, health_router
    from state_store.auth import make_auth_dependency

    app = (
        create_app.__wrapped__() if hasattr(create_app, "__wrapped__") else create_app()
    )
    token = app.state.api_token

    user_store = UserStore(persist_path=tmp_path / "users.json")
    app.state.multi_user = True
    app.state.user_store = user_store

    auth = make_auth_dependency(
        token,
        multi_user=True,
        user_store=user_store,
    )
    app.state.auth_dependency = auth

    app.router.routes.clear()
    app.include_router(api_router, dependencies=[Depends(auth)])
    app.include_router(health_router)

    if STATIC_DIR.is_dir():
        app.mount(
            "/static",
            StaticFiles(directory=str(STATIC_DIR)),
            name="static",
        )

        @app.get("/")
        def serve_dashboard():
            index_path = STATIC_DIR / "index.html"
            html = index_path.read_text()
            token_script = '<script>window.API_TOKEN="";</script>'
            html = html.replace("</head>", f"{token_script}</head>", 1)
            return HTMLResponse(
                content=html,
                headers={"Cache-Control": "no-cache"},
            )

    return app, user_store, token


class TestDashboardTokenInjection:
    @pytest.fixture()
    def legacy_app(self):
        app = (
            create_app.__wrapped__()
            if hasattr(create_app, "__wrapped__")
            else create_app()
        )
        return app

    @pytest.fixture()
    def multi_user_env(self, tmp_path):
        return _make_multi_user_app(tmp_path)

    def test_legacy_dashboard_contains_token(self, legacy_app):
        client = TestClient(legacy_app)
        r = client.get("/")
        assert r.status_code == 200
        html = r.text
        token = legacy_app.state.api_token
        assert f'window.API_TOKEN="{token}"' in html

    def test_multi_user_dashboard_no_token(self, multi_user_env):
        app, _, token = multi_user_env
        client = TestClient(app)
        r = client.get("/")
        assert r.status_code == 200
        html = r.text
        assert token not in html
        assert 'window.API_TOKEN=""' in html

    def test_whoami_user_identity(self, multi_user_env):
        app, user_store, _ = multi_user_env
        client = TestClient(app)

        _, user_token = user_store.create_user("testuser")
        r = client.get(
            "/api/v1/whoami",
            headers={"Authorization": f"Bearer {user_token}"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["kind"] == "user"
        assert data["username"] == "testuser"

    def test_whoami_service_identity(self, multi_user_env):
        app, _, token = multi_user_env
        client = TestClient(app)
        r = client.get(
            "/api/v1/whoami",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["kind"] == "service"
