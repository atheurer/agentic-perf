from fastapi import APIRouter

from . import comments, events, health, tickets, transitions

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(tickets.router)
api_router.include_router(transitions.router)
api_router.include_router(comments.router)
api_router.include_router(events.router)
api_router.include_router(health.router)
