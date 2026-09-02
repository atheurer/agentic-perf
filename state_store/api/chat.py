"""Chat API endpoints.

Provides a conversational interface to agentic-perf via the
dashboard. Each request carries the user's auth token; the
chat agent uses it for all downstream API calls.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..auth import Principal

router = APIRouter(prefix="/chat", tags=["chat"])


class ChatMessageRequest(BaseModel):
    message: str
    ticket_context: str | None = None


class ChatMessageResponse(BaseModel):
    response: str
    usage: dict[str, Any]


class ChatHistoryResponse(BaseModel):
    messages: list[dict[str, str]]
    usage: dict[str, Any]


def _get_chat_agent(request: Request):
    """Get the chat agent from app state."""
    agent = getattr(request.app.state, "chat_agent", None)
    if agent is None:
        raise HTTPException(
            status_code=503,
            detail="Chat agent not available",
        )
    return agent


def _get_principal(request: Request) -> Principal:
    """Authenticate the request and return principal.

    Since the chat router is outside the global auth
    middleware, we authenticate directly here.
    """
    # Check if global auth already set principal
    principal = getattr(request.state, "principal", None)
    if principal:
        return principal

    token = _get_token(request)
    if not token:
        return Principal(
            kind="anonymous",
            username="anonymous",
            is_admin=False,
        )

    # Validate against service token
    api_token = getattr(request.app.state, "api_token", "")
    if token == api_token:
        return Principal(
            kind="service",
            username="service",
            is_admin=True,
        )

    # Validate against user store
    user_store = getattr(request.app.state, "user_store", None)
    if user_store:
        import hashlib

        token_hash = hashlib.sha256(token.encode()).hexdigest()
        for user in user_store.list_users():
            if user.token_hash == token_hash:
                return Principal(
                    kind="user",
                    username=user.username,
                    is_admin=user.is_admin,
                )

    # Token was supplied but didn't match anything — reject it.
    raise HTTPException(
        status_code=401,
        detail="Invalid bearer token",
    )


def _get_user(request: Request) -> str:
    """Get the authenticated username.

    In multi-user mode, returns the authenticated username.
    In single-user mode, returns 'default' to provide a
    stable session key (rather than 'anonymous' which could
    collide with actual anonymous access).
    """
    principal = _get_principal(request)
    if principal.kind == "anonymous":
        # Each anonymous request gets a unique key so sessions
        # don't collide. The session is ephemeral — it will be
        # evicted from the LRU cache and history is not returned.
        import uuid

        return f"anon-{uuid.uuid4().hex[:8]}"
    if principal.kind == "service":
        return "default"
    return principal.username


def _get_token(request: Request) -> str:
    """Extract the bearer token from the request."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return ""


@router.post("/message", response_model=ChatMessageResponse)
async def send_message(
    body: ChatMessageRequest,
    request: Request,
):
    """Send a message to the chat agent."""
    agent = _get_chat_agent(request)
    principal = _get_principal(request)
    user = _get_user(request)
    token = _get_token(request)
    is_anonymous = principal.kind == "anonymous"

    # Anonymous users get read-only access using the
    # service API token (if anonymous_read is enabled)
    if is_anonymous:
        anonymous_read = getattr(request.app.state, "anonymous_read", False)
        if not anonymous_read:
            raise HTTPException(
                status_code=401,
                detail="Authentication required for chat",
            )
        token = getattr(request.app.state, "api_token", "")
        if not token:
            raise HTTPException(
                status_code=503,
                detail="Chat unavailable for anonymous users",
            )

    try:
        response_text = await agent.handle_message(
            user=user,
            message=body.message,
            auth_token=token,
            ticket_context=body.ticket_context,
            readonly=is_anonymous,
        )
    except Exception:
        # Never return 500 to the user — surface the error
        # as a chat response so the conversation continues.
        response_text = (
            "Sorry, I encountered an error processing your "
            "message. Please try again or rephrase your request."
        )
        import logging

        logging.getLogger(__name__).exception("Chat message handling failed")

    usage = agent.get_usage(user)
    return ChatMessageResponse(response=response_text, usage=usage)


@router.get("/history", response_model=ChatHistoryResponse)
async def get_history(request: Request):
    """Get chat history for the current user."""
    agent = _get_chat_agent(request)
    user = _get_user(request)

    principal = _get_principal(request)
    if principal.kind == "anonymous":
        usage = agent.get_usage(user)
        return ChatHistoryResponse(messages=[], usage=usage)

    history = agent.get_history(user)
    usage = agent.get_usage(user)
    return ChatHistoryResponse(messages=history, usage=usage)


@router.delete("/session")
async def clear_session(request: Request):
    """Clear the current user's chat session."""
    agent = _get_chat_agent(request)
    user = _get_user(request)
    agent.clear_session(user)
    return {"status": "cleared"}
