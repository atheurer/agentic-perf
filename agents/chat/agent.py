"""Chat agent — conversational interface for agentic-perf.

Maintains per-user session state and runs a tool-use loop
against the state store API. Each message from the user
triggers an LLM call that may invoke tools to search tickets,
create tickets, send interjections, etc.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from providers.llm.base import LLMProvider
from providers.tracing import (
    bind_trace_context,
    current_trace_context,
    new_trace_context,
    reset_trace_context,
)

from .prompts import CHAT_SYSTEM_PROMPT
from .tools import (
    CHAT_TOOLS,
    DESTRUCTIVE_TOOLS,
    READONLY_TOOLS,
    ChatAuditUnavailable,
    ChatToolAudit,
    execute_tool,
)

# Default fallback values if provider doesn't expose them
_DEFAULT_MAX_TOKENS = 4096
_DEFAULT_TIMEOUT = 60

logger = logging.getLogger(__name__)


def _safe_exception_summary(exc: Exception) -> str:
    """Return useful provider error metadata without logging response text."""
    status_code = getattr(exc, "status_code", None)
    if type(status_code) is not int:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
    if type(status_code) is int:
        return f"{type(exc).__name__} (HTTP {status_code})"
    return type(exc).__name__


def _is_confirmation(message: str) -> bool:
    """Return whether a conversational reply approves a pending action."""
    normalized = message.lower().strip().rstrip(".!?")
    if normalized in {
        "yes",
        "y",
        "confirm",
        "ok",
        "okay",
        "go",
        "go ahead",
        "do it",
        "proceed",
        "submit",
    }:
        return True
    return normalized.startswith(
        (
            "yes, ",
            "yes ",
            "okay, ",
            "okay ",
            "ok, ",
            "ok ",
            "go ahead, ",
            "go ahead ",
            "please submit",
            "please create",
        )
    )


def _is_cancellation(message: str) -> bool:
    """Return whether a conversational reply cancels a pending action."""
    normalized = message.lower().strip().rstrip(".!?")
    if normalized in {"no", "n", "cancel", "abort", "nevermind", "never mind"}:
        return True
    return normalized.startswith(("no, ", "no ", "cancel, ", "cancel "))


# Maximum tool-use iterations per user message to prevent
# runaway loops.
_DEFAULT_MAX_TOOL_ROUNDS = 10

# Maximum conversation history entries before truncation.
_MAX_HISTORY = 100
_CHAT_BUSY_MESSAGE = (
    "A previous chat request is still finishing. Please try again shortly."
)
_CHAT_INDETERMINATE_MESSAGE = (
    "A previous chat action was interrupted and its outcome is uncertain. "
    "This chat is paused to prevent the action from being repeated. "
    "Clear the chat session before continuing."
)
_CHAT_AUDIT_INDETERMINATE_MESSAGE = (
    "The action remains rejected, but its audit lifecycle is incomplete. "
    "This chat is paused. Reconcile the audit history and clear the chat "
    "session before continuing."
)
_CHAT_DELIVERY_INDETERMINATE_MESSAGE = (
    "A chat action may have completed, but the browser did not acknowledge "
    "the response. Check the action's current state before retrying, then "
    "clear the chat session to continue."
)
_CHAT_DELIVERY_ACK_TIMEOUT_SECONDS = 30.0
_CHAT_DELIVERY_SEND_TIMEOUT_SECONDS = 30.0
_CHAT_READONLY_AUDIT_UNAVAILABLE_MESSAGE = (
    "This action was blocked before dispatch, but its rejection audit could "
    "not be confirmed. No action was attempted."
)
_CHAT_READONLY_CANCELLED_MESSAGE = (
    "The read-only lookup was interrupted. You can safely ask again."
)


@dataclass
class ChatSession:
    """Per-user chat session state."""

    user: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    llm_calls: int = 0
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    pending_action: dict[str, Any] | None = None

    def add_user_message(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})
        self._truncate()
        self.last_active = time.time()

    def add_assistant_message(self, text: str) -> None:
        self.messages.append({"role": "assistant", "content": text})
        self._truncate()
        self.last_active = time.time()

    def add_tool_use(
        self,
        tool_calls: list[dict[str, Any]],
        raw_content: list[dict[str, Any]],
    ) -> None:
        """Add assistant tool_use + tool results to history."""
        self.messages.append({"role": "assistant", "content": raw_content})
        self.last_active = time.time()

    def add_tool_results(self, results: list[dict[str, Any]]) -> None:
        """Add tool results to history."""
        self.messages.append({"role": "user", "content": results})
        self._truncate()

    def record_usage(self, usage: dict[str, Any] | None) -> None:
        if not usage:
            return
        self.total_input_tokens += usage.get("input_tokens", 0)
        self.total_output_tokens += usage.get("output_tokens", 0)
        self.cache_read_tokens += usage.get("cache_read_input_tokens", 0)
        self.cache_creation_tokens += usage.get("cache_creation_input_tokens", 0)
        self.llm_calls += 1

    def _truncate(self) -> None:
        if len(self.messages) <= _MAX_HISTORY:
            return
        # Slice from the end, but ensure we don't orphan
        # tool_result blocks from their tool_use blocks.
        cut = self.messages[-_MAX_HISTORY:]
        # Start on a normal user turn: assistant-first histories and
        # tool_results without their preceding tool_use are invalid.
        while cut and (cut[0].get("role") != "user" or self._is_tool_result(cut[0])):
            cut = cut[1:]
        self.messages = cut

    @staticmethod
    def _is_tool_result(msg: dict[str, Any]) -> bool:
        """Check if a message contains tool_result blocks."""
        if msg.get("role") != "user":
            return False
        content = msg.get("content")
        if isinstance(content, list):
            return any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content
            )
        return False


class ChatSessionStore:
    """In-memory store for chat sessions, keyed by user."""

    def __init__(self, max_sessions: int = 100) -> None:
        self._sessions: dict[str, ChatSession] = {}
        self._max_sessions = max_sessions

    def get_or_create(self, user: str) -> ChatSession:
        if user not in self._sessions:
            if len(self._sessions) >= self._max_sessions:
                # Evict oldest session
                oldest = min(
                    self._sessions,
                    key=lambda k: self._sessions[k].last_active,
                )
                del self._sessions[oldest]
            self._sessions[user] = ChatSession(user=user)
        return self._sessions[user]

    def delete(self, user: str) -> bool:
        return self._sessions.pop(user, None) is not None

    def get_usage(self, user: str) -> dict[str, Any]:
        session = self._sessions.get(user)
        if not session:
            return {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "total_tokens": 0,
                "llm_calls": 0,
            }
        cache_read = session.cache_read_tokens
        cache_create = session.cache_creation_tokens
        context_in = session.total_input_tokens + cache_read + cache_create
        total = context_in + session.total_output_tokens
        return {
            "input_tokens": session.total_input_tokens,
            "output_tokens": session.total_output_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_create,
            "total_tokens": total,
            "llm_calls": session.llm_calls,
        }


class ChatAgent:
    """Conversational agent that processes user messages."""

    def __init__(
        self,
        llm: LLMProvider,
        store_url: str,
        session_store: ChatSessionStore | None = None,
        max_tool_rounds: int = _DEFAULT_MAX_TOOL_ROUNDS,
        audit_token: str | None = None,
    ) -> None:
        self._llm = llm
        self._store_url = store_url
        self._sessions = session_store or ChatSessionStore()
        self._client = httpx.AsyncClient(timeout=30.0)
        self._max_tool_rounds = max_tool_rounds
        # The web caller's token may be a restricted user credential. Trace
        # ingestion is intentionally service-only, so the embedded state-store
        # supplies its deployment token instead.
        self._audit_token = audit_token
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._indeterminate_sessions: dict[str, str] = {}
        self._pending_response_deliveries: dict[str, str] = {}
        self._active_response_deliveries: dict[str, tuple[str, dict[str, bool]]] = {}
        self._sent_response_deliveries: dict[str, str] = {}
        self._early_response_delivery_acks: dict[str, str] = {}
        self._delivery_indeterminate_ids: dict[str, str] = {}
        self._delivery_send_timeouts: dict[str, asyncio.TimerHandle] = {}
        self._delivery_timeouts: dict[str, asyncio.TimerHandle] = {}

    async def handle_message(
        self,
        user: str,
        message: str,
        auth_token: str,
        ticket_context: str | None = None,
        readonly: bool = False,
        track_response_delivery: bool = False,
        delivery_id: str | None = None,
    ) -> str:
        """Process a user message and return the assistant response.

        Parameters
        ----------
        user:
            Username for session tracking.
        message:
            The user's message text.
        auth_token:
            Bearer token for API calls (user's own token).
        readonly:
            If True, only read-only tools are available
            (for anonymous users).
        ticket_context:
            Optional ticket ID for context-aware chat on
            the ticket detail page.
        """
        lock = self._session_locks.setdefault(user, asyncio.Lock())
        if lock.locked():
            return _CHAT_BUSY_MESSAGE
        await lock.acquire()
        try:
            if user in self._pending_response_deliveries:
                return _CHAT_BUSY_MESSAGE
            if user in self._indeterminate_sessions:
                return self._indeterminate_sessions[user]

            # Every web request has a root context before it can reach
            # ChatToolAudit. Destructive confirmations retain this root.
            root = new_trace_context(ticket_id=ticket_context, agent_id="chat-agent")
            token = bind_trace_context(root)
            delivery_id = delivery_id or (
                uuid.uuid4().hex if track_response_delivery else None
            )
            mutation_state = {"completed": False, "delivery_interrupted": False}
            if track_response_delivery and delivery_id is not None:
                self._active_response_deliveries[user] = (delivery_id, mutation_state)
            try:
                try:
                    response = await self._handle_message(
                        user,
                        message,
                        auth_token,
                        ticket_context=ticket_context,
                        readonly=readonly,
                        mutation_state=mutation_state,
                    )
                except asyncio.CancelledError:
                    if mutation_state["completed"]:
                        self._mark_request_interrupted(user, delivery_id)
                    raise
                except Exception:
                    if mutation_state["completed"]:
                        self._mark_request_interrupted(user, delivery_id)
                    raise
                if mutation_state["delivery_interrupted"]:
                    if mutation_state["completed"]:
                        self._mark_request_interrupted(user, delivery_id)
                elif track_response_delivery and mutation_state["completed"]:
                    assert delivery_id is not None
                    self._pending_response_deliveries[user] = delivery_id
                    self._arm_response_delivery_send_timeout(user, delivery_id)
                return response
            finally:
                active_delivery = self._active_response_deliveries.get(user)
                if active_delivery is not None and active_delivery[0] == delivery_id:
                    self._active_response_deliveries.pop(user, None)
                reset_trace_context(token)
        finally:
            lock.release()
            # The current implementation never queues on this lock: callers
            # encountering an active request return immediately above.  Once
            # released, this task is its last user, so discard the per-user
            # lock instead of retaining anonymous/user ids indefinitely.
            if self._session_locks.get(user) is lock and not lock.locked():
                self._session_locks.pop(user, None)

    @staticmethod
    def _cancellation_requested() -> bool:
        task = asyncio.current_task()
        return task is not None and task.cancelling() > 0

    @staticmethod
    def _remove_trailing_tool_use(session: ChatSession) -> None:
        if not session.messages or session.messages[-1].get("role") != "assistant":
            return
        content = session.messages[-1].get("content")
        if isinstance(content, list) and any(
            isinstance(block, dict) and block.get("type") == "tool_use"
            for block in content
        ):
            session.messages.pop()

    @staticmethod
    def _remove_confirmation_exchange(session: ChatSession) -> None:
        if len(session.messages) < 3:
            return
        tail = session.messages[-3:]
        if (
            tail[0].get("role") == "assistant"
            and tail[1].get("role") == "user"
            and tail[2].get("role") == "assistant"
        ):
            del session.messages[-3:]

    def _mark_tool_outcome_indeterminate(
        self,
        user: str,
        session: ChatSession,
        *,
        confirmation_exchange: bool,
    ) -> None:
        if confirmation_exchange:
            self._remove_confirmation_exchange(session)
        else:
            self._remove_trailing_tool_use(session)
        session.pending_action = None
        self._indeterminate_sessions[user] = _CHAT_INDETERMINATE_MESSAGE
        session.add_assistant_message(_CHAT_INDETERMINATE_MESSAGE)

    def _mark_rejection_audit_indeterminate(
        self, user: str, session: ChatSession
    ) -> None:
        """Keep a failed rejection audit from reopening the pending action."""
        self._remove_confirmation_exchange(session)
        session.pending_action = None
        self._indeterminate_sessions[user] = _CHAT_AUDIT_INDETERMINATE_MESSAGE
        session.add_assistant_message(_CHAT_AUDIT_INDETERMINATE_MESSAGE)

    def _mark_tool_cancelled(
        self,
        user: str,
        session: ChatSession,
        *,
        tool_name: str,
        confirmation_exchange: bool,
    ) -> None:
        """Reconcile cancelled read-only history without a permanent pause."""
        if tool_name in READONLY_TOOLS:
            if confirmation_exchange:
                self._remove_confirmation_exchange(session)
            else:
                self._remove_trailing_tool_use(session)
            session.pending_action = None
            session.add_assistant_message(_CHAT_READONLY_CANCELLED_MESSAGE)
            return
        self._mark_tool_outcome_indeterminate(
            user, session, confirmation_exchange=confirmation_exchange
        )

    def _mark_tool_batch_indeterminate(
        self,
        user: str,
        session: ChatSession,
        tool_calls: list[Any],
        completed_results: list[dict[str, Any]],
        *,
        interrupted_index: int,
        interrupted_mutation: bool,
    ) -> None:
        """Keep every tool-use paired when a batch stops after a mutation."""
        results_by_id = {result["tool_use_id"]: result for result in completed_results}
        reconciled_results = []
        for index, tool_call in enumerate(tool_calls):
            if tool_call.id in results_by_id:
                reconciled_results.append(results_by_id[tool_call.id])
            elif index == interrupted_index:
                detail = (
                    "This action was interrupted; its outcome is uncertain."
                    if interrupted_mutation
                    else "This read-only lookup was interrupted; no changes were made."
                )
                reconciled_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_call.id,
                        "content": json.dumps({"error": detail}),
                    }
                )
            else:
                reconciled_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_call.id,
                        "content": json.dumps(
                            {"error": "Not run because an earlier tool call stopped."}
                        ),
                    }
                )
        session.add_tool_results(reconciled_results)
        session.pending_action = None
        self._indeterminate_sessions[user] = _CHAT_INDETERMINATE_MESSAGE
        session.add_assistant_message(_CHAT_INDETERMINATE_MESSAGE)

    def _mark_denied_tool_audit_incomplete(
        self,
        session: ChatSession,
        tool_calls: list[Any],
        completed_results: list[dict[str, Any]],
        *,
        rejected_index: int,
    ) -> None:
        """Record a blocked capability whose rejection audit is incomplete."""
        results_by_id = {result["tool_use_id"]: result for result in completed_results}
        reconciled_results = []
        for index, tool_call in enumerate(tool_calls):
            if tool_call.id in results_by_id:
                reconciled_results.append(results_by_id[tool_call.id])
            elif index == rejected_index:
                reconciled_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_call.id,
                        "content": json.dumps(
                            {
                                "error": (
                                    "This action was denied and not dispatched, "
                                    "but its audit lifecycle is incomplete."
                                )
                            }
                        ),
                    }
                )
            else:
                reconciled_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_call.id,
                        "content": json.dumps(
                            {"error": "Not run because an earlier tool call stopped."}
                        ),
                    }
                )
        session.add_tool_results(reconciled_results)
        session.pending_action = None
        session.add_assistant_message(_CHAT_READONLY_AUDIT_UNAVAILABLE_MESSAGE)

    @staticmethod
    def _mark_readonly_batch_cancelled(
        session: ChatSession,
        tool_calls: list[Any],
        completed_results: list[dict[str, Any]],
        *,
        cancelled_index: int,
        cancelled_result: str,
    ) -> None:
        results_by_id = {result["tool_use_id"]: result for result in completed_results}
        reconciled_results = []
        for index, tool_call in enumerate(tool_calls):
            if tool_call.id in results_by_id:
                reconciled_results.append(results_by_id[tool_call.id])
            elif index == cancelled_index:
                reconciled_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_call.id,
                        "content": cancelled_result,
                    }
                )
            else:
                reconciled_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_call.id,
                        "content": json.dumps(
                            {"error": "Not run because an earlier tool call stopped."}
                        ),
                    }
                )
        session.add_tool_results(reconciled_results)
        session.pending_action = None
        session.add_assistant_message(_CHAT_READONLY_CANCELLED_MESSAGE)

    async def handle_message_stream(
        self,
        user: str,
        message: str,
        auth_token: str,
        ticket_context: str | None = None,
        readonly: bool = False,
        delivery_id: str | None = None,
    ):
        """Yield response chunks for SSE streaming.

        Currently wraps the synchronous ``handle_message`` call and
        yields the complete response as a single chunk.  This keeps
        the SSE connection alive and prevents reverse-proxy timeouts.
        True token-level streaming can be layered in later.
        """
        delivery_id = delivery_id or uuid.uuid4().hex
        response = await self.handle_message(
            user=user,
            message=message,
            auth_token=auth_token,
            ticket_context=ticket_context,
            readonly=readonly,
            track_response_delivery=True,
            delivery_id=delivery_id,
        )
        try:
            yield response
        except BaseException:
            self.mark_response_delivery_interrupted(user, delivery_id)
            raise
        else:
            self.mark_response_delivery_sent(user, delivery_id)

    async def _handle_message(
        self,
        user: str,
        message: str,
        auth_token: str,
        mutation_state: dict[str, bool],
        ticket_context: str | None = None,
        readonly: bool = False,
    ) -> str:
        """Handle one already-context-bound web chat request."""
        session = self._sessions.get_or_create(user)

        # Check for pending action confirmation
        if session.pending_action and _is_confirmation(message):
            action = session.pending_action
            try:
                result = await execute_tool(
                    action["tool"],
                    action["input"],
                    self._client,
                    self._store_url,
                    auth_token,
                    audit=ChatToolAudit(
                        self._client, self._store_url, self._audit_token or ""
                    ),
                    tool_call_id=action.get("tool_call_id"),
                    parent_context=action.get("trace_context"),
                )
            except asyncio.CancelledError:
                self._mark_tool_outcome_indeterminate(
                    user, session, confirmation_exchange=True
                )
                raise
            except ChatAuditUnavailable as exc:
                if "outcome is indeterminate" in str(exc):
                    self._mark_tool_outcome_indeterminate(
                        user, session, confirmation_exchange=True
                    )
                    return _CHAT_INDETERMINATE_MESSAGE
                return (
                    "The action could not be started because audit recording is "
                    "unavailable. No action was attempted; you can retry or cancel."
                )
            if self._cancellation_requested():
                self._mark_tool_outcome_indeterminate(
                    user, session, confirmation_exchange=True
                )
                raise asyncio.CancelledError
            mutation_state["completed"] = True
            session.pending_action = None
            parsed = json.loads(result)
            if "error" in parsed:
                response_text = f"Action failed: {parsed['error']}"
            else:
                response_text = (
                    f"Done. {parsed.get('id', '')} {parsed.get('status', '')}"
                ).strip()
            # Remove the entire confirmation exchange:
            # assistant(tool_use) + user(tool_result) +
            # assistant(confirm_msg) = 3 messages.
            if len(session.messages) >= 3:
                session.messages = session.messages[:-3]
            session.add_user_message(message)
            session.add_assistant_message(response_text)
            return response_text
        elif session.pending_action and _is_cancellation(message):
            action = session.pending_action
            # A user rejection is authoritative even when the client
            # disconnects while its audit pair is being written.  Clear the
            # capability before the first await so it can never be approved
            # by a later message.
            session.pending_action = None
            try:
                await ChatToolAudit(
                    self._client, self._store_url, self._audit_token or ""
                ).reject(
                    action["tool"],
                    action["input"],
                    tool_call_id=action.get("tool_call_id"),
                    parent_context=action.get("trace_context"),
                )
            except asyncio.CancelledError:
                self._remove_confirmation_exchange(session)
                raise
            except ChatAuditUnavailable:
                self._mark_rejection_audit_indeterminate(user, session)
                return _CHAT_AUDIT_INDETERMINATE_MESSAGE
            if self._cancellation_requested():
                self._remove_confirmation_exchange(session)
                raise asyncio.CancelledError
            # Remove the entire confirmation exchange (3 msgs)
            if len(session.messages) >= 3:
                session.messages = session.messages[:-3]
            session.add_user_message(message)
            cancel_msg = "Action cancelled."
            session.add_assistant_message(cancel_msg)
            return cancel_msg
        elif session.pending_action:
            # Treat an unrelated reply as an explicit invalidation, rather
            # than silently discarding an agent-visible pending capability.
            # Keep the pending action until the started/rejected pair is
            # durable: if the audit service is down, do not continue this
            # conversation as though no decision had been made.
            action = session.pending_action
            session.pending_action = None
            try:
                await ChatToolAudit(
                    self._client, self._store_url, self._audit_token or ""
                ).reject(
                    action["tool"],
                    action["input"],
                    tool_call_id=action.get("tool_call_id"),
                    parent_context=action.get("trace_context"),
                )
            except asyncio.CancelledError:
                self._remove_confirmation_exchange(session)
                raise
            except ChatAuditUnavailable:
                self._mark_rejection_audit_indeterminate(user, session)
                return _CHAT_AUDIT_INDETERMINATE_MESSAGE
            if self._cancellation_requested():
                self._remove_confirmation_exchange(session)
                raise asyncio.CancelledError

        # Add context prefix for ticket-scoped chat.
        # Auto-fetch ticket details so the agent can diagnose
        # issues without asking the user for information that's
        # already on the ticket.
        if ticket_context:
            ticket_info = ""
            try:
                r = await self._client.get(
                    f"{self._store_url}/api/v1/tickets/{ticket_context}",
                    headers={"Authorization": f"Bearer {auth_token}"},
                )
                if self._cancellation_requested():
                    raise asyncio.CancelledError
                if r.status_code == 200:
                    t = r.json()
                    cf = t.get("custom_fields", {})
                    ticket_info = (
                        f"\nTicket {ticket_context}:"
                        f"\n- Status: {t.get('status', '?')}"
                        f"\n- Summary: {t.get('summary', '')}"
                    )
                    for key in (
                        "harness",
                        "board_selector",
                        "image_version",
                        "samples",
                    ):
                        val = cf.get(key)
                        if val:
                            ticket_info += f"\n- {key}: {val}"
                    gs = cf.get("guidance_summary")
                    if gs:
                        ticket_info += (
                            f"\n- Guidance: {gs.get('reason', '?')}"
                            f" - {gs.get('details', '')}"
                        )
                        actions = gs.get("suggested_actions", [])
                        if actions:
                            ticket_info += "\n  Actions: " + "; ".join(actions)
                    comments = t.get("comments", [])
                    if comments:
                        last = comments[-1]
                        ticket_info += (
                            f"\n- Last [{last.get('author', '')}]: "
                            f"{last.get('body', '')}"
                        )
            except Exception:
                if self._cancellation_requested():
                    raise asyncio.CancelledError
                pass

            # Always prefix with the current ticket so the
            # LLM knows which ticket "this" refers to, even
            # when the user switches between ticket views.
            # Full ticket data is fetched once per ticket;
            # subsequent messages get just the identity prefix.
            has_full_context = any(
                f"[Context: viewing ticket {ticket_context}]"
                in str(m.get("content", ""))
                for m in session.messages
            )
            if has_full_context:
                prefixed = f"[You are viewing ticket {ticket_context}]\nUser: {message}"
            else:
                prefixed = (
                    f"[Context: viewing ticket {ticket_context}]"
                    f"{ticket_info}\n\nUser: {message}"
                )
            session.add_user_message(prefixed)
        else:
            session.add_user_message(message)

        # Build system prompt with actual budget values.
        # Use replace() instead of format() because the prompt
        # contains JSON examples with literal braces.
        max_tokens = getattr(self._llm, "max_tokens", None) or "default"
        timeout = getattr(self._llm, "timeout", None) or "default"
        system_prompt = (
            CHAT_SYSTEM_PROMPT.replace("{max_tokens}", str(max_tokens))
            .replace("{timeout}", str(timeout))
            .replace("{max_tool_rounds}", str(self._max_tool_rounds))
        )

        # Run the tool-use loop
        # Filter tools for anonymous/readonly sessions
        if readonly:
            available_tools = [t for t in CHAT_TOOLS if t.name in READONLY_TOOLS]
            system_prompt += (
                "\n\nYou are in read-only mode (anonymous user). "
                "You can search and view tickets, read "
                "documentation, and answer questions. You "
                "cannot create tickets, send interjections, "
                "stop tickets, or manage users. If the user "
                "asks for these actions, explain that they "
                "need to log in with a bearer token first.\n"
                "Each message is independent — anonymous "
                "sessions do not retain conversation history "
                "between messages."
            )
        else:
            available_tools = CHAT_TOOLS

        # Deadline-aware execution: always respond before
        # the HTTP timeout.  Reserve time for a final LLM
        # call to summarize what was found.
        llm_timeout = getattr(self._llm, "timeout", None) or 60
        deadline = time.monotonic() + llm_timeout
        _WRAP_UP_RESERVE = 15  # seconds for final response
        _MIN_ROUND_TIME = 10  # minimum for a tool round
        mutating_action_completed = mutation_state["completed"]

        for _round in range(self._max_tool_rounds):
            remaining = deadline - time.monotonic()

            # Not enough time for another tool round —
            # ask the LLM to respond with what it has.
            if remaining < _WRAP_UP_RESERVE + _MIN_ROUND_TIME:
                wrap_prompt = (
                    system_prompt + "\n\n**IMPORTANT: You are almost out of "
                    "time. Respond NOW with the best answer "
                    "you can from the information you already "
                    "have. Do NOT call any more tools.**"
                )
                try:
                    response = await self._llm.complete(
                        system_prompt=wrap_prompt,
                        messages=session.messages,
                        tools=[],
                    )
                    if self._cancellation_requested():
                        raise asyncio.CancelledError
                    session.record_usage(response.usage)
                    text = response.text or (
                        "I wasn't able to fully complete your "
                        "request in time, but here's what I "
                        "found so far. Try asking a follow-up "
                        "for more details."
                    )
                except Exception:
                    if self._cancellation_requested():
                        raise asyncio.CancelledError
                    text = (
                        "I ran out of time processing your "
                        "request. Here's a tip: for ticket "
                        "creation, just describe what you want "
                        "and I'll create it directly."
                    )
                session.add_assistant_message(text)
                return text

            try:
                response = await self._llm.complete(
                    system_prompt=system_prompt,
                    messages=session.messages,
                    tools=available_tools,
                )
                if self._cancellation_requested():
                    raise asyncio.CancelledError
            except Exception as exc:
                if self._cancellation_requested():
                    raise asyncio.CancelledError from exc
                # LLM call failed (timeout, rate limit, etc.)
                logger.warning(
                    "Chat LLM call failed on round %d: %s",
                    _round + 1,
                    _safe_exception_summary(exc),
                )
                if _round > 0:
                    # Later rounds: return partial results.
                    text = (
                        "I encountered an issue while processing "
                        "your request. Here's what I found so "
                        "far — try asking a follow-up for more "
                        "details."
                    )
                    session.add_assistant_message(text)
                    return text

                # Round 0: retry once without tools for a
                # direct answer instead of giving up.
                logger.info("Chat retrying round 0 without tools")
                try:
                    response = await self._llm.complete(
                        system_prompt=system_prompt,
                        messages=session.messages,
                        tools=[],
                    )
                    if self._cancellation_requested():
                        raise asyncio.CancelledError
                    session.record_usage(response.usage)
                    text = response.text or (
                        "I wasn't able to use my tools for "
                        "this request. Could you try rephrasing?"
                    )
                except Exception as retry_exc:
                    if self._cancellation_requested():
                        raise asyncio.CancelledError from retry_exc
                    logger.warning(
                        "Chat retry without tools also failed: %s",
                        _safe_exception_summary(retry_exc),
                    )
                    text = (
                        "I'm having trouble processing your "
                        "request right now. Please try again."
                    )
                session.add_assistant_message(text)
                return text

            session.record_usage(response.usage)

            # No tool calls — return the text response
            if not response.tool_calls:
                text = response.text or ""
                session.add_assistant_message(text)
                return text

            # Process tool calls
            session.add_tool_use(
                [
                    {
                        "id": tc.id,
                        "name": tc.name,
                        "input": tc.input,
                    }
                    for tc in response.tool_calls
                ],
                response.raw_content,
            )

            tool_results = []
            known_tool_names = {tool.name for tool in CHAT_TOOLS}
            for index, tc in enumerate(response.tool_calls):
                # The model's advertised tool list is guidance, not an
                # authorization boundary. Recheck every returned call before
                # reaching the audited dispatcher.
                if tc.name not in known_tool_names:
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tc.id,
                            "content": json.dumps(
                                {"error": f"Unknown or unavailable tool: {tc.name}"}
                            ),
                        }
                    )
                    continue
                if readonly and tc.name not in READONLY_TOOLS:
                    try:
                        await ChatToolAudit(
                            self._client,
                            self._store_url,
                            self._audit_token or "",
                        ).reject(
                            tc.name,
                            tc.input,
                            tool_call_id=tc.id,
                        )
                    except asyncio.CancelledError:
                        self._mark_readonly_batch_cancelled(
                            session,
                            response.tool_calls,
                            tool_results,
                            cancelled_index=index,
                            cancelled_result=json.dumps(
                                {
                                    "error": (
                                        "This action was denied and its rejection "
                                        "audit pair completed during cancellation. "
                                        "No action was attempted."
                                    )
                                }
                            ),
                        )
                        raise
                    except (ChatAuditUnavailable, ValueError):
                        self._mark_denied_tool_audit_incomplete(
                            session,
                            response.tool_calls,
                            tool_results,
                            rejected_index=index,
                        )
                        return _CHAT_READONLY_AUDIT_UNAVAILABLE_MESSAGE
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tc.id,
                            "content": json.dumps(
                                {
                                    "error": (
                                        "This tool is unavailable in read-only mode. "
                                        "The request was rejected and recorded; "
                                        "no action was attempted."
                                    )
                                }
                            ),
                        }
                    )
                    continue

                # Code-level guardrail: destructive tools
                # require explicit user confirmation
                if tc.name in DESTRUCTIVE_TOOLS:
                    session.pending_action = {
                        "tool": tc.name,
                        "input": tc.input,
                        "tool_call_id": tc.id,
                        "trace_context": current_trace_context(),
                    }
                    confirm_msg = (
                        "Here is the ticket I’m ready to create:\n\n"
                        f"```json\n{json.dumps(tc.input, indent=2)}\n```\n\n"
                        "Please provide confirmation that you’d like me to submit it. "
                        "You can reply naturally, or say no to cancel."
                    )
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tc.id,
                            "content": json.dumps(
                                {
                                    "status": "confirmation_required",
                                    "message": "Awaiting user confirmation",
                                }
                            ),
                        }
                    )
                    session.add_tool_results(tool_results)
                    session.add_assistant_message(confirm_msg)
                    return confirm_msg

                try:
                    result = await execute_tool(
                        tc.name,
                        tc.input,
                        self._client,
                        self._store_url,
                        auth_token,
                        audit=ChatToolAudit(
                            self._client, self._store_url, self._audit_token or ""
                        ),
                        tool_call_id=tc.id,
                    )
                except asyncio.CancelledError:
                    if mutating_action_completed or tc.name not in READONLY_TOOLS:
                        self._mark_tool_batch_indeterminate(
                            user,
                            session,
                            response.tool_calls,
                            tool_results,
                            interrupted_index=index,
                            interrupted_mutation=tc.name not in READONLY_TOOLS,
                        )
                    else:
                        self._mark_tool_cancelled(
                            user,
                            session,
                            tool_name=tc.name,
                            confirmation_exchange=False,
                        )
                    raise
                except ChatAuditUnavailable as exc:
                    action_outcome_uncertain = "outcome is indeterminate" in str(exc)
                    if tc.name in READONLY_TOOLS and not mutating_action_completed:
                        self._remove_trailing_tool_use(session)
                        message_text = (
                            "The read-only lookup could not be recorded. "
                            "No changes were made; you can safely ask again."
                        )
                        session.add_assistant_message(message_text)
                        return message_text
                    if action_outcome_uncertain or mutating_action_completed:
                        self._mark_tool_batch_indeterminate(
                            user,
                            session,
                            response.tool_calls,
                            tool_results,
                            interrupted_index=index,
                            interrupted_mutation=(
                                tc.name not in READONLY_TOOLS
                                and action_outcome_uncertain
                            ),
                        )
                        return _CHAT_INDETERMINATE_MESSAGE
                    self._remove_trailing_tool_use(session)
                    message_text = (
                        "I couldn't start the requested action because audit "
                        "recording is unavailable. No action was attempted."
                    )
                    session.add_assistant_message(message_text)
                    return message_text
                if self._cancellation_requested():
                    if mutating_action_completed or tc.name not in READONLY_TOOLS:
                        self._mark_tool_batch_indeterminate(
                            user,
                            session,
                            response.tool_calls,
                            tool_results,
                            interrupted_index=index,
                            interrupted_mutation=tc.name not in READONLY_TOOLS,
                        )
                    else:
                        self._mark_tool_cancelled(
                            user,
                            session,
                            tool_name=tc.name,
                            confirmation_exchange=False,
                        )
                    raise asyncio.CancelledError
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tc.id,
                        "content": result,
                    }
                )
                if tc.name not in READONLY_TOOLS:
                    mutating_action_completed = True
                    mutation_state["completed"] = True

            session.add_tool_results(tool_results)

        # Exhausted tool rounds
        fallback = (
            "I've used all my available steps for this message. "
            "If you need more detail, try a follow-up question."
        )
        session.add_assistant_message(fallback)
        return fallback

    def get_history(self, user: str) -> list[dict[str, Any]]:
        """Return displayable chat history for a user."""
        session = self._sessions.get_or_create(user)
        history = []
        for msg in session.messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "user" and isinstance(content, str):
                history.append({"role": "user", "text": content})
            elif role == "assistant" and isinstance(content, str):
                history.append({"role": "assistant", "text": content})
            # Skip tool_use/tool_result entries in display
        return history

    def is_session_indeterminate(self, user: str) -> bool:
        """Whether an interrupted action requires reconciliation before chat."""
        return user in self._indeterminate_sessions

    def _mark_request_interrupted(
        self, user: str, delivery_id: str | None = None
    ) -> None:
        if user in self._indeterminate_sessions:
            return
        session = self._sessions.get_or_create(user)
        session.pending_action = None
        self._indeterminate_sessions[user] = _CHAT_DELIVERY_INDETERMINATE_MESSAGE
        if delivery_id is not None:
            self._delivery_indeterminate_ids[user] = delivery_id
        session.add_assistant_message(_CHAT_DELIVERY_INDETERMINATE_MESSAGE)

    def _arm_response_delivery_send_timeout(self, user: str, delivery_id: str) -> None:
        """Bound time spent sending a completed mutation's response body."""
        previous = self._delivery_send_timeouts.pop(user, None)
        if previous is not None:
            previous.cancel()
        loop = asyncio.get_running_loop()
        self._delivery_send_timeouts[user] = loop.call_later(
            _CHAT_DELIVERY_SEND_TIMEOUT_SECONDS,
            self.mark_response_delivery_interrupted,
            user,
            delivery_id,
        )

    def mark_response_delivery_sent(self, user: str, delivery_id: str) -> None:
        """Start the ACK window after the ASGI response send completes."""
        pending = self._pending_response_deliveries.get(user) == delivery_id
        indeterminate = self._delivery_indeterminate_ids.get(user) == delivery_id
        if not pending and not indeterminate:
            return
        send_timeout = self._delivery_send_timeouts.pop(user, None)
        if send_timeout is not None:
            send_timeout.cancel()
        self._sent_response_deliveries[user] = delivery_id
        if self._early_response_delivery_acks.get(user) == delivery_id:
            self._early_response_delivery_acks.pop(user, None)
            self.mark_response_delivery_complete(user, delivery_id)
            return
        if indeterminate:
            # A late send completion is not proof that the browser received
            # the body. Keep the pause until its request-scoped ACK arrives.
            return
        timeout = self._delivery_timeouts.pop(user, None)
        if timeout is not None:
            timeout.cancel()
        loop = asyncio.get_running_loop()
        self._delivery_timeouts[user] = loop.call_later(
            _CHAT_DELIVERY_ACK_TIMEOUT_SECONDS,
            self.mark_response_delivery_interrupted,
            user,
            delivery_id,
        )

    def mark_response_delivery_complete(self, user: str, delivery_id: str) -> bool:
        """Release a response only when its owning user acknowledges its ID."""
        pending_id = self._pending_response_deliveries.get(user)
        indeterminate_id = self._delivery_indeterminate_ids.get(user)
        if pending_id != delivery_id and indeterminate_id != delivery_id:
            return False
        if pending_id == delivery_id:
            if self._sent_response_deliveries.get(user) != delivery_id:
                self._early_response_delivery_acks[user] = delivery_id
                return False
            self._pending_response_deliveries.pop(user, None)
            self._sent_response_deliveries.pop(user, None)
            self._early_response_delivery_acks.pop(user, None)
            send_timeout = self._delivery_send_timeouts.pop(user, None)
            if send_timeout is not None:
                send_timeout.cancel()
            timeout = self._delivery_timeouts.pop(user, None)
            if timeout is not None:
                timeout.cancel()
        if indeterminate_id != delivery_id:
            return True
        self._delivery_indeterminate_ids.pop(user, None)
        self._indeterminate_sessions.pop(user, None)
        self._sent_response_deliveries.pop(user, None)
        self._early_response_delivery_acks.pop(user, None)
        session = self._sessions.get_or_create(user)
        session.messages = [
            message
            for message in session.messages
            if not (
                message.get("role") == "assistant"
                and message.get("content") == _CHAT_DELIVERY_INDETERMINATE_MESSAGE
            )
        ]
        return True

    def mark_response_delivery_interrupted(
        self, user: str, delivery_id: str | None
    ) -> None:
        """Pause only the matching request when response delivery is uncertain."""
        if delivery_id is None:
            return
        if self._pending_response_deliveries.get(user) == delivery_id:
            self._pending_response_deliveries.pop(user, None)
            self._sent_response_deliveries.pop(user, None)
            # Preserve an early ACK until a late ASGI send completion can
            # validate it. A later explicit ACK can also clear this pause.
            send_timeout = self._delivery_send_timeouts.pop(user, None)
            if send_timeout is not None:
                send_timeout.cancel()
            timeout = self._delivery_timeouts.pop(user, None)
            if timeout is not None:
                timeout.cancel()
            self._mark_request_interrupted(user, delivery_id)
            return
        active = self._active_response_deliveries.get(user)
        if active is None or active[0] != delivery_id:
            return
        mutation_state = active[1]
        mutation_state["delivery_interrupted"] = True
        if mutation_state["completed"]:
            self._mark_request_interrupted(user, delivery_id)

    def is_response_delivery_pending(self, user: str) -> bool:
        """Whether a completed mutation is awaiting browser acknowledgement."""
        return user in self._pending_response_deliveries

    def is_request_in_flight(self, user: str) -> bool:
        """Whether a request currently owns this user's chat session lock."""
        lock = self._session_locks.get(user)
        return lock is not None and lock.locked()

    def get_usage(self, user: str) -> dict[str, Any]:
        usage = self._sessions.get_usage(user)
        model = getattr(self._llm, "_model", "") or ""
        usage["model"] = model
        if model and usage.get("total_tokens", 0) > 0:
            try:
                from providers.cost import estimate_cost

                usage["estimated_cost_usd"] = estimate_cost(
                    model=model,
                    input_tokens=usage["input_tokens"],
                    output_tokens=usage["output_tokens"],
                    cache_read_input_tokens=usage.get(
                        "cache_read_input_tokens",
                        0,
                    ),
                    cache_creation_input_tokens=usage.get(
                        "cache_creation_input_tokens",
                        0,
                    ),
                )
            except Exception:
                usage["estimated_cost_usd"] = 0.0
        else:
            usage["estimated_cost_usd"] = 0.0
        return usage

    def clear_session(self, user: str) -> bool:
        lock = self._session_locks.get(user)
        if (
            lock is not None and lock.locked()
        ) or user in self._pending_response_deliveries:
            return False
        self._indeterminate_sessions.pop(user, None)
        self._delivery_indeterminate_ids.pop(user, None)
        self._sent_response_deliveries.pop(user, None)
        self._early_response_delivery_acks.pop(user, None)
        timeout = self._delivery_timeouts.pop(user, None)
        if timeout is not None:
            timeout.cancel()
        send_timeout = self._delivery_send_timeouts.pop(user, None)
        if send_timeout is not None:
            send_timeout.cancel()
        self._sessions.delete(user)
        return True
