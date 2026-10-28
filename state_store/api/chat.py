"""Chat API endpoints.

Provides a conversational interface to agentic-perf via the
dashboard. Each request carries the user's auth token; the
chat agent uses it for all downstream API calls.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import anyio
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from starlette.responses import JSONResponse, StreamingResponse

from ..auth import Principal

logger = logging.getLogger(__name__)
_CHAT_TASK_CLEANUP_TIMEOUT_SECONDS = 2.0
_CHAT_TASKS_AWAITING_CLEANUP: set[asyncio.Task[str]] = set()

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
    indeterminate: bool = False


class _ChatDeliveryJSONResponse(JSONResponse):
    """Acknowledge response delivery to the agent after ASGI send completes."""

    def __init__(self, content: Any, agent: Any, user: str) -> None:
        super().__init__(content=content)
        self._agent = agent
        self._user = user

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        except BaseException:
            delivery_interrupted = getattr(
                self._agent, "mark_response_delivery_interrupted", None
            )
            if delivery_interrupted is not None:
                delivery_interrupted(self._user)
            raise
        else:
            delivery_complete = getattr(
                self._agent, "mark_response_delivery_complete", None
            )
            if delivery_complete is not None:
                delivery_complete(self._user)


def _get_chat_agent(request: Request):
    """Get the chat agent from app state."""
    agent = getattr(request.app.state, "chat_agent", None)
    if agent is None:
        raise HTTPException(
            status_code=503,
            detail="Chat agent not available",
        )
    return agent


def _consume_chat_task_result(task: asyncio.Task[str]) -> None:
    """Retrieve a task's result after its SSE consumer has gone away."""
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("Chat task failed after its response stream closed")
    finally:
        _CHAT_TASKS_AWAITING_CLEANUP.discard(task)


async def _cancel_chat_task(task: asyncio.Task[str]) -> None:
    """Cancel once, then wait a bounded time for audit cleanup to finish."""
    if task.done():
        return

    task.cancel()
    with anyio.CancelScope(shield=True):
        done, _ = await asyncio.wait({task}, timeout=_CHAT_TASK_CLEANUP_TIMEOUT_SECONDS)
    if done:
        return

    # Do not cancel again: the child may be persisting its terminal audit event.
    # Keep a strong reference until it finishes, and reject later user work via
    # ChatAgent's per-session in-flight guard.
    logger.warning("Chat task is still cleaning up after SSE stream closed")
    _CHAT_TASKS_AWAITING_CLEANUP.add(task)
    task.add_done_callback(_consume_chat_task_result)


class _ChatStreamingResponse(StreamingResponse):
    """Close the SSE iterator even when the ASGI server send fails."""

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            close_iterator = getattr(self.body_iterator, "aclose", None)
            if close_iterator is not None:
                with anyio.CancelScope(shield=True):
                    await close_iterator()


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

    # Validate against user store (including disabled/TTL checks)
    user_store = getattr(request.app.state, "user_store", None)
    if user_store:
        import hashlib
        from datetime import datetime, timezone

        token_hash = hashlib.sha256(token.encode()).hexdigest()
        for user in user_store.list_users():
            if user.token_hash == token_hash:
                if user.disabled:
                    raise HTTPException(
                        status_code=403,
                        detail="User account is disabled",
                    )
                token_ttl_days = getattr(request.app.state, "token_ttl_days", 0)
                if token_ttl_days > 0:
                    issued = user.token_issued_at or user.created_at
                    age = datetime.now(timezone.utc) - issued
                    if age.days >= token_ttl_days:
                        raise HTTPException(
                            status_code=401,
                            detail="Token expired",
                        )
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
            track_response_delivery=True,
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
    response = ChatMessageResponse(response=response_text, usage=usage)
    return _ChatDeliveryJSONResponse(
        content=response.model_dump(),
        agent=agent,
        user=user,
    )


@router.post("/message/stream")
async def send_message_stream(
    body: ChatMessageRequest,
    request: Request,
):
    """Stream chat response as Server-Sent Events.

    Keeps the HTTP connection alive with SSE chunks, preventing
    reverse-proxy timeouts (e.g. OpenShift route 504s).
    """
    agent = _get_chat_agent(request)
    principal = _get_principal(request)
    user = _get_user(request)
    token = _get_token(request)
    is_anonymous = principal.kind == "anonymous"

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

    async def event_generator():
        # Run the LLM call in a task so we can send heartbeats
        # while it processes.  SSE comments (": heartbeat\n\n")
        # are ignored by browsers but keep the reverse-proxy
        # connection alive past its idle timeout.
        task = asyncio.create_task(
            agent.handle_message(
                user=user,
                message=body.message,
                auth_token=token,
                ticket_context=body.ticket_context,
                readonly=is_anonymous,
                track_response_delivery=True,
            )
        )
        task_result_observed = False
        try:
            while not task.done():
                yield ": heartbeat\n\n"
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=15)
                except asyncio.TimeoutError:
                    pass  # task still running, send another heartbeat
            response_text = task.result()
            task_result_observed = True
        except Exception:
            task_result_observed = task.done()
            logger.exception("Chat streaming failed")
            response_text = (
                "Sorry, I encountered an error processing your "
                "message. Please try again or rephrase your request."
            )
        finally:
            await _cancel_chat_task(task)
            if task.done() and not task_result_observed:
                _consume_chat_task_result(task)
        try:
            yield f"data: {json.dumps({'text': response_text})}\n\n"
        except BaseException:
            delivery_interrupted = getattr(
                agent, "mark_response_delivery_interrupted", None
            )
            if delivery_interrupted is not None:
                delivery_interrupted(user)
            raise
        else:
            delivery_complete = getattr(agent, "mark_response_delivery_complete", None)
            if delivery_complete is not None:
                delivery_complete(user)
        usage = agent.get_usage(user)
        yield f"data: {json.dumps({'done': True, 'usage': usage})}\n\n"

    return _ChatStreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/history", response_model=ChatHistoryResponse)
async def get_history(request: Request):
    """Get chat history for the current user."""
    agent = _get_chat_agent(request)
    user = _get_user(request)

    principal = _get_principal(request)
    if principal.kind == "anonymous":
        usage = agent.get_usage(user)
        return ChatHistoryResponse(messages=[], usage=usage, indeterminate=False)

    history = agent.get_history(user)
    usage = agent.get_usage(user)
    return ChatHistoryResponse(
        messages=history,
        usage=usage,
        indeterminate=agent.is_session_indeterminate(user),
    )


@router.delete("/session")
async def clear_session(request: Request):
    """Clear the current user's chat session."""
    agent = _get_chat_agent(request)
    user = _get_user(request)
    if not agent.clear_session(user):
        raise HTTPException(
            status_code=409,
            detail="A chat request is still finishing; try clearing the session again shortly.",
        )
    return {"status": "cleared"}
