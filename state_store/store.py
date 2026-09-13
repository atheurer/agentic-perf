from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

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
    PayloadDescriptor,
    TraceEventV1,
    child_context,
    current_trace_context,
)

from .audit import AuditLog, get_actor
from .directives import parse_verbatim_directives
from .models import (
    _VALIDATION_RESERVED_FIELDS,
    VALID_TRANSITIONS,
    AcquireOrchestratorLeaseRequest,
    AddCommentRequest,
    ApprovalRequest,
    Comment,
    ConsumeApprovalRequest,
    CreateApprovalRequest,
    CreateTicketRequest,
    OrchestratorLease,
    ResolveApprovalRequest,
    Ticket,
    TicketStatus,
    TransitionRequest,
    imported_fixture_reserved_field,
)

logger = logging.getLogger(__name__)


class InvalidTransition(Exception):
    pass


class TicketNotFound(Exception):
    pass


class ClaimFenceError(Exception):
    """A claim mutation was rejected by the state-store fencing contract."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        super().__init__(detail or reason)


class OrchestratorLeaseHeld(Exception):
    """Raised when a different live orchestrator owns the control lease."""

    def __init__(self, holder: OrchestratorLease, remaining_seconds: float) -> None:
        self.holder = holder
        self.remaining_seconds = max(0.0, remaining_seconds)
        super().__init__("another orchestrator holds the state-store lease")


class TicketDispatchBlocked(Exception):
    """A ticket is intentionally prevented from entering agent execution."""

    pass


class TicketStore:
    def __init__(
        self,
        persist_dir: str | Path | None = None,
        audit_log: AuditLog | None = None,
        event_bus: object | None = None,
        trace_store: object | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._tickets: dict[str, Ticket] = {}
        self._lock = threading.Lock()
        self._global_seq = 0
        self._persist_dir = Path(persist_dir) if persist_dir else DEFAULT_PERSIST_DIR
        self._persist_dir.mkdir(parents=True, exist_ok=True)
        self._audit = audit_log
        self._event_bus = event_bus
        self._trace_store = trace_store
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lease_path = self._persist_dir / "orchestrator-lease.json"
        self._load_from_disk()

    def _lease_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc)

    def _read_orchestrator_lease(self) -> OrchestratorLease | None:
        try:
            return OrchestratorLease.model_validate(
                json.loads(self._lease_path.read_text(encoding="utf-8"))
            )
        except (
            FileNotFoundError,
            OSError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
        ):
            return None

    def _write_orchestrator_lease(self, lease: OrchestratorLease | None) -> None:
        if lease is None:
            try:
                self._lease_path.unlink()
            except FileNotFoundError:
                return
            self._fsync_lease_directory()
            return
        temporary = self._lease_path.with_name(
            f".{self._lease_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        payload = json.dumps(lease.model_dump(mode="json"), sort_keys=True) + "\n"
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self._lease_path)
        self._fsync_lease_directory()

    def _fsync_lease_directory(self) -> None:
        """Make lease creation and deletion survive a host crash."""
        try:
            directory_fd = os.open(self._persist_dir, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            logger.debug("could not fsync lease directory", exc_info=True)

    def get_orchestrator_lease(self) -> OrchestratorLease | None:
        with self._lock:
            lease = self._read_orchestrator_lease()
            if lease is not None and lease.expires_at <= self._lease_now():
                self._write_orchestrator_lease(None)
                self._expire_all_pending_approvals_unlocked()
                self._audit_log(
                    "orchestrator_lease_expire", "-", {"epoch": lease.epoch}
                )
                return None
            return lease.model_copy() if lease else None

    def acquire_orchestrator_lease(
        self, request: AcquireOrchestratorLeaseRequest
    ) -> OrchestratorLease:
        with self._lock:
            now = self._lease_now()
            current = self._read_orchestrator_lease()
            if current is not None and current.expires_at > now:
                if current.session_id == request.session_id:
                    renewed = current.model_copy(
                        update={
                            "renewed_at": now,
                            "expires_at": now + timedelta(seconds=request.ttl_seconds),
                        }
                    )
                    self._write_orchestrator_lease(renewed)
                    self._audit_log(
                        "orchestrator_lease_acquire",
                        "-",
                        {
                            "session_id": str(request.session_id),
                            "epoch": current.epoch,
                            "result": "idempotent",
                        },
                    )
                    return renewed
                raise OrchestratorLeaseHeld(
                    current, (current.expires_at - now).total_seconds()
                )
            epoch = (current.epoch + 1) if current is not None else 1
            if current is not None:
                self._cancel_pending_approvals_unlocked_all(
                    reason="orchestrator lease takeover"
                )
            lease = OrchestratorLease(
                session_id=request.session_id,
                instance_name=request.instance_name,
                host=request.host,
                pid=request.pid,
                process_start_id=request.process_start_id,
                epoch=epoch,
                acquired_at=now,
                renewed_at=now,
                expires_at=now + timedelta(seconds=request.ttl_seconds),
            )
            self._write_orchestrator_lease(lease)
            self._audit_log(
                "orchestrator_lease_acquire",
                "-",
                {
                    "session_id": str(lease.session_id),
                    "epoch": epoch,
                    "result": "acquired",
                },
            )
            return lease.model_copy()

    def _expire_pending_approvals_unlocked(self, ticket: Ticket) -> None:
        raw = ticket.custom_fields.get("approval_requests", {})
        if not isinstance(raw, dict):
            return
        now = self._lease_now()
        changed = False
        for approval_id, value in list(raw.items()):
            if not isinstance(value, dict):
                continue
            current = ApprovalRequest.model_validate(value)
            if (
                current.status == "pending"
                and current.expires_at is not None
                and current.expires_at <= now
            ):
                raw[approval_id] = current.model_copy(
                    update={
                        "status": "expired",
                        "resolved_at": now,
                        "resolved_by": "system",
                        "resolution_reason": "approval request expired",
                        "record_version": current.record_version + 1,
                    }
                ).model_dump(mode="json")
                self._audit_log(
                    "approval_expired",
                    ticket.id,
                    {
                        "approval_request_id": approval_id,
                        "validation_id": current.validation_id,
                        "execution_intent_digest": current.execution_intent_digest,
                        "reason": "approval request expired",
                        "actor": "system",
                    },
                )
                self._trace_mutation(
                    ticket.id,
                    "approval_expired",
                    attributes={"approval_request_id": approval_id},
                )
                changed = True
        if changed:
            ticket.updated_at = now
            self._persist_ticket(ticket)

    def _expire_all_pending_approvals_unlocked(self) -> None:
        for ticket in self._tickets.values():
            self._expire_pending_approvals_unlocked(ticket)

    def _cancel_pending_approvals_unlocked_all(self, *, reason: str) -> None:
        for ticket in self._tickets.values():
            self._cancel_pending_approvals_unlocked(ticket, reason=reason)
            self._persist_ticket(ticket)

    def renew_orchestrator_lease(
        self, session_id: uuid.UUID, epoch: int, ttl_seconds: float
    ) -> OrchestratorLease:
        with self._lock:
            now = self._lease_now()
            current = self._read_orchestrator_lease()
            if (
                current is None
                or current.session_id != session_id
                or current.epoch != epoch
                or current.expires_at <= now
            ):
                raise PermissionError("orchestrator lease is not owned or has expired")
            lease = current.model_copy(
                update={
                    "renewed_at": now,
                    "expires_at": now + timedelta(seconds=ttl_seconds),
                }
            )
            self._write_orchestrator_lease(lease)
            self._audit_log(
                "orchestrator_lease_renew",
                "-",
                {"session_id": str(session_id), "epoch": epoch, "result": "renewed"},
            )
            return lease.model_copy()

    def release_orchestrator_lease(self, session_id: uuid.UUID, epoch: int) -> bool:
        with self._lock:
            current = self._read_orchestrator_lease()
            if (
                current is None
                or current.session_id != session_id
                or current.epoch != epoch
            ):
                self._audit_log(
                    "orchestrator_lease_release",
                    "-",
                    {
                        "session_id": str(session_id),
                        "epoch": epoch,
                        "result": "not_owner",
                    },
                )
                return False
            self._write_orchestrator_lease(None)
            self._audit_log(
                "orchestrator_lease_release",
                "-",
                {"session_id": str(session_id), "epoch": epoch, "result": "released"},
            )
            return True

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
            protected = _VALIDATION_RESERVED_FIELDS.intersection(custom_fields)
            if protected:
                raise ValueError("benchmark validation fields are reserved")
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
        session_id: uuid.UUID | None = None,
        epoch: int | None = None,
        claim_id: str | None = None,
        reviewer_authorized: bool = False,
    ) -> Ticket:
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None:
                raise TicketNotFound(f"Ticket {ticket_id} not found")
            self._validate_claim_fence(session_id, epoch, ticket_id)
            self._validate_ticket_claim(ticket_id, session_id, epoch, claim_id)

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
                elif ticket.custom_fields.get("imported_fixture"):
                    if not request.reviewed_resume:
                        self._trace_mutation(
                            ticket_id,
                            "transition_ticket",
                            rejected=True,
                            attributes={"reason": "imported_fixture_requires_review"},
                        )
                        raise InvalidTransition(
                            "Imported fixture requires reviewed_resume=true before "
                            "it can become executable"
                        )
                    if not reviewer_authorized:
                        self._trace_mutation(
                            ticket_id,
                            "transition_ticket",
                            rejected=True,
                            attributes={"reason": "reviewer_authorization_required"},
                        )
                        raise InvalidTransition(
                            "Imported fixture resume requires an authorized "
                            "service or administrator reviewer"
                        )
                    # Imported fixtures intentionally have no previous status.
                    # A reviewed operator must explicitly re-enter the normal
                    # pipeline at triage rather than accidentally resuming a
                    # copied in-progress operation.
                    allowed = [
                        TicketStatus.TRIAGE_PENDING,
                        TicketStatus.AWAITING_CUSTOMER_GUIDANCE,
                    ]
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

            if request.reviewed_resume and ticket.custom_fields.get("imported_fixture"):
                ticket.custom_fields["imported_fixture_reviewed"] = {
                    "reviewed_at": datetime.now(timezone.utc).isoformat(),
                    "reviewed_by": triggered_by,
                }

            if new_status == TicketStatus.AWAITING_CUSTOMER_GUIDANCE:
                if current != TicketStatus.AWAITING_CUSTOMER_GUIDANCE:
                    ticket.previous_status = current
            else:
                ticket.previous_status = None
                self._cancel_pending_approvals_unlocked(
                    ticket,
                    reason="ticket resumed without resolving approval",
                )

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

    def update_fields(
        self,
        ticket_id: str,
        fields: dict,
        session_id: uuid.UUID | None = None,
        epoch: int | None = None,
        claim_id: str | None = None,
    ) -> Ticket:
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None:
                raise TicketNotFound(f"Ticket {ticket_id} not found")
            self._validate_claim_fence(session_id, epoch, ticket_id)
            self._validate_ticket_claim(ticket_id, session_id, epoch, claim_id)
            protected = _VALIDATION_RESERVED_FIELDS.intersection(fields)
            if protected:
                raise ValueError(
                    "benchmark validation fields are immutable; use the validations API"
                )
            if self._contains_imported_fixture_reserved_field(fields):
                raise ValueError(
                    "imported fixture control and provenance fields are immutable; "
                    "use import-state or the reviewed resume operation"
                )
            if {"execution_plan", "abort_requested"}.intersection(fields):
                self._cancel_pending_approvals_unlocked(
                    ticket,
                    reason="execution intent changed or ticket aborted",
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
    def _contains_imported_fixture_reserved_field(value: object) -> bool:
        """Find protected fixture metadata at any nested update path."""
        if isinstance(value, dict):
            return any(
                imported_fixture_reserved_field(key)
                or TicketStore._contains_imported_fixture_reserved_field(item)
                for key, item in value.items()
            )
        if isinstance(value, list):
            return any(
                TicketStore._contains_imported_fixture_reserved_field(item)
                for item in value
            )
        return False

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
            # Append-only validation evidence is safe to merge: concurrent
            # controller successes must not be thrown away merely because the
            # active-pointer version advanced.  Only a duplicate ID conflicts.
            validation_id = record.get("validation_id")
            if isinstance(validation_id, str) and validation_id in manifest["records"]:
                existing = manifest["records"][validation_id]
                request_hash = hashlib.sha256(
                    json.dumps(
                        record, sort_keys=True, separators=(",", ":"), allow_nan=False
                    ).encode()
                ).hexdigest()
                existing_hash = existing.get("_validation_record_hash")
                if existing_hash is None:
                    legacy = {
                        key: value
                        for key, value in existing.items()
                        if key
                        not in {
                            "record_type",
                            "state",
                            "ticket_id",
                            "created_at",
                            "_validation_record_hash",
                        }
                    }
                    existing_hash = hashlib.sha256(
                        json.dumps(
                            legacy,
                            sort_keys=True,
                            separators=(",", ":"),
                            allow_nan=False,
                        ).encode()
                    ).hexdigest()
                if existing_hash == request_hash:
                    return ticket.model_copy(), None
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
            immutable["_validation_record_hash"] = hashlib.sha256(
                json.dumps(
                    record, sort_keys=True, separators=(",", ":"), allow_nan=False
                ).encode()
            ).hexdigest()
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
            descriptor = PayloadDescriptor.model_validate(
                immutable["validation_output"]
            )
            if self._trace_store is not None and descriptor.digest:
                self._trace_store.put_payload_descriptor(descriptor)
            self._persist_ticket(ticket)
            attrs = {
                "ticket_id": ticket_id,
                "validation_id": validation_id,
                "version": manifest["version"],
                "runfile_fingerprint": immutable["runfile_fingerprint"],
                "execution_intent_digest": immutable["execution_intent_digest"],
                "execution_plan_fingerprint": immutable["execution_plan_fingerprint"],
                "validator_command": immutable["validator_command"],
                "validator_version": immutable["validator_version"],
                "validation_output": immutable["validation_output"],
                "creator": immutable["creator"],
            }
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
            raw_approvals = ticket.custom_fields.get("approval_requests", {})
            if isinstance(raw_approvals, dict):
                for approval_id, value in list(raw_approvals.items()):
                    if (
                        isinstance(value, dict)
                        and value.get("status") == "pending"
                        and value.get("validation_id") == validation_id
                    ):
                        self._cancel_pending_approvals_unlocked(
                            ticket,
                            reason="validation superseded or invalidated",
                            validation_id=validation_id,
                        )
                        break
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

    def add_comment(
        self,
        ticket_id: str,
        request: AddCommentRequest,
        session_id: uuid.UUID | None = None,
        epoch: int | None = None,
        claim_id: str | None = None,
    ) -> Comment:
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None:
                raise TicketNotFound(f"Ticket {ticket_id} not found")
            self._validate_claim_fence(session_id, epoch, ticket_id)
            self._validate_ticket_claim(ticket_id, session_id, epoch, claim_id)
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

    def create_approval_request(
        self,
        ticket_id: str,
        request: CreateApprovalRequest,
        *,
        created_by: str,
    ) -> ApprovalRequest:
        """Persist one immutable benchmark approval before pausing the ticket."""
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None:
                raise TicketNotFound(f"Ticket {ticket_id} not found")
            lease = self._read_orchestrator_lease()
            claim = ticket.custom_fields.get("claim")
            rejection_reason = None
            if lease is None or lease.expires_at <= self._lease_now():
                rejection_reason = "no active leader lease"
            elif not isinstance(claim, dict):
                rejection_reason = "claim_missing"
            else:
                try:
                    claim_expires = datetime.fromisoformat(claim["expires"])
                    if claim_expires.tzinfo is None:
                        claim_expires = claim_expires.replace(tzinfo=timezone.utc)
                    if claim_expires <= self._lease_now():
                        rejection_reason = "claim_expired"
                    elif (
                        not isinstance(claim.get("session_id"), str)
                        or not claim["session_id"]
                        or not isinstance(claim.get("epoch"), int)
                        or isinstance(claim["epoch"], bool)
                        or claim["epoch"] <= 0
                        or not isinstance(claim.get("claim_id"), str)
                        or not claim["claim_id"]
                    ):
                        rejection_reason = "claim_malformed"
                    elif (
                        not request.session_id
                        or not request.session_epoch
                        or not request.claim_id
                        or request.session_id != str(lease.session_id)
                        or request.session_epoch != str(lease.epoch)
                        or claim["session_id"] != str(lease.session_id)
                        or claim["epoch"] != lease.epoch
                        or request.claim_id != claim["claim_id"]
                        or request.ticket_attempt != claim["claim_id"]
                    ):
                        rejection_reason = "claim_owned_by_other_session"
                except (KeyError, TypeError, ValueError):
                    rejection_reason = "claim_malformed"
            if rejection_reason is not None:
                self._audit_log(
                    "approval_requested_rejected",
                    ticket_id,
                    {
                        "reason": rejection_reason,
                        "validation_id": request.validation_id,
                        "claim_id": request.claim_id,
                    },
                )
                self._trace_mutation(
                    ticket_id,
                    "approval_requested",
                    rejected=True,
                    attributes={"reason": rejection_reason},
                )
                raise ValueError(f"approval rejected: {rejection_reason}")
            fields = ticket.custom_fields
            manifest = fields.get("benchmark_validations", {})
            records = manifest.get("records", {}) if isinstance(manifest, dict) else {}
            validation = records.get(request.validation_id)
            if not isinstance(validation, dict):
                legacy = fields.get("validated_run_file", {})
                validation = legacy if isinstance(legacy, dict) else None
            if (
                not isinstance(validation, dict)
                or validation.get("validation_id") != request.validation_id
                or validation.get("state", "executable") != "executable"
                or validation.get("runfile_fingerprint")
                != request.presented_run_file_digest
                or validation.get("execution_intent_digest")
                != request.execution_intent_digest
            ):
                raise ValueError(
                    "approval does not match an executable validation record"
                )
            raw = ticket.custom_fields.setdefault("approval_requests", {})
            if not isinstance(raw, dict):
                raise ValueError("approval request state is malformed")
            self._expire_pending_approvals_unlocked(ticket)
            for value in raw.values():
                if not isinstance(value, dict):
                    continue
                existing = ApprovalRequest.model_validate(value)
                if (
                    existing.status == "pending"
                    and existing.validation_id == request.validation_id
                    and existing.presented_run_file_digest
                    == request.presented_run_file_digest
                    and existing.execution_intent_digest
                    == request.execution_intent_digest
                    and existing.waiter_owner == request.waiter_owner
                    and existing.invocation_id == request.invocation_id
                    and existing.tool_call_id == request.tool_call_id
                    and existing.session_id == request.session_id
                    and existing.session_epoch == request.session_epoch
                    and existing.claim_id == request.claim_id
                    and existing.ticket_attempt == request.ticket_attempt
                ):
                    return existing
            approval_id = f"apr-{uuid.uuid4().hex}"
            expires_at = request.expires_at or (self._lease_now() + timedelta(hours=1))
            if expires_at <= self._lease_now():
                raise ValueError("approval request expiry must be in the future")
            claim = ticket.custom_fields.get("claim")
            if request.claim_id and (
                not isinstance(claim, dict) or request.claim_id != claim.get("claim_id")
            ):
                self._audit_log(
                    "approval_requested_rejected",
                    ticket_id,
                    {
                        "reason": "claim identity mismatch",
                        "validation_id": request.validation_id,
                    },
                )
                raise ValueError(
                    "approval claim identity does not match active ticket claim"
                )
            lease = self._read_orchestrator_lease()
            if lease is not None and lease.expires_at > self._lease_now():
                if (
                    request.claim_id or request.waiter_owner or request.invocation_id
                ) and (not request.session_id or not request.session_epoch):
                    raise ValueError("approval requires active session and epoch")
                if request.session_id and request.session_id != str(lease.session_id):
                    raise ValueError("approval session does not match active lease")
                if request.session_epoch and request.session_epoch != str(lease.epoch):
                    raise ValueError("approval epoch does not match active lease")
                bound_session_id = str(lease.session_id)
                bound_session_epoch = str(lease.epoch)
            else:
                bound_session_id = request.session_id
                bound_session_epoch = request.session_epoch
            approval = ApprovalRequest(
                approval_request_id=approval_id,
                ticket_id=ticket_id,
                created_by=created_by,
                **request.model_dump(
                    exclude={
                        "expires_at",
                        "claim_id",
                        "waiter_owner",
                        "ticket_attempt",
                        "session_id",
                        "session_epoch",
                    }
                ),
                session_id=bound_session_id,
                session_epoch=bound_session_epoch,
                expires_at=expires_at,
                waiter_owner=request.waiter_owner,
                claim_id=claim.get("claim_id") if isinstance(claim, dict) else None,
                ticket_attempt=(
                    request.ticket_attempt
                    or (claim.get("claim_id") if isinstance(claim, dict) else None)
                ),
            )
            raw[approval_id] = approval.model_dump(mode="json")
            ticket.updated_at = datetime.now(timezone.utc)
            self._persist_ticket(ticket)
            self._audit_log(
                "approval_requested",
                ticket_id,
                {
                    "approval_request_id": approval_id,
                    "validation_id": request.validation_id,
                },
            )
            self._trace_mutation(
                ticket_id,
                "approval_requested",
                attributes={"approval_request_id": approval_id},
            )
            return approval

    def _cancel_pending_approvals_unlocked(
        self, ticket: Ticket, *, reason: str, validation_id: str | None = None
    ) -> None:
        raw = ticket.custom_fields.get("approval_requests", {})
        if not isinstance(raw, dict):
            return
        now = datetime.now(timezone.utc)
        for approval_id, value in list(raw.items()):
            if not isinstance(value, dict) or value.get("status") != "pending":
                continue
            current = ApprovalRequest.model_validate(value)
            if validation_id is not None and current.validation_id != validation_id:
                continue
            raw[approval_id] = current.model_copy(
                update={
                    "status": "cancelled",
                    "resolved_at": now,
                    "resolved_by": "system",
                    "resolution_reason": reason,
                    "record_version": current.record_version + 1,
                }
            ).model_dump(mode="json")
            # Keep cancellation causal and durable; approval data remains
            # queryable without relying on comment ordering.
            self._audit_log(
                "approval_cancelled",
                ticket.id,
                {
                    "approval_request_id": approval_id,
                    "validation_id": current.validation_id,
                    "execution_intent_digest": current.execution_intent_digest,
                    "reason": reason,
                    "actor": "system",
                },
            )
            self._trace_mutation(
                ticket.id,
                "approval_cancelled",
                attributes={"approval_request_id": approval_id, "reason": reason},
            )

    def consume_approval_request(
        self,
        ticket_id: str,
        approval_request_id: str,
        request: ConsumeApprovalRequest,
        *,
        consumed_by: str,
    ) -> ApprovalRequest:
        """Atomically spend an approved request exactly once."""
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None:
                raise TicketNotFound(f"Ticket {ticket_id} not found")
            raw = ticket.custom_fields.get("approval_requests", {})
            value = raw.get(approval_request_id) if isinstance(raw, dict) else None
            if not isinstance(value, dict):
                raise TicketNotFound("approval request not found")
            current = ApprovalRequest.model_validate(value)
            self._expire_pending_approvals_unlocked(ticket)
            value = raw.get(approval_request_id) if isinstance(raw, dict) else None
            if isinstance(value, dict):
                current = ApprovalRequest.model_validate(value)
            if current.status != "approved":
                raise ValueError("approval request is not approved")
            if current.consumed_at is not None:
                raise ValueError("approval request was already consumed")
            for key in (
                "validation_id",
                "presented_run_file_digest",
                "execution_intent_digest",
            ):
                if getattr(current, key) != getattr(request, key):
                    raise ValueError(f"approval request {key} does not match")
            if current.session_id is not None:
                lease = self._read_orchestrator_lease()
                if (
                    lease is None
                    or lease.expires_at <= self._lease_now()
                    or request.session_id != current.session_id
                    or request.session_epoch != current.session_epoch
                    or str(lease.session_id) != current.session_id
                    or str(lease.epoch) != current.session_epoch
                ):
                    raise ValueError("approval requires the active fenced session")
            if (
                current.ticket_attempt is not None
                and request.ticket_attempt != current.ticket_attempt
            ):
                raise ValueError("approval ticket attempt does not match")
            claim = ticket.custom_fields.get("claim")
            if current.session_id is None or current.claim_id is None:
                raise ValueError(
                    "approval is not bound to an active fenced ticket attempt"
                )
            if current.claim_id is not None:
                if (
                    not isinstance(claim, dict)
                    or claim.get("claim_id") != current.claim_id
                    or request.claim_id != current.claim_id
                    or datetime.fromisoformat(claim["expires"]) <= self._lease_now()
                ):
                    raise ValueError("approval requires the active ticket claim")
            consumed = current.model_copy(
                update={
                    "consumed_at": datetime.now(timezone.utc),
                    "record_version": current.record_version + 1,
                }
            )
            raw[approval_request_id] = consumed.model_dump(mode="json")
            ticket.updated_at = datetime.now(timezone.utc)
            self._persist_ticket(ticket)
            self._audit_log(
                "approval_consumed",
                ticket_id,
                {
                    "approval_request_id": approval_request_id,
                    "consumed_by": consumed_by,
                },
            )
            self._trace_mutation(
                ticket_id,
                "approval_consumed",
                attributes={"approval_request_id": approval_request_id},
            )
            return consumed

    def list_approval_requests(self, ticket_id: str) -> list[ApprovalRequest]:
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None:
                raise TicketNotFound(f"Ticket {ticket_id} not found")
            self._expire_pending_approvals_unlocked(ticket)
            raw = ticket.custom_fields.get("approval_requests", {})
            if not isinstance(raw, dict):
                return []
            return [
                ApprovalRequest.model_validate(value)
                for value in raw.values()
                if isinstance(value, dict)
            ]

    def resolve_approval_request(
        self,
        ticket_id: str,
        approval_request_id: str,
        request: ResolveApprovalRequest,
        *,
        resolved_by: str,
    ) -> ApprovalRequest:
        """Resolve exactly once, checking the immutable intent with CAS semantics."""
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None:
                raise TicketNotFound(f"Ticket {ticket_id} not found")
            raw = ticket.custom_fields.get("approval_requests", {})
            current_raw = (
                raw.get(approval_request_id) if isinstance(raw, dict) else None
            )
            if not isinstance(current_raw, dict):
                raise TicketNotFound("approval request not found")
            current = ApprovalRequest.model_validate(current_raw)
            self._expire_pending_approvals_unlocked(ticket)
            current_raw = (
                raw.get(approval_request_id) if isinstance(raw, dict) else None
            )
            if isinstance(current_raw, dict):
                current = ApprovalRequest.model_validate(current_raw)
            if current.status != "pending":
                raise ValueError(f"approval request is already {current.status}")
            if request.comment_id:
                for item in raw.values():
                    if (
                        isinstance(item, dict)
                        and item.get("resolution_comment_id") == request.comment_id
                    ):
                        raise ValueError("resolution comment was already consumed")
            for key, expected in (
                ("validation_id", request.validation_id),
                ("presented_run_file_digest", request.presented_run_file_digest),
                ("execution_intent_digest", request.execution_intent_digest),
            ):
                if expected is not None and expected != getattr(current, key):
                    raise ValueError(f"approval request {key} does not match")
            resolution_comment_id = request.comment_id
            if request.comment and resolution_comment_id is None:
                comment = Comment(
                    id=uuid.uuid4().hex[:8], author=resolved_by, body=request.comment
                )
                ticket.comments.append(comment)
                resolution_comment_id = comment.id
            resolved = current.model_copy(
                update={
                    "status": request.decision,
                    "resolved_at": datetime.now(timezone.utc),
                    "resolved_by": resolved_by,
                    "resolution_comment_id": resolution_comment_id,
                    "resolution_reason": request.reason,
                    "record_version": current.record_version + 1,
                }
            )
            raw[approval_request_id] = resolved.model_dump(mode="json")
            ticket.updated_at = datetime.now(timezone.utc)
            self._persist_ticket(ticket)
            self._audit_log(
                "approval_resolved",
                ticket_id,
                {
                    "approval_request_id": approval_request_id,
                    "status": request.decision,
                },
            )
            self._trace_mutation(
                ticket_id,
                "approval_resolved",
                attributes={
                    "approval_request_id": approval_request_id,
                    "status": request.decision,
                },
            )
            return resolved

    def get_tickets_since(self, since_seq: int) -> list[Ticket]:
        with self._lock:
            return [
                t.model_copy()
                for t in self._tickets.values()
                if t.transition_seq > since_seq
            ]

    def claim_ticket(
        self,
        ticket_id: str,
        owner: str,
        duration_seconds: int = 300,
        *,
        session_id: uuid.UUID | None = None,
        epoch: int | None = None,
        claim_id: str | None = None,
        instance_name: str | None = None,
    ) -> dict | None:
        """Atomically claim a ticket for dispatch.

        Returns the claim dict on success, None if already claimed by
        another owner with an unexpired lease.
        """
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None:
                raise TicketNotFound(f"Ticket {ticket_id} not found")

            self._validate_claim_fence(session_id, epoch, ticket_id)

            if ticket.custom_fields.get(
                "imported_fixture"
            ) and not ticket.custom_fields.get("imported_fixture_reviewed"):
                self._audit_log(
                    "claim_ticket",
                    ticket_id,
                    {"owner": owner, "result": "rejected_imported_fixture"},
                )
                self._trace_mutation(
                    ticket_id,
                    "claim_ticket",
                    rejected=True,
                    attributes={"reason": "imported_fixture_requires_review"},
                )
                raise TicketDispatchBlocked(
                    "Imported fixture is non-dispatchable until an explicit "
                    "reviewed resume operation is recorded"
                )

            now = datetime.now(timezone.utc)
            existing = ticket.custom_fields.get("claim")
            if existing:
                expires = datetime.fromisoformat(existing["expires"])
                if expires > now and (
                    existing["owner"] != owner
                    or (
                        session_id is not None
                        and existing.get("session_id") != str(session_id)
                    )
                ):
                    if session_id is not None:
                        reason = "claim_owned_by_other_session"
                        self._audit_log(
                            "claim_fence_rejected",
                            ticket_id,
                            {"reason": reason},
                        )
                        self._trace_mutation(
                            ticket_id,
                            "claim_ticket",
                            rejected=True,
                            attributes={"reason": reason},
                        )
                        raise ClaimFenceError(
                            reason,
                            "ticket claim is owned by another orchestrator session",
                        )
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
                "claim_id": existing.get("claim_id", f"claim-{uuid.uuid4().hex}")
                if isinstance(existing, dict)
                else f"claim-{uuid.uuid4().hex}",
                "owner": owner,
                "expires": expires.isoformat(),
                "status": ticket.status.value,
            }
            if session_id is not None:
                claim.update(
                    {
                        "session_id": str(session_id),
                        "epoch": epoch,
                        "claim_id": claim_id or str(uuid.uuid4()),
                        "instance_name": instance_name or owner,
                    }
                )
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

    def release_claim(
        self,
        ticket_id: str,
        owner: str,
        *,
        session_id: uuid.UUID | None = None,
        epoch: int | None = None,
        claim_id: str | None = None,
    ) -> bool:
        """Release a claim if owned by the given owner."""
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None:
                raise TicketNotFound(f"Ticket {ticket_id} not found")

            self._validate_claim_fence(session_id, epoch, ticket_id)

            existing = ticket.custom_fields.get("claim")
            if not existing or existing["owner"] != owner:
                self._audit_log(
                    "release_claim",
                    ticket_id,
                    {"owner": owner, "result": "not_owner"},
                )
                return False
            if datetime.fromisoformat(existing["expires"]) <= self._lease_now():
                reason = "claim_expired"
                self._audit_log("claim_fence_rejected", ticket_id, {"reason": reason})
                self._trace_mutation(
                    ticket_id,
                    "claim_fence",
                    rejected=True,
                    attributes={"reason": reason},
                )
                raise ClaimFenceError(reason, "ticket claim has expired")
            self._validate_claim_owner(existing, session_id, epoch, claim_id, ticket_id)

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
        self,
        ticket_id: str,
        owner: str,
        duration_seconds: int = 300,
        *,
        session_id: uuid.UUID | None = None,
        epoch: int | None = None,
        claim_id: str | None = None,
    ) -> dict | None:
        """Extend an existing claim's expiry. Returns updated claim or None."""
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None:
                raise TicketNotFound(f"Ticket {ticket_id} not found")

            self._validate_claim_fence(session_id, epoch, ticket_id)

            existing = ticket.custom_fields.get("claim")
            if not existing or existing["owner"] != owner:
                self._audit_log(
                    "renew_claim",
                    ticket_id,
                    {"owner": owner, "result": "not_owner"},
                )
                self._trace_mutation(ticket_id, "renew_claim", rejected=True)
                return None
            if datetime.fromisoformat(existing["expires"]) <= self._lease_now():
                reason = "claim_expired"
                self._audit_log("claim_fence_rejected", ticket_id, {"reason": reason})
                self._trace_mutation(
                    ticket_id,
                    "claim_fence",
                    rejected=True,
                    attributes={"reason": reason},
                )
                raise ClaimFenceError(reason, "ticket claim has expired")
            self._validate_claim_owner(existing, session_id, epoch, claim_id, ticket_id)

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

    def _validate_claim_fence(
        self,
        session_id: uuid.UUID | None,
        epoch: int | None,
        ticket_id: str,
        *,
        require_active: bool = False,
    ) -> None:
        def reject(reason: str, detail: str) -> None:
            self._audit_log(
                "claim_fence_rejected",
                ticket_id,
                {
                    "reason": reason,
                    "session_id": str(session_id) if session_id else None,
                    "epoch": epoch,
                },
            )
            self._trace_mutation(
                ticket_id,
                "claim_fence",
                rejected=True,
                attributes={"reason": reason, "epoch": epoch},
            )
            raise ClaimFenceError(reason, detail)

        if session_id is None and epoch is None and not require_active:
            return
        if session_id is None or epoch is None:
            reject("not_leader", "session_id and epoch are required")
        lease = self._read_orchestrator_lease()
        now = self._lease_now()
        if lease is None or lease.expires_at <= now:
            reject("not_leader", "no active orchestrator lease")
        if lease.epoch != epoch:
            reject("stale_epoch", "orchestrator fencing epoch is stale")
        if lease.session_id != session_id:
            reject("not_leader", "orchestrator session is not the leader")

    def require_claim_fence(
        self,
        ticket_id: str,
        session_id: uuid.UUID | None,
        epoch: int | None,
    ) -> None:
        """Validate a claim API identity, including the active lease requirement."""
        self._validate_claim_fence(
            session_id,
            epoch,
            ticket_id,
            require_active=True,
        )

    def reject_claim_fence(self, ticket_id: str, reason: str, detail: str) -> None:
        self._audit_log("claim_fence_rejected", ticket_id, {"reason": reason})
        self._trace_mutation(
            ticket_id,
            "claim_fence",
            rejected=True,
            attributes={"reason": reason},
        )
        raise ClaimFenceError(reason, detail)

    def _validate_ticket_claim(
        self,
        ticket_id: str,
        session_id: uuid.UUID | None,
        epoch: int | None,
        claim_id: str | None,
    ) -> None:
        if session_id is None and epoch is None and claim_id is None:
            return
        ticket = self._tickets.get(ticket_id)
        claim = ticket.custom_fields.get("claim") if ticket else None
        if not isinstance(claim, dict) or not claim_id:
            self.reject_claim_fence(
                ticket_id, "claim_missing", "ticket claim is missing"
            )
        try:
            expires = datetime.fromisoformat(claim["expires"])
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires <= self._lease_now():
                self.reject_claim_fence(
                    ticket_id, "claim_expired", "ticket claim has expired"
                )
            if (
                not isinstance(claim["session_id"], str)
                or not claim["session_id"]
                or not isinstance(claim["epoch"], int)
                or isinstance(claim["epoch"], bool)
                or claim["epoch"] <= 0
                or not isinstance(claim["claim_id"], str)
                or not claim["claim_id"]
            ):
                raise ValueError("invalid claim identity")
        except (KeyError, TypeError, ValueError):
            self.reject_claim_fence(
                ticket_id,
                "claim_malformed",
                "ticket claim identity or expiry is malformed",
            )
        if (
            claim.get("session_id") != str(session_id)
            or claim.get("epoch") != epoch
            or claim.get("claim_id") != claim_id
        ):
            self.reject_claim_fence(
                ticket_id,
                "claim_owned_by_other_session",
                "mutation claim fence does not match ticket claim",
            )

    def _validate_claim_owner(
        self,
        existing: dict,
        session_id: uuid.UUID | None,
        epoch: int | None,
        claim_id: str | None,
        ticket_id: str,
    ) -> None:
        if session_id is None:
            return
        if not claim_id:
            reason = "claim_owned_by_other_session"
            self._audit_log("claim_fence_rejected", ticket_id, {"reason": reason})
            self._trace_mutation(
                ticket_id, "claim_fence", rejected=True, attributes={"reason": reason}
            )
            raise ClaimFenceError(reason, "claim_id is required")
        if (
            existing.get("session_id") != str(session_id)
            or existing.get("epoch") != epoch
        ):
            reason = "claim_owned_by_other_session"
            self._audit_log("claim_fence_rejected", ticket_id, {"reason": reason})
            self._trace_mutation(
                ticket_id, "claim_fence", rejected=True, attributes={"reason": reason}
            )
            raise ClaimFenceError(reason, "claim is owned by another session or epoch")
        if claim_id is not None and existing.get("claim_id") != claim_id:
            reason = "claim_owned_by_other_session"
            self._audit_log("claim_fence_rejected", ticket_id, {"reason": reason})
            self._trace_mutation(
                ticket_id, "claim_fence", rejected=True, attributes={"reason": reason}
            )
            raise ClaimFenceError(reason, "claim id mismatch")

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
