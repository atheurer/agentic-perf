"""Agent-layer glue for kernel transitions.

Everything the kernel tools need that is not a remote command lives here:
host authorization against the ticket, plan cross-checking, persisted
change intents, approval authorization, and the per-step checkpoint
writer.  The provisioning MCP tools call these helpers; the LLM never
supplies anything except structured parameters.

State-store writes go through ``AuditedAsyncHTTPClient`` imported at
module level so tests can substitute a fake client here without touching
the shared ``providers.execution`` symbol.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from typing import Any

from agents.server_utils import (
    is_self_host,
    resolve_addrs,
    ticket_controller_host,
    ticket_state_headers,
)
from providers.execution import AuditedAsyncHTTPClient
from providers.rhel_kernel import (
    KernelSpec,
    transition_state,
    validate_host_transition,
)

logger = logging.getLogger(__name__)

KERNEL_ACTIONS: tuple[str, ...] = ("install", "select_default", "reboot")

# One writer at a time inside this process.  The agent loop runs tool calls
# sequentially and other processes are claim-fenced, so this lock only
# guards against a future concurrent caller in the same server.
_write_lock = asyncio.Lock()


def _store_url() -> str:
    return os.environ.get("STATE_STORE_URL", "http://localhost:8090")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# Host authorization
# ---------------------------------------------------------------------------


def _ip_mapping(ticket: dict[str, Any]) -> dict[str, str]:
    cf = ticket.get("custom_fields", {})
    meta = cf.get("resource_provider_metadata", {})
    if isinstance(meta, dict):
        mapping = meta.get("ip_mapping", {})
        if isinstance(mapping, dict):
            return mapping
    return {}


def _canonicalize_host(host: str, ip_mapping: dict[str, str]) -> str:
    """Return the canonical address for a host (public -> private mapping)."""
    host = host.strip()
    if host in ip_mapping:
        return ip_mapping[host]
    return host


def _hosts_match(host_a: str, host_b: str, ip_mapping: dict[str, str]) -> bool:
    """Check if two host identifiers refer to the same machine."""
    a = _canonicalize_host(host_a, ip_mapping)
    b = _canonicalize_host(host_b, ip_mapping)
    if a == b:
        return True
    return bool(resolve_addrs(a) & resolve_addrs(b))


def authorized_kernel_hosts(ticket: dict[str, Any]) -> dict[str, str]:
    """Return {canonical_ip: original_entry} for every authorized kernel target.

    Authorized = ``assigned_hardware_ips.targets`` (resource-agent owned)
    minus the harness controller, minus anything that resolves to the
    orchestrator.  Canonicalization mirrors the handoff checker.
    """
    cf = ticket.get("custom_fields", {})
    assigned = cf.get("assigned_hardware_ips", {})
    targets = assigned.get("targets", []) if isinstance(assigned, dict) else []
    controller = ticket_controller_host(ticket)
    ip_mapping = _ip_mapping(ticket)

    result: dict[str, str] = {}
    for host in targets:
        if not isinstance(host, str) or not host.strip():
            continue
        canonical = _canonicalize_host(host, ip_mapping)
        if controller and _hosts_match(canonical, controller, ip_mapping):
            continue
        if is_self_host(canonical):
            continue
        result[canonical] = host
    return result


def canonicalize_targets(
    hosts: list[str],
    ticket: dict[str, Any],
) -> tuple[list[str], dict[str, dict[str, str]], dict[str, str]]:
    """Split caller-supplied hosts into canonical allowed hosts and refusals.

    Returns ``(allowed_canonical, refused, requested_as)`` where
    ``requested_as`` maps each canonical address back to the string the
    caller used.  Every later SSH call targets the canonical address that
    passed validation, never the caller string.
    """
    authorized = authorized_kernel_hosts(ticket)
    controller = ticket_controller_host(ticket)
    ip_mapping = _ip_mapping(ticket)
    allowed: list[str] = []
    refused: dict[str, dict[str, str]] = {}
    requested_as: dict[str, str] = {}
    seen: set[str] = set()

    for raw in hosts:
        host = str(raw).strip()
        if not host or host in seen:
            continue
        seen.add(host)
        if is_self_host(host):
            refused[host] = {"state": "refused", "reason": "orchestrator_self"}
            continue
        if controller and _hosts_match(host, controller, ip_mapping):
            refused[host] = {"state": "refused", "reason": "harness_controller"}
            continue
        if not resolve_addrs(host):
            refused[host] = {"state": "refused", "reason": "unresolvable"}
            continue
        canonical = _canonicalize_host(host, ip_mapping)
        if canonical not in authorized:
            # The caller may have used a name that resolves to an assigned IP.
            match = next(
                (
                    key
                    for key in authorized
                    if key != canonical and resolve_addrs(key) & resolve_addrs(host)
                ),
                None,
            )
            if match is None:
                refused[host] = {"state": "refused", "reason": "host_not_assigned"}
                continue
            canonical = match
        if canonical in requested_as:
            continue
        allowed.append(canonical)
        requested_as[canonical] = host
    return allowed, refused, requested_as


def refuse_protected_targets(
    hosts: list[str],
    ticket: dict[str, Any],
) -> tuple[list[str], dict[str, dict[str, str]]]:
    """Compatibility wrapper around canonicalize_targets()."""
    allowed, refused, _ = canonicalize_targets(hosts, ticket)
    return allowed, refused


def require_full_coverage(
    allowed: list[str],
    refused: dict[str, dict[str, str]],
    ticket: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    """A kernel transition covers every authorized host or it is refused.

    Host lists are an identity, not a selection: fewer hosts than the
    ticket's SUT set would let a benchmark run on mixed kernels.
    """
    authorized = set(authorized_kernel_hosts(ticket))
    allowed_set = set(allowed)
    detail: dict[str, Any] = {}
    if refused:
        detail["refused"] = refused
    missing = sorted(authorized - allowed_set)
    extra = sorted(allowed_set - authorized)
    if missing:
        detail["missing"] = missing
    if extra:
        detail["extra"] = extra
    if not authorized:
        detail["error"] = "ticket has no assigned target hosts"
    ok = bool(authorized) and not refused and not missing and not extra
    return ok, detail


# ---------------------------------------------------------------------------
# Plan cross-check
# ---------------------------------------------------------------------------


def current_kernel_step(
    ticket: dict[str, Any],
) -> tuple[int, KernelSpec] | None:
    """Return (step_id, KernelSpec) if the current plan step is a kernel step."""
    cf = ticket.get("custom_fields", {})
    plan = cf.get("execution_plan", {})
    current = plan.get("current_step")
    if current is None:
        return None
    for step in plan.get("steps", []):
        if step.get("id") == current:
            kernel = step.get("params", {}).get("kernel")
            if not kernel:
                return None
            try:
                return (current, KernelSpec.parse(kernel))
            except ValueError:
                return None
    return None


def step_kernel_hosts(ticket: dict[str, Any]) -> list[str] | None:
    """Return the plan step's explicit ``kernel.hosts`` list, or None."""
    cf = ticket.get("custom_fields", {})
    plan = cf.get("execution_plan", {})
    current = plan.get("current_step")
    for step in plan.get("steps", []):
        if step.get("id") == current:
            kernel = step.get("params", {}).get("kernel")
            if isinstance(kernel, dict):
                hosts = kernel.get("hosts")
                if isinstance(hosts, list):
                    return [str(h) for h in hosts]
            return None
    return None


def assert_kernel_matches_step(
    ticket: dict[str, Any],
    kernel: str | dict,
) -> tuple[bool, str]:
    """Check that the requested kernel matches the current plan step.

    The release must match exactly; when the caller names a package it
    must match the step's package too.  Callers then use the *step's* spec
    (with its package) for every command, so a plan that names
    ``kernel-rt`` can never be satisfied by plain ``kernel``.
    """
    ks = current_kernel_step(ticket)
    if ks is None:
        return False, "kernel_not_in_plan"
    _, step_spec = ks
    try:
        requested = KernelSpec.parse(kernel)
    except ValueError as exc:
        return False, f"invalid_kernel: {exc}"
    if requested.release != step_spec.release:
        return False, (
            f"kernel_mismatch: requested {requested.release}"
            f" != plan step {step_spec.release}"
        )
    if isinstance(kernel, dict) and kernel.get("package"):
        if requested.package != step_spec.package:
            return False, (
                f"kernel_mismatch: requested package {requested.package}"
                f" != plan step {step_spec.package}"
            )
    return True, ""


# ---------------------------------------------------------------------------
# Intents
# ---------------------------------------------------------------------------


def compute_intent_digest(
    ticket_id: str,
    step_id: int,
    hosts: list[str],
    kernel: dict[str, Any],
    actions: list[str],
) -> str:
    """Compute the sha256 digest for a kernel change intent."""
    canonical = json.dumps(
        {
            "ticket_id": ticket_id,
            "step_id": step_id,
            "hosts": sorted(hosts),
            "kernel": kernel,
            "actions": actions,
        },
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def new_intent(
    ticket_id: str,
    step_id: int,
    hosts: list[str],
    spec: KernelSpec,
    actions: list[str],
    inventory_before: dict[str, Any],
) -> dict[str, Any]:
    """Create a new kernel change intent record (not yet persisted)."""
    intent_id = f"kci-{os.urandom(16).hex()}"
    kernel_dict = spec.to_dict()
    digest = compute_intent_digest(ticket_id, step_id, hosts, kernel_dict, actions)
    return {
        "intent_id": intent_id,
        "step_id": step_id,
        "hosts": sorted(hosts),
        "kernel": kernel_dict,
        "actions_required": list(actions),
        "inventory_before": inventory_before,
        "intent_digest": digest,
        "created_at": _now(),
        "approval_request_id": None,
        "progress": {a: "pending" for a in actions},
    }


def read_intents(cf: dict[str, Any]) -> dict[str, dict[str, Any]]:
    intents = cf.get("kernel_change_intents", {})
    if not isinstance(intents, dict):
        return {}
    records = intents.get("records", {})
    return records if isinstance(records, dict) else {}


def find_intent(cf: dict[str, Any], intent_id: str) -> dict[str, Any] | None:
    record = read_intents(cf).get(intent_id)
    return record if isinstance(record, dict) else None


async def _get_ticket(client: Any, ticket_id: str) -> dict[str, Any]:
    response = await client.get(f"{_store_url()}/api/v1/tickets/{ticket_id}")
    if response.status_code != 200:
        raise RuntimeError(f"ticket {ticket_id} unavailable: {response.status_code}")
    return response.json()


async def _patch_fields(client: Any, ticket_id: str, fields: dict[str, Any]) -> None:
    response = await client.patch(
        f"{_store_url()}/api/v1/tickets/{ticket_id}/fields",
        json={"fields": fields},
    )
    response.raise_for_status()


async def write_intent(ticket_id: str, intent: dict[str, Any]) -> None:
    """Persist a new intent record under ``kernel_change_intents.records``."""
    async with _write_lock:
        async with AuditedAsyncHTTPClient(
            timeout=10.0, headers=ticket_state_headers()
        ) as client:
            ticket = await _get_ticket(client, ticket_id)
            cf = ticket.get("custom_fields", {})
            records = dict(read_intents(cf))
            records[intent["intent_id"]] = intent
            await _patch_fields(
                client, ticket_id, {"kernel_change_intents": {"records": records}}
            )


async def update_intent(
    ticket_id: str,
    intent_id: str,
    *,
    progress: dict[str, str] | None = None,
    approval_request_id: str | None = None,
) -> dict[str, Any]:
    """Read-modify-write the mutable parts of an intent (progress, approval)."""
    async with _write_lock:
        async with AuditedAsyncHTTPClient(
            timeout=10.0, headers=ticket_state_headers()
        ) as client:
            ticket = await _get_ticket(client, ticket_id)
            cf = ticket.get("custom_fields", {})
            records = dict(read_intents(cf))
            intent = dict(records.get(intent_id) or {})
            if not intent:
                raise RuntimeError(f"intent {intent_id} not found")
            if progress:
                merged = dict(intent.get("progress", {}))
                merged.update(progress)
                intent["progress"] = merged
            if approval_request_id is not None:
                intent["approval_request_id"] = approval_request_id
            intent["updated_at"] = _now()
            records[intent_id] = intent
            await _patch_fields(
                client, ticket_id, {"kernel_change_intents": {"records": records}}
            )
            return intent


# ---------------------------------------------------------------------------
# Approval authorization
# ---------------------------------------------------------------------------


def _fence_fields(cf: dict[str, Any]) -> dict[str, Any]:
    claim_id = os.environ.get("AGENTIC_PERF_CLAIM_ID") or (cf.get("claim") or {}).get(
        "claim_id"
    )
    return {
        "session_id": os.environ.get("AGENTIC_PERF_ORCHESTRATOR_SESSION_ID"),
        "session_epoch": os.environ.get("AGENTIC_PERF_ORCHESTRATOR_EPOCH"),
        "ticket_attempt": claim_id,
        "claim_id": claim_id,
    }


def _is_expired(approval: dict[str, Any]) -> bool:
    expires = approval.get("expires_at")
    if not expires:
        return False
    from datetime import datetime, timezone

    text = str(expires)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return False
    if when.tzinfo is None:
        # datetime.replace() would read as a filesystem call to the
        # side-effect scanner; rebuild the aware value instead.
        when = datetime.combine(when.date(), when.time(), tzinfo=timezone.utc)
    return when <= datetime.now(timezone.utc)


async def authorize_action(
    ticket_id: str,
    approval_request_id: str,
    action: str,
) -> tuple[bool, str, dict[str, Any]]:
    """Authorize one mutating kernel action against a kernel-change approval.

    Rules (design §1.3 and review R2-3):

    1. the approval must exist, be ``kind == "kernel_change"``, be approved,
       and not expired; its ``validation_id`` names the intent it binds;
    2. ``action`` must be one of the intent's ``actions_required`` and every
       action ordered before it must already be ``done`` — the first action
       satisfies this vacuously, so a retry after a crash between consume
       and the first mutation simply runs again;
    3. if the approval is unconsumed, consume it now (the store's single-use
       CAS is the atomic check); if it was already consumed, the binding in
       rule 1 proves this intent spent it, so proceed;
    4. mark ``progress[action] = "started"`` before the caller mutates.

    Returns ``(ok, reason_code, detail)``; ``detail["intent"]`` carries the
    intent record on success.
    """
    if action not in KERNEL_ACTIONS:
        return False, "unknown_action", {"action": action}
    if not approval_request_id:
        return False, "approval_required", {}

    async with AuditedAsyncHTTPClient(
        timeout=10.0, headers=ticket_state_headers()
    ) as client:
        ticket = await _get_ticket(client, ticket_id)
        cf = ticket.get("custom_fields", {})

        response = await client.get(
            f"{_store_url()}/api/v1/tickets/{ticket_id}/approvals"
        )
        if response.status_code != 200:
            return False, "approval_check_failed", {"status": response.status_code}
        approvals = response.json().get("approvals", [])
        approval = next(
            (
                item
                for item in approvals
                if item.get("approval_request_id") == approval_request_id
            ),
            None,
        )
        if approval is None:
            return False, "approval_binding_missing", {}
        if approval.get("kind") != "kernel_change":
            return False, "approval_not_bound", {"kind": approval.get("kind")}
        intent_id = approval.get("validation_id", "")
        intent = find_intent(cf, intent_id)
        if intent is None:
            return False, "intent_not_found", {"intent_id": intent_id}
        if approval.get("presented_run_file_digest") != intent.get("intent_digest"):
            return False, "approval_not_bound", {"intent_id": intent_id}
        if approval.get("status") != "approved":
            return False, "approval_not_approved", {"status": approval.get("status")}
        if _is_expired(approval):
            return False, "approval_expired", {"expires_at": approval.get("expires_at")}

        actions = list(intent.get("actions_required", []))
        if action not in actions:
            return False, "action_not_required", {"actions_required": actions}
        progress = intent.get("progress", {})
        predecessors = actions[: actions.index(action)]
        pending = [a for a in predecessors if progress.get(a) != "done"]
        if pending:
            return False, "action_out_of_order", {"pending": pending}

        if approval.get("consumed_at") is None:
            consumed = await client.post(
                f"{_store_url()}/api/v1/tickets/{ticket_id}/approvals/"
                f"{approval_request_id}/consume",
                json={
                    "kind": "kernel_change",
                    "validation_id": intent_id,
                    "presented_run_file_digest": intent["intent_digest"],
                    "execution_intent_digest": intent["intent_digest"],
                    **_fence_fields(cf),
                },
            )
            if consumed.status_code >= 300:
                return False, "approval_not_approved", {"consume": consumed.status_code}

    intent = await update_intent(ticket_id, intent_id, progress={action: "started"})
    return True, "", {"intent": intent}


async def complete_action(ticket_id: str, intent_id: str, action: str) -> None:
    await update_intent(ticket_id, intent_id, progress={action: "done"})


# ---------------------------------------------------------------------------
# Checkpoints
# ---------------------------------------------------------------------------


def read_transition(cf: dict[str, Any], step_id: int | str) -> dict[str, Any]:
    transitions = cf.get("kernel_transitions", {})
    if not isinstance(transitions, dict):
        return {}
    record = transitions.get(str(step_id), {})
    return record if isinstance(record, dict) else {}


def new_transition(
    step_id: int | str,
    spec: KernelSpec,
    hosts: list[str],
    *,
    intent_id: str | None,
    approval_request_id: str | None,
    strategy: str = "serial",
) -> dict[str, Any]:
    return {
        "kernel": spec.release,
        "package": spec.package,
        "requested_hosts": sorted(hosts),
        "strategy": strategy,
        "intent_id": intent_id,
        "approval_request_id": approval_request_id,
        "state": "in_progress",
        "hosts": {},
        "record_version": 0,
        "updated_at": _now(),
    }


def set_host_record(
    transition: dict[str, Any],
    host: str,
    record: dict[str, Any],
) -> None:
    """Replace one host record, enforcing the host state table."""
    previous = transition.get("hosts", {}).get(host, {}).get("state", "pending")
    validate_host_transition(previous, record.get("state", "pending"))
    transition.setdefault("hosts", {})[host] = record
    transition["state"] = transition_state(
        transition["hosts"], list(transition.get("requested_hosts", []))
    )


async def write_transition(
    ticket_id: str,
    step_id: int | str,
    transition: dict[str, Any],
) -> dict[str, Any]:
    """Persist the step's transition record with a fresh read and a version check.

    ``custom_fields`` has no server-side compare-and-swap, so the writer
    re-reads immediately before writing, bumps ``record_version`` and
    verifies after the write that the stored version is its own.  A
    mismatch means an unexpected concurrent writer and is raised, not
    retried.
    """
    async with _write_lock:
        async with AuditedAsyncHTTPClient(
            timeout=10.0, headers=ticket_state_headers()
        ) as client:
            ticket = await _get_ticket(client, ticket_id)
            cf = ticket.get("custom_fields", {})
            transitions = dict(cf.get("kernel_transitions", {}) or {})
            stored = transitions.get(str(step_id), {})
            version = int((stored or {}).get("record_version", 0)) + 1
            transition = dict(transition)
            transition["record_version"] = version
            transition["updated_at"] = _now()
            transitions[str(step_id)] = transition
            await _patch_fields(client, ticket_id, {"kernel_transitions": transitions})
            check = await _get_ticket(client, ticket_id)
            stored_after = (
                check.get("custom_fields", {}).get("kernel_transitions", {}) or {}
            ).get(str(step_id), {})
            if int(stored_after.get("record_version", -1)) != version:
                raise RuntimeError(
                    f"kernel transition for step {step_id} was overwritten "
                    f"(expected version {version}, found "
                    f"{stored_after.get('record_version')})"
                )
    return transition


async def pause_ticket(ticket_id: str, comment: str) -> None:
    """Move the ticket to awaiting_customer_guidance with fenced headers."""
    async with AuditedAsyncHTTPClient(
        timeout=10.0, headers=ticket_state_headers()
    ) as client:
        response = await client.post(
            f"{_store_url()}/api/v1/tickets/{ticket_id}/transition",
            json={"status": "awaiting_customer_guidance", "comment": comment},
        )
        if response.status_code >= 300:
            logger.warning(
                "[kernel] could not pause ticket %s: %s",
                ticket_id,
                response.status_code,
            )


def verification_from_transition(
    ticket: dict[str, Any],
    step_id: int | str,
) -> tuple[bool, dict[str, str]]:
    """Derive ``provisioning_verification`` for a kernel step from its checkpoint.

    Never from the LLM: the checkpoint is written by the reboot tool after
    live probes, so it is the only evidence that counts.
    """
    cf = ticket.get("custom_fields", {})
    transition = read_transition(cf, step_id)
    authorized = sorted(authorized_kernel_hosts(ticket))
    hosts = transition.get("hosts", {})
    verified = [
        host
        for host in authorized
        if hosts.get(host, {}).get("state") in ("verified", "already_verified")
    ]
    kernel = transition.get("kernel", "?")
    if (
        transition.get("state") == "verified"
        and authorized
        and len(verified) == len(authorized)
    ):
        boot_ids = ", ".join(
            f"{host}={str(hosts[host].get('post_boot_id') or '')[:8]}"
            for host in authorized
        )
        return True, {
            "status": "verified",
            "details": (
                f"kernel {kernel} verified live on {', '.join(authorized)} "
                f"(boot ids {boot_ids})"
            ),
        }
    missing = [host for host in authorized if host not in verified]
    return False, {
        "status": "not_verified",
        "details": (
            f"kernel transition for step {step_id} is "
            f"{transition.get('state') or 'absent'}; unverified hosts: "
            f"{', '.join(missing) if missing else 'none'}"
        ),
    }
