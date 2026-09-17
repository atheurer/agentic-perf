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


def approval_decision(message: str) -> str | None:
    """Recognize an unambiguous natural-language approval response.

    This is intentionally conservative.  A comment is treated as an approval
    only when it clearly refers to approving/rejecting the pending action;
    arbitrary guidance must continue through the normal resume path.
    """
    normalized = " ".join(message.lower().strip().split()).rstrip(".!?")
    if not normalized:
        return None
    if normalized in {
        "reject",
        "rejected",
        "i reject",
        "no, reject",
        "no reject",
        "do not approve",
        "don't approve",
        "do not approve this",
        "don't approve this",
    }:
        return "rejected"
    if normalized in {
        "request changes",
        "changes requested",
        "please request changes",
    }:
        return "changes_requested"
    if normalized in {
        "approve",
        "approved",
        "i approve",
        "i approve this",
        "yes, approve",
        "yes approve",
        "yes, i approve",
        "yes i approve",
        "please approve",
        "please approve this",
        "go ahead and approve",
    }:
        return "approved"
    return None


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
