from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from paths import TICKET_DIR as DEFAULT_PERSIST_DIR

from .audit import AuditLog
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
    def __init__(
        self,
        persist_dir: str | Path | None = None,
        audit_log: AuditLog | None = None,
        event_bus: object | None = None,
    ) -> None:
        self._tickets: dict[str, Ticket] = {}
        self._lock = threading.Lock()
        self._global_seq = 0
        self._persist_dir = Path(persist_dir) if persist_dir else DEFAULT_PERSIST_DIR
        self._persist_dir.mkdir(parents=True, exist_ok=True)
        self._audit = audit_log
        self._event_bus = event_bus
        self._load_from_disk()

    def _audit_log(self, mutation: str, ticket_id: str, data: dict) -> None:
        if self._audit is not None:
            self._audit.log(mutation, ticket_id, data)

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
            self._audit_log(
                "create_ticket",
                ticket.id,
                {"summary": ticket.summary[:200]},
            )
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

    def transition_ticket(
        self,
        ticket_id: str,
        request: TransitionRequest,
        triggered_by: str = "system",
    ) -> Ticket:
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None:
                raise TicketNotFound(f"Ticket {ticket_id} not found")

            new_status = request.status
            current = ticket.status

            if current == TicketStatus.AWAITING_CUSTOMER_GUIDANCE:
                if new_status == TicketStatus.AWAITING_TEARDOWN:
                    allowed = [TicketStatus.AWAITING_TEARDOWN]
                    # Atomically mark the abort and retire the
                    # execution plan so _advance_plan and resumed
                    # agents see the marker immediately.
                    ticket.custom_fields["abort_requested"] = {
                        "requested_at": datetime.now(
                            timezone.utc,
                        ).isoformat(),
                    }
                    plan = ticket.custom_fields.get("execution_plan")
                    if isinstance(plan, dict):
                        steps = plan.get("steps", [])
                        idx = plan.get("current_step", 0)
                        if (
                            isinstance(steps, list)
                            and isinstance(idx, int)
                            and 0 <= idx < len(steps)
                            and isinstance(steps[idx], dict)
                        ):
                            steps[idx]["status"] = "aborted"
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

            old_status = current.value
            ticket.status = new_status
            ticket.status_trail.append(new_status.value)
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
            self._audit_log(
                "transition_ticket",
                ticket_id,
                {
                    "old_status": old_status,
                    "new_status": new_status.value,
                    "comment": request.comment,
                },
            )

            # Emit transition event so the dashboard
            # Emit status_change for the dashboard breadcrumb
            # trail. This is the authoritative record of state
            # transitions — one event per transition, emitted
            # at the point where state actually changes.
            # Agents separately emit "transition" events with
            # additional context (agent name, reasoning) for
            # the live feed. The UI uses status_change for
            # breadcrumbs and transition for the feed.
            if self._event_bus:
                try:
                    self._event_bus.emit(
                        ticket_id,
                        triggered_by,
                        "status_change",
                        {
                            "from": old_status,
                            "to": new_status.value,
                            "comment": request.comment or "",
                        },
                    )
                except Exception as e:
                    logger.exception(f"[store] Failed to emit status_change event: {e}")

            return ticket.model_copy()

    def update_fields(self, ticket_id: str, fields: dict) -> Ticket:
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None:
                raise TicketNotFound(f"Ticket {ticket_id} not found")
            ticket.custom_fields.update(fields)
            ticket.updated_at = datetime.now(timezone.utc)
            self._persist_ticket(ticket)
            self._audit_log(
                "update_fields",
                ticket_id,
                {"field_names": sorted(fields.keys())},
            )
            return ticket.model_copy()

    def set_owners(self, ticket_id: str, owners: list[str]) -> Ticket:
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None:
                raise TicketNotFound(f"Ticket {ticket_id} not found")
            old_owners = list(ticket.owners)
            ticket.owners = list(owners)
            ticket.updated_at = datetime.now(timezone.utc)
            self._persist_ticket(ticket)
            self._audit_log(
                "set_owners",
                ticket_id,
                {"old_owners": old_owners, "new_owners": list(owners)},
            )
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
            self._audit_log(
                "add_comment",
                ticket_id,
                {"author": request.author, "comment_id": comment.id},
            )
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
                    self._audit_log(
                        "claim_ticket",
                        ticket_id,
                        {
                            "owner": owner,
                            "result": "rejected",
                            "held_by": existing["owner"],
                        },
                    )
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
            self._audit_log(
                "claim_ticket",
                ticket_id,
                {
                    "owner": owner,
                    "duration_seconds": duration_seconds,
                    "result": "claimed",
                },
            )
            return claim

    def release_claim(self, ticket_id: str, owner: str) -> bool:
        """Release a claim if owned by the given owner."""
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None:
                raise TicketNotFound(f"Ticket {ticket_id} not found")

            existing = ticket.custom_fields.get("claim")
            if not existing or existing["owner"] != owner:
                self._audit_log(
                    "release_claim",
                    ticket_id,
                    {"owner": owner, "result": "not_owner"},
                )
                return False

            ticket.custom_fields.pop("claim", None)
            ticket.updated_at = datetime.now(timezone.utc)
            self._persist_ticket(ticket)
            self._audit_log(
                "release_claim",
                ticket_id,
                {"owner": owner, "result": "released"},
            )
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
                self._audit_log(
                    "renew_claim",
                    ticket_id,
                    {"owner": owner, "result": "not_owner"},
                )
                return None

            now = datetime.now(timezone.utc)
            expires = now + timedelta(seconds=duration_seconds)
            existing["expires"] = expires.isoformat()
            ticket.updated_at = now
            self._persist_ticket(ticket)
            self._audit_log(
                "renew_claim",
                ticket_id,
                {
                    "owner": owner,
                    "duration_seconds": duration_seconds,
                    "result": "renewed",
                },
            )
            return existing

    def force_close(self, ticket_id: str, comment: str = "") -> Ticket:
        """Close a ticket regardless of current status.

        Administrative action that bypasses the state machine.
        Used by stop-all (hard mode) for tickets that have no
        active agent — normal transitions cannot reach CLOSED
        from early-pipeline statuses like NEW or TRIAGE_PENDING.
        """
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None:
                raise TicketNotFound(f"Ticket {ticket_id} not found")

            if ticket.status == TicketStatus.CLOSED:
                self._audit_log(
                    "force_close",
                    ticket_id,
                    {"old_status": "closed", "result": "already_closed"},
                )
                return ticket.model_copy()

            old_status = ticket.status.value
            ticket.previous_status = ticket.status
            ticket.status = TicketStatus.CLOSED
            ticket.status_trail.append(TicketStatus.CLOSED.value)
            ticket.updated_at = datetime.now(timezone.utc)
            self._global_seq += 1
            ticket.transition_seq = self._global_seq
            ticket.custom_fields.pop("claim", None)
            ticket.custom_fields.pop("stop_requested", None)

            if comment:
                ticket.comments.append(
                    Comment(
                        id=uuid.uuid4().hex[:8],
                        author="system",
                        body=comment,
                    )
                )

            self._persist_ticket(ticket)
            self._audit_log(
                "force_close",
                ticket_id,
                {"old_status": old_status, "comment": comment},
            )
            return ticket.model_copy()

    def archive_ticket(self, ticket_id: str) -> dict:
        """Remove a closed ticket from active memory and move its files to archive."""
        with self._lock:
            if ticket_id not in self._tickets:
                raise TicketNotFound(f"Ticket {ticket_id} not found")
            ticket = self._tickets[ticket_id]
            if ticket.status != TicketStatus.CLOSED:
                raise ValueError(
                    f"Ticket {ticket_id} is {ticket.status.value}, not closed. "
                    "Only closed tickets can be archived."
                )
            del self._tickets[ticket_id]

        archive_dir = self._persist_dir.parent / "archive" / "tickets"
        archive_dir.mkdir(parents=True, exist_ok=True)
        archived = []

        ticket_path = self._persist_dir / f"{ticket_id}.json"
        if ticket_path.exists():
            dest = archive_dir / f"{ticket_id}.json"
            ticket_path.rename(dest)
            archived.append(str(dest))

        from paths import LOG_DIR

        log_path = LOG_DIR / f"{ticket_id}.jsonl"
        if log_path.exists():
            log_archive_dir = self._persist_dir.parent / "archive" / "logs"
            log_archive_dir.mkdir(parents=True, exist_ok=True)
            dest = log_archive_dir / f"{ticket_id}.jsonl"
            log_path.rename(dest)
            archived.append(str(dest))

        logger.info(f"Archived ticket {ticket_id}: {archived}")
        return {"ticket_id": ticket_id, "archived_files": archived}

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
