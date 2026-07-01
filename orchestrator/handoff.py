"""Handoff validation between agent stages.

Before dispatching the next agent, the orchestrator checks that the
previous agent's results meet the next agent's preconditions. This
catches problems like insufficient hosts or missing installations
before burning LLM tokens on a doomed run.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def check_handoff(status: str, ticket: dict[str, Any]) -> tuple[bool, str]:
    """Validate that a ticket is ready for the given status.

    Returns (ok, reason). If ok is False, reason explains what's wrong.
    """
    checker = _CHECKS.get(status)
    if checker is None:
        return True, ""
    return checker(ticket)


def _resolve_unique_hosts(
    controller: str | None,
    targets: list[str],
    ip_mapping: dict[str, str],
) -> set[str]:
    """Resolve all IPs to a canonical form using ip_mapping, return unique hosts."""
    reverse_map: dict[str, str] = {}
    for pub, priv in ip_mapping.items():
        reverse_map[pub] = priv
        reverse_map[priv] = priv

    def canonical(ip: str) -> str:
        return reverse_map.get(ip, ip)

    hosts = set()
    if controller:
        hosts.add(canonical(controller))
    for t in targets:
        hosts.add(canonical(t))
    return hosts


def _check_awaiting_provision(ticket: dict[str, Any]) -> tuple[bool, str]:
    """Validate resource allocation before provisioning."""
    cf = ticket.get("custom_fields", {})
    min_hosts = cf.get("min_hosts", 1)
    roles = cf.get("required_roles", [])
    ips = cf.get("assigned_hardware_ips", {})
    controller = ips.get("controller")
    targets = ips.get("targets", [])

    # Jumpstarter devices don't have IPs until the provisioning
    # agent flashes and boots them. Skip this check for Jumpstarter.
    provider = cf.get("resource_provider")
    if not controller and not targets:
        if provider == "jumpstarter":
            return True, "ok (Jumpstarter — IP discovered during provisioning)"
        return False, "No hosts allocated — assigned_hardware_ips is empty"

    meta = cf.get("resource_provider_metadata", {})
    ip_mapping: dict[str, str] = {}
    if isinstance(meta, dict):
        ip_mapping = meta.get("ip_mapping", {})
        if not ip_mapping:
            for sub in meta.values():
                if isinstance(sub, dict) and "ip_mapping" in sub:
                    ip_mapping.update(sub["ip_mapping"])

    unique_hosts = _resolve_unique_hosts(controller, targets, ip_mapping)
    total_hosts = len(unique_hosts)

    if total_hosts < min_hosts:
        return (
            False,
            f"Insufficient hosts: {total_hosts} unique host(s) allocated "
            f"but min_hosts={min_hosts} required (roles: {roles}). "
            f"Controller={controller}, targets={targets}",
        )

    controller_canonical = None
    if controller:
        reverse = {v: v for v in ip_mapping.values()}
        reverse.update({k: v for k, v in ip_mapping.items()})
        controller_canonical = reverse.get(controller, controller)

    unique_targets = {(ip_mapping.get(t, t) if t in ip_mapping else t) for t in targets}
    if controller_canonical:
        unique_targets.discard(controller_canonical)

    if len(roles) > 1 and len(unique_targets) < 1:
        return (
            False,
            f"Need at least 1 target host separate from the controller "
            f"for roles {roles}, but all targets resolve to the same "
            f"host as the controller ({controller})",
        )

    return True, ""


def _check_executing_benchmark(ticket: dict[str, Any]) -> tuple[bool, str]:
    """Validate provisioning before benchmark execution."""
    cf = ticket.get("custom_fields", {})

    if not cf.get("provisioning_complete", False):
        hosts = cf.get("hosts_provisioned", [])
        harness = cf.get("harness_name", "unknown")
        return (
            False,
            f"Provisioning not marked complete "
            f"(harness={harness}, hosts_provisioned={hosts})",
        )

    return True, ""


def _check_awaiting_review(ticket: dict[str, Any]) -> tuple[bool, str]:
    """Validate benchmark execution before review."""
    cf = ticket.get("custom_fields", {})

    benchmark_status = cf.get("benchmark_status")
    run_id = cf.get("run_id")

    if not run_id and benchmark_status != "completed":
        return (
            False,
            f"No run_id and benchmark_status={benchmark_status!r} "
            f"— benchmark may not have completed",
        )

    return True, ""


def _check_evaluating_convergence(
    ticket: dict[str, Any],
) -> tuple[bool, str]:
    """Validate benchmark execution before convergence evaluation."""
    cf = ticket.get("custom_fields", {})
    benchmark_status = cf.get("benchmark_status")
    run_id = cf.get("run_id")

    if not run_id and benchmark_status != "completed":
        return (
            False,
            f"No run_id and benchmark_status={benchmark_status!r} "
            f"— benchmark may not have completed",
        )
    return True, ""


_CHECKS: dict[str, Any] = {
    "awaiting_provision": _check_awaiting_provision,
    "executing_benchmark": _check_executing_benchmark,
    "awaiting_review": _check_awaiting_review,
    "evaluating_convergence": _check_evaluating_convergence,
}
