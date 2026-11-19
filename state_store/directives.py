from __future__ import annotations

import difflib
import re

_FENCE_RE = re.compile(r"```agent:([^\n]+)\n(.*?)```", re.DOTALL)

# Canonical agent keys for verbatim agent:<target> fences. These must match
# the keys the agents pass to AgentBase._get_scoped_context (see
# agents/{provisioning,benchmark,resource,review}/agent.py) and the set
# documented in docs/verbatim-directives.md. An unsupported target is
# preserved verbatim by the parser but delivered to no agent, so ingestion
# must reject it visibly instead of storing it silently.
VALID_AGENT_TARGETS = frozenset({"provision", "benchmark", "review", "resource"})


def parse_verbatim_directives(description: str) -> dict[str, str]:
    """Extract agent:* fenced blocks from a ticket description.

    Each block's targets (comma-separated) map to the verbatim content.
    Multiple blocks for the same target are joined with a blank line.
    """
    directives: dict[str, str] = {}
    for match in _FENCE_RE.finditer(description):
        targets = [t.strip() for t in match.group(1).split(",")]
        content = match.group(2).strip()
        for target in targets:
            if target in directives:
                directives[target] = directives[target] + "\n\n" + content
            else:
                directives[target] = content
    return directives


def validate_verbatim_targets(description: str) -> list[str]:
    """Return error messages for unsupported agent:<target> fence targets.

    The parser preserves whatever target string the author wrote; only the
    canonical keys in VALID_AGENT_TARGETS are ever read by an agent. A fence
    like ```` ```agent:provisioning ```` is therefore stored verbatim and
    silently dropped from every agent's context — an expensive failure mode
    when the block carries hard-won operational guidance. Each returned
    message names the offending target, the closest valid key when one
    exists (suffix slips such as ``provisioning`` → ``provision`` are the
    natural error), and the full valid set.
    """
    errors: list[str] = []
    for match in _FENCE_RE.finditer(description):
        for raw in match.group(1).split(","):
            target = raw.strip()
            if not target or target in VALID_AGENT_TARGETS:
                continue
            suggestion = difflib.get_close_matches(
                target, sorted(VALID_AGENT_TARGETS), n=1, cutoff=0.6
            )
            hint = f" (did you mean '{suggestion[0]}'?)" if suggestion else ""
            errors.append(
                f"unsupported agent directive target '{target}'{hint}; "
                f"valid targets are: {', '.join(sorted(VALID_AGENT_TARGETS))}"
            )
    return errors
