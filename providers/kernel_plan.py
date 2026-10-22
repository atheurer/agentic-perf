"""Deterministic kernel-comparison plan builder (epic #995 wiring).

Turns the ``kernel_comparison`` directive into the canonical A/B
execution plan by transforming the triage-generated plan in code — the
LLM decides *that* a comparison is wanted and supplies the releases; the
builder owns the step shape, ordering, and labels. This mirrors the
project principle that plan topology is an invariant, not a suggestion.

Directive shape (after directive normalization):

    kernel_comparison:
      baseline:  <release A, exact uname -r string>   # required
      candidate: <release B, exact uname -r string>   # required
      order:     AB | ABBA | BAAB                     # optional, default AB
      package:   <rpm package name>                   # optional, default kernel
      repo_url:  <http(s) URL serving the packages>   # optional

Every kernel step boots ALL benchmark hosts (kernel.hosts stays unset);
partial coverage is not a supported configuration in v1.
"""

from __future__ import annotations

import re
from typing import Any

from providers.rhel_kernel import (
    _MAX_RELEASE_LEN,
    _PACKAGE_NAME_RE,
    KERNEL_RELEASE_RE,
)

# A repo URL is validated here for plan-level rejection and again (with
# the same rule) by KernelSpec at tool time. Fail at triage, not mid-run.
_REPO_URL_RE = re.compile(r"^https?://[^'\s\\]+$")

_VALID_ORDERS = ("AB", "ABBA", "BAAB")
_LETTER_ROLES = {"A": "baseline", "B": "candidate"}

MAX_REPO_URL_LEN = 512


def validate_kernel_comparison(directive: Any) -> tuple[dict[str, Any] | None, str]:
    """Validate and normalize a kernel_comparison directive.

    Returns ``(normalized, "")`` or ``(None, reason)``. The reason is
    written verbatim into the triage pause comment.
    """
    if not isinstance(directive, dict):
        return None, "kernel_comparison directive must be a mapping"
    baseline = str(directive.get("baseline") or "").strip()
    candidate = str(directive.get("candidate") or "").strip()
    if not baseline or not candidate:
        return (
            None,
            "kernel_comparison requires both 'baseline' and 'candidate' releases",
        )
    for name, release in (("baseline", baseline), ("candidate", candidate)):
        if len(release) > _MAX_RELEASE_LEN or not KERNEL_RELEASE_RE.match(release):
            return None, (
                f"kernel_comparison.{name} release {release!r} is not an exact "
                "uname -r style release string (VERSION-RELEASE.ARCH)"
            )
    order = str(directive.get("order") or "AB").upper()
    if order not in _VALID_ORDERS:
        return None, (
            f"kernel_comparison.order {order!r} is not one of "
            f"{', '.join(_VALID_ORDERS)}"
        )
    package = str(directive.get("package") or "kernel")
    if not _PACKAGE_NAME_RE.match(package):
        return None, f"kernel_comparison.package {package!r} is not a package name"
    repo_url = str(directive.get("repo_url") or "").strip()
    if repo_url:
        if len(repo_url) > MAX_REPO_URL_LEN or not _REPO_URL_RE.match(repo_url):
            return None, (
                "kernel_comparison.repo_url must be a plain http(s) URL "
                "with no whitespace or quotes"
            )
    return {
        "baseline": baseline,
        "candidate": candidate,
        "order": order,
        "package": package,
        "repo_url": repo_url,
    }, ""


def _kernel_step(step_id: int, release: str, params: dict[str, Any]) -> dict[str, Any]:
    kernel: dict[str, Any] = {
        "release": release,
        "package": params["package"],
        "reboot_strategy": "serial",
        "hosts": None,
    }
    if params["repo_url"]:
        kernel["repo_url"] = params["repo_url"]
    return {
        "id": step_id,
        "agent_type": "provision",
        "status": "pending",
        "params": {"label": "kernel", "kernel": kernel},
        "results": {},
    }


def _benchmark_copy(
    step_id: int, template: dict[str, Any], role: str
) -> dict[str, Any]:
    copy = dict(template)
    copy["id"] = step_id
    copy["status"] = "pending"
    copy["results"] = {}
    params = dict(copy.get("params") or {})
    base_label = str(params.get("label") or "benchmark")
    params["label"] = f"{base_label}-kernel-{role}"
    params["kernel_role"] = role
    # One workload group across every benchmark copy: the group is the
    # marker the execution path uses to enforce the epic's immutability
    # requirement — later steps in the group must present an identical
    # runfile fingerprint to the first one's recorded result.
    params["workload_group"] = "kernel-ab"
    copy["params"] = params
    return copy


def build_kernel_comparison_plan(
    plan: dict[str, Any], directive: dict[str, Any]
) -> tuple[dict[str, Any] | None, str]:
    """Transform a plan into the canonical kernel-comparison shape.

    Returns ``(new_plan, "")`` or ``(None, reason)``. The plan must not
    have progressed past the first benchmark step; resource and harness
    provision steps are preserved as-is, every original benchmark step is
    replaced by one kernel step + one benchmark step per order letter,
    and any steps after the last benchmark (review, teardown) are kept.
    """
    normalized, err = validate_kernel_comparison(directive)
    if normalized is None:
        return None, err

    steps = plan.get("steps") or []
    if not steps:
        return None, "execution plan has no steps to transform"
    bench_indexes = [
        i for i, s in enumerate(steps) if s.get("agent_type") == "benchmark"
    ]
    if not bench_indexes:
        return None, "execution plan has no benchmark step to build the comparison from"
    first_bench, last_bench = bench_indexes[0], bench_indexes[-1]
    current = plan.get("current_step") or 0
    if current >= first_bench:
        return None, (
            "execution plan already progressed past the first benchmark step; "
            "the kernel_comparison directive must be present at submission time"
        )
    template = steps[first_bench]

    head = [dict(s) for s in steps[:first_bench]]
    tail = [dict(s) for s in steps[last_bench + 1 :]]

    new_steps: list[dict[str, Any]] = list(head)
    for letter in normalized["order"]:
        release = normalized["baseline" if letter == "A" else "candidate"]
        kernel_step = _kernel_step(len(new_steps), release, normalized)
        kernel_step["params"]["label"] = f"kernel-{letter}"
        new_steps.append(kernel_step)
        new_steps.append(
            _benchmark_copy(len(new_steps), template, _LETTER_ROLES[letter])
        )
    new_steps.extend(tail)

    for i, s in enumerate(new_steps):
        s["id"] = i

    return {
        "current_step": plan.get("current_step", 0),
        "run_ids": list(plan.get("run_ids") or []),
        "steps": new_steps,
    }, ""


def kernel_steps(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Return all provision steps that have a kernel param."""
    return [
        step for step in plan.get("steps", []) if step.get("params", {}).get("kernel")
    ]


def preceding_kernel_step(
    plan: dict[str, Any],
    benchmark_step_id: int,
) -> dict[str, Any] | None:
    """Find the most recent kernel provision step before a benchmark step.

    The handoff gate uses this to require a verified transition before a
    benchmark may run (orchestrator/handoff.py::
    _check_executing_benchmark).
    """
    steps = plan.get("steps", [])
    result = None
    for step in steps:
        if step["id"] >= benchmark_step_id:
            break
        if step.get("params", {}).get("kernel"):
            result = step
    return result
