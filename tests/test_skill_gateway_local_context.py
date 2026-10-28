from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agents.skill_gateway import skill_context_gateway
from providers.skills.gateway import OrganizationSkillResolver
from providers.skills.local_context import LocalContextSource
from providers.skills.source_epoch import SourceEpoch


def test_local_context_identity_tracks_locations_not_file_contents(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    manifest = root / "manifest.json"
    manifest.write_text('{"entries": []}')
    source = LocalContextSource(manifest, root=root)

    identity = source.binding_identity
    assert len(identity) == 64
    assert str(root) not in identity
    assert (
        LocalContextSource(root / "." / "manifest.json", root=root).binding_identity
        == identity
    )

    manifest.write_text('{"entries": [], "version": 2}')
    assert source.binding_identity == identity
    assert (
        LocalContextSource(root / "other.json", root=root).binding_identity != identity
    )
    assert LocalContextSource(manifest, root=tmp_path).binding_identity != identity


@pytest.mark.asyncio
async def test_crucible_gateway_pins_real_project_context_for_ticket(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    (project / "docs").mkdir(parents=True)
    guide = project / "docs" / "guide.md"
    guide.write_text("Original Crucible guidance.\n")
    manifest = project / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "id": "guide",
                        "path": "docs/guide.md",
                        "harness": "crucible",
                        "phase": "triage",
                        "agent": "triage-agent",
                        "entrypoint": True,
                    }
                ]
            }
        )
    )
    ticket_id = "PERF-PROJECT-CONTEXT"
    events = []
    provider = SimpleNamespace(
        organization_resolver=OrganizationSkillResolver.from_instance_config({}),
        source_epoch=SourceEpoch(
            tmp_path / "epochs", ticket_id=ticket_id, audit_emit=events.append
        ),
        project_context_source=LocalContextSource(manifest, root=project),
    )

    async def invoke(**kwargs: str) -> dict:
        return json.loads(
            await skill_context_gateway(
                provider,
                ticket_id=ticket_id,
                agent_name="triage-agent",
                phase="triage",
                subject="harness/crucible",
                **kwargs,
            )
        )

    first = await invoke()
    project_doc = next(
        item for item in first["documents"] if item["scope"] == "project"
    )
    assert first["found"] is True
    assert project_doc["source_path"] == "docs/guide.md"
    assert project_doc["ref"] in first["entrypoints"]
    original = await invoke(operation="read", ref=project_doc["ref"])
    assert original["document"]["content"] == "Original Crucible guidance.\n"
    search = await invoke(operation="search", query="Original")
    project_results = next(
        item for item in search["sources"] if item["scope"] == "project"
    )
    assert project_results["found"] is True
    assert project_results["results"][0]["ref"] == project_doc["ref"]

    guide.write_text("Changed Crucible guidance.\n")
    manifest.write_text('{"entries": []}')
    repeated = await invoke()
    assert repeated["documents"] == first["documents"]
    pinned = await invoke(operation="read", ref=project_doc["ref"])
    assert pinned["document"]["content"] == "Original Crucible guidance.\n"

    # Changing the configured source must not replace a ticket's existing pin.
    other_root = tmp_path / "other"
    other_root.mkdir()
    provider.project_context_source = LocalContextSource(other_root / "missing.json")
    rebound = await invoke()
    assert rebound["documents"] == first["documents"]
    assert events
