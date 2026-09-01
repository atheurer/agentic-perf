from __future__ import annotations

from agents.triage.prompts import TRIAGE_SYSTEM_PROMPT


def _resource_section(prompt: str) -> str:
    """Extract the resource bullet block from the scoped_context section."""
    lines = prompt.splitlines()
    start = None
    end = None
    for i, line in enumerate(lines):
        if start is None and '"resource":' in line:
            start = i
        elif start is not None and (
            line.strip().startswith('"provision":')
            or line.strip().startswith('"benchmark":')
            or line.strip().startswith('"review":')
        ):
            end = i
            break
    if start is None:
        return ""
    if end is None:
        end = len(lines)
    return "\n".join(lines[start:end])


class TestTriagePromptVerbatimFQDN:
    """Prompt-contract tests: triage must instruct verbatim host preservation."""

    def test_resource_section_mentions_verbatim(self):
        section = _resource_section(TRIAGE_SYSTEM_PROMPT)
        assert "verbatim" in section.lower(), (
            "resource section must instruct verbatim host preservation"
        )

    def test_resource_section_mentions_fqdn(self):
        section = _resource_section(TRIAGE_SYSTEM_PROMPT)
        assert "FQDN" in section, "resource section must mention FQDNs"

    def test_resource_section_forbids_geographic_labels(self):
        section = _resource_section(TRIAGE_SYSTEM_PROMPT)
        lower = section.lower()
        assert "geographic" in lower or "shorthand" in lower, (
            "resource section must forbid geographic labels or shorthand"
        )

    def test_resource_section_explains_ssh_consequence(self):
        section = _resource_section(TRIAGE_SYSTEM_PROMPT)
        lower = section.lower()
        assert "ssh" in lower, (
            "resource section must explain that the resource agent SSHes "
            "to these strings"
        )

    def test_shared_section_preserves_identifiers(self):
        lines = TRIAGE_SYSTEM_PROMPT.splitlines()
        shared_block = []
        in_shared = False
        for line in lines:
            if '"shared":' in line:
                in_shared = True
            elif in_shared and (
                line.strip().startswith('"resource":')
                or line.strip().startswith('"provision":')
            ):
                break
            if in_shared:
                shared_block.append(line)
        shared_text = "\n".join(shared_block).lower()
        assert "character-for-character" in shared_text or "verbatim" in shared_text, (
            "shared section must instruct exact host identifier preservation"
        )
