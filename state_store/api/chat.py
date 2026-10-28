"""Chat API endpoints.

Provides a conversational interface to agentic-perf via the
dashboard. Each request carries the user's auth token; the
chat agent uses it for all downstream API calls.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

import anyio
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse, StreamingResponse

from ..auth import Principal

logger = logging.getLogger(__name__)
_CHAT_TASK_CLEANUP_TIMEOUT_SECONDS = 2.0
_CHAT_TASKS_AWAITING_CLEANUP: set[asyncio.Task[str]] = set()

router = APIRouter(prefix="/chat", tags=["chat"])


class ChatMessageRequest(BaseModel):
    message: str
    ticket_context: str | None = None


class ChatDeliveryAckRequest(BaseModel):
    delivery_id: str = Field(min_length=1, max_length=128)


class ChatMessageResponse(BaseModel):
    response: str
    usage: dict[str, Any]
    delivery_id: str = Field(
        description=(
            "A request-scoped response ID. After consuming the complete JSON body, "
            "POST it to /chat/message/ack with the same authenticated user. Older "
            "clients may omit the ACK; the session will become indeterminate "
            "after the bounded ACK wait instead of allowing a silent retry."
        )
    )
    indeterminate: bool = False
    delivery_pending: bool = False


class ChatHistoryResponse(BaseModel):
    messages: list[dict[str, str]]
    usage: dict[str, Any]
    indeterminate: bool = False
    delivery_pending: bool = False
    request_in_flight: bool = False


class _ChatDeliveryJSONResponse(JSONResponse):
    """Track transport completion while waiting for a browser ACK."""

    def __init__(self, content: Any, agent: Any, user: str, delivery_id: str) -> None:
        super().__init__(content=content)
        self._agent = agent
        self._user = user
        self._delivery_id = delivery_id

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        except BaseException:
            delivery_interrupted = getattr(
                self._agent, "mark_response_delivery_interrupted", None
            )
            if delivery_interrupted is not None:
                delivery_interrupted(self._user, self._delivery_id)
            raise
        else:
            delivery_sent = getattr(self._agent, "mark_response_delivery_sent", None)
            if delivery_sent is not None:
                delivery_sent(self._user, self._delivery_id)


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

    def __init__(
        self,
        content: Any,
        *,
        agent: Any,
        user: str,
        delivery_id: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(content, **kwargs)
        self._agent = agent
        self._user = user
        self._delivery_id = delivery_id

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            try:
                await super().__call__(scope, receive, send)
            finally:
                close_iterator = getattr(self.body_iterator, "aclose", None)
                if close_iterator is not None:
                    with anyio.CancelScope(shield=True):
                        await close_iterator()
        except BaseException:
            delivery_interrupted = getattr(
                self._agent, "mark_response_delivery_interrupted", None
            )
            if delivery_interrupted is not None:
                delivery_interrupted(self._user, self._delivery_id)
            raise
        else:
            delivery_sent = getattr(self._agent, "mark_response_delivery_sent", None)
            if delivery_sent is not None:
                delivery_sent(self._user, self._delivery_id)


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
    """Send a message and return a delivery ID for a post-read ACK.

    Existing request bodies remain valid. Clients should POST the returned
    ``delivery_id`` to ``/message/ack`` after consuming the complete response.
    A client that does not ACK remains guarded, then enters recovery state after
    the bounded wait; the server never silently enables an uncertain retry.
    """
    agent = _get_chat_agent(request)
    principal = _get_principal(request)
    user = _get_user(request)
    delivery_id = uuid.uuid4().hex
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
            delivery_id=delivery_id,
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
    response = ChatMessageResponse(
        response=response_text,
        usage=usage,
        delivery_id=delivery_id,
        indeterminate=agent.is_session_indeterminate(user),
        delivery_pending=agent.is_response_delivery_pending(user),
    )
    return _ChatDeliveryJSONResponse(
        content=response.model_dump(),
        agent=agent,
        user=user,
        delivery_id=delivery_id,
    )


@router.post("/message/ack")
async def acknowledge_message_delivery(
    body: ChatDeliveryAckRequest,
    request: Request,
):
    """Acknowledge a response ID for its authenticated owner.

    The request ID is generated by the server and is scoped to the user that
    received it. Duplicate, late, unknown, and cross-user ACKs cannot settle a
    different request. A late ACK can clear only the delivery pause created by
    its own response ID.
    """
    agent = _get_chat_agent(request)
    _get_principal(request)
    user = _get_user(request)
    acknowledged = agent.mark_response_delivery_complete(user, body.delivery_id)
    return {"acknowledged": acknowledged}


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
    delivery_id = uuid.uuid4().hex
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
                delivery_id=delivery_id,
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
        yield f"data: {json.dumps({'text': response_text})}\n\n"
        usage = agent.get_usage(user)
        yield (
            f"data: {json.dumps({'done': True, 'usage': usage, 'delivery_id': delivery_id, 'indeterminate': agent.is_session_indeterminate(user), 'delivery_pending': agent.is_response_delivery_pending(user)})}\n\n"
        )

    return _ChatStreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        agent=agent,
        user=user,
        delivery_id=delivery_id,
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
        delivery_pending=agent.is_response_delivery_pending(user),
        request_in_flight=agent.is_request_in_flight(user),
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
