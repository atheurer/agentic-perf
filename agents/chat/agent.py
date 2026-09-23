"""Chat agent — conversational interface for agentic-perf.

Maintains per-user session state and runs a tool-use loop
against the state store API. Each message from the user
triggers an LLM call that may invoke tools to search tickets,
create tickets, send interjections, etc.
"""

from __future__ import annotations

import json
import logging
import time
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
from .tools import CHAT_TOOLS, DESTRUCTIVE_TOOLS, ChatToolAudit, execute_tool

# Default fallback values if provider doesn't expose them
_DEFAULT_MAX_TOKENS = 4096
_DEFAULT_TIMEOUT = 60

logger = logging.getLogger(__name__)


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
        if len(self.messages) > _MAX_HISTORY:
            # Keep first message (context) and last N
            self.messages = self.messages[-_MAX_HISTORY:]


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

    async def handle_message(
        self,
        user: str,
        message: str,
        auth_token: str,
        ticket_context: str | None = None,
        readonly: bool = False,
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
        # Every web request has a root context before it can reach
        # ChatToolAudit.  Destructive confirmation actions persist this root in
        # the session so their later confirmation/cancellation is still a
        # child of the original request rather than an unrelated trace.
        root = new_trace_context(ticket_id=ticket_context, agent_id="chat-agent")
        token = bind_trace_context(root)
        try:
            return await self._handle_message(
                user,
                message,
                auth_token,
                ticket_context=ticket_context,
                readonly=readonly,
            )
        finally:
            reset_trace_context(token)

    async def _handle_message(
        self,
        user: str,
        message: str,
        auth_token: str,
        ticket_context: str | None = None,
        readonly: bool = False,
    ) -> str:
        """Handle one already-context-bound web chat request."""
        session = self._sessions.get_or_create(user)

        # Check for pending action confirmation
        if session.pending_action and _is_confirmation(message):
            action = session.pending_action
            session.pending_action = None
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
            session.pending_action = None
            await ChatToolAudit(
                self._client, self._store_url, self._audit_token or ""
            ).reject(
                action["tool"],
                action["input"],
                tool_call_id=action.get("tool_call_id"),
                parent_context=action.get("trace_context"),
            )
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
            await ChatToolAudit(
                self._client, self._store_url, self._audit_token or ""
            ).reject(
                action["tool"],
                action["input"],
                tool_call_id=action.get("tool_call_id"),
                parent_context=action.get("trace_context"),
            )
            session.pending_action = None

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
            from .tools import READONLY_TOOLS

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
                    session.record_usage(response.usage)
                    text = response.text or (
                        "I wasn't able to fully complete your "
                        "request in time, but here's what I "
                        "found so far. Try asking a follow-up "
                        "for more details."
                    )
                except Exception:
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
            except Exception as exc:
                # LLM call failed (timeout, rate limit, etc.)
                logger.warning(
                    "Chat LLM call failed on round %d: %s",
                    _round + 1,
                    exc,
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
                    session.record_usage(response.usage)
                    text = response.text or (
                        "I wasn't able to use my tools for "
                        "this request. Could you try rephrasing?"
                    )
                except Exception as retry_exc:
                    logger.warning(
                        "Chat retry without tools also failed: %s",
                        retry_exc,
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
            for tc in response.tool_calls:
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
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tc.id,
                        "content": result,
                    }
                )

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
        return self._sessions.delete(user)
