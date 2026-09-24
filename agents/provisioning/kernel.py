"""Agent-layer glue for kernel transitions.

Handles host authorization, plan cross-checking, intent management,
and approval verification. All kernel tools go through these helpers
to enforce the safety invariants.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any

from agents.server_utils import (
    is_self_host,
    resolve_addrs,
    ticket_controller_host,
)
from providers.rhel_kernel import KernelSpec

logger = logging.getLogger(__name__)


def authorized_kernel_hosts(
    ticket: dict[str, Any],
) -> dict[str, str]:
    """Return {canonical_ip: original_arg} for authorized kernel targets.

    Authorized = assigned_hardware_ips.targets minus the controller.
    Uses the same canonicalization as handoff validation.
    """
    cf = ticket.get("custom_fields", {})
    assigned = cf.get("assigned_hardware_ips", {})
    targets = assigned.get("targets", [])
    controller = ticket_controller_host(ticket)

    ip_mapping = {}
    meta = cf.get("resource_provider_metadata", {})
    if isinstance(meta, dict):
        ip_mapping = meta.get("ip_mapping", {})

    result: dict[str, str] = {}
    for host in targets:
        canonical = _canonicalize_host(host, ip_mapping)
        if controller and _hosts_match(canonical, controller, ip_mapping):
            continue
        if is_self_host(canonical):
            continue
        result[canonical] = host
    return result


def _canonicalize_host(
    host: str,
    ip_mapping: dict[str, str],
) -> str:
    """Return the canonical address for a host."""
    if host in ip_mapping:
        return ip_mapping[host]
    return host


def _hosts_match(
    host_a: str,
    host_b: str,
    ip_mapping: dict[str, str],
) -> bool:
    """Check if two host identifiers refer to the same machine."""
    a = _canonicalize_host(host_a, ip_mapping)
    b = _canonicalize_host(host_b, ip_mapping)
    if a == b:
        return True
    a_addrs = resolve_addrs(a)
    b_addrs = resolve_addrs(b)
    return bool(a_addrs & b_addrs)


def refuse_protected_targets(
    hosts: list[str],
    ticket: dict[str, Any],
) -> tuple[list[str], dict[str, dict[str, str]]]:
    """Split hosts into (allowed, refused) based on safety rules.

    Returns (allowed_hosts, {host: {"state": "refused", "reason": ...}}).
    """
    authorized = authorized_kernel_hosts(ticket)
    controller = ticket_controller_host(ticket)
    allowed: list[str] = []
    refused: dict[str, dict[str, str]] = {}

    for host in hosts:
        if is_self_host(host):
            refused[host] = {
                "state": "refused",
                "reason": "orchestrator_self",
            }
        elif controller and _hosts_match(host, controller, {}):
            refused[host] = {
                "state": "refused",
                "reason": "harness_controller",
            }
        elif not resolve_addrs(host):
            refused[host] = {
                "state": "refused",
                "reason": "unresolvable",
            }
        elif host not in authorized and host not in authorized.values():
            canonical = _canonicalize_host(
                host,
                ticket.get("custom_fields", {})
                .get("resource_provider_metadata", {})
                .get("ip_mapping", {}),
            )
            if canonical not in authorized:
                refused[host] = {
                    "state": "refused",
                    "reason": "host_not_assigned",
                }
            else:
                allowed.append(canonical)
        else:
            allowed.append(host)
    return allowed, refused


def current_kernel_step(
    ticket: dict[str, Any],
) -> tuple[int, KernelSpec] | None:
    """Return (step_id, KernelSpec) if the current plan step is a kernel step."""
    cf = ticket.get("custom_fields", {})
    plan = cf.get("execution_plan", {})
    current = plan.get("current_step")
    if current is None:
        return None
    steps = plan.get("steps", [])
    for step in steps:
        if step.get("id") == current:
            params = step.get("params", {})
            kernel = params.get("kernel")
            if kernel:
                try:
                    spec = KernelSpec.parse(kernel)
                    return (current, spec)
                except ValueError:
                    return None
            return None
    return None


def assert_kernel_matches_step(
    ticket: dict[str, Any],
    kernel: str | dict,
) -> tuple[bool, str]:
    """Check that the requested kernel matches the current plan step.

    Returns (ok, reason).
    """
    ks = current_kernel_step(ticket)
    if ks is None:
        return False, "kernel_not_in_plan"
    step_id, step_spec = ks
    try:
        requested = KernelSpec.parse(kernel)
    except ValueError as exc:
        return False, f"invalid_kernel: {exc}"
    if requested.release != step_spec.release:
        return False, (
            f"kernel_mismatch: requested {requested.release}"
            f" != plan step {step_spec.release}"
        )
    return True, ""


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
    """Create a new kernel change intent record."""
    import time

    intent_id = f"kci-{os.urandom(16).hex()}"
    kernel_dict = spec.to_dict()
    digest = compute_intent_digest(
        ticket_id,
        step_id,
        hosts,
        kernel_dict,
        actions,
    )
    return {
        "intent_id": intent_id,
        "step_id": step_id,
        "hosts": sorted(hosts),
        "kernel": kernel_dict,
        "actions_required": actions,
        "inventory_before": inventory_before,
        "intent_digest": digest,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "approval_request_id": None,
        "progress": {a: "pending" for a in actions},
    }
