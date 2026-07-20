from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from paths import TICKET_DIR as DEFAULT_PERSIST_DIR

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

    def create_ticket(
        self,
        request: CreateTicketRequest,
        *,
        created_by: str = "",
        owners: list[str] | None = None,
    ) -> Ticket:
        with self._lock:
            self._global_seq += 1
            ticket = Ticket(
                id=f"PERF-{uuid.uuid4().hex[:8].upper()}",
                summary=request.summary,
                description=request.description,
                custom_fields=request.custom_fields,
                status=TicketStatus.NEW,
                transition_seq=self._global_seq,
                created_by=created_by,
                owners=list(owners) if owners else [],
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
                    # Allow resuming to previous status, its forward
                    # transitions, and any earlier pipeline status
                    # so the user can re-route (e.g., back to
                    # awaiting_hardware after a handoff failure).
                    allowed = list(VALID_TRANSITIONS.get(ticket.previous_status, []))
                    allowed.append(TicketStatus.AWAITING_CUSTOMER_GUIDANCE)
                    allowed.append(ticket.previous_status)
                    for s in [
                        TicketStatus.TRIAGE_PENDING,
                        TicketStatus.AWAITING_HARDWARE,
                        TicketStatus.AWAITING_PROVISION,
                        TicketStatus.EXECUTING_BENCHMARK,
                        TicketStatus.AWAITING_REVIEW,
                    ]:
                        if s not in allowed:
                            allowed.append(s)
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

    def set_owners(self, ticket_id: str, owners: list[str]) -> Ticket:
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None:
                raise TicketNotFound(f"Ticket {ticket_id} not found")
            ticket.owners = list(owners)
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

    def claim_ticket(
        self, ticket_id: str, owner: str, duration_seconds: int = 300
    ) -> dict | None:
        """Atomically claim a ticket for dispatch.

        Returns the claim dict on success, None if already claimed by
        another owner with an unexpired lease.
        """
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None:
                raise TicketNotFound(f"Ticket {ticket_id} not found")

            now = datetime.now(timezone.utc)
            existing = ticket.custom_fields.get("claim")
            if existing:
                expires = datetime.fromisoformat(existing["expires"])
                if expires > now and existing["owner"] != owner:
                    return None

            expires = now + timedelta(seconds=duration_seconds)
            claim = {
                "owner": owner,
                "expires": expires.isoformat(),
                "status": ticket.status.value,
            }
            ticket.custom_fields["claim"] = claim
            ticket.updated_at = now
            self._persist_ticket(ticket)
            return claim

    def release_claim(self, ticket_id: str, owner: str) -> bool:
        """Release a claim if owned by the given owner."""
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None:
                raise TicketNotFound(f"Ticket {ticket_id} not found")

            existing = ticket.custom_fields.get("claim")
            if not existing or existing["owner"] != owner:
                return False

            ticket.custom_fields.pop("claim", None)
            ticket.updated_at = datetime.now(timezone.utc)
            self._persist_ticket(ticket)
            return True

    def renew_claim(
        self, ticket_id: str, owner: str, duration_seconds: int = 300
    ) -> dict | None:
        """Extend an existing claim's expiry. Returns updated claim or None."""
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None:
                raise TicketNotFound(f"Ticket {ticket_id} not found")

            existing = ticket.custom_fields.get("claim")
            if not existing or existing["owner"] != owner:
                return None

            now = datetime.now(timezone.utc)
            expires = now + timedelta(seconds=duration_seconds)
            existing["expires"] = expires.isoformat()
            ticket.updated_at = now
            self._persist_ticket(ticket)
            return existing

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
