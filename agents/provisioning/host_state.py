"""Host tuning state across reboots.

A reboot invalidates the *verification* of a host's tuning, not the
*declaration* of it.  This module keeps that distinction in one place so the
orchestrator and the provisioning tools cannot disagree about it:

* a reboot records an intent (why it happened, and whether the tuning contract
  is meant to survive it);
* a tuning verification records that a contract was observed to hold;
* :func:`tuning_gate` decides, per host, what the pipeline may do next.

Deliberately pure: no I/O, no SSH, no knowledge of any NIC.  The caller passes
the intent and verification records it already persists.
"""

from __future__ import annotations

from typing import Any, Iterable

INTENT_VERSION = 1

# Actions returned by tuning_gate().
OK = "ok"  # verified after the last reboot; pipeline may proceed
REAPPLY = "reapply"  # contract preserved: re-apply, then verify
HOLD = "hold"  # declaration is stale/unknown: stop and re-declare


def make_reboot_intent(
    hosts: Iterable[str],
    *,
    reason: str,
    preserve_tuning: str,
    by: str,
    at: str,
    contract_ref: str | None = None,
) -> dict[str, Any]:
    """Build the durable record a reboot leaves behind.

    ``preserve_tuning`` is ``"true"``, ``"false"`` or ``"auto"``.  ``auto``
    resolves to "false" when the reason indicates host state may change
    (kernel/driver/firmware/MTU work), because a stale declaration must never
    be re-applied silently.
    """
    resolved = resolve_preserve_tuning(reason, preserve_tuning)
    return {
        "version": INTENT_VERSION,
        "hosts": sorted(hosts),
        "reason": reason,
        "preserve_tuning": resolved,
        "requested_preserve_tuning": preserve_tuning,
        "by": by,
        "at": at,
        "contract_ref": contract_ref,
    }


# Only reasons that can move NIC/host state belong here.  "reboot" itself is
# deliberately absent: the word appears in benign reasons ("operator asked for
# a reboot") and the reboot is the event, not the cause.
_STATE_CHANGING_MARKERS = (
    "kernel",
    "driver",
    "firmware",
    "mtu",
    "iommu",
    "upgrade",
    "channel",
    "qdisc",
    "offload",
)


def resolve_preserve_tuning(reason: str, preserve_tuning: str) -> bool:
    """Resolve the tri-state intent to a boolean."""
    value = (preserve_tuning or "auto").strip().lower()
    if value in ("true", "yes", "1"):
        return True
    if value in ("false", "no", "0"):
        return False
    if value not in ("auto", ""):
        raise ValueError(
            f"preserve_tuning must be true/false/auto, got {preserve_tuning!r}"
        )
    text = (reason or "").lower()
    return not any(marker in text for marker in _STATE_CHANGING_MARKERS)


def tuning_gate(
    hosts: Iterable[str],
    intents: Iterable[dict[str, Any]],
    verifications: Iterable[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Decide what may happen to each host after the reboots recorded so far.

    ``intents`` and ``verifications`` are ordered oldest-first.  A host is ``ok``
    only when its most recent verification is newer than its most recent reboot
    intent.  Otherwise the intent decides: preserved contract -> ``reapply``;
    anything else -> ``hold``.  A reboot with no intent at all is ``hold`` (an
    out-of-band reboot must never pass silently).
    """
    last_intent: dict[str, dict[str, Any]] = {}
    for intent in intents:
        for host in intent.get("hosts", ()):  # type: ignore[union-attr]
            last_intent[host] = intent

    last_verify: dict[str, dict[str, Any]] = {}
    for record in verifications:
        host = record.get("host")
        if host:
            last_verify[host] = record

    result: dict[str, dict[str, Any]] = {}
    for host in sorted(hosts):
        intent = last_intent.get(host)
        verify = last_verify.get(host)
        if intent is None:
            result[host] = {
                "action": OK if verify else HOLD,
                "reason": "no reboot recorded"
                if verify
                else "no reboot and no verification",
                "last_verification": verify.get("at") if verify else None,
            }
            continue
        intent_at = str(intent.get("at") or "")
        verify_at = str(verify.get("at") or "") if verify else ""
        if verify and verify_at > intent_at:
            result[host] = {
                "action": OK,
                "reason": "verified after the last reboot",
                "last_verification": verify_at,
            }
            continue
        if intent.get("preserve_tuning") is True:
            result[host] = {
                "action": REAPPLY,
                "reason": f"contract preserved across reboot ({intent.get('reason') or 'no reason given'})",
                "last_verification": verify_at or None,
            }
        else:
            result[host] = {
                "action": HOLD,
                "reason": (
                    f"tuning must be re-declared after reboot ({intent.get('reason') or 'no reason given'}); "
                    f"requested preserve_tuning={intent.get('requested_preserve_tuning')}"
                ),
                "last_verification": verify_at or None,
            }
    return result
