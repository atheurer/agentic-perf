"""CloudEvents signal endpoint for async operation completion.

Accepts `CloudEvents <https://cloudevents.io/>`_-formatted completion
signals for tickets in ``async_wait`` status.  Validates the event
against the ticket's ``async_context``, stores the signal payload,
and transitions to the resume status.

CloudEvents is a CNCF specification for describing event data in a
standard way.  Using it here means any system that emits CloudEvents
(Horreum, Jumpstarter, CI/CD pipelines) can resume agentic-perf
tickets without a custom adapter.

Required CloudEvents attributes:
    - ``specversion``: must be ``"1.0"``
    - ``type``: event type (e.g., ``dev.agentic-perf.benchmark.complete``)
    - ``source``: event origin URI
    - ``id``: unique event ID (for deduplication)
    - ``subject``: ticket ID this event pertains to

The ``subject`` field is cross-checked against the URL path
``ticket_id`` for safety.  The ``id`` field is matched against
``async_context.operation_id``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from state_store.models import TransitionRequest
from state_store.store import TicketNotFound

router = APIRouter()


class CloudEvent(BaseModel):
    """CloudEvents v1.0 structured-mode envelope.

    Only the required attributes plus ``subject`` and ``data``
    are modeled.  Unknown extension attributes are accepted
    and stored but not validated.
    """

    specversion: str = Field(
        description="CloudEvents spec version (must be '1.0')",
    )
    type: str = Field(
        description=("Event type (e.g., 'dev.agentic-perf.benchmark.complete')"),
    )
    source: str = Field(
        description="Event source URI",
    )
    id: str = Field(
        description=("Unique event ID — matched against async_context.operation_id"),
    )
    subject: str | None = Field(
        default=None,
        description=(
            "Ticket ID this event pertains to (cross-checked against URL path)"
        ),
    )
    time: str | None = Field(
        default=None,
        description="Event timestamp (ISO 8601)",
    )
    datacontenttype: str | None = Field(
        default=None,
        description="Content type of data (default: application/json)",
    )
    data: dict[str, Any] = Field(
        default_factory=dict,
        description="Event payload",
    )


def _get_store(request: Request):
    return request.app.state.store


@router.post("/tickets/{ticket_id}/signal")
async def receive_signal(
    ticket_id: str,
    event: CloudEvent,
    request: Request,
) -> dict[str, Any]:
    """Receive a CloudEvents completion signal for an async_wait ticket.

    Validates that:
    - The event is CloudEvents v1.0
    - The ticket exists and is in async_wait status
    - The event ``id`` matches ``async_context.operation_id``
    - The event ``subject`` (if set) matches the URL ticket_id

    On success, stores the event and transitions the ticket to
    the resume status specified in ``async_context``.
    """
    # Validate CloudEvents version
    if event.specversion != "1.0":
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported CloudEvents specversion: "
                f"'{event.specversion}' (expected '1.0')"
            ),
        )

    # Cross-check subject if provided
    if event.subject and event.subject != ticket_id:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Event subject '{event.subject}' does not "
                f"match ticket_id '{ticket_id}'"
            ),
        )

    store = _get_store(request)

    try:
        ticket = store.get_ticket(ticket_id)
    except TicketNotFound:
        raise HTTPException(
            status_code=404,
            detail="Ticket not found",
        )

    if ticket.status.value != "async_wait":
        raise HTTPException(
            status_code=409,
            detail=(f"Ticket is in '{ticket.status.value}', not 'async_wait'"),
        )

    async_ctx = ticket.custom_fields.get("async_context", {})
    if not async_ctx:
        raise HTTPException(
            status_code=409,
            detail="Ticket has no async_context",
        )

    expected_op = async_ctx.get("operation_id", "")
    if event.id != expected_op:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Event ID mismatch: event has "
                f"'{event.id}', ticket expects "
                f"'{expected_op}'"
            ),
        )

    # Store the CloudEvent on the ticket
    async_ctx["signal_received"] = {
        "specversion": event.specversion,
        "type": event.type,
        "source": event.source,
        "id": event.id,
        "subject": event.subject,
        "time": event.time,
        "data": event.data,
    }
    store.update_fields(
        ticket_id,
        {"async_context": async_ctx},
    )

    # Transition to the resume status
    resume_to = async_ctx.get("resume_to_status")
    if not resume_to:
        raise HTTPException(
            status_code=500,
            detail="async_context has no resume_to_status",
        )

    store.transition_ticket(
        ticket_id,
        TransitionRequest(
            status=resume_to,
            comment=(
                f"Async complete: {event.type} from {event.source} (id={event.id})"
            ),
        ),
    )

    return {
        "status": "resumed",
        "ticket_id": ticket_id,
        "resumed_to": resume_to,
        "event_id": event.id,
        "event_type": event.type,
    }
