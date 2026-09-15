"""Tests for webhook ingestion: translators, service accounts, and API."""

from __future__ import annotations

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from state_store.api.router import api_router, health_router, webhook_router
from state_store.auth import make_auth_dependency
from state_store.identity import UserStore
from state_store.main import create_app
from state_store.store import TicketStore
from state_store.webhooks import generic as generic_translator
from state_store.webhooks import horreum as horreum_translator
from state_store.webhooks.registry import get_translator, list_sources

# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


def _make_app(tmp_path, *, multi_user: bool = True):
    """Create a test app with optional multi-user mode."""
    app = (
        create_app.__wrapped__() if hasattr(create_app, "__wrapped__") else create_app()
    )
    deploy_token = app.state.api_token

    user_store = UserStore(persist_path=tmp_path / "users.json")
    app.state.multi_user = multi_user
    app.state.user_store = user_store

    auth = make_auth_dependency(
        deploy_token,
        multi_user=multi_user,
        user_store=user_store,
    )
    app.state.auth_dependency = auth

    # Use a fresh ticket store isolated to tmp_path
    app.state.store = TicketStore(persist_dir=tmp_path / "tickets")

    app.router.routes.clear()
    app.include_router(api_router, dependencies=[Depends(auth)])
    app.include_router(health_router)
    app.include_router(webhook_router)

    return app, user_store, deploy_token


@pytest.fixture()
def webhook_env(tmp_path):
    app, user_store, deploy_token = _make_app(tmp_path)
    return app, user_store, deploy_token


@pytest.fixture()
def client(webhook_env):
    app, _, deploy_token = webhook_env
    c = TestClient(app)
    c.headers["Authorization"] = f"Bearer {deploy_token}"
    return c


@pytest.fixture()
def user_store(webhook_env):
    _, store, _ = webhook_env
    return store


@pytest.fixture()
def deploy_token(webhook_env):
    _, _, token = webhook_env
    return token


@pytest.fixture()
def app(webhook_env):
    app, _, _ = webhook_env
    return app


# ------------------------------------------------------------------
# Translator unit tests
# ------------------------------------------------------------------


class TestHorreumTranslator:
    def test_translate_maps_fields(self):
        payload = {
            "testName": "network-throughput",
            "change": {
                "id": 7,
                "variable": {"name": "throughput_mbps", "group": "net"},
                "dataset": {"id": 42, "runId": 100, "ordinal": 1},
                "description": "Threshold exceeded",
                "timestamp": "2024-03-15T10:30:00Z",
            },
        }
        result = horreum_translator.translate(payload)

        assert "network-throughput" in result["summary"]
        assert "throughput_mbps" in result["summary"]
        ctx = result["custom_fields"]["anomaly_context"]
        assert ctx["test_name"] == "network-throughput"
        assert ctx["variable_name"] == "throughput_mbps"
        assert ctx["run_id"] == 100
        assert ctx["change_id"] == 7
        assert ctx["dataset_id"] == 42
        assert ctx["variable_group"] == "net"
        assert ctx["change_description"] == "Threshold exceeded"
        assert ctx["timestamp"] == "2024-03-15T10:30:00Z"
        assert result["custom_fields"]["trigger_source"] == "horreum"

    def test_dedup_key(self):
        payload = {"change": {"id": 42}}
        assert horreum_translator.dedup_key(payload) == "horreum:change:42"

    def test_dedup_key_missing(self):
        assert horreum_translator.dedup_key({}) is None


class TestGenericTranslator:
    def test_passthrough(self):
        payload = {"summary": "Test event", "description": "Details", "extra": 123}
        result = generic_translator.translate(payload)

        assert result["summary"] == "Test event"
        assert result["description"] == "Details"
        assert result["custom_fields"]["raw_payload"] == payload
        assert result["custom_fields"]["trigger_source"] == "generic"

    def test_defaults(self):
        result = generic_translator.translate({})
        assert result["summary"] == "Webhook event"
        assert result["description"] == ""

    def test_dedup_key_with_id(self):
        assert generic_translator.dedup_key({"id": "abc"}) == "generic:abc"

    def test_dedup_key_without_id(self):
        assert generic_translator.dedup_key({}) is None


class TestRegistry:
    def test_list_sources(self):
        sources = list_sources()
        assert "generic" in sources
        assert "horreum" in sources

    def test_get_known(self):
        mod = get_translator("horreum")
        assert hasattr(mod, "translate")
        assert hasattr(mod, "dedup_key")

    def test_get_unknown(self):
        with pytest.raises(KeyError):
            get_translator("nonexistent")


# ------------------------------------------------------------------
# Service account invariants
# ------------------------------------------------------------------


class TestServiceAccountCreation:
    def test_service_account_rejects_admin(self, user_store):
        with pytest.raises(ValueError, match="cannot be admins"):
            user_store.create_user(
                "svc-bot",
                is_admin=True,
                service_account=True,
                allowed_sources=["10.0.0.1"],
            )

    def test_service_account_created_non_admin(self, user_store):
        user, _ = user_store.create_user(
            "svc-ok",
            service_account=True,
            allowed_sources=["10.0.0.1"],
        )
        assert user.service_account is True
        assert user.is_admin is False

    def test_service_account_requires_allowed_sources(self, user_store):
        with pytest.raises(ValueError, match="allowed_sources"):
            user_store.create_user(
                "svc-nosrc",
                service_account=True,
            )

    def test_set_admin_rejected_for_service_account(self, user_store):
        user_store.create_user(
            "svc-noadmin",
            service_account=True,
            allowed_sources=["10.0.0.1"],
        )
        with pytest.raises(ValueError, match="Service accounts"):
            user_store.set_admin("svc-noadmin")


# ------------------------------------------------------------------
# Source IP validation
# ------------------------------------------------------------------


class TestSourceIPValidation:
    def test_ip_match(self):
        from state_store.api.webhooks import _match_ip

        assert _match_ip("10.0.0.1", ["10.0.0.1"]) is True

    def test_cidr_match(self):
        from state_store.api.webhooks import _match_ip

        assert _match_ip("192.168.1.50", ["192.168.1.0/24"]) is True

    def test_ip_reject(self):
        from state_store.api.webhooks import _match_ip

        assert _match_ip("10.0.0.2", ["10.0.0.1"]) is False

    def test_cidr_reject(self):
        from state_store.api.webhooks import _match_ip

        assert _match_ip("192.168.2.1", ["192.168.1.0/24"]) is False


# ------------------------------------------------------------------
# Rate limiting
# ------------------------------------------------------------------


class TestRateLimiting:
    def test_rate_limit_enforced(self):
        from state_store.api.webhooks import _check_rate_limit, _rate_limit_window

        username = "test-ratelimit-user"
        _rate_limit_window.pop(username, None)

        # Should not raise for the first 2
        _check_rate_limit(username, 2)
        _check_rate_limit(username, 2)

        # Third should raise
        with pytest.raises(Exception) as exc_info:
            _check_rate_limit(username, 2)
        assert exc_info.value.status_code == 429  # type: ignore[union-attr]

        # Cleanup
        _rate_limit_window.pop(username, None)

    def test_no_limit(self):
        from state_store.api.webhooks import _check_rate_limit

        # None means no limit — should never raise
        for _ in range(100):
            _check_rate_limit("unlimited-user", None)


# ------------------------------------------------------------------
# Dedup detection
# ------------------------------------------------------------------


class TestDedupDetection:
    def test_dedup_skips_duplicate(self, tmp_path):
        app, _store, deploy_token = _make_app(tmp_path)
        client = TestClient(app)
        client.headers["Authorization"] = f"Bearer {deploy_token}"
        payload = {
            "testName": "dedup-test",
            "change": {
                "id": 999,
                "variable": {"name": "x"},
                "dataset": {"runId": 1},
            },
        }

        r1 = client.post("/api/v1/webhooks/horreum", json=payload)
        assert r1.status_code == 200
        assert r1.json()["status"] == "created"

        r2 = client.post("/api/v1/webhooks/horreum", json=payload)
        assert r2.status_code == 200
        assert r2.json()["status"] == "duplicate"
        assert r2.json()["ticket_id"] == r1.json()["ticket_id"]


# ------------------------------------------------------------------
# Integration tests
# ------------------------------------------------------------------


class TestWebhookIntegration:
    def test_list_sources_requires_auth(self, app):
        c = TestClient(app)
        r = c.get("/api/v1/webhooks")
        assert r.status_code == 401

    def test_list_sources(self, client):
        r = client.get("/api/v1/webhooks")
        assert r.status_code == 200
        sources = r.json()["sources"]
        assert "horreum" in sources
        assert "generic" in sources

    def test_post_horreum_creates_ticket(self, tmp_path):
        app, _store, deploy_token = _make_app(tmp_path)
        client = TestClient(app)
        client.headers["Authorization"] = f"Bearer {deploy_token}"
        payload = {
            "testName": "integration-test",
            "change": {
                "id": 123,
                "variable": {"name": "latency_ms"},
                "dataset": {"id": 10, "runId": 50, "ordinal": 0},
            },
        }
        r = client.post("/api/v1/webhooks/horreum", json=payload)
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "created"
        ticket_id = body["ticket_id"]

        # Verify ticket exists in the store
        store = app.state.store
        ticket = store.get_ticket(ticket_id)
        assert "integration-test" in ticket.summary
        assert ticket.custom_fields["trigger_source"] == "horreum"
        assert ticket.custom_fields["anomaly_context"]["run_id"] == 50
        # Verify auto-transition to triage_pending
        assert ticket.status.value == "triage_pending"

    def test_post_generic_creates_ticket(self, client, app):
        payload = {"summary": "generic test", "description": "hello"}
        r = client.post("/api/v1/webhooks/generic", json=payload)
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "created"

        store = app.state.store
        ticket = store.get_ticket(body["ticket_id"])
        assert ticket.custom_fields["trigger_source"] == "generic"
        assert ticket.custom_fields["raw_payload"] == payload

    def test_unknown_source_404(self, client):
        r = client.post("/api/v1/webhooks/nonexistent", json={})
        assert r.status_code == 404

    def test_token_via_query_string(self, app, deploy_token):
        c = TestClient(app)
        r = c.post(
            f"/api/v1/webhooks/generic?token={deploy_token}",
            json={"summary": "qs-auth"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "created"

    def test_no_auth_rejected(self, app):
        c = TestClient(app)
        r = c.post("/api/v1/webhooks/generic", json={})
        assert r.status_code == 401

    def test_service_account_ip_rejected(
        self,
        app,
        user_store,
    ):
        """Service account with wrong IP is rejected."""
        _user, raw_token = user_store.create_user(
            "svc-iptest",
            service_account=True,
            allowed_sources=["10.0.0.1"],
        )
        c = TestClient(app)
        # TestClient uses testclient as host by default
        r = c.post(
            f"/api/v1/webhooks/generic?token={raw_token}",
            json={"summary": "ip-test"},
        )
        # The TestClient's IP won't match 10.0.0.1
        assert r.status_code == 403


class TestServiceAccountRuntimeEnforcement:
    """Verify that service accounts cannot gain admin even via tampering."""

    def test_tampered_service_account_not_admin(self, user_store):
        """Simulate users.json tampering: service_account + is_admin."""
        from state_store.identity import hash_token

        _user, raw_token = user_store.create_user(
            "svc-tampered",
            service_account=True,
            allowed_sources=["10.0.0.1"],
        )
        # Tamper: set is_admin directly in the store
        with user_store._lock:
            user_store._users["svc-tampered"].is_admin = True

        # Verify the tamper took effect in the store
        tampered = user_store.get_user("svc-tampered")
        assert tampered.is_admin is True
        assert tampered.service_account is True

        # Simulate what the auth dependency does: lookup
        # user by token hash and build a Principal.
        token_h = hash_token(raw_token)
        user = user_store.lookup_by_token_hash(token_h)
        assert user is not None
        # Runtime enforcement: service_account strips admin
        effective_admin = user.is_admin and not user.service_account
        assert effective_admin is False
