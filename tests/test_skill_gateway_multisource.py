from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agents.review.prompts import REVIEW_SYSTEM_PROMPT
from agents.skill_gateway import SKILL_GATEWAY_TOOL_DESCRIPTION, skill_context_gateway
from providers.skills.gateway import OrganizationSkillResolver, SkillGatewayError


def _write_package(
    repository: Path,
    *,
    namespace: str,
    name: str,
    subject: str,
    instruction: str,
    shared_note: str = "Shared note from maintainers.\n",
    runtime_config: dict | None = None,
) -> None:
    package = repository / "skills" / namespace / name
    package.mkdir(parents=True)
    skill = (
        "---\n"
        f"name: {name}\n"
        f"description: Organization guidance for {subject}.\n"
        "metadata:\n"
        f"  subject: {subject}\n"
        "---\n"
        f"\n{instruction}\n"
    )
    (package / "SKILL.md").write_text(skill)
    (package / "notes.md").write_text(shared_note)
    manifest = {
        "schema_version": 1,
        "subject": subject,
        "documents": [
            {"path": "SKILL.md", "entrypoint": True},
            {"path": "notes.md", "entrypoint": False},
        ],
    }
    (package / "skill.json").write_text(json.dumps(manifest))
    if runtime_config is not None:
        config = repository / "service-config" / namespace / f"{name}.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps(runtime_config))


def _resolver(*sources: tuple[str, Path]) -> OrganizationSkillResolver:
    return OrganizationSkillResolver.from_instance_config(
        {
            "skill_gateway": {
                "organization": {
                    "sources": [
                        {"id": source_id, "kind": "path", "path": str(root)}
                        for source_id, root in sources
                    ]
                }
            }
        }
    )


def test_named_sources_discover_subject_union(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    _write_package(
        first,
        namespace="harness",
        name="crucible",
        subject="harness/crucible",
        instruction="Use the Crucible run contract.",
    )
    _write_package(
        second,
        namespace="harness",
        name="zathras",
        subject="harness/zathras",
        instruction="Use the Zathras run contract.",
    )

    resolver = _resolver(("crucible", first), ("zathras", second))

    assert resolver.configured_subjects() == ["harness/crucible", "harness/zathras"]
    result = resolver.bootstrap("harness/crucible")
    assert result["sources"][0]["id"] == "crucible", result
    assert resolver.bootstrap("harness/zathras")["sources"][0]["id"] == "zathras"

    reverse_order = _resolver(("zathras", second), ("crucible", first))
    assert [
        source["id"]
        for source in reverse_order.bootstrap("harness/crucible")["sources"]
    ] == [source["id"] for source in result["sources"]]
    shorthand = OrganizationSkillResolver.from_instance_config(
        {
            "skill_gateway": {
                "organization": {"source": {"kind": "path", "path": str(first)}}
            }
        }
    )
    assert shorthand.configured_subjects() == ["harness/crucible"]


def test_overlapping_subjects_keep_provenance_and_report_variants(
    tmp_path: Path,
) -> None:
    first, second = tmp_path / "team-a", tmp_path / "team-b"
    subject = "domain/networking"
    _write_package(
        first,
        namespace="domain",
        name="networking",
        subject=subject,
        instruction="Prefer the organization network profile A.",
        runtime_config={"default_profile": "A"},
    )
    _write_package(
        second,
        namespace="domain",
        name="networking",
        subject=subject,
        instruction="Prefer the organization network profile B.",
        runtime_config={"default_profile": "B"},
    )
    resolver = _resolver(("team-a", first), ("team-b", second))

    result = resolver.bootstrap(subject)
    skill_docs = [
        doc for doc in result["documents"] if doc["source_path"] == "SKILL.md"
    ]
    assert {doc["source_id"] for doc in skill_docs} == {"team-a", "team-b"}
    assert {item["path"] for item in result["overlaps"]} == {"SKILL.md", "notes.md"}
    assert (
        next(item for item in result["overlaps"] if item["path"] == "SKILL.md")[
            "same_content"
        ]
        is False
    )
    assert any(
        {doc["source_id"] for doc in duplicate["documents"]} == {"team-a", "team-b"}
        for duplicate in result["duplicates"]
    )
    assert result["runtime_config_conflict"] is True
    assert result["runtime_config_sources"] == ["team-a", "team-b"]

    contents = {
        doc["source_id"]: resolver.read(subject, doc["ref"])["content"]
        for doc in skill_docs
    }
    assert "profile A" in contents["team-a"]
    assert "profile B" in contents["team-b"]
    with pytest.raises(SkillGatewayError, match="runtime configurations conflict"):
        resolver.get_runtime_config(subject)


def test_gateway_surfaces_conflicts_and_agents_can_raise_hitl(tmp_path: Path) -> None:
    first, second = tmp_path / "team-a", tmp_path / "team-b"
    subject = "domain/networking"
    _write_package(
        first,
        namespace="domain",
        name="networking",
        subject=subject,
        instruction="Use path A.",
    )
    _write_package(
        second,
        namespace="domain",
        name="networking",
        subject=subject,
        instruction="Use path B.",
    )
    resolver = _resolver(("team-a", first), ("team-b", second))

    class Provider:
        organization_resolver = resolver

    response = json.loads(
        asyncio.run(
            skill_context_gateway(
                Provider(),
                ticket_id="TEST-MULTISOURCE",
                agent_name="benchmark-agent",
                phase="benchmark",
                subject=subject,
            )
        )
    )

    conflict = response["context_conflicts"]["potential_document_conflicts"]
    assert conflict
    assert {doc["source_id"] for doc in conflict[0]["documents"]} == {
        "team-a",
        "team-b",
    }
    assert "request_clarification" in SKILL_GATEWAY_TOOL_DESCRIPTION
    assert "authenticated user, organization, upstream, then the bundled" in (
        SKILL_GATEWAY_TOOL_DESCRIPTION
    )
    assert "mandatory organization policy" in SKILL_GATEWAY_TOOL_DESCRIPTION
    assert "mandatory organization policy" in SKILL_GATEWAY_TOOL_DESCRIPTION
    assert "call request_clarification before submitting" in REVIEW_SYSTEM_PROMPT
    assert "does not break ties between peer sources" in REVIEW_SYSTEM_PROMPT
