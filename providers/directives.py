"""Directive key normalization and validation.

Normalizes user-provided directive keys to canonical forms
before any agent processes them.  Unrecognized keys are
flagged with fuzzy-match suggestions so users get immediate
feedback instead of silent failures.

The recognized key set is built from two sources:
1. Core keys: orchestrator and generic agent directives
2. Skill-contributed keys: each harness declares its own
   recognized directives and aliases via get_directive_schema()
   on its skill provider, or via register_directives() for
   standalone harnesses without a provider.

Design principle: "LLM decides intent; code enforces
invariants."  Users express intent through directives; the
system normalizes variable terminology to canonical forms
in code.
"""

from __future__ import annotations

import logging
import math
from difflib import get_close_matches
from typing import Any

logger = logging.getLogger(__name__)

# ── Core directive keys ───────────────────────────────────
# Keys recognized by the orchestrator and generic agents.
# Harness-specific keys are contributed dynamically by skill
# providers via get_directive_schema() or standalone harnesses
# via register_directives().

_CORE_DIRECTIVES: set[str] = {
    # Board / resource
    "board_selector",
    "resource_provider",
    "endpoint_type",
    "exclude_hosts",
    # Image
    "image_version",
    "image_name",
    "image_type",
    "image_server",
    "release",
    # Harness selection
    "harness",
    # Fleet
    "fleet",
    "convergence_strategy",
    "failure_policy",
    # Behavior
    "skip_teardown",
    "disable_hitl_timeout",
    "review_mode",
    "no_host_mounts",
    "host_cleanup",
    "system_config",
    "update_harness",
    "user_pre_run_approval",
    "on_existing_install",
    "firewall_policy",
    "test_interfaces",
    # This resource directive must be known to the orchestrator even when the
    # Jumpstarter provider is imported lazily in a separate resource process.
    "jumpstarter_serial",
}

_CORE_ALIASES: dict[str, str] = {
    "exclude_host": "exclude_hosts",
}

_CORE_DESCRIPTIONS: dict[str, str] = {
    "board_selector": "Jumpstarter board label selector — pass through verbatim from user input",
    "resource_provider": "Resource provider: 'jumpstarter', 'quads', 'aws', or 'user_provided'",
    "endpoint_type": "Host connection type (default: 'remotehosts')",
    "exclude_hosts": "Host names to exclude from allocation (list)",
    "image_version": "OS image version (e.g. 'AutoSD-10', 'RHIVOS-2')",
    "image_name": "Image variant name (e.g. 'qa', 'ps', 'fusa-minimal')",
    "image_type": "Image type: 'regular' or 'ostree'",
    "image_server": "Base URL of the image download server",
    "release": "Release path (e.g. 'monthly/autosd10-202608', 'nightly')",
    "harness": "Benchmark harness to use (e.g. 'boot-time', 'crucible')",
    "fleet": "Enable fleet investigation mode (bool)",
    "convergence_strategy": "Fleet convergence strategy",
    "failure_policy": "Fleet failure handling: 'continue' or 'stop'",
    "skip_teardown": "Skip resource teardown after completion (bool)",
    "disable_hitl_timeout": "Disable HITL timeout for manual investigation (bool)",
    "review_mode": "Review agent mode",
    "no_host_mounts": "Disable host filesystem mounts in containers (bool)",
    "host_cleanup": "Host cleanup policy: 'required' or 'skip'",
    "system_config": "Post-provision system configuration operations (list)",
    "update_harness": "Update harness packages on target before benchmark (bool)",
    "jumpstarter_serial": "Enable Jumpstarter serial capture mode (bool)",
    "user_pre_run_approval": "Require user approval before executing the benchmark (bool, default true)",
    "on_existing_install": "Policy for pre-existing installations: 'reuse', 'skip', or 'reinstall'",
    "firewall_policy": "Firewall configuration policy for the benchmark environment",
    "test_interfaces": "Network interfaces to use for the benchmark test",
}

# ── Suffix / prefix normalization ─────────────────────────

_DURATION_SUFFIXES = (
    "_milliseconds",
    "_millisecond",
    "_seconds",
    "_second",
    "_sec",
    "_ms",
    "_s",
)
_DURATION_DIRECTIVES = {"power_off_delay"}
_COUNT_SUFFIXES = ("_count", "_num")
_STRIP_PREFIXES = ("jumpstarter_",)

# ── Dynamic registry ─────────────────────────────────────
# Populated at startup from skill providers and standalone
# harness registrations.

_contributed_directives: set[str] = set()
_contributed_aliases: dict[str, str] = {}


_contributed_descriptions: dict[str, str] = {}


def register_directives(
    recognized: set[str] | None = None,
    aliases: dict[str, str] | None = None,
    descriptions: dict[str, str] | None = None,
) -> None:
    """Register harness-specific directives and aliases.

    Called by standalone harnesses (boot-time, etc.) at module
    load, or by collect_from_providers() for provider-backed
    harnesses at orchestrator startup.

    Args:
        recognized: canonical directive key names.
        aliases: variant name → canonical key mappings.
        descriptions: canonical key → human-readable description
            for inclusion in agent prompts.
    """
    if recognized:
        _contributed_directives.update(recognized)
    if aliases:
        _contributed_aliases.update(aliases)
    if descriptions:
        _contributed_descriptions.update(descriptions)


def collect_from_providers(skill_provider: Any) -> None:
    """Collect directive schemas from all registered harness providers.

    Calls get_directive_schema() on each provider and merges
    the results into the dynamic registry.
    """
    if not hasattr(skill_provider, "list_harnesses"):
        return
    for harness_name in skill_provider.list_harnesses():
        provider = skill_provider.get_provider(harness_name)
        if provider and hasattr(provider, "get_directive_schema"):
            schema = provider.get_directive_schema()
            register_directives(
                recognized=schema.get("recognized"),
                aliases=schema.get("aliases"),
                descriptions=schema.get("descriptions"),
            )


def get_recognized_directives() -> set[str]:
    """Return the full set of recognized directive keys."""
    # Ensure standalone harness registrations have run.
    try:
        import providers.skills.boot_time  # noqa: F401
    except ImportError:
        pass
    return _CORE_DIRECTIVES | _contributed_directives


def _get_all_aliases() -> dict[str, str]:
    """Return the full alias map."""
    return {**_CORE_ALIASES, **_contributed_aliases}


def _strip_suffixes(key: str, recognized: set[str]) -> str | None:
    """Strip unit/count suffixes only when they match the key's meaning."""
    for suffix in _DURATION_SUFFIXES:
        if key.endswith(suffix):
            base = key[: -len(suffix)]
            if base in recognized and base in _DURATION_DIRECTIVES:
                return base
    for suffix in _COUNT_SUFFIXES:
        if key.endswith(suffix):
            base = key[: -len(suffix)]
            if base in recognized and _is_count_key(base):
                return base
    return None


def _strip_prefixes(key: str, recognized: set[str]) -> str | None:
    """Try stripping known prefixes to find a recognized base key."""
    for prefix in _STRIP_PREFIXES:
        if key.startswith(prefix):
            base = key[len(prefix) :]
            if base in recognized and (
                base in _DURATION_DIRECTIVES or _is_count_key(base)
            ):
                return base
    return None


def _is_count_key(key: str) -> bool:
    """Return whether a canonical key describes a count value."""
    return key in {"count", "sample", "samples"} or key.endswith(
        ("_count", "_num", "_samples")
    )


def _convert_milliseconds(value: Any) -> int | float | None:
    """Convert a numeric millisecond value to canonical seconds."""
    if isinstance(value, bool):
        return None
    try:
        milliseconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(milliseconds):
        return None

    seconds = milliseconds / 1000
    return int(seconds) if seconds.is_integer() else seconds


def _coerce_sample_count(value: Any) -> int | None:
    """Accept integer strings and integral numbers without truncation."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        try:
            count = int(value)
        except (ValueError, OverflowError):
            return None
        return count if count > 0 else None
    if (
        isinstance(value, float)
        and math.isfinite(value)
        and value.is_integer()
        and value > 0
    ):
        return int(value)
    return None


def normalize_key(key: str) -> tuple[str, str | None]:
    """Normalize a single directive key to its canonical form.

    Returns (canonical_key, reason) where reason is None if
    the key was already canonical, or a description of the
    normalization applied.
    """
    recognized = get_recognized_directives()
    aliases = _get_all_aliases()

    if key in recognized:
        return key, None

    # 1. Semantic equivalence (exact match)
    if key in aliases:
        canonical = aliases[key]
        return canonical, f"'{key}' is an alias for '{canonical}'"

    # 2. Suffix stripping
    base = _strip_suffixes(key, recognized)
    if base:
        return base, f"'{key}' normalized to '{base}' (suffix stripped)"

    # 3. Prefix stripping
    base = _strip_prefixes(key, recognized)
    if base:
        return base, f"'{key}' normalized to '{base}' (prefix stripped)"

    # 4. Combined: prefix + suffix
    for prefix in _STRIP_PREFIXES:
        if key.startswith(prefix):
            unprefixed = key[len(prefix) :]
            base = _strip_suffixes(unprefixed, recognized)
            if base:
                return base, (
                    f"'{key}' normalized to '{base}' (prefix + suffix stripped)"
                )

    # 5. Unrecognized
    return key, None


def normalize_directives(
    directives: dict[str, Any],
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Normalize all directive keys to canonical forms.

    Returns:
        (normalized_directives, normalizations_applied, unrecognized_keys)
    """
    recognized = get_recognized_directives()
    normalized: dict[str, Any] = {}
    applied: list[str] = []
    unrecognized: list[str] = []

    for key, value in directives.items():
        canonical, reason = normalize_key(key)
        conversion_note = None

        if reason and key.endswith(("_ms", "_millisecond", "_milliseconds")):
            converted = _convert_milliseconds(value)
            if converted is None:
                # Keep malformed or unsupported millisecond values visible to
                # the user instead of silently treating them as seconds.
                canonical = key
                reason = None
            else:
                value = converted
                conversion_note = (
                    f"'{key}' value converted from milliseconds to seconds"
                )

        if canonical in recognized:
            # Handle value semantics for special cases
            if canonical == "reboot_method" and key != "reboot_method":
                if isinstance(value, bool) and value:
                    inferred = _infer_reboot_method(key)
                    if inferred is None:
                        unrecognized.append(
                            f"Invalid value for '{key}' ({value!r}) — "
                            "expected a method string or a boolean key naming "
                            "cold, warm, or ssh"
                        )
                        continue
                    value = inferred
                elif isinstance(value, bool) and not value:
                    applied.append(f"'{key}'=false skipped (no reboot method to infer)")
                    continue
            if canonical == "sample_count":
                original_value = value
                converted = _coerce_sample_count(value)
                if converted is None:
                    unrecognized.append(
                        f"Invalid value for '{key}' ({original_value!r}) — "
                        "expected an integer greater than zero for 'sample_count'"
                    )
                    continue
                else:
                    value = converted
                    if type(original_value) is not int:
                        conversion_note = "'sample_count' value converted to an integer"

            if canonical in normalized:
                logger.warning(
                    "[directives] Duplicate key after normalization: "
                    "'%s' -> '%s' (already set, ignoring)",
                    key,
                    canonical,
                )
                continue

            normalized[canonical] = value
            if reason:
                applied.append(reason)
                logger.info("[directives] %s", reason)
            if conversion_note:
                applied.append(conversion_note)
                logger.info("[directives] %s", conversion_note)
        else:
            suggestions = get_close_matches(key, recognized, n=3, cutoff=0.5)
            if suggestions:
                hint = (
                    f"Unrecognized directive '{key}' "
                    f"— did you mean: {', '.join(suggestions)}?"
                )
            else:
                hint = f"Unrecognized directive '{key}' — no close match found"
            unrecognized.append(hint)
            normalized[key] = value

    return normalized, applied, unrecognized


def _infer_reboot_method(key: str) -> str | None:
    """Infer reboot method from boolean-style key names."""
    key_lower = key.lower()
    if "cold" in key_lower:
        return "cold"
    if "warm" in key_lower:
        return "warm"
    if "ssh" in key_lower:
        return "ssh"
    return None


def format_normalization_report(
    applied: list[str],
    unrecognized: list[str],
) -> str | None:
    """Format a human-readable report of normalization changes.

    Returns None if no changes were made and no warnings needed.
    """
    if not applied and not unrecognized:
        return None

    parts: list[str] = []
    if applied:
        parts.append("**Directive normalization:**")
        for note in applied:
            parts.append(f"- {note}")
    if unrecognized:
        parts.append("**Unrecognized directives:**")
        for hint in unrecognized:
            parts.append(f"- ⚠️ {hint}")
    return "\n".join(parts)


def format_directive_schema_for_prompt(
    keys: set[str],
) -> str:
    """Format the directive schema as a prompt-ready string.

    Args:
        keys: set of canonical directive key names to include.
            Use get_recognized_directives() for the full set.

    Only keys that have a registered description are included.
    Keys without descriptions are omitted to avoid confusing
    the LLM with unexplained names.

    Returns a string suitable for injection into an agent's
    system prompt, listing canonical keys with descriptions
    and instructing the LLM to map user intent to these keys.
    """
    if not keys:
        return ""

    # Merge core + contributed descriptions
    all_descriptions = dict(_CORE_DESCRIPTIONS)
    all_descriptions.update(_contributed_descriptions)

    lines = [
        "## Recognized Directives",
        "",
        "When working with ticket directives, use these EXACT",
        "canonical key names. If the user or ticket uses a different",
        "name for the same concept, map it to the canonical key.",
        "",
    ]
    for key in sorted(keys):
        desc = all_descriptions.get(key, "")
        if desc:
            lines.append(f"- **{key}**: {desc}")
        else:
            lines.append(f"- **{key}**")
    lines.append("")
    lines.append(
        "Use ONLY these canonical keys for directives. Every "
        "directive that affects execution has explicit code handling "
        "it \u2014 unrecognized keys are silently ignored by tools and "
        "harnesses. If the user's intent clearly matches a recognized "
        "directive "
        "but uses a different name, map it to the canonical "
        "key (e.g. 'iterations' → 'sample_count', 'os' → "
        "'image_version'). If the intent is ambiguous or does not "
        "match any recognized directive, do NOT guess — include the "
        "instruction in scoped_context for the downstream agent to "
        "interpret instead."
    )
    return "\n".join(lines)


# ── Schema description guidelines ────────────────────────
#
# When adding descriptions to _CORE_DESCRIPTIONS or harness
# descriptions dicts, follow these rules:
#
# 1. ONLY describe directives where the LLM needs to MAP
#    user intent to a canonical key name.  Examples:
#      "sample_count": "Number of reboot cycles (int)"
#      "reboot_method": "'warm' or 'cold'"
#    These help the LLM recognize that "iterations", "cycles",
#    or "num_reboots" all mean sample_count.
#
# 2. Do NOT describe directives where the VALUE must be an
#    exact match against external data (labels, selectors,
#    hostnames).  Examples of what to OMIT:
#      board_selector — must match actual Jumpstarter labels
#      exclude_hosts — must match real hostnames
#    Including these encourages the LLM to CONSTRUCT values
#    from description text instead of preserving user input
#    or deferring to the agent that owns the lookup.
#
# 3. Do NOT describe directives that are purely internal
#    (set by code, never by users or LLMs):
#      jumpstarter_serial, endpoint_type, etc.
#
# 4. Keep descriptions short and include the type and an
#    example value:
#      "power_off_delay": "Seconds between power off and on (int, e.g. 20)"
#    Not: "The number of seconds to wait after powering off
#    the board before sending the power on command via the
#    Jumpstarter SNMP PDU controller."
