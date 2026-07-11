"""Tests for state store API authentication."""

from __future__ import annotations

import pytest
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
