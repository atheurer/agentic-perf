"""Tests for state store API authentication."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from state_store.main import create_app


@pytest.fixture
def app(tmp_path):
    application = create_app()
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
        from unittest.mock import MagicMock

        from state_store.auth import make_auth_dependency

        dep = make_auth_dependency(app.state.api_token)
        mock_request = MagicMock()
        mock_request.headers = {"authorization": "Bearer tést-tokën"}
        with pytest.raises(HTTPException) as exc_info:
            await dep(mock_request)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_empty_bearer_rejected(self):
        """Empty bearer token is rejected before any comparison."""
        from unittest.mock import MagicMock

        from state_store.auth import make_auth_dependency

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
        from unittest.mock import MagicMock

        from state_store.auth import make_auth_dependency

        dep = make_auth_dependency("")
        mock_request = MagicMock()
        mock_request.headers = {"authorization": "Bearer some-token"}
        with pytest.raises(HTTPException) as exc_info:
            await dep(mock_request)
        assert exc_info.value.status_code == 401
