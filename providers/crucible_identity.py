"""Shared read-only Crucible identity probes.

Crucible's post-processing requires ``/root/.crucible/identity`` to carry
non-empty ``CRUCIBLE_NAME`` and ``CRUCIBLE_EMAIL`` values (the run schema
validates ``email`` against ``^.+$``). A crucible install does not create the
file, so without a preflight a benchmark completes all of its samples and then
discards post-processing with a schema error that never names the identity
(observed on PERF-AFD1570F and PERF-8C8B42B6: two full 27-test runs lost).

The probe is read-only shell over SSH and shared by the provisioning
verification path and the benchmark execution preflight.
"""

from __future__ import annotations

import shlex


def crucible_identity_probe_command(
    identity_path: str = "/root/.crucible/identity",
) -> str:
    """Build a read-only shell probe for a non-empty Crucible email value.

    Prints ``identity=1`` when the file is readable and carries a non-empty,
    non-comment ``CRUCIBLE_EMAIL`` value, and ``identity=0`` otherwise. Handles
    optional ``export`` prefixes, inline comments, and surrounding quotes.
    """
    quoted_path = shlex.quote(identity_path)
    return (
        f"if test -r {quoted_path} && "
        "awk -F= '"
        "/^[[:space:]]*#/ { next } "
        "$1 ~ /^[[:space:]]*(export[[:space:]]+)?CRUCIBLE_EMAIL[[:space:]]*$/ { "
        "value = $2; "
        'sub(/[[:space:]]+#.*/, "", value); '
        'gsub(/^[[:space:]]+|[[:space:]]+$/, "", value); '
        'if ((substr(value, 1, 1) == "\\"" && '
        'substr(value, length(value), 1) == "\\"") || '
        '(substr(value, 1, 1) == sprintf("%c", 39) && '
        'substr(value, length(value), 1) == sprintf("%c", 39))) '
        "value = substr(value, 2, length(value) - 2); "
        'gsub(/^[[:space:]]+|[[:space:]]+$/, "", value); '
        'found = (value != "" && value !~ /^#/) '
        f"}} END {{ exit !found }}' {quoted_path}; "
        "then printf 'identity=1'; else printf 'identity=0'; fi"
    )
