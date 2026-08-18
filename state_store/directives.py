from __future__ import annotations

import re

_FENCE_RE = re.compile(r"```agent:([\w,\s]+)\n(.*?)```", re.DOTALL)


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
