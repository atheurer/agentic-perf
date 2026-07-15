"""Skill loader for the introspection agent.

Loads error classification patterns and detection thresholds from
skill files, with private-skills overrides for org-specific tuning.

Public skills:  skills/introspection/*.yaml    (shipped with repo)
Private skills: ~/.agentic-perf/private-skills/introspection.json

Private skills extend and override public defaults. For error
patterns, private lists are appended. For thresholds, private
values replace public defaults.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SKILLS_DIR = Path(__file__).resolve().parent.parent.parent / "skills"
_INTROSPECTION_SKILLS = _SKILLS_DIR / "introspection"


def _load_yaml_simple(path: Path) -> dict[str, Any]:
    """Load a simple YAML file without requiring PyYAML.

    Supports only the subset used by introspection skills:
    top-level keys with scalar or list-of-string values.
    Comments (#) and blank lines are skipped.
    """
    if not path.exists():
        return {}
    result: dict[str, Any] = {}
    current_key = ""
    current_list: list[str] | None = None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("- "):
                # List item under current key.
                val = stripped[2:].strip().strip("\"'")
                if current_list is not None:
                    current_list.append(val)
                continue
            if ":" in stripped:
                # Flush previous list.
                if current_key and current_list is not None:
                    result[current_key] = current_list
                parts = stripped.split(":", 1)
                key = parts[0].strip()
                val_str = parts[1].strip() if len(parts) > 1 else ""
                if val_str:
                    # Scalar value.
                    val_str = val_str.strip("\"'")
                    # Try numeric.
                    try:
                        if "." in val_str:
                            result[key] = float(val_str)
                        else:
                            result[key] = int(val_str)
                    except ValueError:
                        result[key] = val_str
                    current_key = ""
                    current_list = None
                else:
                    # Start of a list.
                    current_key = key
                    current_list = []
        # Flush final list.
        if current_key and current_list is not None:
            result[current_key] = current_list
    except Exception:
        logger.warning(f"Failed to load skill file {path}", exc_info=True)
    return result


def _load_private_overrides() -> dict[str, Any]:
    """Load private-skills overrides for introspection."""
    from paths import PRIVATE_SKILLS_DIR

    path = PRIVATE_SKILLS_DIR / "introspection.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning(
            f"Failed to load private introspection skills from {path}",
            exc_info=True,
        )
        return {}


def load_error_patterns() -> dict[str, list[re.Pattern[str]]]:
    """Load error classification patterns from skills.

    Returns a dict of error_class -> list of compiled regexes.
    Classes: 'infrastructure', 'transient'. Anything that
    doesn't match either is classified as 'logic'.
    """
    raw = _load_yaml_simple(
        _INTROSPECTION_SKILLS / "error-patterns.yaml",
    )

    # Merge private overrides.
    private = _load_private_overrides()
    private_patterns = private.get("error_patterns", {})
    for cls, patterns in private_patterns.items():
        if isinstance(patterns, list):
            existing = raw.get(cls, [])
            if isinstance(existing, list):
                existing.extend(patterns)
                raw[cls] = existing
            else:
                raw[cls] = patterns

    # Compile patterns.
    compiled: dict[str, list[re.Pattern[str]]] = {}
    for cls in ("infrastructure", "transient"):
        patterns = raw.get(cls, [])
        if isinstance(patterns, list):
            compiled[cls] = [
                re.compile(p, re.IGNORECASE) for p in patterns if isinstance(p, str)
            ]
        else:
            compiled[cls] = []

    return compiled


def load_thresholds() -> dict[str, Any]:
    """Load detection thresholds from skills.

    Returns a dict of threshold_name -> value. Private overrides
    replace public defaults for matching keys.
    """
    defaults = _load_yaml_simple(
        _INTROSPECTION_SKILLS / "detection-thresholds.yaml",
    )

    # Merge private overrides.
    private = _load_private_overrides()
    private_thresholds = private.get("thresholds", {})
    defaults.update(private_thresholds)

    return defaults
