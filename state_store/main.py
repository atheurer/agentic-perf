from __future__ import annotations

from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from providers.events import EventBus

from .api.router import api_router
from .store import TicketStore

STATIC_DIR = Path(__file__).parent / "static"


def create_app() -> FastAPI:
    app = FastAPI(title="Agentic Perf State Store", version="0.1.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.state.store = TicketStore()
    app.state.event_bus = EventBus()
    app.include_router(api_router)

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

        @app.get("/")
        def serve_dashboard():
            return FileResponse(
                str(STATIC_DIR / "index.html"),
                headers={"Cache-Control": "no-cache"},
            )

    return app


app = create_app()

if __name__ == "__main__":
    uvicorn.run(
        "state_store.main:app",
        host="0.0.0.0",
        port=8090,
        reload=True,
    )
