from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from orchestrator.config import _load_config_file
from providers.events import EventBus

from .api.router import api_router, health_router
from .auth import load_or_generate_token, make_auth_dependency
from .store import TicketStore

STATIC_DIR = Path(__file__).parent / "static"


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
    raw_ttl = auth_cfg.get("token_ttl_days", 0)
    if isinstance(raw_ttl, bool) or not isinstance(raw_ttl, int) or raw_ttl < 0:
        raise ValueError(
            f"auth.token_ttl_days must be a non-negative integer, got {raw_ttl!r}"
        )
    token_ttl_days: int = raw_ttl
    app.state.multi_user = multi_user

    user_store = None
    if multi_user:
        from .identity import UserStore

        user_store = UserStore()
    app.state.user_store = user_store

    if token_ttl_days > 0 and multi_user and user_store is not None:
        _log = logging.getLogger(__name__)
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
            _log.warning(
                "Token TTL is %d days — %d user(s) have already-expired tokens: %s",
                token_ttl_days,
                len(expired),
                ", ".join(sorted(expired)),
            )

    auth = make_auth_dependency(
        token,
        multi_user=multi_user,
        user_store=user_store,
        token_ttl_days=token_ttl_days,
    )
    app.state.auth_dependency = auth

    app.state.store = TicketStore()
    app.state.event_bus = EventBus()
    app.include_router(api_router, dependencies=[Depends(auth)])
    app.include_router(health_router)

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
