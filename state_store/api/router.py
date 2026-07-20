from fastapi import APIRouter

from . import (
    comments,
    events,
    groups,
    health,
    interject,
    owners,
    stop,
    stream,
    tickets,
    transitions,
    transitions_info,
    users,
    whoami,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(tickets.router)
api_router.include_router(transitions.router)
api_router.include_router(comments.router)
api_router.include_router(events.router)
api_router.include_router(events.usage_router)
api_router.include_router(stop.router)
api_router.include_router(stream.router)
api_router.include_router(interject.router)
api_router.include_router(owners.router)
api_router.include_router(transitions_info.router)
api_router.include_router(users.router)
api_router.include_router(groups.router)
api_router.include_router(whoami.router)

health_router = APIRouter(prefix="/api/v1")
health_router.include_router(health.router)
