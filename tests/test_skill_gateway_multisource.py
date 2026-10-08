from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agents.review.prompts import REVIEW_SYSTEM_PROMPT
from agents.skill_gateway import SKILL_GATEWAY_TOOL_DESCRIPTION, skill_context_gateway
from providers.skills.gateway import OrganizationSkillResolver, SkillGatewayError
from providers.skills.multi import MultiHarnessSkillProvider
from providers.skills.private import PrivateSkillProvider
from tests.conftest import MockSkillProvider


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


def _resolver(
    *sources: tuple[str, Path], snapshot_root: Path | None = None
) -> OrganizationSkillResolver:
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
        },
        snapshot_root=snapshot_root,
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


def test_docs_only_subject_preserves_defaults_without_selecting_legacy_config(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "org"
    _write_package(
        repository,
        namespace="harness",
        name="crucible",
        subject="harness/crucible",
        instruction="Use the organization Crucible workflow.",
    )
    private_root = tmp_path / "private-skills"
    private_root.mkdir()
    (private_root / "crucible.json").write_text(
        json.dumps({"execution": {"endpoint_type": "kube"}})
    )
    resolver = _resolver(("org", repository))
    private = PrivateSkillProvider(private_root, resolver=resolver)

    class DefaultsProvider(MockSkillProvider):
        async def get_default_config(self) -> dict:
            return {"execution": {"controller_required": True}}

    provider = MultiHarnessSkillProvider(
        {"crucible": DefaultsProvider()},
        private=private,
    )

    assert resolver.has_subject("harness/crucible")
    assert not resolver.uses_organization_config("harness/crucible")
    assert resolver.get_runtime_config("harness/crucible") is None
    assert asyncio.run(private.get_all_private_config("crucible")) == {}
    assert asyncio.run(provider.get_all_private_config("crucible")) == {
        "execution": {"controller_required": True}
    }


def test_explicit_empty_canonical_config_is_not_treated_as_docs_only(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "org"
    _write_package(
        repository,
        namespace="harness",
        name="crucible",
        subject="harness/crucible",
        instruction="Use the organization Crucible workflow.",
        runtime_config={},
    )
    private_root = tmp_path / "private-skills"
    private_root.mkdir()
    (private_root / "crucible.json").write_text(
        json.dumps({"execution": {"endpoint_type": "kube"}})
    )
    resolver = _resolver(("org", repository))
    private = PrivateSkillProvider(private_root, resolver=resolver)
    provider = MultiHarnessSkillProvider({"crucible": MockSkillProvider()}, private)

    assert resolver.has_subject("harness/crucible")
    assert resolver.uses_organization_config("harness/crucible")
    assert resolver.get_runtime_config("harness/crucible") == {}
    assert asyncio.run(provider.get_all_private_config("crucible")) == {}


def test_explicit_legacy_binding_keeps_legacy_config_with_docs(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "org"
    _write_package(
        repository,
        namespace="harness",
        name="crucible",
        subject="harness/crucible",
        instruction="Use the organization Crucible workflow.",
    )
    private_root = tmp_path / "private-skills"
    private_root.mkdir()
    (private_root / "crucible.json").write_text(
        json.dumps({"execution": {"endpoint_type": "remotehosts"}})
    )
    resolver = OrganizationSkillResolver.from_instance_config(
        {
            "skill_gateway": {
                "organization": {
                    "source": {"kind": "path", "path": str(repository)},
                    "subjects": {"harness/crucible": {"legacy_config": True}},
                }
            }
        }
    )
    private = PrivateSkillProvider(private_root, resolver=resolver)

    assert resolver.has_subject("harness/crucible")
    assert resolver.uses_legacy_config("harness/crucible")
    assert not resolver.uses_organization_config("harness/crucible")
    assert asyncio.run(private.get_all_private_config("crucible")) == {
        "execution": {"endpoint_type": "remotehosts"}
    }


def test_unconfigured_subject_keeps_existing_legacy_config_behavior(
    tmp_path: Path,
) -> None:
    private_root = tmp_path / "private-skills"
    private_root.mkdir()
    (private_root / "crucible.json").write_text(
        json.dumps({"execution": {"endpoint_type": "remotehosts"}})
    )
    resolver = OrganizationSkillResolver.from_instance_config({})
    private = PrivateSkillProvider(private_root, resolver=resolver)

    assert not private.organization_resolver.has_subject("harness/crucible")
    assert asyncio.run(private.get_all_private_config("crucible")) == {
        "execution": {"endpoint_type": "remotehosts"}
    }


def test_unavailable_canonical_config_does_not_fall_back_to_legacy(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "org"
    _write_package(
        repository,
        namespace="harness",
        name="crucible",
        subject="harness/crucible",
        instruction="Use the organization Crucible workflow.",
        runtime_config={},
    )
    resolver = _resolver(("org", repository))
    (repository / "service-config" / "harness" / "crucible.json").unlink()
    private_root = tmp_path / "private-skills"
    private_root.mkdir()
    (private_root / "crucible.json").write_text(
        json.dumps({"execution": {"endpoint_type": "remotehosts"}})
    )
    private = PrivateSkillProvider(private_root, resolver=resolver)

    assert resolver.uses_organization_config("harness/crucible")
    with pytest.raises(SkillGatewayError, match="organization skill is missing"):
        asyncio.run(private.get_all_private_config("crucible"))


def test_ticket_snapshot_read_handles_reverse_source_configuration_order(
    tmp_path: Path,
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    subject = "harness/crucible"
    _write_package(
        first,
        namespace="harness",
        name="crucible",
        subject=subject,
        instruction="Crucible guidance from team A.",
    )
    _write_package(
        second,
        namespace="harness",
        name="crucible",
        subject=subject,
        instruction="Crucible guidance from team B.",
    )
    resolver = _resolver(
        ("team-b", second),
        ("team-a", first),
        snapshot_root=tmp_path / "snapshots",
    ).for_attempt("PERF-MULTISOURCE-SNAPSHOT", "attempt-1", "benchmark")
    resolver.audit_emit = lambda _event: None

    bootstrap = resolver.bootstrap(subject)
    assert bootstrap["status"] == "available"
    assert [source["id"] for source in bootstrap["sources"]] == [
        "team-a",
        "team-b",
    ]

    document = resolver.read(subject, bootstrap["entrypoints"][0])
    assert "Crucible guidance from team A." in document["content"]


def test_path_source_keeps_document_content_across_attempt_restart(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "organization-path"
    subject = "harness/crucible"
    _write_package(
        repository,
        namespace="harness",
        name="crucible",
        subject=subject,
        instruction="Path-backed guidance before restart.",
    )
    snapshot_root = tmp_path / "snapshots"
    first = _resolver(("org", repository), snapshot_root=snapshot_root)
    first.audit_emit = lambda _event: None
    first = first.for_attempt("PERF-PATH-EPOCH", "attempt-1", "benchmark")
    bootstrap = first.bootstrap(subject)
    notes_ref = next(
        item["ref"]
        for item in bootstrap["documents"]
        if item["source_path"] == "notes.md"
    )
    (repository / "skills" / "harness" / "crucible" / "notes.md").write_text(
        "Changed after the initial attempt read.\n"
    )

    resumed = _resolver(("org", repository), snapshot_root=snapshot_root)
    resumed.audit_emit = lambda _event: None
    resumed = resumed.for_attempt("PERF-PATH-EPOCH", "attempt-1", "review")
    document = resumed.read(subject, notes_ref)
    assert document["content"] == "Shared note from maintainers.\n"


def test_git_repository_revision_is_shared_across_subjects_and_resume(
    tmp_path: Path, monkeypatch
) -> None:
    from providers.skills.git_source import PreparedGitSource, parse_git_source

    repository = tmp_path / "organization"
    repository.mkdir()
    _write_package(
        repository,
        namespace="harness",
        name="crucible",
        subject="harness/crucible",
        instruction="Crucible source revision guidance.",
    )
    _write_package(
        repository,
        namespace="harness",
        name="zathras",
        subject="harness/zathras",
        instruction="Zathras source revision guidance.",
    )
    revision = "a" * 40
    prepared_revisions = []

    async def prepare(source, *, pinned_commit=None, **_kwargs):
        identity = parse_git_source(source).identity
        prepared_revisions.append(pinned_commit)
        return PreparedGitSource(repository, pinned_commit or revision, identity)

    monkeypatch.setattr("providers.skills.git_source.prepare_git_source", prepare)
    config = {
        "skill_gateway": {
            "organization": {
                "sources": [
                    {
                        "id": "org",
                        "kind": "git",
                        "url": "https://git.example.org/team/context.git",
                        "ref": "main",
                    }
                ]
            }
        }
    }
    common = {
        "raw_config": config,
        "snapshot_root": tmp_path / "pins",
        "audit_emit": lambda _event: None,
        "ticket_id": "PERF-GIT-EPOCH",
        "attempt_id": "attempt-1",
        "phase": "benchmark",
    }

    resolver = asyncio.run(OrganizationSkillResolver.from_instance_config_async(**common))
    assert prepared_revisions == [None]
    first = resolver.bootstrap("harness/crucible")
    second = resolver.bootstrap("harness/zathras")
    assert first["sources"][0]["revision"] == revision
    assert second["sources"][0]["revision"] == revision

    resumed = asyncio.run(OrganizationSkillResolver.from_instance_config_async(**common))
    assert prepared_revisions == [None, revision]
    assert resumed.bootstrap("harness/crucible")["sources"][0]["revision"] == revision
    assert resumed.bootstrap("harness/zathras")["sources"][0]["revision"] == revision


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
    description = " ".join(SKILL_GATEWAY_TOOL_DESCRIPTION.split())
    assert "does not yet load user-scoped skill packages" in description
    assert "mandatory organization policy" in SKILL_GATEWAY_TOOL_DESCRIPTION
    assert "mandatory organization policy" in SKILL_GATEWAY_TOOL_DESCRIPTION
    assert "call request_clarification before submitting" in REVIEW_SYSTEM_PROMPT
    assert "does not break ties between peer sources" in REVIEW_SYSTEM_PROMPT
