from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

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
    assert "does not yet load user-scoped skill packages" in prompt
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
    docs.mkdir(parents=True, exist_ok=True)
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
    def __init__(
        self,
        organization_root: Path | None,
        project_source: LocalContextSource,
        snapshot_root: Path,
    ):
        organization = (
            {
                "source": {
                    "kind": "path",
                    "path": str(organization_root),
                }
            }
            if organization_root is not None
            else {}
        )
        self.organization_resolver = OrganizationSkillResolver.from_instance_config(
            {
                "skill_gateway": {
                    "organization": organization
                }
            },
            snapshot_root=snapshot_root,
            audit_emit=lambda _event: None,
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
    provider = _Provider(org_root, project_source, tmp_path / "pins")

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
    assert all(item["benchmark_scope"] == "uperf" for item in project_docs)
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
    assert all(item["benchmark_scope"] == "uperf" for item in project_inventory)
    conflicts = response["context_conflicts"]
    assert conflicts["cross_source_comparison_required"] == {
        "project_vs_organization": True,
        "project_vs_software": True,
    }
    overlap = conflicts["cross_source_potential_overlaps"]
    assert any(
        set(conflict["scopes"]) == {"project", "organization"} for conflict in overlap
    )


def test_gateway_reads_searches_and_enforces_project_scope(
    tmp_path: Path, monkeypatch
) -> None:
    org_root = tmp_path / "org"
    org_root.mkdir()
    _write_org_package(org_root)
    provider = _Provider(
        org_root,
        _write_project_source(tmp_path / "project"),
        tmp_path / "pins",
    )

    async def controller_context_gateway(**_kwargs):
        return json.dumps({"found": False, "reason": "not_installed"})

    monkeypatch.setattr(
        "agents.server_utils.controller_context_gateway",
        controller_context_gateway,
    )
    bootstrap = json.loads(
        asyncio.run(
            skill_context_gateway(
                provider,
                ticket_id="PERF-TEST",
                agent_name="benchmark-agent",
                phase="benchmark",
                subject="harness/crucible",
                operation="bootstrap",
                benchmark="uperf",
            )
        )
    )
    project_refs = {
        item["path"]: item["ref"]
        for item in bootstrap["documents"]
        if item["scope"] == "project"
    }
    ref = project_refs["skills/crucible/run-file-pitfalls.md"]
    assert ref.startswith("skill://project/")
    assert "benchmark=uperf&revision=" in ref

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


def test_project_search_pumps_large_stdin_and_stdout_concurrently() -> None:
    from agents.skill_gateway import _search_project_documents

    class _Source:
        @staticmethod
        def read(_path: str) -> str:
            return "\n".join(f"MATCH item-{index:05d}" for index in range(50_000))

    documents = [
        {
            "ref": "skill://project/skills/crucible/results.md",
            "path": "skills/crucible/results.md",
            "source_path": "skills/crucible/results.md",
        }
    ]

    async def _search():
        return await asyncio.wait_for(
            _search_project_documents(
                _Source(),
                documents,
                "MATCH",
                offset=0,
                max_bytes=16384,
            ),
            timeout=5,
        )

    result = asyncio.run(_search())
    assert result["matches_count"] == 4096
    assert result["search_limited"] is True
    assert result["matches"]


def test_project_search_discards_incomplete_truncated_grep_record() -> None:
    from agents.skill_gateway import _search_project_documents

    class _Source:
        @staticmethod
        def read(_path: str) -> str:
            # The first grep record ends one byte before the 2 MiB output cap.
            # The next record is therefore clipped to its line-number digit,
            # which has no ':' and used to crash the record parser.
            output_cap = 2 * 1024 * 1024
            first_line = "MATCH " + ("x" * (output_cap - 10))
            return first_line + "\nMATCH second\n"

    documents = [
        {
            "ref": "skill://project/skills/crucible/results.md",
            "path": "skills/crucible/results.md",
            "source_path": "skills/crucible/results.md",
        }
    ]
    result = asyncio.run(
        _search_project_documents(
            _Source(),
            documents,
            "MATCH",
            offset=0,
            max_bytes=16384,
        )
    )

    assert result["matches_count"] == 1
    assert result["matches"][0]["line"] == 1
    assert result["matches"][0]["snippet"].startswith("MATCH ")
    assert result["search_limited"] is True


def test_gateway_filters_project_docs_by_phase_and_agent(tmp_path: Path) -> None:
    org_root = tmp_path / "org"
    org_root.mkdir()
    _write_org_package(org_root)
    provider = _Provider(
        org_root,
        _write_project_source(tmp_path / "project"),
        tmp_path / "pins",
    )

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


def test_bootstrap_reports_project_revision_when_no_docs_match_phase(
    tmp_path: Path,
) -> None:
    project_source = _write_project_source(tmp_path / "project")
    provider = _Provider(None, project_source, tmp_path / "pins")

    response = json.loads(
        asyncio.run(
            skill_context_gateway(
                provider,
                ticket_id="PERF-PROJECT-EMPTY-PHASE",
                agent_name="provisioning-agent",
                phase="provisioning",
                subject="harness/crucible",
            )
        )
    )

    project_source_summary = next(
        item
        for item in response["sources"]
        if item.get("source") == "agentic-perf" and item.get("scope") == "project"
    )
    assert project_source_summary["document_count"] == 0
    assert project_source_summary["revision"]


def test_project_context_is_pinned_across_reads_and_provider_restart(
    tmp_path: Path, monkeypatch
) -> None:
    project_root = tmp_path / "project"
    source = _write_project_source(project_root)
    ticket_id = "PERF-PROJECT-PIN"
    snapshot_root = tmp_path / "pins"

    async def controller_context_gateway(**_kwargs):
        return json.dumps({"found": False, "reason": "not_installed"})

    monkeypatch.setattr(
        "agents.server_utils.controller_context_gateway",
        controller_context_gateway,
    )
    provider = _Provider(None, source, snapshot_root)
    bootstrap = json.loads(
        asyncio.run(
            skill_context_gateway(
                provider,
                ticket_id=ticket_id,
                agent_name="benchmark-agent",
                phase="benchmark",
                subject="harness/crucible",
                benchmark="uperf",
            )
        )
    )
    assert provider.organization_resolver.ticket_id == ticket_id
    assert provider.organization_resolver.attempt_id == "initial"
    ref = next(
        item["ref"]
        for item in bootstrap["documents"]
        if item["scope"] == "project" and item["source_path"].endswith("uperf.md")
    )

    target = project_root / "skills" / "crucible" / "uperf.md"
    target.write_text("Changed after bootstrap.\n")

    async def read(current_provider):
        return json.loads(
            await skill_context_gateway(
                current_provider,
                ticket_id=ticket_id,
                agent_name="benchmark-agent",
                phase="benchmark",
                subject="harness/crucible",
                operation="read",
                ref=ref,
            )
        )

    first_read = asyncio.run(read(provider))
    assert first_read["document"]["content"] == "Use Crucible uperf metadata.\n"

    restarted = _Provider(None, _write_project_source(project_root), snapshot_root)
    resumed_read = asyncio.run(read(restarted))
    assert resumed_read["document"]["content"] == "Use Crucible uperf metadata.\n"

    source_key = hashlib.sha256(b"local:agentic-perf-project").hexdigest()
    epoch = restarted.organization_resolver._source_epoch()
    (snapshot_root / epoch.key / f"source-{source_key}.json").unlink()
    corrupted_read = asyncio.run(read(_Provider(None, source, snapshot_root)))
    assert corrupted_read["reason"] == "invalid_snapshot"


def test_failed_project_capture_does_not_pin_an_incomplete_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    project_root = tmp_path / "project"
    source = _write_project_source(project_root)
    manifest_path = project_root / "skills" / "context-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["entries"].append(
        {
            "id": "required-but-missing",
            "path": "skills/crucible/required.md",
            "harness": "crucible",
            "phase": "benchmark",
            "subjects": ["benchmark"],
        }
    )
    manifest_path.write_text(json.dumps(manifest))
    snapshot_root = tmp_path / "pins"
    provider = _Provider(None, source, snapshot_root)
    ticket_id = "PERF-PROJECT-PARTIAL"

    async def controller_context_gateway(**_kwargs):
        return json.dumps({"found": False, "reason": "not_installed"})

    monkeypatch.setattr(
        "agents.server_utils.controller_context_gateway",
        controller_context_gateway,
    )

    async def bootstrap(current_provider):
        return json.loads(
            await skill_context_gateway(
                current_provider,
                ticket_id=ticket_id,
                agent_name="benchmark-agent",
                phase="benchmark",
                subject="harness/crucible",
                benchmark="uperf",
            )
        )

    failed = asyncio.run(bootstrap(provider))
    assert failed["found"] is False
    assert failed["reason"] == "snapshot_unavailable"
    bound = provider.organization_resolver.for_attempt(
        ticket_id, "initial", "benchmark"
    )
    epoch = bound._source_epoch()
    assert epoch.read("local:agentic-perf-project", source.binding_identity) is None

    missing_document = project_root / "skills" / "crucible" / "required.md"
    missing_document.write_text("Required project guidance.\n")
    recovered = asyncio.run(bootstrap(provider))
    assert recovered["found"] is True
    assert any(
        item.get("source_path") == "skills/crucible/required.md"
        for item in recovered["documents"]
    )


@pytest.mark.parametrize(
    "entry",
    [
        None,
        {"id": "outside", "path": "../outside.md"},
        {"id": "missing", "path": "skills/crucible/missing.md"},
    ],
)
def test_local_context_capture_rejects_malformed_or_missing_mapped_entries(
    tmp_path: Path, entry
) -> None:
    project_root = tmp_path / "project"
    source = _write_project_source(project_root)
    manifest_path = project_root / "skills" / "context-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["entries"].append(entry)
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(ValueError):
        source.capture_snapshot()


def test_project_context_binding_change_fails_closed(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / "project"
    _write_project_source(project_root)
    snapshot_root = tmp_path / "pins"

    async def controller_context_gateway(**_kwargs):
        return json.dumps({"found": False, "reason": "not_installed"})

    monkeypatch.setattr(
        "agents.server_utils.controller_context_gateway",
        controller_context_gateway,
    )
    provider = _Provider(None, _write_project_source(project_root), snapshot_root)
    bootstrap = json.loads(
        asyncio.run(
            skill_context_gateway(
                provider,
                ticket_id="PERF-PROJECT-BINDING",
                agent_name="benchmark-agent",
                phase="benchmark",
                subject="harness/crucible",
            )
        )
    )
    ref = next(
        item["ref"] for item in bootstrap["documents"] if item["scope"] == "project"
    )

    other_root = tmp_path / "other-project"
    _write_project_source(other_root)
    changed_provider = _Provider(
        None, _write_project_source(other_root), snapshot_root
    )
    result = json.loads(
        asyncio.run(
            skill_context_gateway(
                changed_provider,
                ticket_id="PERF-PROJECT-BINDING",
                agent_name="benchmark-agent",
                phase="benchmark",
                subject="harness/crucible",
                operation="read",
                ref=ref,
            )
        )
    )
    assert result["reason"] == "source_binding_changed"
