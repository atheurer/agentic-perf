"""Deterministic host-identity extraction and transcription recovery.

Triage is the only stage that copies user-provided host identities out of
ticket prose into ``required_hosts[].host``. A single dropped DNS label in
that transcription used to travel through every later handoff and wedge the
ticket permanently, because the stored string is re-validated verbatim on
every step transition (#1381, PERF-8807325A).

The helpers here are deliberately conservative:

- Extraction recognizes FQDN-shaped tokens and IP literals. It never
  invents hostnames.
- Recovery accepts only an exact case-insensitive match, or a unique
  candidate that differs from the submitted string by EXACTLY ONE omitted
  interior DNS label (the observed failure mode — a dropped middle label
  like ``rdu3.``). First and last labels must be identical. There is no
  edit-distance or multi-label matching: a wrong host must never be
  silently bound to a submitted identity.
"""

from __future__ import annotations

import ipaddress
import re

# FQDN-shaped tokens: DNS labels separated by dots, at least two labels,
# each label 1-63 chars of letters/digits/hyphen not starting or ending
# with a hyphen. Must not be preceded/followed by more name characters.
_HOST_TOKEN = re.compile(
    r"(?<![A-Za-z0-9.-])"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?![A-Za-z0-9-])"
)

# IPv4 and IPv6 literals, surrounded by non-address characters.
_IPV4_TOKEN = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
_IPV6_TOKEN = re.compile(
    r"(?<![A-Fa-f0-9:])[A-Fa-f0-9:]*:[A-Fa-f0-9:]+(?![A-Fa-f0-9:])"
)


def _is_ip_literal(value: str) -> bool:
    """True when the value parses as an IPv4 or IPv6 address."""
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def extract_host_identities(text: str) -> set[str]:
    """Extract host-shaped tokens (FQDNs and IPs) from prose.

    The result is a case-preserved set; callers compare case-insensitively
    through ``resolve_submitted_host``. IP literals are included verbatim —
    they are unambiguous and immune to the label-dropping failure mode.
    """
    if not isinstance(text, str) or not text:
        return set()
    found: set[str] = set()
    for match in _HOST_TOKEN.finditer(text):
        token = match.group(0).rstrip(".")
        # Require at least one letter: pure digit/hyphen dot-separated
        # tokens are version strings ("5.14.0-70.13.1"), not hostnames.
        if token and not _is_ip_literal(token) and any(c.isalpha() for c in token):
            found.add(token)
    for match in _IPV4_TOKEN.finditer(text):
        found.add(match.group(0))
    for match in _IPV6_TOKEN.finditer(text):
        token = match.group(0)
        if _is_ip_literal(token):
            found.add(token)
    return found


def _labels(host: str) -> list[str]:
    return host.split(".")


def _single_interior_label_deletion(submitted: str, candidate: str) -> bool:
    """True when ``submitted`` equals ``candidate`` minus exactly one
    interior label (first and last labels identical)."""
    sub = _labels(submitted)
    cand = _labels(candidate)
    if len(cand) != len(sub) + 1:
        return False
    if not sub or sub[0] != cand[0] or sub[-1] != cand[-1]:
        return False
    # cand must equal sub with one extra label somewhere strictly inside.
    for skip in range(1, len(cand) - 1):
        if cand[:skip] + cand[skip + 1 :] == sub:
            return True
    return False


def resolve_submitted_host(
    submitted: str, candidates: set[str] | list[str] | tuple[str, ...]
) -> tuple[str | None, str]:
    """Resolve a submitted host identity against authoritative candidates.

    Returns ``(canonical, reason)``. ``canonical`` is the authoritative
    string when the submission matches exactly (case-insensitive) or is
    unambiguously recoverable by restoring exactly one omitted interior
    DNS label; otherwise ``None`` and ``reason`` explains the outcome for
    an operator-facing message.
    """
    submitted = submitted.strip()
    if not submitted:
        return None, "empty host identity"
    if _is_ip_literal(submitted):
        # IP literals are exact-match only; there is no label structure to
        # recover, and never a reason to rewrite one.
        for candidate in candidates:
            if str(candidate).strip() == submitted:
                return str(candidate).strip(), "exact"
        return None, "IP address not found verbatim"

    lowered = submitted.lower()
    matches = [
        str(candidate).strip()
        for candidate in candidates
        if str(candidate).strip().lower() == lowered
    ]
    if len(matches) == 1:
        return matches[0], "exact"
    if len(matches) > 1:
        return None, "multiple case-insensitive matches"

    deletions = [
        str(candidate).strip()
        for candidate in candidates
        if _single_interior_label_deletion(lowered, str(candidate).strip().lower())
    ]
    if len(deletions) == 1:
        return deletions[0], "restored one omitted DNS label"
    if len(deletions) > 1:
        return None, "multiple candidates differ by one omitted label"
    return None, "no exact or single-label-deletion match"
