"""Tests for anonymous read-only access.

Uses route clearing + re-mounting to avoid module-level router
pollution between tests (the api_router is a FastAPI singleton).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


def _make_app(*, anonymous_read: bool):
    """Create a state store app with controlled auth config.

    Clears and re-mounts routes to avoid module-level router
    pollution between tests (the api_router is a singleton).
    """
    from state_store.auth import make_auth_dependency
    from state_store.main import create_app, mount_routers

    app = create_app()
    token = app.state.api_token

    auth = make_auth_dependency(
        token,
        anonymous_read=anonymous_read,
    )
    app.state.auth_dependency = auth
    app.state.anonymous_read = anonymous_read

    app.router.routes.clear()
    mount_routers(app, auth)

    return app


class TestAnonymousReadEnabled:
    @pytest.fixture
    def app(self):
        return _make_app(anonymous_read=True)

    @pytest.fixture
    def anon_client(self, app):
        return TestClient(app)

    @pytest.fixture
    def authed_client(self, app):
        c = TestClient(app)
        c.headers["Authorization"] = f"Bearer {app.state.api_token}"
        return c

    def test_get_tickets_without_token(self, anon_client):
        r = anon_client.get("/api/v1/tickets")
        assert r.status_code == 200

    def test_get_ticket_without_token(self, authed_client, anon_client):
        r = authed_client.post(
            "/api/v1/tickets",
            json={"summary": "test", "description": "test"},
        )
        assert r.status_code == 200
        ticket_id = r.json()["id"]

        r = anon_client.get(f"/api/v1/tickets/{ticket_id}")
        assert r.status_code == 200
        assert r.json()["id"] == ticket_id

    def test_post_ticket_without_token_rejected(self, anon_client):
        r = anon_client.post(
            "/api/v1/tickets",
            json={"summary": "test", "description": "test"},
        )
        assert r.status_code == 401

    def test_transition_without_token_rejected(self, authed_client, anon_client):
        r = authed_client.post(
            "/api/v1/tickets",
            json={"summary": "test", "description": "test"},
        )
        ticket_id = r.json()["id"]

        r = anon_client.post(
            f"/api/v1/tickets/{ticket_id}/transition",
            json={"status": "triage_pending"},
        )
        assert r.status_code == 401

    def test_patch_fields_without_token_rejected(self, authed_client, anon_client):
        r = authed_client.post(
            "/api/v1/tickets",
            json={"summary": "test", "description": "test"},
        )
        ticket_id = r.json()["id"]

        r = anon_client.patch(
            f"/api/v1/tickets/{ticket_id}/fields",
            json={"fields": {"foo": "bar"}},
        )
        assert r.status_code == 401

    def test_get_events_without_token(self, authed_client, anon_client):
        r = authed_client.post(
            "/api/v1/tickets",
            json={"summary": "test", "description": "test"},
        )
        ticket_id = r.json()["id"]

        r = anon_client.get(f"/api/v1/tickets/{ticket_id}/events")
        assert r.status_code == 200


class TestAnonymousReadDisabled:
    """Verify default behavior via auth dependency unit tests."""

    async def test_auth_dependency_rejects_unauthenticated_get(self):
        from unittest.mock import AsyncMock

        from fastapi import HTTPException

        from state_store.auth import make_auth_dependency

        verify = make_auth_dependency("test-token", anonymous_read=False)

        request = AsyncMock()
        request.method = "GET"
        request.headers = {}

        with pytest.raises(HTTPException) as exc_info:
            await verify(request)
        assert exc_info.value.status_code == 401

    async def test_auth_dependency_allows_anonymous_get(self):
        from unittest.mock import AsyncMock

        from state_store.auth import ANONYMOUS_PRINCIPAL, make_auth_dependency

        verify = make_auth_dependency("test-token", anonymous_read=True)

        request = AsyncMock()
        request.method = "GET"
        request.headers = {}
        request.state = type("State", (), {})()

        result = await verify(request)
        assert result == ANONYMOUS_PRINCIPAL
