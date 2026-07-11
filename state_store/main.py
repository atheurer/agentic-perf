from __future__ import annotations

import os
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

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
    auth = make_auth_dependency(token)

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
            token_script = f'<script>window.API_TOKEN="{token}";</script>'
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
