from __future__ import annotations

import asyncio
import json
from pathlib import Path

from agents.skill_context import skill_context_prompt
from agents.skill_gateway import skill_context_gateway
from providers.skills.gateway import OrganizationSkillResolver
from providers.skills.local_context import LocalContextSource


def test_context_prompt_states_guidance_hierarchy_and_separate_runtime_domain():
    prompt = " ".join(skill_context_prompt("harness/crucible").split())
    assert (
        "authenticated user context before organization context, organization before"
        in prompt
    )
    assert "upstream before bundled project-local docs" in prompt
    assert "temporary fallback with the lowest default authority" in prompt
    assert "installed controller/version evidence" in prompt
    assert "mandatory organization policy" in prompt


def _write_org_package(root: Path) -> None:
    package = root / "skills" / "harness" / "crucible"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "---\n"
        "name: crucible\n"
        "description: Organization guidance for Crucible.\n"
        "metadata:\n"
        "  subject: harness/crucible\n"
        "---\n\n"
        "Organization Crucible workflow.\n"
    )
    (package / "run-file-pitfalls.md").write_text("Organization run-file preference.\n")
    (package / "skill.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "subject": "harness/crucible",
                "documents": [
                    {"path": "SKILL.md", "entrypoint": True},
                    {"path": "run-file-pitfalls.md", "entrypoint": False},
                ],
            }
        )
    )


def _write_project_source(root: Path) -> LocalContextSource:
    docs = root / "skills" / "crucible"
    docs.mkdir(parents=True)
    (docs / "run-file-pitfalls.md").write_text(
        "Do not add remotehost to an ordinary uperf client.\n"
    )
    (docs / "uperf.md").write_text("Use Crucible uperf metadata.\n")
    (docs / "review.md").write_text("Review only scoped evidence.\n")
    manifest = root / "skills" / "context-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "entries": [
                    {
                        "id": "run-file",
                        "path": "skills/crucible/run-file-pitfalls.md",
                        "harness": "crucible",
                        "phase": "benchmark",
                        "agent": "benchmark-agent",
                        "subjects": ["run-file"],
                        "entrypoint": True,
                    },
                    {
                        "id": "uperf",
                        "path": "skills/crucible/uperf.md",
                        "harness": "crucible",
                        "benchmark": "uperf",
                        "phase": "benchmark",
                        "agent": "benchmark-agent",
                        "subjects": ["benchmark"],
                        "entrypoint": True,
                    },
                    {
                        "id": "review",
                        "path": "skills/crucible/review.md",
                        "harness": "crucible",
                        "phase": "review",
                        "agent": "review-agent",
                        "subjects": ["results"],
                        "entrypoint": True,
                    },
                ],
            }
        )
    )
    return LocalContextSource(manifest, root=root)


class _Provider:
    def __init__(self, organization_root: Path, project_source: LocalContextSource):
        self.organization_resolver = OrganizationSkillResolver.from_instance_config(
            {
                "skill_gateway": {
                    "organization": {
                        "source": {
                            "kind": "path",
                            "path": str(organization_root),
                        }
                    }
                }
            }
        )
        self.project_context_source = project_source

    async def get_all_private_config(self, _: str) -> dict:
        return {}


def test_gateway_bootstraps_project_docs_with_distinct_provenance_and_overlaps(
    tmp_path: Path, monkeypatch
) -> None:
    org_root = tmp_path / "org"
    org_root.mkdir()
    _write_org_package(org_root)
    project_source = _write_project_source(tmp_path / "project")
    provider = _Provider(org_root, project_source)

    async def controller_context_gateway(**kwargs):
        assert kwargs["operation"] == "bootstrap"
        return json.dumps(
            {
                "found": True,
                "document": {
                    "path": "docs/how-run-files-work.md",
                    "source_path": "docs/how-run-files-work.md",
                    "content": "Installed software reference.",
                },
            }
        )

    monkeypatch.setattr(
        "agents.server_utils.controller_context_gateway",
        controller_context_gateway,
    )
    response = json.loads(
        asyncio.run(
            skill_context_gateway(
                provider,
                ticket_id="PERF-TEST",
                agent_name="benchmark-agent",
                phase="benchmark",
                subject="harness/crucible",
                benchmark="uperf",
            )
        )
    )

    project_docs = [
        item for item in response["documents"] if item["scope"] == "project"
    ]
    assert {item["source"] for item in project_docs} == {"agentic-perf"}
    assert {item["provenance"]["entry_id"] for item in project_docs} == {
        "run-file",
        "uperf",
    }
    assert all(item["revision"] for item in project_docs)
    assert {item["authority"] for item in project_docs} == {"supplemental"}
    assert all("manifest" not in item["provenance"] for item in project_docs)
    assert all(item["entrypoint"] for item in project_docs)
    assert all(item["ref"].startswith("skill://project/") for item in project_docs)
    assert any(item["scope"] == "organization" for item in response["documents"])
    assert any(item["scope"] == "software" for item in response["documents"])
    inventory = response["context_manifest"]
    assert inventory["document_count"] == len(response["documents"])
    project_inventory = [
        item for item in inventory["documents"] if item["scope"] == "project"
    ]
    assert {item["source_path"] for item in project_inventory} == {
        "skills/crucible/run-file-pitfalls.md",
        "skills/crucible/uperf.md",
    }
    assert {item["source_id"] for item in project_inventory} == {"agentic-perf"}
    assert all(item["authority"] == "supplemental" for item in project_inventory)
    conflicts = response["context_conflicts"]
    assert conflicts["cross_source_comparison_required"] == {
        "project_vs_organization": True,
        "project_vs_software": True,
    }
    overlap = conflicts["cross_source_potential_overlaps"]
    assert any(
        set(conflict["scopes"]) == {"project", "organization"} for conflict in overlap
    )


def test_gateway_reads_searches_and_enforces_project_scope(tmp_path: Path) -> None:
    org_root = tmp_path / "org"
    org_root.mkdir()
    _write_org_package(org_root)
    provider = _Provider(org_root, _write_project_source(tmp_path / "project"))
    ref = "skill://project/skills/crucible/run-file-pitfalls.md"

    read = json.loads(
        asyncio.run(
            skill_context_gateway(
                provider,
                ticket_id="PERF-TEST",
                agent_name="benchmark-agent",
                phase="benchmark",
                subject="harness/crucible",
                operation="read",
                ref=ref,
            )
        )
    )
    assert read["document"]["scope"] == "project"
    assert "Do not add remotehost" in read["document"]["content"]

    pointer = json.loads(
        asyncio.run(
            skill_context_gateway(
                provider,
                ticket_id="PERF-TEST",
                agent_name="benchmark-agent",
                phase="benchmark",
                subject="harness/crucible",
                operation="read",
                from_ref=ref,
                path="uperf.md",
                benchmark="uperf",
            )
        )
    )
    assert pointer["document"]["content"] == "Use Crucible uperf metadata.\n"

    search = json.loads(
        asyncio.run(
            skill_context_gateway(
                provider,
                ticket_id="PERF-TEST",
                agent_name="benchmark-agent",
                phase="benchmark",
                subject="harness/crucible",
                operation="search",
                from_ref=ref,
                query="remotehost",
                benchmark="uperf",
            )
        )
    )
    project_result = next(
        item for item in search["sources"] if item["scope"] == "project"
    )
    assert project_result["matches_count"] == 1
    assert project_result["matches"][0]["ref"] == ref

    denied = json.loads(
        asyncio.run(
            skill_context_gateway(
                provider,
                ticket_id="PERF-TEST",
                agent_name="benchmark-agent",
                phase="benchmark",
                subject="harness/crucible",
                operation="read",
                ref="skill://project/skills/crucible/review.md",
            )
        )
    )
    assert denied["reason"] == "invalid_ref"


def test_gateway_filters_project_docs_by_phase_and_agent(tmp_path: Path) -> None:
    org_root = tmp_path / "org"
    org_root.mkdir()
    _write_org_package(org_root)
    provider = _Provider(org_root, _write_project_source(tmp_path / "project"))

    response = json.loads(
        asyncio.run(
            skill_context_gateway(
                provider,
                ticket_id="PERF-TEST",
                agent_name="review-agent",
                phase="review",
                subject="harness/crucible",
                operation="bootstrap",
            )
        )
    )
    project_docs = [
        item for item in response["documents"] if item["scope"] == "project"
    ]
    assert [item["source_path"] for item in project_docs] == [
        "skills/crucible/review.md"
    ]
