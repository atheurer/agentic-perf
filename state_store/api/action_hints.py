"""Action hints for multi-step API workflows.

When an API call is only one part of a multi-step process, the
response includes an ``action_required`` object telling the
caller what to do next. This removes the need for callers to
know the state machine — the API tells them.
"""

from __future__ import annotations

from typing import Any

from state_store.models import Ticket, TicketStatus

_AGENT_AUTHORS = frozenset(
    {
        "system",
        "orchestrator",
        "triage-agent",
        "resource-agent",
        "platform-agent",
        "provisioning-agent",
        "benchmark-agent",
        "review-agent",
        "teardown-agent",
    }
)


def after_create(ticket: Ticket) -> dict[str, Any] | None:
    """Hint after ticket creation (status=new)."""
    if ticket.status != TicketStatus.NEW:
        return None
    return {
        "method": "POST",
        "path": f"/api/v1/tickets/{ticket.id}/transition",
        "body": {"status": "triage_pending"},
        "reason": (
            "Ticket was created with status 'new'. Transition to"
            " 'triage_pending' to start the agent pipeline."
        ),
    }


def after_comment(
    ticket: Ticket,
    comment_author: str,
) -> dict[str, Any] | None:
    """Hint after posting a comment on a paused ticket."""
    if ticket.status != TicketStatus.AWAITING_CUSTOMER_GUIDANCE:
        return None
    if comment_author in _AGENT_AUTHORS:
        return None
    previous = ticket.previous_status
    if previous is None:
        return None
    return {
        "method": "POST",
        "path": f"/api/v1/tickets/{ticket.id}/transition",
        "body": {"status": previous.value},
        "reason": (
            f"Ticket is awaiting_customer_guidance. Transition to"
            f" '{previous.value}' to resume the agent."
        ),
    }
