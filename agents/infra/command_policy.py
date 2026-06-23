"""Per-agent command policy enforcement for the infra MCP server.

Defense-in-depth: catches LLM hallucinations that would produce obviously
destructive commands. Not a sandbox — shell constructs can bypass binary
checks. True sandboxing (containers/seccomp) is Phase 4.
"""
from __future__ import annotations

import json
import logging
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

POLICIES_DIR = Path(__file__).parent / "policies"

GLOBAL_BLOCKED_PATTERNS: list[re.Pattern] = [
    re.compile(r"rm\s+-[^\s]*r[^\s]*f[^\s]*\s+/\s*$"),
    re.compile(r"rm\s+-[^\s]*f[^\s]*r[^\s]*\s+/\s*$"),
    re.compile(r"mkfs\."),
    re.compile(r"dd\s+.*of\s*=\s*/dev/"),
    re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;"),
    re.compile(r">\s*/dev/sd[a-z]"),
    re.compile(r"\b(shutdown|reboot|halt|poweroff)\b"),
    re.compile(r"\bchmod\s+777\s+/"),
]


@dataclass
class CommandPolicy:
    agent_name: str
    allowed_binaries: set[str] = field(default_factory=set)
    blocked_patterns: list[re.Pattern] = field(default_factory=list)
    max_timeout: int = 7200


def load_policy(agent_name: str) -> CommandPolicy:
    normalized = agent_name.replace("-agent", "").replace("-", "_")
    policy_path = POLICIES_DIR / f"{normalized}.json"
    if not policy_path.exists():
        logger.warning("No policy file for %s, using empty allowlist", agent_name)
        return CommandPolicy(agent_name=agent_name)

    with open(policy_path) as f:
        data: dict[str, Any] = json.load(f)

    agent_blocked = [
        re.compile(p) for p in data.get("blocked_patterns", [])
    ]

    return CommandPolicy(
        agent_name=agent_name,
        allowed_binaries=set(data.get("allowed_binaries", [])),
        blocked_patterns=agent_blocked,
        max_timeout=data.get("max_timeout", 7200),
    )


def check_command(
    command: str, policy: CommandPolicy
) -> tuple[bool, str]:
    """Check a command against a policy.

    Returns (allowed, reason). If allowed is False, reason explains why.
    """
    command = command.strip()
    if not command:
        return False, "Empty command"

    for pattern in GLOBAL_BLOCKED_PATTERNS:
        if pattern.search(command):
            return False, f"Blocked by global safety pattern: {pattern.pattern}"

    for pattern in policy.blocked_patterns:
        if pattern.search(command):
            return False, f"Blocked by agent policy pattern: {pattern.pattern}"

    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    if not tokens:
        return False, "Empty command after parsing"

    binary = Path(tokens[0]).name
    # Handle common shell prefixes: env vars (FOO=bar cmd), cd && cmd, etc.
    for token in tokens:
        if "=" in token and not token.startswith("-"):
            continue
        binary = Path(token).name
        break

    if not policy.allowed_binaries:
        return False, f"No binaries allowed for agent {policy.agent_name}"

    if binary not in policy.allowed_binaries:
        return False, f"Binary {binary!r} not in allowlist for {policy.agent_name}"

    return True, "OK"
