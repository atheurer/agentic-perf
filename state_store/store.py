from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .models import (
    VALID_TRANSITIONS,
    AddCommentRequest,
    Comment,
    CreateTicketRequest,
    Ticket,
    TicketStatus,
    TransitionRequest,
)

logger = logging.getLogger(__name__)

DEFAULT_PERSIST_DIR = Path.home() / ".agentic-perf" / "tickets"


class InvalidTransition(Exception):
    pass


class TicketNotFound(Exception):
    pass


class TicketStore:
    def __init__(self, persist_dir: str | Path | None = None) -> None:
        self._tickets: dict[str, Ticket] = {}
        self._lock = threading.Lock()
        self._global_seq = 0
        self._persist_dir = Path(persist_dir) if persist_dir else DEFAULT_PERSIST_DIR
        self._persist_dir.mkdir(parents=True, exist_ok=True)
        self._load_from_disk()

    def create_ticket(self, request: CreateTicketRequest) -> Ticket:
        with self._lock:
            self._global_seq += 1
            ticket = Ticket(
                id=f"PERF-{uuid.uuid4().hex[:8].upper()}",
                summary=request.summary,
                description=request.description,
                custom_fields=request.custom_fields,
                status=TicketStatus.NEW,
                transition_seq=self._global_seq,
            )
            self._tickets[ticket.id] = ticket
            self._persist_ticket(ticket)
            return ticket.model_copy()

    def get_ticket(self, ticket_id: str) -> Ticket:
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None:
                raise TicketNotFound(f"Ticket {ticket_id} not found")
            return ticket.model_copy()

    def list_tickets(self, status: TicketStatus | None = None) -> list[Ticket]:
        with self._lock:
            tickets = list(self._tickets.values())
            if status is not None:
                tickets = [t for t in tickets if t.status == status]
            return [t.model_copy() for t in tickets]

    def transition_ticket(self, ticket_id: str, request: TransitionRequest) -> Ticket:
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None:
                raise TicketNotFound(f"Ticket {ticket_id} not found")

            new_status = request.status
            current = ticket.status

            if current == TicketStatus.AWAITING_CUSTOMER_GUIDANCE:
                if new_status == TicketStatus.AWAITING_TEARDOWN:
                    allowed = [TicketStatus.AWAITING_TEARDOWN]
                elif ticket.previous_status is None:
                    raise InvalidTransition(
                        "Cannot resume from AWAITING_CUSTOMER_GUIDANCE: no previous status"
                    )
                else:
                    allowed = VALID_TRANSITIONS.get(ticket.previous_status, [])
                    allowed = list(allowed) + [
                        TicketStatus.AWAITING_CUSTOMER_GUIDANCE,
                        ticket.previous_status,
                    ]
            else:
                allowed = VALID_TRANSITIONS.get(current, [])

            if new_status not in allowed:
                raise InvalidTransition(
                    f"Cannot transition from {current.value} to {new_status.value}. "
                    f"Allowed: {[s.value for s in allowed]}"
                )

            if new_status == TicketStatus.AWAITING_CUSTOMER_GUIDANCE:
                if current != TicketStatus.AWAITING_CUSTOMER_GUIDANCE:
                    ticket.previous_status = current
            else:
                ticket.previous_status = None

            ticket.status = new_status
            ticket.updated_at = datetime.now(timezone.utc)
            self._global_seq += 1
            ticket.transition_seq = self._global_seq

            if request.comment:
                ticket.comments.append(
                    Comment(
                        id=uuid.uuid4().hex[:8],
                        author="system",
                        body=request.comment,
                    )
                )

            self._persist_ticket(ticket)
            return ticket.model_copy()

    def update_fields(self, ticket_id: str, fields: dict) -> Ticket:
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None:
                raise TicketNotFound(f"Ticket {ticket_id} not found")
            ticket.custom_fields.update(fields)
            ticket.updated_at = datetime.now(timezone.utc)
            self._persist_ticket(ticket)
            return ticket.model_copy()

    def add_comment(self, ticket_id: str, request: AddCommentRequest) -> Comment:
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None:
                raise TicketNotFound(f"Ticket {ticket_id} not found")
            comment = Comment(
                id=uuid.uuid4().hex[:8],
                author=request.author,
                body=request.body,
            )
            ticket.comments.append(comment)
            ticket.updated_at = datetime.now(timezone.utc)
            self._persist_ticket(ticket)
            return comment.model_copy()

    def get_tickets_since(self, since_seq: int) -> list[Ticket]:
        with self._lock:
            return [
                t.model_copy()
                for t in self._tickets.values()
                if t.transition_seq > since_seq
            ]

    def _persist_ticket(self, ticket: Ticket) -> None:
        path = self._persist_dir / f"{ticket.id}.json"
        try:
            path.write_text(
                ticket.model_dump_json(indent=2),
                encoding="utf-8",
            )
        except OSError:
            logger.exception(f"Failed to persist ticket {ticket.id}")

    def _load_from_disk(self) -> None:
        if not self._persist_dir.exists():
            return
        for path in sorted(self._persist_dir.glob("PERF-*.json")):
            try:
                ticket = Ticket.model_validate_json(path.read_text(encoding="utf-8"))
                self._tickets[ticket.id] = ticket
                if ticket.transition_seq > self._global_seq:
                    self._global_seq = ticket.transition_seq
            except Exception:
                logger.exception(f"Failed to load ticket from {path}")
