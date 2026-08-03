"""Tests for rate limiting: token bucket, per-principal, auth-failure.

Uses an injectable clock for deterministic timing — no real sleeps,
no freezegun (which has no precedent in this repo).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from state_store.auth import Principal
from state_store.main import create_app
from state_store.ratelimit import (
    AuthFailureLimiter,
    RateLimiter,
    TokenBucket,
    make_rate_limit_dependency,
)

# ── Injectable clock ────────────────────────────────────────


class FakeClock:
    """Deterministic monotonic clock for tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


# ── TokenBucket unit tests ──────────────────────────────────


class TestTokenBucket:
    def test_allows_within_burst(self):
        clock = FakeClock()
        bucket = TokenBucket(rpm=60, burst=5, clock=clock)
        for _ in range(5):
            assert bucket.consume() is None

    def test_rejects_over_burst(self):
        clock = FakeClock()
        bucket = TokenBucket(rpm=60, burst=3, clock=clock)
        for _ in range(3):
            assert bucket.consume() is None
        wait = bucket.consume()
        assert wait is not None
        assert wait > 0

    def test_refills_over_time(self):
        clock = FakeClock()
        bucket = TokenBucket(rpm=60, burst=3, clock=clock)
        for _ in range(3):
            bucket.consume()
        assert bucket.consume() is not None
        clock.advance(1.0)
        assert bucket.consume() is None

    def test_zero_rpm_never_refills(self):
        clock = FakeClock()
        bucket = TokenBucket(rpm=0, burst=1, clock=clock)
        assert bucket.consume() is None
        clock.advance(100.0)
        wait = bucket.consume()
        assert wait is not None
        assert wait == float("inf")

    def test_infinity_retry_after_clamped(self):
        """float('inf') from zero-RPM bucket does not crash header serialization."""
        from state_store.ratelimit import _clamp_retry_after

        assert _clamp_retry_after(float("inf")) == "1"
        assert _clamp_retry_after(float("nan")) == "1"
        assert _clamp_retry_after(-1.0) == "1"
        assert _clamp_retry_after(0.5) == "1"
        assert _clamp_retry_after(7200.0) == "3600"
        assert _clamp_retry_after(2.3) == "3"


# ── RateLimiter unit tests ──────────────────────────────────


class TestRateLimiter:
    def test_service_principal_exempt(self):
        clock = FakeClock()
        limiter = RateLimiter(
            rpm=1,
            burst=1,
            exempt_service=True,
            clock=clock,
        )
        svc = Principal(kind="service", username="deployment", is_admin=True)
        for _ in range(100):
            assert limiter.check(svc) is None

    def test_user_principal_limited(self):
        clock = FakeClock()
        limiter = RateLimiter(rpm=60, burst=2, clock=clock)
        user = Principal(kind="user", username="alice", is_admin=False)
        assert limiter.check(user) is None
        assert limiter.check(user) is None
        wait = limiter.check(user)
        assert wait is not None
        assert wait > 0

    def test_per_principal_isolation(self):
        clock = FakeClock()
        limiter = RateLimiter(rpm=60, burst=1, clock=clock)
        alice = Principal(kind="user", username="alice", is_admin=False)
        bob = Principal(kind="user", username="bob", is_admin=False)
        assert limiter.check(alice) is None
        assert limiter.check(alice) is not None
        assert limiter.check(bob) is None

    def test_bounded_table_eviction(self):
        clock = FakeClock()
        limiter = RateLimiter(
            rpm=60,
            burst=5,
            max_keys=3,
            clock=clock,
        )
        for i in range(5):
            p = Principal(kind="user", username=f"user{i}", is_admin=False)
            limiter.check(p)
        assert len(limiter._buckets) == 3

    def test_exempt_service_false(self):
        clock = FakeClock()
        limiter = RateLimiter(
            rpm=60,
            burst=1,
            exempt_service=False,
            clock=clock,
        )
        svc = Principal(kind="service", username="deployment", is_admin=True)
        assert limiter.check(svc) is None
        assert limiter.check(svc) is not None


# ── AuthFailureLimiter tests ────────────────────────────────


class TestAuthFailureLimiter:
    def test_blocks_after_burst(self):
        clock = FakeClock()
        limiter = AuthFailureLimiter(
            failures_per_min=3,
            clock=clock,
        )
        for _ in range(3):
            limiter.record_failure("1.2.3.4")
        wait = limiter.is_blocked("1.2.3.4")
        assert wait is not None
        assert wait > 0

    def test_does_not_block_different_ip(self):
        clock = FakeClock()
        limiter = AuthFailureLimiter(
            failures_per_min=2,
            clock=clock,
        )
        limiter.record_failure("1.2.3.4")
        limiter.record_failure("1.2.3.4")
        assert limiter.is_blocked("5.6.7.8") is None

    def test_refills_after_time(self):
        clock = FakeClock()
        limiter = AuthFailureLimiter(
            failures_per_min=2,
            clock=clock,
        )
        limiter.record_failure("1.2.3.4")
        limiter.record_failure("1.2.3.4")
        assert limiter.is_blocked("1.2.3.4") is not None
        clock.advance(60.0)
        assert limiter.is_blocked("1.2.3.4") is None

    def test_bounded_table(self):
        clock = FakeClock()
        limiter = AuthFailureLimiter(
            failures_per_min=10,
            max_keys=3,
            clock=clock,
        )
        for i in range(5):
            limiter.record_failure(f"10.0.0.{i}")
        assert len(limiter._buckets) == 3


# ── Integration tests (via TestClient) ──────────────────────


class TestRateLimitIntegration:
    @pytest.fixture()
    def app_and_token(self):
        app = create_app()
        return app, app.state.api_token

    def test_health_not_rate_limited(self, app_and_token):
        app, token = app_and_token
        client = TestClient(app)
        client.headers["Authorization"] = f"Bearer {token}"
        for _ in range(20):
            r = client.get("/api/v1/health")
            assert r.status_code == 200

    def test_retry_after_header_on_429(self, tmp_path):
        """Multi-user app with a tiny limit returns 429 + Retry-After."""
        from state_store.auth import make_auth_dependency
        from state_store.identity import UserStore
        from state_store.main import mount_routers

        clock = FakeClock()
        user_store = UserStore(persist_path=tmp_path / "users.json")
        app = create_app()
        token = app.state.api_token

        app.state.multi_user = True
        app.state.user_store = user_store

        rate_limiter = RateLimiter(
            rpm=60,
            burst=1,
            exempt_service=True,
            clock=clock,
        )
        rate_limit_dep = make_rate_limit_dependency(rate_limiter)

        auth = make_auth_dependency(
            token,
            multi_user=True,
            user_store=user_store,
        )
        app.state.auth_dependency = auth

        app.router.routes.clear()
        mount_routers(app, auth, rate_limit_dep)

        _, user_token = user_store.create_user("testuser")
        client = TestClient(app)
        client.headers["Authorization"] = f"Bearer {user_token}"

        r1 = client.get("/api/v1/tickets")
        assert r1.status_code == 200

        r2 = client.get("/api/v1/tickets")
        assert r2.status_code == 429
        assert "Retry-After" in r2.headers
        assert int(r2.headers["Retry-After"]) >= 1

    def test_service_exempt_under_tiny_limit(self, tmp_path):
        """Deployment token bypasses rate limiting."""
        from state_store.auth import make_auth_dependency
        from state_store.identity import UserStore
        from state_store.main import mount_routers

        clock = FakeClock()
        user_store = UserStore(persist_path=tmp_path / "users.json")
        app = create_app()
        token = app.state.api_token

        app.state.multi_user = True
        app.state.user_store = user_store

        rate_limiter = RateLimiter(
            rpm=1,
            burst=1,
            exempt_service=True,
            clock=clock,
        )
        rate_limit_dep = make_rate_limit_dependency(rate_limiter)

        auth = make_auth_dependency(
            token,
            multi_user=True,
            user_store=user_store,
        )

        app.router.routes.clear()
        mount_routers(app, auth, rate_limit_dep)

        client = TestClient(app)
        client.headers["Authorization"] = f"Bearer {token}"

        for _ in range(10):
            r = client.get("/api/v1/tickets")
            assert r.status_code == 200

    def test_disabled_limiter_never_429s(self):
        """When rate_limit is None, no 429s are returned."""
        rate_limit_dep = make_rate_limit_dependency(None)
        app = create_app()
        token = app.state.api_token

        from state_store.main import mount_routers

        app.router.routes.clear()
        mount_routers(app, app.state.auth_dependency, rate_limit_dep)

        client = TestClient(app)
        client.headers["Authorization"] = f"Bearer {token}"

        for _ in range(50):
            r = client.get("/api/v1/tickets")
            assert r.status_code == 200

    def test_auth_failure_limiter_blocks_bad_tokens(self):
        """Repeated bad tokens from the same IP get 429."""
        clock = FakeClock()
        from state_store.auth import make_auth_dependency
        from state_store.main import mount_routers
        from state_store.ratelimit import AuthFailureLimiter

        auth_limiter = AuthFailureLimiter(
            failures_per_min=3,
            clock=clock,
        )
        app = create_app()
        token = app.state.api_token

        auth = make_auth_dependency(
            token,
            auth_failure_limiter=auth_limiter,
        )

        app.router.routes.clear()
        mount_routers(app, auth)

        client = TestClient(app)

        for _ in range(3):
            r = client.get(
                "/api/v1/tickets",
                headers={"Authorization": "Bearer bad-token"},
            )
            assert r.status_code == 401

        r = client.get(
            "/api/v1/tickets",
            headers={"Authorization": "Bearer bad-token"},
        )
        assert r.status_code == 429
        assert "Retry-After" in r.headers

        r_good = client.get(
            "/api/v1/tickets",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r_good.status_code == 429

    def test_events_limit_bound(self):
        """GET /tickets/{id}/events rejects limit > 1000."""
        app = create_app()
        token = app.state.api_token
        client = TestClient(app)
        client.headers["Authorization"] = f"Bearer {token}"

        from state_store.models import CreateTicketRequest

        ticket = app.state.store.create_ticket(
            CreateTicketRequest(summary="test", description="test"),
        )

        r = client.get(
            f"/api/v1/tickets/{ticket.id}/events?limit=2000",
        )
        assert r.status_code == 422

        r = client.get(
            f"/api/v1/tickets/{ticket.id}/events?limit=500",
        )
        assert r.status_code == 200

    def test_tickets_since_requires_auth(self):
        """GET /tickets/since/{seq} is behind auth."""
        app = create_app()
        client = TestClient(app)

        r = client.get("/api/v1/tickets/since/0")
        assert r.status_code == 401

        client.headers["Authorization"] = f"Bearer {app.state.api_token}"
        r = client.get("/api/v1/tickets/since/0")
        assert r.status_code == 200

    def test_usage_summary_cache(self):
        """Second call within TTL returns cached result."""

        from state_store.api import events as events_mod

        old_cache = events_mod._summary_cache
        old_ts = events_mod._summary_cache_ts
        try:
            events_mod._summary_cache = {}
            events_mod._summary_cache_ts = 0.0

            app = create_app()
            token = app.state.api_token
            client = TestClient(app)
            client.headers["Authorization"] = f"Bearer {token}"

            r1 = client.get("/api/v1/usage/summary")
            assert r1.status_code == 200

            assert events_mod._summary_cache_ts > 0

            r2 = client.get("/api/v1/usage/summary")
            assert r2.status_code == 200
            assert r1.json() == r2.json()
        finally:
            events_mod._summary_cache = old_cache
            events_mod._summary_cache_ts = old_ts


class TestConfigValidation:
    def test_zero_burst_rejected_when_enabled(self, monkeypatch):
        """burst=0 raises ValueError when rate limiting is enabled."""
        from state_store.main import _validate_positive_int

        with pytest.raises(ValueError, match="must be > 0"):
            _validate_positive_int(0, "rate_limit.burst", allow_zero=False)

    def test_zero_burst_allowed_when_disabled(self):
        """burst=0 is accepted when rate limiting is disabled."""
        from state_store.main import _validate_positive_int

        assert _validate_positive_int(0, "rate_limit.burst", allow_zero=True) == 0
