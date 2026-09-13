from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from paths import TICKET_DIR as DEFAULT_PERSIST_DIR
from providers.execution import (
    AuditedFilesystem,
    RootedPath,
    durable_filesystem_emitter,
)
from providers.tracing import (
    ActionDescriptor,
    ActionType,
    LifecycleDescriptor,
    LifecycleState,
    OperationOutcome,
    TraceEventV1,
    child_context,
    current_trace_context,
)

from .audit import AuditLog, get_actor
from .directives import parse_verbatim_directives
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
        trace_store: object | None = None,
    ) -> None:
        self._tickets: dict[str, Ticket] = {}
        self._lock = threading.Lock()
        self._global_seq = 0
        self._persist_dir = Path(persist_dir) if persist_dir else DEFAULT_PERSIST_DIR
        self._persist_dir.mkdir(parents=True, exist_ok=True)
        self._audit = audit_log
        self._event_bus = event_bus
        self._trace_store = trace_store
        self._load_from_disk()

    def _audit_log(self, mutation: str, ticket_id: str, data: dict) -> None:
        if self._audit is not None:
            self._audit.log(mutation, ticket_id, data)

    def _filesystem(self, ticket_id: str) -> AuditedFilesystem:
        """Return the ticket-owned mutation boundary without exposing host paths."""
        emit = (
            self._trace_store.insert_event_result
            if self._trace_store is not None
            else durable_filesystem_emitter()
        )
        return AuditedFilesystem(
            RootedPath(
                self._persist_dir.parent,
                "ticket",
                physical_prefix=self._persist_dir.name,
            ),
            ticket_id=ticket_id,
            emit=emit,
            critical=True,
        )

    def _trace_mutation(
        self,
        ticket_id: str,
        mutation: str,
        *,
        rejected: bool = False,
        attributes: dict | None = None,
    ) -> None:
        """Persist a state outcome under the request context when available."""
        context = current_trace_context()
        if self._trace_store is None or context is None:
            return
        child = child_context(context)
        try:
            self._trace_store.insert_event_result(
                TraceEventV1(
                    ticket_id=ticket_id,
                    agent_id=context.agent_id,
                    invocation_id=context.invocation_id,
                    trace_id=child.trace_id,
                    action_id=child.action_id,
                    parent_action_id=child.parent_action_id,
                    action=ActionDescriptor(type=ActionType.STATE, target=mutation),
                    lifecycle=LifecycleDescriptor(
                        state=LifecycleState.REJECTED
                        if rejected
                        else LifecycleState.COMPLETED
                    ),
                    duration_ms=0,
                    outcome=OperationOutcome.REJECTED
                    if rejected
                    else OperationOutcome.SUCCESS,
                    attributes=attributes,
                )
            )
        except Exception:
            logger.exception("failed to persist state mutation trace")

    def create_ticket(
        self,
        request: CreateTicketRequest,
        *,
        created_by: str = "",
        owners: list[str] | None = None,
    ) -> Ticket:
        with self._lock:
            self._global_seq += 1
            custom_fields = dict(request.custom_fields)
            verbatim = parse_verbatim_directives(request.description)
            if verbatim:
                custom_fields["verbatim_directives"] = verbatim
            ticket = Ticket(
                id=f"PERF-{uuid.uuid4().hex[:8].upper()}",
                summary=request.summary,
                description=request.description,
                custom_fields=custom_fields,
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
            self._trace_mutation(ticket.id, "create_ticket")
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
                self._trace_mutation(
                    ticket_id,
                    "transition_ticket",
                    rejected=True,
                    attributes={
                        "old_state": current.value,
                        "new_state": new_status.value,
                    },
                )
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
            self._trace_mutation(
                ticket_id,
                "transition_ticket",
                attributes={"old_state": old_status, "new_state": new_status.value},
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
            protected = {
                "benchmark_validation",
                "benchmark_validations",
                "validated_run_file",
            }.intersection(fields)
            if protected:
                raise ValueError(
                    "benchmark validation fields are immutable; use the validations API"
                )
            ticket.custom_fields.update(fields)
            ticket.updated_at = datetime.now(timezone.utc)
            self._persist_ticket(ticket)
            self._audit_log(
                "update_fields",
                ticket_id,
                {"field_names": sorted(fields.keys())},
            )
            self._trace_mutation(ticket_id, "update_fields")
            return ticket.model_copy()

    @staticmethod
    def _validation_manifest(ticket: Ticket) -> dict:
        """Return the canonical validation manifest, migrating legacy data once.

        The old overwrite-only field is retained as a display compatibility
        snapshot, but is never used for authorization after this migration.
        """
        manifest = ticket.custom_fields.get("benchmark_validations")
        if isinstance(manifest, dict) and isinstance(manifest.get("records"), dict):
            return manifest
        legacy = ticket.custom_fields.get("benchmark_validation")
        records: dict[str, dict] = {}
        active_id = None
        if isinstance(legacy, dict) and isinstance(legacy.get("validation_id"), str):
            active_id = legacy["validation_id"]
            record = dict(legacy)
            record["record_type"] = "legacy_validation"
            record["state"] = "legacy_unapproved"
            record.setdefault("created_at", ticket.updated_at.isoformat())
            record.setdefault("creator", {"migration": "legacy_benchmark_validation"})
            if isinstance(record.get("run_file"), dict):
                record.setdefault(
                    "runfile_fingerprint",
                    hashlib.sha256(
                        json.dumps(
                            record["run_file"], sort_keys=True, separators=(",", ":")
                        ).encode()
                    ).hexdigest(),
                )
            records[active_id] = record
        manifest = {
            "schema_version": 1,
            "version": 0,
            "active_validation_id": active_id,
            "records": records,
        }
        ticket.custom_fields["benchmark_validations"] = manifest
        return manifest

    def _validation_conflict(self, ticket: Ticket, manifest: dict) -> dict:
        return {
            "current_version": manifest["version"],
            "active_validation_id": manifest.get("active_validation_id"),
        }

    def create_validation(
        self, ticket_id: str, record: dict, expected_version: int
    ) -> tuple[Ticket | None, dict | None]:
        """Append a validation record iff the caller observed this manifest version."""
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None:
                raise TicketNotFound(f"Ticket {ticket_id} not found")
            manifest = self._validation_manifest(ticket)
            if manifest["version"] != expected_version:
                self._audit_log(
                    "create_validation_rejected",
                    ticket_id,
                    self._validation_conflict(ticket, manifest),
                )
                self._trace_mutation(
                    ticket_id,
                    "create_validation",
                    rejected=True,
                    attributes=self._validation_conflict(ticket, manifest),
                )
                return None, self._validation_conflict(ticket, manifest)
            validation_id = record.get("validation_id")
            if (
                not isinstance(validation_id, str)
                or not validation_id
                or validation_id in manifest["records"]
            ):
                raise ValueError("validation_id must be a new non-empty identifier")
            run_file = record.get("run_file")
            if not isinstance(run_file, dict):
                raise ValueError("validation record requires a run_file")
            runfile_digest = hashlib.sha256(
                json.dumps(run_file, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            if record.get("runfile_fingerprint") != runfile_digest:
                raise ValueError("runfile fingerprint does not match")
            plan = ticket.custom_fields.get("execution_plan", {})
            steps = plan.get("steps", []) if isinstance(plan, dict) else []
            index = plan.get("current_step", 0) if isinstance(plan, dict) else 0
            params = (
                steps[index].get("params", {})
                if isinstance(index, int) and index < len(steps)
                else {}
            )
            plan_digest = hashlib.sha256(
                json.dumps(params, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            if record.get("execution_plan_fingerprint") != plan_digest:
                raise ValueError("execution plan fingerprint does not match")
            intent = {
                "runfile": runfile_digest,
                "params": plan_digest,
                "harness": record.get("harness"),
                "controller": record.get("controller"),
                "run_command": record.get("run_command"),
            }
            if (
                record.get("execution_intent_digest")
                != hashlib.sha256(
                    json.dumps(intent, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
            ):
                raise ValueError("execution intent digest does not match")
            immutable = dict(record)
            immutable["record_type"] = "validation"
            immutable["state"] = "executable"
            immutable["ticket_id"] = ticket_id
            immutable.setdefault("created_at", datetime.now(timezone.utc).isoformat())
            immutable.setdefault("creator", get_actor())
            manifest["records"][validation_id] = immutable
            manifest["active_validation_id"] = validation_id
            manifest["version"] += 1
            ticket.custom_fields["validated_run_file"] = immutable
            ticket.updated_at = datetime.now(timezone.utc)
            self._persist_ticket(ticket)
            attrs = {"validation_id": validation_id, "version": manifest["version"]}
            self._audit_log("create_validation", ticket_id, attrs)
            self._trace_mutation(ticket_id, "create_validation", attributes=attrs)
            return ticket.model_copy(), None

    def supersede_validation(
        self,
        ticket_id: str,
        validation_id: str,
        replacement_validation_id: str | None,
        reason: str,
        expected_version: int,
    ) -> tuple[Ticket | None, dict | None]:
        """Append, rather than mutate, the evidence that retires a validation."""
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None:
                raise TicketNotFound(f"Ticket {ticket_id} not found")
            manifest = self._validation_manifest(ticket)
            if manifest["version"] != expected_version:
                self._audit_log(
                    "supersede_validation_rejected",
                    ticket_id,
                    self._validation_conflict(ticket, manifest),
                )
                self._trace_mutation(
                    ticket_id,
                    "supersede_validation",
                    rejected=True,
                    attributes=self._validation_conflict(ticket, manifest),
                )
                return None, self._validation_conflict(ticket, manifest)
            if validation_id not in manifest["records"]:
                raise ValueError("unknown validation_id")
            if (
                replacement_validation_id
                and replacement_validation_id not in manifest["records"]
            ):
                raise ValueError("unknown replacement_validation_id")
            supersession_id = f"sup-{uuid.uuid4().hex}"
            manifest["records"][supersession_id] = {
                "record_type": "supersession",
                "state": "superseded",
                "validation_id": supersession_id,
                "supersedes_validation_id": validation_id,
                "replacement_validation_id": replacement_validation_id,
                "reason": reason,
                "writer": get_actor(),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            if manifest.get("active_validation_id") == validation_id:
                manifest["active_validation_id"] = replacement_validation_id
            manifest["version"] += 1
            ticket.updated_at = datetime.now(timezone.utc)
            self._persist_ticket(ticket)
            attrs = {
                "validation_id": validation_id,
                "replacement_validation_id": replacement_validation_id,
                "reason": reason,
                "version": manifest["version"],
            }
            self._audit_log("supersede_validation", ticket_id, attrs)
            self._trace_mutation(ticket_id, "supersede_validation", attributes=attrs)
            return ticket.model_copy(), None

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
            self._trace_mutation(ticket_id, "add_comment")
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
                    self._trace_mutation(ticket_id, "claim_ticket", rejected=True)
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
            self._trace_mutation(ticket_id, "claim_ticket")
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
                self._trace_mutation(ticket_id, "renew_claim", rejected=True)
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
            self._trace_mutation(ticket_id, "renew_claim")
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
        filesystem = self._filesystem(ticket_id)
        filesystem.mkdir("archive/tickets")
        archived = []

        from paths import LOG_DIR

        log_path = LOG_DIR / f"{ticket_id}.jsonl"
        if log_path.exists():
            filesystem.mkdir("archive/logs")
            # Logs are outside the ticket persistence root.  Their move remains
            # explicitly scoped and emits only ticket-owned logical references.
            log_filesystem = AuditedFilesystem(
                RootedPath(LOG_DIR.parent, "ticket"),
                ticket_id=ticket_id,
                emit=(
                    self._trace_store.insert_event_result
                    if self._trace_store
                    else durable_filesystem_emitter()
                ),
                critical=True,
            )
            log_filesystem.rename(
                f"logs/{ticket_id}.jsonl", f"archive/logs/{ticket_id}.jsonl"
            )
            archived.append(f"ticket://archive/logs/{ticket_id}.jsonl")

        # Move the durable ticket record last.  If an earlier companion move
        # fails, restart still loads the ticket and can safely retry archive.
        ticket_path = self._persist_dir / f"{ticket_id}.json"
        if ticket_path.exists():
            filesystem.rename(
                f"{self._persist_dir.name}/{ticket_id}.json",
                f"archive/tickets/{ticket_id}.json",
            )
            archived.insert(0, f"ticket://archive/tickets/{ticket_id}.json")

        # Keep the closed ticket reachable if any durable move fails.  This is
        # intentionally after both moves, so a primary archive failure is not
        # hidden by an in-memory deletion.
        with self._lock:
            self._tickets.pop(ticket_id, None)

        logger.info(f"Archived ticket {ticket_id}: {archived}")
        return {"ticket_id": ticket_id, "archived_files": archived}

    def _persist_ticket(self, ticket: Ticket) -> None:
        try:
            self._filesystem(ticket.id).write(
                f"{self._persist_dir.name}/{ticket.id}.json",
                ticket.model_dump_json(indent=2),
            )
        except OSError:
            logger.exception(f"Failed to persist ticket {ticket.id}")

    def _load_from_disk(self) -> None:
        if not self._persist_dir.exists():
            return
        for path in sorted(self._persist_dir.glob("PERF-*.json")):
            try:
                ticket = Ticket.model_validate_json(path.read_text(encoding="utf-8"))
                if "benchmark_validations" not in ticket.custom_fields and isinstance(
                    ticket.custom_fields.get("benchmark_validation"), dict
                ):
                    self._validation_manifest(ticket)
                    self._persist_ticket(ticket)
                    self._audit_log(
                        "migrate_benchmark_validation", ticket.id, {"version": 0}
                    )
                self._tickets[ticket.id] = ticket
                if ticket.transition_seq > self._global_seq:
                    self._global_seq = ticket.transition_seq
            except Exception:
                logger.exception(f"Failed to load ticket from {path}")
