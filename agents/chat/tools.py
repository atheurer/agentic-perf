"""Chat agent tool definitions and handlers.

Each tool wraps a state store API call. The chat agent's
LLM calls these via tool_use to interact with tickets.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from providers.llm.base import ToolDefinition
from providers.tracing import (
    ActionType,
    IdempotencyDescriptor,
    LifecycleState,
    MonotonicTimer,
    OperationOutcome,
    TraceEventV1,
    TraceRecorder,
    child_context,
    current_trace_context,
    new_trace_context,
)

logger = logging.getLogger(__name__)


class ChatAuditUnavailable(RuntimeError):
    """The required durable audit entry could not be accepted.

    Chat mutations must never be attempted without an audit entry.  This is
    deliberately distinct from a handler error so callers can safely tell the
    user that no action was attempted.
    """


# This maps the advertised chat capability to the concrete handler and the
# state-store API domain it mutates.  It is intentionally local to the
# dispatcher: the audit event records the same operation owner that executes
# the request, rather than treating ChatToolAudit as the owner of every effect.
_CHAT_MUTATION_CONTRACTS: dict[str, tuple[str, str]] = {
    "create_ticket": ("agents.chat.tools._create_ticket", "/api/v1/tickets"),
    "start_ticket": (
        "agents.chat.tools._start_ticket",
        "/api/v1/tickets/{ticket_id}/transition",
    ),
    "send_interjection": (
        "agents.chat.tools._send_interjection",
        "/api/v1/tickets/{ticket_id}/interject",
    ),
    "reply_to_guidance": (
        "agents.chat.tools._reply_to_guidance",
        "/api/v1/tickets/{ticket_id}/comments",
    ),
    "update_ticket_fields": (
        "agents.chat.tools._update_ticket_fields",
        "/api/v1/tickets/{ticket_id}/fields",
    ),
    "stop_ticket": (
        "agents.chat.tools._stop_ticket",
        "/api/v1/tickets/{ticket_id}/stop",
    ),
    "create_user": ("agents.chat.tools._create_user", "/api/v1/users"),
    "rotate_user_token": (
        "agents.chat.tools._rotate_user_token",
        "/api/v1/users/{username}/rotate-token",
    ),
}


def _chat_target(context: Any, tool_name: str) -> str | None:
    """Return a redacted, stable external API target for a mutation event."""
    contract = _CHAT_MUTATION_CONTRACTS.get(tool_name)
    if not contract:
        return None
    _, route = contract
    return f"state-store:{route.format(ticket_id=context.ticket_id or 'unknown', username='redacted')}"


# High-impact actions that require code-level confirmation.
# Low-risk transitions (start, reply, interject) rely on
# the LLM's prompt-level confirmation only.

# Read-only tools available to anonymous users.
READONLY_TOOLS = frozenset(
    {
        "search_tickets",
        "get_ticket",
        "list_field_options",
        "list_skills",
        "read_skill",
        "read_doc",
    }
)

DESTRUCTIVE_TOOLS = frozenset(
    {
        "create_ticket",
        "stop_ticket",
        "create_user",
        "rotate_user_token",
    }
)


class ChatToolAudit:
    """Durably pair every chat-tool dispatch with a trace lifecycle.

    Chat tools do not inherit :class:`AgentBase`'s loop, so they must not rely
    on its audit boundary.  This small adapter is the sole production entry
    point for ``CHAT_TOOLS`` and writes the same trace envelope used by native
    agents.  The entry event is a fail-closed authorization boundary: a tool
    handler is not called until trace ingestion accepts it.  Terminal delivery
    is always attempted, including cancellation, so a started operation has a
    correlated outcome whenever the transport remains available.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        store_url: str,
        auth_token: str,
        *,
        record: Callable[[TraceEventV1], Any] | None = None,
    ) -> None:
        if not auth_token:
            raise ValueError("chat audit requires a service trace-ingestion token")
        self._client = client
        self._store_url = store_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {auth_token}"}
        self._record = record

    async def _emit(
        self,
        context: Any,
        state: LifecycleState,
        *,
        tool_name: str,
        tool_input: dict[str, Any],
        timer: MonotonicTimer | None = None,
        outcome: OperationOutcome | None = None,
        error: BaseException | None = None,
        required: bool = False,
    ) -> None:
        contract = _CHAT_MUTATION_CONTRACTS.get(tool_name)
        target = _chat_target(context, tool_name)
        attributes: dict[str, Any] = {"tool_surface": "chat"}
        if contract:
            owner, _ = contract
            attributes.update(
                operation_owner=owner,
                operation_key=f"chat:{tool_name}:{context.tool_call_id or context.action_id}",
            )
        event = TraceRecorder().record(
            context,
            ActionType.TOOL,
            state,
            phase=tool_name,
            target=target,
            duration_ms=timer.elapsed_ms() if timer else None,
            outcome=outcome,
            error=error,
            attributes=attributes,
        )
        if contract:
            # Do not persist raw chat input in traces.  A stable digest links
            # retries/replays while the per-call key identifies this operation
            # lifecycle to the event consumer.
            request_hash = hashlib.sha256(
                json.dumps(tool_input, sort_keys=True, default=str).encode()
            ).hexdigest()
            event = event.model_copy(
                update={
                    "idempotency": IdempotencyDescriptor(
                        key=attributes["operation_key"], request_hash=request_hash
                    )
                }
            )
        try:
            if self._record is not None:
                result = self._record(event)
                if hasattr(result, "__await__"):
                    await result
                return
            response = await self._client.post(
                f"{self._store_url}/api/v1/traces/events",
                headers=self._headers,
                json=event.model_dump(mode="json"),
            )
            response.raise_for_status()
        except Exception as exc:
            logger.exception("failed to record chat tool audit event")
            if required:
                raise ChatAuditUnavailable(
                    "audit ingestion unavailable; chat tool was not attempted"
                ) from exc

    async def invoke(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        handler: Callable[[], Awaitable[str]],
        *,
        tool_call_id: str | None = None,
        parent_context: Any | None = None,
    ) -> str:
        """Record ``STARTED`` and one terminal event around one named tool."""
        ticket_id = tool_input.get("ticket_id")
        root_context = (
            parent_context
            or current_trace_context()
            or new_trace_context(
                ticket_id=ticket_id if isinstance(ticket_id, str) else "chat",
                agent_id="chat-agent",
            )
        )
        context = child_context(
            root_context,
            tool_call_id=tool_call_id or f"chat-{root_context.action_id}",
            **(
                {"ticket_id": ticket_id}
                if isinstance(ticket_id, str) and ticket_id
                else {}
            ),
        )
        timer = MonotonicTimer()
        await self._emit(
            context,
            LifecycleState.STARTED,
            tool_name=tool_name,
            tool_input=tool_input,
            required=True,
        )
        try:
            result = await handler()
        except BaseException as exc:
            state = (
                LifecycleState.CANCELLED
                if isinstance(exc, asyncio.CancelledError)
                else LifecycleState.FAILED
            )
            outcome = (
                OperationOutcome.CANCELLED
                if state == LifecycleState.CANCELLED
                else OperationOutcome.FAILURE
            )
            try:
                # The handler has already crossed its effect boundary.  Never
                # quietly return an ordinary handler failure when its terminal
                # audit outcome could not be persisted: callers must treat the
                # operation as indeterminate and reconcile it.
                await self._emit(
                    context,
                    state,
                    tool_name=tool_name,
                    tool_input=tool_input,
                    timer=timer,
                    outcome=outcome,
                    error=exc,
                    required=True,
                )
            except ChatAuditUnavailable as audit_error:
                raise ChatAuditUnavailable(
                    "chat tool outcome is indeterminate; audit terminal was not persisted"
                ) from audit_error
            raise
        failed = False
        try:
            payload = json.loads(result)
            failed = isinstance(payload, dict) and "error" in payload
        except (TypeError, json.JSONDecodeError):
            pass
        try:
            await self._emit(
                context,
                LifecycleState.FAILED if failed else LifecycleState.COMPLETED,
                tool_name=tool_name,
                tool_input=tool_input,
                timer=timer,
                outcome=(
                    OperationOutcome.FAILURE if failed else OperationOutcome.SUCCESS
                ),
                required=True,
            )
        except ChatAuditUnavailable as audit_error:
            raise ChatAuditUnavailable(
                "chat tool outcome is indeterminate; audit terminal was not persisted"
            ) from audit_error
        return result

    async def reject(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        *,
        tool_call_id: str | None = None,
        parent_context: Any | None = None,
    ) -> None:
        """Record a user-rejected confirmation without running its handler.

        A confirmation is an agent-visible capability invocation.  Recording a
        started/rejected pair makes a later user cancellation causally visible
        while preserving the original web-request root across the two HTTP
        requests that make up the confirmation flow.
        """
        ticket_id = tool_input.get("ticket_id")
        root_context = (
            parent_context
            or current_trace_context()
            or new_trace_context(
                ticket_id=ticket_id if isinstance(ticket_id, str) else "chat",
                agent_id="chat-agent",
            )
        )
        context = child_context(
            root_context,
            tool_call_id=tool_call_id or f"chat-{root_context.action_id}",
            **(
                {"ticket_id": ticket_id}
                if isinstance(ticket_id, str) and ticket_id
                else {}
            ),
        )
        timer = MonotonicTimer()
        await self._emit(
            context,
            LifecycleState.STARTED,
            tool_name=tool_name,
            tool_input=tool_input,
            required=True,
        )
        await self._emit(
            context,
            LifecycleState.REJECTED,
            tool_name=tool_name,
            tool_input=tool_input,
            timer=timer,
            outcome=OperationOutcome.REJECTED,
            required=True,
        )


def _require(params: dict[str, Any], *keys: str) -> str | None:
    """Check required params, return error string or None."""
    missing = [k for k in keys if k not in params]
    if missing:
        return json.dumps(
            {"error": f"Missing required parameters: {', '.join(missing)}"}
        )
    return None


CHAT_TOOLS: list[ToolDefinition] = [
    ToolDefinition(
        name="search_tickets",
        description=(
            "Search tickets by status, board type, harness, "
            "or keywords. Returns a summary list."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Filter by status (e.g., 'executing_benchmark', 'closed')",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 10)",
                },
                "query": {
                    "type": "string",
                    "description": "Keyword search in summary/description",
                },
            },
        },
    ),
    ToolDefinition(
        name="get_ticket",
        description=(
            "Get full details for a specific ticket by ID. "
            "Includes status, comments, custom_fields, and "
            "guidance_summary if available."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "ticket_id": {
                    "type": "string",
                    "description": "Ticket ID (e.g., PERF-ABC123)",
                },
            },
            "required": ["ticket_id"],
        },
    ),
    ToolDefinition(
        name="create_ticket",
        description=(
            "Create a new ticket draft. The chat system shows "
            "the draft and asks the user for conversational "
            "confirmation before executing this tool."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Ticket summary",
                },
                "description": {
                    "type": "string",
                    "description": "Ticket description",
                },
                "custom_fields": {
                    "type": "object",
                    "description": "Custom fields including harness, board_selector, samples, etc.",
                },
            },
            "required": ["summary", "description", "custom_fields"],
        },
    ),
    ToolDefinition(
        name="start_ticket",
        description=(
            "Transition a new ticket to triage_pending to "
            "start processing. Call after create_ticket."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "ticket_id": {
                    "type": "string",
                    "description": "Ticket ID to start",
                },
            },
            "required": ["ticket_id"],
        },
    ),
    ToolDefinition(
        name="send_interjection",
        description=(
            "Send a message to a running ticket's agent. "
            "Used to correct hypotheses, add context, or "
            "override assumptions mid-execution."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "ticket_id": {
                    "type": "string",
                    "description": "Ticket ID",
                },
                "message": {
                    "type": "string",
                    "description": "Message to send to the agent",
                },
            },
            "required": ["ticket_id", "message"],
        },
    ),
    ToolDefinition(
        name="reply_to_guidance",
        description=(
            "Reply to a ticket at awaiting_customer_guidance "
            "and resume it. Use this (not send_interjection) "
            "when a ticket is paused for guidance. Adds a "
            "comment and transitions the ticket to resume "
            "processing."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "ticket_id": {
                    "type": "string",
                    "description": "Ticket ID",
                },
                "message": {
                    "type": "string",
                    "description": "Response to the agent's question",
                },
                "resume_status": {
                    "type": "string",
                    "description": (
                        "Status to transition to. Check the "
                        "ticket's status_trail to find where "
                        "it was before hitting guidance. "
                        "Common values: 'preparing_platform', "
                        "'analyzing', 'awaiting_review', "
                        "'executing_benchmark', 'building_image'. "
                        "Required if the ticket has no "
                        "action_required hint."
                    ),
                },
                "approval_request_id": {
                    "type": "string",
                    "description": "Explicit approval request ID when selecting among multiple pending requests.",
                },
            },
            "required": ["ticket_id", "message"],
        },
    ),
    ToolDefinition(
        name="update_ticket_fields",
        description=(
            "Update custom_fields on a ticket. Use this to fix "
            "missing or incorrect directives like image_version, "
            "board_selector, etc. on a paused ticket before "
            "resuming it."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "ticket_id": {
                    "type": "string",
                    "description": "Ticket ID",
                },
                "fields": {
                    "type": "object",
                    "description": (
                        "Fields to update in custom_fields. "
                        "Also updates directives if the field "
                        "is a known directive."
                    ),
                },
            },
            "required": ["ticket_id", "fields"],
        },
    ),
    ToolDefinition(
        name="stop_ticket",
        description=("Stop a running ticket. Confirm with the user first."),
        input_schema={
            "type": "object",
            "properties": {
                "ticket_id": {
                    "type": "string",
                    "description": "Ticket ID to stop",
                },
                "reason": {
                    "type": "string",
                    "description": "Reason for stopping",
                },
            },
            "required": ["ticket_id"],
        },
    ),
    ToolDefinition(
        name="list_field_options",
        description=(
            "List valid values for ticket fields like "
            "board_selector, harness, image_version, "
            "image_name, image_type. Call this before "
            "creating a ticket to ensure correct values."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "field": {
                    "type": "string",
                    "description": (
                        "Field name: board_selector, harness, "
                        "image_version, image_name, image_type"
                    ),
                },
            },
            "required": ["field"],
        },
    ),
    ToolDefinition(
        name="list_skills",
        description=(
            "List available skill documentation categories "
            "and files. These are documentation categories, "
            "NOT a complete list of supported harnesses or "
            "capabilities. The system may support additional "
            "harnesses and tools not listed here."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": (
                        "Harness category (e.g., 'boot-time', "
                        "'caib', 'jumpstarter', 'fleet'). "
                        "Leave empty to list all categories."
                    ),
                },
            },
        },
    ),
    ToolDefinition(
        name="read_skill",
        description=(
            "Read a skill document for domain knowledge. "
            "Use list_skills first to find available docs. "
            "Skills explain harness usage, board configuration, "
            "image selection, investigation methods, etc."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "Skill category (e.g., 'boot-time')",
                },
                "filename": {
                    "type": "string",
                    "description": "Skill filename (e.g., 'boot-time-analysis.md')",
                },
            },
            "required": ["category", "filename"],
        },
    ),
    ToolDefinition(
        name="read_doc",
        description=(
            "Read a documentation page. Available docs include "
            "ticket-directives.md (field formats, image_version "
            "vs image_build), configuration.md, user-guide.md, "
            "architecture.md."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Doc filename (e.g., 'ticket-directives.md')",
                },
            },
            "required": ["filename"],
        },
    ),
    ToolDefinition(
        name="list_users",
        description=(
            "List all users in the system with their admin "
            "status. Available to all authenticated users."
        ),
        input_schema={
            "type": "object",
            "properties": {},
        },
    ),
    ToolDefinition(
        name="create_user",
        description=(
            "Create a new user account. Admin only. Returns "
            "the new user's bearer token."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "username": {
                    "type": "string",
                    "description": "Username (lowercase, alphanumeric, hyphens, underscores)",
                },
                "is_admin": {
                    "type": "boolean",
                    "description": "Whether the user should have admin privileges (default false)",
                },
            },
            "required": ["username"],
        },
    ),
    ToolDefinition(
        name="rotate_user_token",
        description=(
            "Rotate a user's bearer token. Admin only. Returns the new token."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "username": {
                    "type": "string",
                    "description": "Username whose token to rotate",
                },
            },
            "required": ["username"],
        },
    ),
]


async def execute_tool(
    tool_name: str,
    tool_input: dict[str, Any],
    client: httpx.AsyncClient,
    store_url: str,
    auth_token: str,
    *,
    audit: ChatToolAudit | None = None,
    tool_call_id: str | None = None,
    parent_context: Any | None = None,
) -> str:
    """Execute a chat tool through its required audit boundary."""
    headers = {"Authorization": f"Bearer {auth_token}"}

    async def _dispatch() -> str:
        try:
            return await _dispatch_tool(
                tool_name, tool_input, client, store_url, headers
            )
        except Exception as exc:
            # Sanitize: do not expose internal paths or stack traces
            msg = str(exc)
            # Strip file paths and module references
            if "/" in msg or "\\" in msg:
                msg = "An internal error occurred"
            return json.dumps({"error": msg[:200]})

    # This is the sole production dispatcher.  A missing audit transport is a
    # fail-closed error, never a convenience route around trace evidence.
    if audit is None:
        raise ChatAuditUnavailable("chat tool dispatch requires an audit boundary")
    return await audit.invoke(
        tool_name,
        tool_input,
        _dispatch,
        tool_call_id=tool_call_id,
        parent_context=parent_context,
    )


async def _dispatch_tool(
    tool_name: str,
    tool_input: dict[str, Any],
    client: httpx.AsyncClient,
    store_url: str,
    headers: dict[str, str],
) -> str:
    """Resolve one advertised chat name to its concrete implementation."""
    if tool_name == "search_tickets":
        return await _search_tickets(client, store_url, headers, tool_input)
    elif tool_name == "get_ticket":
        return await _get_ticket(client, store_url, headers, tool_input)
    elif tool_name == "create_ticket":
        return await _create_ticket(client, store_url, headers, tool_input)
    elif tool_name == "start_ticket":
        return await _start_ticket(client, store_url, headers, tool_input)
    elif tool_name == "send_interjection":
        return await _send_interjection(client, store_url, headers, tool_input)
    elif tool_name == "reply_to_guidance":
        return await _reply_to_guidance(client, store_url, headers, tool_input)
    elif tool_name == "list_skills":
        return _list_skills(tool_input)
    elif tool_name == "read_skill":
        return _read_skill(tool_input)
    elif tool_name == "read_doc":
        return _read_doc(tool_input)
    elif tool_name == "list_users":
        return await _list_users(client, store_url, headers, tool_input)
    elif tool_name == "create_user":
        return await _create_user(client, store_url, headers, tool_input)
    elif tool_name == "rotate_user_token":
        return await _rotate_user_token(client, store_url, headers, tool_input)
    elif tool_name == "list_field_options":
        return await _list_field_options(client, store_url, headers, tool_input)
    elif tool_name == "update_ticket_fields":
        return await _update_ticket_fields(client, store_url, headers, tool_input)
    elif tool_name == "stop_ticket":
        return await _stop_ticket(client, store_url, headers, tool_input)
    return json.dumps({"error": f"Unknown tool: {tool_name}"})


async def _search_tickets(
    client: httpx.AsyncClient,
    store_url: str,
    headers: dict[str, str],
    params: dict[str, Any],
) -> str:
    limit = params.get("limit", 10)
    r = await client.get(
        f"{store_url}/api/v1/tickets",
        headers=headers,
        params={"limit": limit},
    )
    r.raise_for_status()
    tickets = r.json()

    # Filter by status/query if provided
    status_filter = params.get("status", "").lower()
    query_filter = params.get("query", "").lower()

    results = []
    for t in tickets:
        if status_filter and t.get("status", "").lower() != status_filter:
            continue
        if query_filter:
            text = (t.get("summary", "") + " " + t.get("description", "")).lower()
            if query_filter not in text:
                continue
        results.append(
            {
                "id": t["id"],
                "summary": t.get("summary", "")[:100],
                "status": t.get("status", ""),
                "created_at": t.get("created_at", "")[:19],
            }
        )
        if len(results) >= limit:
            break

    return json.dumps({"tickets": results, "count": len(results)})


async def _get_ticket(
    client: httpx.AsyncClient,
    store_url: str,
    headers: dict[str, str],
    params: dict[str, Any],
) -> str:
    err = _require(params, "ticket_id")
    if err:
        return err
    ticket_id = params["ticket_id"]
    r = await client.get(
        f"{store_url}/api/v1/tickets/{ticket_id}",
        headers=headers,
    )
    r.raise_for_status()
    ticket = r.json()

    # Return a trimmed view
    cf = ticket.get("custom_fields", {})
    comments = ticket.get("comments", [])
    last_comments = comments[-5:] if comments else []

    directives = cf.get("directives", {})
    result = {
        "id": ticket["id"],
        "summary": ticket.get("summary", ""),
        "description": ticket.get("description", ""),
        "status": ticket.get("status", ""),
        "status_trail": ticket.get("status_trail", []),
        "created_at": ticket.get("created_at", ""),
        "harness": directives.get("harness") or cf.get("harness"),
        "board_selector": (
            directives.get("board_selector") or cf.get("board_selector")
        ),
        "samples": cf.get("samples"),
        "image_version": (directives.get("image_version") or cf.get("image_version")),
        "directives": directives,
        "guidance_summary": cf.get("guidance_summary"),
        "verdict": cf.get("verdict"),
        "benchmark_status": cf.get("benchmark_status"),
        "image_build_result": cf.get("image_build_result"),
        "hypothesis": cf.get("hypothesis"),
        "last_comments": [
            {
                "author": c.get("author", ""),
                "body": c.get("body", "")[:500],
            }
            for c in last_comments
        ],
    }
    return json.dumps(result)


async def _create_ticket(
    client: httpx.AsyncClient,
    store_url: str,
    headers: dict[str, str],
    params: dict[str, Any],
) -> str:
    err = _require(params, "summary", "description")
    if err:
        return err
    r = await client.post(
        f"{store_url}/api/v1/tickets",
        headers=headers,
        json={
            "summary": params["summary"],
            "description": params["description"],
            "custom_fields": params.get("custom_fields", {}),
        },
    )
    r.raise_for_status()
    ticket = r.json()
    ticket_id = ticket["id"]

    # Auto-start: transition to triage_pending so the
    # user doesn't have to separately say "start it"
    try:
        r2 = await client.post(
            f"{store_url}/api/v1/tickets/{ticket_id}/transition",
            headers=headers,
            json={"status": "triage_pending"},
        )
        r2.raise_for_status()
        return json.dumps({"id": ticket_id, "status": "triage_pending"})
    except Exception:
        # Created but failed to start — still report success
        return json.dumps(
            {
                "id": ticket_id,
                "status": "created",
                "note": "Created but auto-start failed",
            }
        )


async def _start_ticket(
    client: httpx.AsyncClient,
    store_url: str,
    headers: dict[str, str],
    params: dict[str, Any],
) -> str:
    ticket_id = params["ticket_id"]
    r = await client.post(
        f"{store_url}/api/v1/tickets/{ticket_id}/transition",
        headers=headers,
        json={"status": "triage_pending"},
    )
    r.raise_for_status()
    return json.dumps({"id": ticket_id, "status": "triage_pending"})


async def _send_interjection(
    client: httpx.AsyncClient,
    store_url: str,
    headers: dict[str, str],
    params: dict[str, Any],
) -> str:
    err = _require(params, "ticket_id", "message")
    if err:
        return err
    ticket_id = params["ticket_id"]
    r = await client.post(
        f"{store_url}/api/v1/tickets/{ticket_id}/interject",
        headers=headers,
        json={"message": params["message"]},
    )
    r.raise_for_status()
    return json.dumps({"status": "interjection_sent"})


async def _reply_to_guidance(
    client: httpx.AsyncClient,
    store_url: str,
    headers: dict[str, str],
    params: dict[str, Any],
) -> str:
    ticket_id = params["ticket_id"]
    message = params["message"]

    # Approval replies are authority-bearing only when exactly one immutable
    # request is pending.  Generic clarification replies retain the legacy
    # comment/transition behavior below.
    approvals_response = await client.get(
        f"{store_url}/api/v1/tickets/{ticket_id}/approvals",
        headers=headers,
    )
    approvals_response.raise_for_status()
    pending = [
        item
        for item in approvals_response.json().get("approvals", [])
        if item.get("status") == "pending"
    ]
    selected_id = params.get("approval_request_id")
    if selected_id:
        pending = [
            item for item in pending if item.get("approval_request_id") == selected_id
        ]
        if not pending:
            return json.dumps(
                {
                    "status": "approval_rejected",
                    "message": "approval_request_id is unknown or no longer pending",
                }
            )
    normalized = message.strip().lower()
    decision = {
        "approve": "approved",
        "approved": "approved",
        "reject": "rejected",
        "rejected": "rejected",
        "request changes": "changes_requested",
        "changes_requested": "changes_requested",
    }.get(normalized)
    if decision and pending:
        if len(pending) != 1:
            return json.dumps(
                {
                    "status": "ambiguous_approval",
                    "message": "Multiple approval requests are pending; specify approval_request_id.",
                    "approval_request_ids": [
                        item["approval_request_id"] for item in pending
                    ],
                }
            )
        resolved = await client.post(
            f"{store_url}/api/v1/tickets/{ticket_id}/approvals/"
            f"{pending[0]['approval_request_id']}/resolve",
            headers=headers,
            json={"decision": decision, "comment": message},
        )
        resolved.raise_for_status()
        ticket_response = await client.get(
            f"{store_url}/api/v1/tickets/{ticket_id}",
            headers=headers,
        )
        ticket_response.raise_for_status()
        previous = ticket_response.json().get("previous_status")
        if previous:
            resumed = await client.post(
                f"{store_url}/api/v1/tickets/{ticket_id}/transition",
                headers=headers,
                json={
                    "status": previous,
                    "comment": "Benchmark approval resolved; resuming pipeline",
                },
            )
            resumed.raise_for_status()
        if decision != "approved":
            return json.dumps({"status": "approval_resolved", "decision": decision})
        return json.dumps({"status": "approval_resolved", "decision": decision})

    # Determine resume status BEFORE adding comment
    # (adding a comment changes the last comment, losing
    # the action_required hint)
    resume_status = params.get("resume_status")
    if not resume_status:
        r = await client.get(
            f"{store_url}/api/v1/tickets/{ticket_id}",
            headers=headers,
        )
        r.raise_for_status()
        ticket = r.json()
        # Search comments in reverse for action_required
        for comment in reversed(ticket.get("comments", [])):
            action = comment.get("action_required", {})
            if action and action.get("body", {}).get("status"):
                resume_status = action["body"]["status"]
                break

        # Fallback: infer from status trail (the status
        # before awaiting_customer_guidance)
        if not resume_status:
            trail = ticket.get("status_trail", [])
            for s in reversed(trail):
                if s != "awaiting_customer_guidance":
                    resume_status = s
                    break

    # Add comment
    await client.post(
        f"{store_url}/api/v1/tickets/{ticket_id}/comments",
        headers=headers,
        json={"author": "chat-agent", "body": message},
    )

    if resume_status:
        r = await client.post(
            f"{store_url}/api/v1/tickets/{ticket_id}/transition",
            headers=headers,
            json={"status": resume_status},
        )
        r.raise_for_status()
        return json.dumps(
            {
                "status": "replied_and_resumed",
                "new_status": resume_status,
            }
        )

    return json.dumps(
        {
            "status": "replied",
            "note": "Comment added but could not determine resume status",
        }
    )


def _list_skills(params: dict[str, Any]) -> str:
    """List available skill documents with descriptions."""
    from pathlib import Path

    skills_dir = Path(__file__).resolve().parents[2] / "skills"
    category = params.get("category", "")

    def _title(path: Path) -> str:
        """Extract title from first markdown heading."""
        try:
            for line in path.read_text().splitlines()[:5]:
                if line.startswith("# "):
                    return line[2:].strip()
        except Exception:
            pass
        return path.stem

    if category:
        cat_dir = skills_dir / category
        if not cat_dir.is_dir():
            return json.dumps({"error": f"Unknown category: {category}"})
        files = [
            {"file": f.name, "title": _title(f)}
            for f in sorted(cat_dir.iterdir())
            if f.suffix == ".md"
        ]
        return json.dumps({"category": category, "files": files})

    categories = []
    for d in sorted(skills_dir.iterdir()):
        if not d.is_dir():
            continue
        first_md = next(
            (f for f in sorted(d.iterdir()) if f.suffix == ".md"),
            None,
        )
        desc = _title(first_md) if first_md else d.name
        categories.append({"name": d.name, "description": desc})
    return json.dumps({"categories": categories})


def _read_skill(params: dict[str, Any]) -> str:
    """Read a skill document."""
    from pathlib import Path

    skills_dir = Path(__file__).resolve().parents[2] / "skills"
    category = params.get("category", "")
    filename = params.get("filename", "")

    path = (skills_dir / category / filename).resolve()
    if not path.is_relative_to(skills_dir.resolve()):
        return json.dumps({"error": "Invalid path"})
    if not path.is_file():
        return json.dumps({"error": f"Not found: {category}/{filename}"})

    content = path.read_text()
    # Truncate large docs to stay within token budget
    if len(content) > 8000:
        content = content[:8000] + "\n\n[truncated]"
    return json.dumps({"file": filename, "content": content})


def _read_doc(params: dict[str, Any]) -> str:
    """Read a documentation page."""
    from pathlib import Path

    docs_dir = Path(__file__).resolve().parents[2] / "docs"
    filename = params.get("filename", "")

    path = (docs_dir / filename).resolve()
    if not path.is_relative_to(docs_dir.resolve()):
        return json.dumps({"error": "Invalid path"})
    if not path.is_file():
        return json.dumps({"error": f"Not found: {filename}"})

    content = path.read_text()
    if len(content) > 8000:
        content = content[:8000] + "\n\n[truncated]"
    return json.dumps({"file": filename, "content": content})


async def _list_users(
    client: httpx.AsyncClient,
    store_url: str,
    headers: dict[str, str],
    params: dict[str, Any],
) -> str:
    r = await client.get(
        f"{store_url}/api/v1/users",
        headers=headers,
    )
    if r.status_code == 403:
        return json.dumps({"error": "Permission denied"})
    r.raise_for_status()
    users = r.json()
    result = [
        {
            "username": u.get("username", ""),
            "is_admin": u.get("is_admin", False),
        }
        for u in users
    ]
    return json.dumps({"users": result, "count": len(result)})


async def _create_user(
    client: httpx.AsyncClient,
    store_url: str,
    headers: dict[str, str],
    params: dict[str, Any],
) -> str:
    err = _require(params, "username")
    if err:
        return err
    r = await client.post(
        f"{store_url}/api/v1/users",
        headers=headers,
        json={
            "username": params["username"],
            "is_admin": params.get("is_admin", False),
        },
    )
    if r.status_code == 403:
        return json.dumps({"error": "Admin privileges required to create users"})
    r.raise_for_status()
    data = r.json()
    return json.dumps(
        {
            "username": params["username"],
            "token": data.get("token", ""),
            "status": "created",
        }
    )


async def _rotate_user_token(
    client: httpx.AsyncClient,
    store_url: str,
    headers: dict[str, str],
    params: dict[str, Any],
) -> str:
    username = params["username"]
    r = await client.post(
        f"{store_url}/api/v1/users/{username}/rotate-token",
        headers=headers,
    )
    if r.status_code == 403:
        return json.dumps({"error": "Admin privileges required to rotate tokens"})
    r.raise_for_status()
    data = r.json()
    return json.dumps(
        {
            "username": username,
            "token": data.get("token", ""),
            "status": "rotated",
        }
    )


async def _list_field_options(
    client: httpx.AsyncClient,
    store_url: str,
    headers: dict[str, str],
    params: dict[str, Any],
) -> str:
    field = params.get("field", "").lower()

    # Query the state store for available benchmarks/boards
    # by examining existing tickets and config
    options: dict[str, Any] = {}

    if field == "harness":
        # List available harnesses from the benchmark registry
        try:
            r = await client.get(
                f"{store_url}/api/v1/tickets",
                headers=headers,
                params={"limit": 100},
            )
            r.raise_for_status()
            tickets = r.json()
            harnesses = set()
            for t in tickets:
                h = t.get("custom_fields", {}).get("harness")
                if h:
                    harnesses.add(h)
            options = {
                "field": "harness",
                "values": sorted(harnesses) or ["boot-time"],
                "note": "Use exact harness names as listed",
            }
        except Exception:
            options = {
                "field": "harness",
                "values": ["boot-time"],
                "note": "Could not query — showing known defaults",
            }
    elif field == "board_selector":
        options = {
            "field": "board_selector",
            "format": "board-type=<type>",
            "note": (
                "Query available boards from recent tickets. Common values shown below."
            ),
        }
        try:
            r = await client.get(
                f"{store_url}/api/v1/tickets",
                headers=headers,
                params={"limit": 200},
            )
            r.raise_for_status()
            tickets = r.json()
            selectors = set()
            for t in tickets:
                bs = t.get("custom_fields", {}).get("board_selector")
                if bs and "=" in bs:
                    selectors.add(bs)
            options["values"] = sorted(selectors)
        except Exception:
            options["values"] = []
    elif field in ("image_version", "image_name", "image_type"):
        options = {
            "field": field,
            "note": "Query from recent tickets",
        }
        try:
            r = await client.get(
                f"{store_url}/api/v1/tickets",
                headers=headers,
                params={"limit": 100},
            )
            r.raise_for_status()
            tickets = r.json()
            values = set()
            for t in tickets:
                cf = t.get("custom_fields", {})
                v = cf.get(field) or cf.get("directives", {}).get(field)
                if v and isinstance(v, str):
                    values.add(v)
            options["values"] = sorted(values)
        except Exception:
            options["values"] = []
    else:
        options = {
            "field": field,
            "error": (
                "Unknown field. Valid fields: "
                "board_selector, harness, image_version, "
                "image_name, image_type"
            ),
        }

    return json.dumps(options)


async def _update_ticket_fields(
    client: httpx.AsyncClient,
    store_url: str,
    headers: dict[str, str],
    params: dict[str, Any],
) -> str:
    err = _require(params, "ticket_id", "fields")
    if err:
        return err
    ticket_id = params["ticket_id"]
    fields = params["fields"]

    # Also update directives for known directive fields
    directive_keys = {
        "image_version",
        "image_name",
        "image_type",
        "release",
        "board_selector",
        "harness",
        "samples",
    }
    directives_update = {}
    for k, v in fields.items():
        if k in directive_keys:
            directives_update[k] = v

    update = dict(fields)
    if directives_update:
        # Merge with existing directives
        r = await client.get(
            f"{store_url}/api/v1/tickets/{ticket_id}",
            headers=headers,
        )
        r.raise_for_status()
        ticket = r.json()
        existing = ticket.get("custom_fields", {}).get("directives", {})
        existing.update(directives_update)
        update["directives"] = existing

    r = await client.patch(
        f"{store_url}/api/v1/tickets/{ticket_id}/fields",
        headers=headers,
        json={"fields": update},
    )
    if r.status_code == 403:
        return json.dumps({"error": "Permission denied"})
    r.raise_for_status()
    return json.dumps(
        {
            "status": "updated",
            "fields_set": list(fields.keys()),
        }
    )


async def _stop_ticket(
    client: httpx.AsyncClient,
    store_url: str,
    headers: dict[str, str],
    params: dict[str, Any],
) -> str:
    ticket_id = params["ticket_id"]
    reason = params.get("reason", "Stopped by user via chat")
    r = await client.post(
        f"{store_url}/api/v1/tickets/{ticket_id}/stop",
        headers=headers,
        json={"reason": reason},
    )
    r.raise_for_status()
    return json.dumps({"status": "stopped"})
