"""Metric direction classification for benchmark comparison.

Determines whether a higher or lower value is "better" for a given
metric name, using pattern matching with optional per-harness overrides.
"""

from __future__ import annotations

import re

_LOWER_PATTERNS = re.compile(
    r"latency|[_-]lat\b|ttft|tpot|elapsed|[-_]time|duration|jitter|loss",
    re.IGNORECASE,
)
_HIGHER_PATTERNS = re.compile(
    r"throughput|iops|bw|bandwidth|gbps|mbps|ops|rps|tps|tokens[_-]per",
    re.IGNORECASE,
)


def metric_direction(
    metric: str,
    harness: str | None = None,
    overrides: dict[str, str] | None = None,
) -> str | None:
    """Return "higher", "lower", or None for a metric name.

    Checks overrides first (from private skill config
    review.metric_directions), then falls back to pattern matching.
    """
    if overrides and metric in overrides:
        return overrides[metric]

    if _LOWER_PATTERNS.search(metric):
        return "lower"
    if _HIGHER_PATTERNS.search(metric):
        return "higher"
    return None
