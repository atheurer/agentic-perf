from __future__ import annotations

import uvicorn
from fastapi import FastAPI

from providers.events import EventBus

from .api.router import api_router
from .store import TicketStore


def create_app() -> FastAPI:
    app = FastAPI(title="Agentic Perf State Store", version="0.1.0")
    app.state.store = TicketStore()
    app.state.event_bus = EventBus()
    app.include_router(api_router)
    return app


app = create_app()

if __name__ == "__main__":
    uvicorn.run(
        "state_store.main:app",
        host="0.0.0.0",
        port=8090,
        reload=True,
    )
