from fastapi import APIRouter

from . import (
    artifacts,
    audit,
    chat,
    comments,
    events,
    groups,
    health,
    interject,
    owners,
    stop,
    stream,
    tickets,
    trace_operations,
    traces,
    transitions,
    transitions_info,
    users,
    validations,
    webhooks,
    whoami,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(audit.router)
api_router.include_router(artifacts.router)
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
api_router.include_router(traces.router)
api_router.include_router(trace_operations.router)
api_router.include_router(validations.router)
# Chat router handles its own auth (supports anonymous read-only)
chat_router = APIRouter(prefix="/api/v1")
chat_router.include_router(chat.router)

health_router = APIRouter(prefix="/api/v1")
health_router.include_router(health.router)

# Webhook POST does its own auth; register outside the global dep.
webhook_router = APIRouter(prefix="/api/v1")
webhook_router.include_router(webhooks.router)
