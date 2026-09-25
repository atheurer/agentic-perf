from __future__ import annotations

from agents.triage.prompts import TRIAGE_SYSTEM_PROMPT


def test_workflow_directive_uses_canonical_arcaflow_harness():
    from agents.triage.agent import _canonicalize_workflow_harness

    directives = {"workflow_source": "https://example.test/workflow.yaml"}
    assert _canonicalize_workflow_harness(directives)["harness"] == "arcaflow-plugins"


def test_workflow_harness_canonicalization_precedes_default_resolution():
    from types import SimpleNamespace

    from agents.base import AgentBase
    from agents.triage.agent import _canonicalize_workflow_harness

    directives = {"workflow_source": "https://example.test/workflow.yaml"}
    directives = _canonicalize_workflow_harness(directives)
    assert (
        AgentBase._effective_harness(
            directives, SimpleNamespace(default_harness="crucible")
        )
        == "arcaflow-plugins"
    )


def test_direct_harness_filter_preserves_mixed_host_roles_and_specs():
    from agents.triage.agent import _filter_direct_required_hosts

    required_hosts = [
        {
            "roles": ["controller", "client"],
            "min_memory_gb": 128,
            "nic_speed": 100,
            "host": "sut.example.test",
        },
        {"roles": ["controller"], "host": "controller.example.test"},
        {"roles": ["server"], "min_cores": 32},
    ]

    assert _filter_direct_required_hosts(required_hosts) == [
        {
            "roles": ["client"],
            "min_memory_gb": 128,
            "nic_speed": 100,
            "host": "sut.example.test",
        },
        {"roles": ["server"], "min_cores": 32},
    ]


def test_direct_harness_filter_falls_back_when_all_hosts_are_controllers():
    from agents.triage.agent import _filter_direct_required_hosts

    assert _filter_direct_required_hosts([{"roles": ["controller"]}]) == [
        {"roles": ["client"]}
    ]


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


def _provision_section(prompt: str) -> str:
    """Extract the provision bullet from the scoped_context section."""
    lines = prompt.splitlines()
    start = next(i for i, line in enumerate(lines) if '"provision":' in line)
    end = next(
        i
        for i, line in enumerate(lines[start + 1 :], start + 1)
        if line.strip().startswith('- "benchmark":')
    )
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


class TestTriagePackageRequirementBoundary:
    """Triage must not create provisioning package requests from tool-params."""

    def test_provision_section_requires_an_explicit_user_or_contract_source(self):
        section = _provision_section(TRIAGE_SYSTEM_PROMPT).lower()
        assert "only" in section
        assert "explicitly requests" in section
        assert "platform" in section and "contract" in section

    def test_provision_section_separates_tool_params_from_host_packages(self):
        section = _provision_section(TRIAGE_SYSTEM_PROMPT).lower()
        assert "tool-params" in section
        assert "no implied relationship" in section
        assert '"benchmark"' in section

    def test_provision_section_preserves_explicit_kernel_package_request(self):
        section = _provision_section(TRIAGE_SYSTEM_PROMPT).lower()
        assert "install `kernel`" in section
        assert '`{"tool": "kernel"}`' in section
