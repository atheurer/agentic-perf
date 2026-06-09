from fastapi import APIRouter

from . import comments, health, tickets, transitions

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(tickets.router)
api_router.include_router(transitions.router)
api_router.include_router(comments.router)
api_router.include_router(health.router)
