from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from orchestrator.config import _load_config_file
from providers.events import EventBus

from .api.router import api_router, health_router
from .auth import load_or_generate_token, make_auth_dependency
from .ratelimit import (
    AuthFailureLimiter,
    RateLimiter,
    make_rate_limit_dependency,
)
from .store import TicketStore

STATIC_DIR = Path(__file__).parent / "static"

logger = logging.getLogger(__name__)


def _validate_positive_int(
    value: Any,
    name: str,
    *,
    allow_zero: bool = True,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer, got {value!r}")
    if not allow_zero and value == 0:
        raise ValueError(f"{name} must be > 0, got 0")
    return value


def mount_routers(
    app: FastAPI,
    auth: Any,
    rate_limit: Any = None,
) -> None:
    """Mount API and health routers with dependencies.

    Factored out so test helpers (which clear and re-mount routers)
    can use the same wiring as production. ``rate_limit`` may be
    ``None`` to skip the rate-limit dependency.
    """
    deps: list = [Depends(auth)]
    if rate_limit is not None:
        deps.append(Depends(rate_limit))
    app.include_router(api_router, dependencies=deps)
    app.include_router(health_router)


def create_app() -> FastAPI:
    app = FastAPI(title="Agentic Perf State Store", version="0.1.0")

    port = int(os.environ.get("STORE_PORT", "8090"))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            f"http://localhost:{port}",
            f"http://127.0.0.1:{port}",
        ],
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )

    token = load_or_generate_token()
    app.state.api_token = token

    cfg = _load_config_file()
    auth_cfg = cfg.get("auth", {})
    multi_user = auth_cfg.get("multi_user", False)
    token_ttl_days = _validate_positive_int(
        auth_cfg.get("token_ttl_days", 0),
        "auth.token_ttl_days",
    )
    app.state.multi_user = multi_user

    user_store = None
    if multi_user:
        from .identity import UserStore

        user_store = UserStore()
    app.state.user_store = user_store

    if token_ttl_days > 0 and multi_user and user_store is not None:
        now = datetime.now(timezone.utc)

        def _token_age_days(u) -> int:
            issued = u.token_issued_at or u.created_at
            if issued.tzinfo is None:
                issued = issued.replace(tzinfo=timezone.utc)
            return (now - issued).days

        expired = [
            u.username
            for u in user_store.list_users()
            if _token_age_days(u) >= token_ttl_days
        ]
        if expired:
            logger.warning(
                "Token TTL is %d days — %d user(s) have already-expired tokens: %s",
                token_ttl_days,
                len(expired),
                ", ".join(sorted(expired)),
            )

    # ── Rate limiting ────────────────────────────────────────
    rl_cfg = cfg.get("rate_limit", {})
    rl_enabled = rl_cfg.get("enabled", True)
    rl_per_user_rpm = _validate_positive_int(
        rl_cfg.get("per_user_rpm", 600),
        "rate_limit.per_user_rpm",
        allow_zero=not rl_enabled,
    )
    rl_burst = _validate_positive_int(
        rl_cfg.get("burst", 30),
        "rate_limit.burst",
        allow_zero=not rl_enabled,
    )
    rl_exempt_service = rl_cfg.get("exempt_service", True)
    rl_auth_failures_per_min = _validate_positive_int(
        rl_cfg.get("auth_failures_per_min", 30),
        "rate_limit.auth_failures_per_min",
        allow_zero=not rl_enabled,
    )

    auth_failure_limiter: AuthFailureLimiter | None = None
    rate_limiter: RateLimiter | None = None
    rate_limit_dep = None

    if rl_enabled:
        auth_failure_limiter = AuthFailureLimiter(
            failures_per_min=rl_auth_failures_per_min,
        )
        if multi_user:
            rate_limiter = RateLimiter(
                rpm=rl_per_user_rpm,
                burst=rl_burst,
                exempt_service=rl_exempt_service,
            )
            rate_limit_dep = make_rate_limit_dependency(rate_limiter)
            logger.info(
                "Rate limiting enabled: %d rpm, burst %d, service exempt=%s",
                rl_per_user_rpm,
                rl_burst,
                rl_exempt_service,
            )
        logger.info(
            "Auth failure limiting enabled: %d failures/min",
            rl_auth_failures_per_min,
        )

    app.state.rate_limiter = rate_limiter
    app.state.auth_failure_limiter = auth_failure_limiter

    auth = make_auth_dependency(
        token,
        multi_user=multi_user,
        user_store=user_store,
        token_ttl_days=token_ttl_days,
        auth_failure_limiter=auth_failure_limiter,
    )
    app.state.auth_dependency = auth

    app.state.store = TicketStore()
    app.state.event_bus = EventBus()
    mount_routers(app, auth, rate_limit_dep)

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

        @app.get("/")
        def serve_dashboard():
            index_path = STATIC_DIR / "index.html"
            html = index_path.read_text()
            inject_token = "" if multi_user else token
            token_script = f'<script>window.API_TOKEN="{inject_token}";</script>'
            html = html.replace("</head>", f"{token_script}</head>", 1)
            return HTMLResponse(
                content=html,
                headers={"Cache-Control": "no-cache"},
            )

    return app


app = create_app()

if __name__ == "__main__":
    uvicorn.run(
        "state_store.main:app",
        host="0.0.0.0",
        port=8090,
    )
