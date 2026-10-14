from __future__ import annotations

import asyncio
import json
from pathlib import Path

from agents.server_utils import build_skill_provider_async
from providers.skills.git_source import PreparedGitSource, parse_git_source
from providers.skills.source_epoch import SourceEpoch


def _write_package(root: Path, *, note: str, runtime: dict) -> None:
    package = root / "skills" / "harness" / "crucible"
    package.mkdir(parents=True, exist_ok=True)
    (package / "SKILL.md").write_text(
        "---\nname: crucible\ndescription: Crucible guidance.\n"
        "metadata:\n  subject: harness/crucible\n---\n\nUse the org guide.\n"
    )
    (package / "notes.md").write_text(note)
    (package / "skill.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "subject": "harness/crucible",
                "documents": [
                    {"path": "SKILL.md", "entrypoint": True},
                    {"path": "notes.md", "entrypoint": False},
                ],
            }
        )
    )
    service_config = root / "service-config" / "harness" / "crucible.json"
    service_config.parent.mkdir(parents=True, exist_ok=True)
    service_config.write_text(json.dumps(runtime))


def _config_file(path: Path, organization: dict) -> None:
    path.write_text(json.dumps({"skill_gateway": {"organization": organization}}))


def test_path_only_providers_are_isolated_per_ticket(
    tmp_path: Path, monkeypatch
) -> None:
    import paths
    import providers.skills.gateway as gateway

    home = tmp_path / "service-home"
    monkeypatch.setattr(gateway, "AGENTIC_PERF_HOME", home)
    monkeypatch.setattr(paths, "CONFIG_PATH", tmp_path / "config.json")
    org_root = tmp_path / "organization"
    _write_package(
        org_root,
        note="First ticket's pinned document.\n",
        runtime={"execution": {"endpoint_type": "kube"}},
    )
    _config_file(
        paths.CONFIG_PATH,
        {"sources": [{"id": "org", "kind": "path", "path": str(org_root)}]},
    )

    async def scenario():
        first = await build_skill_provider_async(
            ticket_id="PERF-ISOLATION-A",
            attempt_id="attempt-1",
            skill_phase="benchmark",
        )
        first_resolver = first.organization_resolver
        first_bootstrap = first_resolver.bootstrap("harness/crucible")
        first_note_ref = next(
            item["ref"]
            for item in first_bootstrap["documents"]
            if item["source_path"].endswith("notes.md")
        )

        _write_package(
            org_root,
            note="Second ticket's independent document.\n",
            runtime={"execution": {"endpoint_type": "podman"}},
        )
        second = await build_skill_provider_async(
            ticket_id="PERF-ISOLATION-B",
            attempt_id="attempt-1",
            skill_phase="benchmark",
        )
        second_resolver = second.organization_resolver
        second_bootstrap = second_resolver.bootstrap("harness/crucible")
        second_note_ref = next(
            item["ref"]
            for item in second_bootstrap["documents"]
            if item["source_path"].endswith("notes.md")
        )

        async def read(resolver, ref):
            await asyncio.sleep(0)
            return resolver.read("harness/crucible", ref)

        first_doc, second_doc = await asyncio.gather(
            read(first_resolver, first_note_ref),
            read(second_resolver, second_note_ref),
        )
        return (
            first,
            second,
            first_bootstrap,
            second_bootstrap,
            first_doc,
            second_doc,
        )

    first, second, first_bootstrap, second_bootstrap, first_doc, second_doc = (
        asyncio.run(scenario())
    )
    first_resolver = first.organization_resolver
    second_resolver = second.organization_resolver
    assert first_resolver is not second_resolver
    assert first_resolver.ticket_id == "PERF-ISOLATION-A"
    assert second_resolver.ticket_id == "PERF-ISOLATION-B"
    assert first_doc["content"] == "First ticket's pinned document.\n"
    assert second_doc["content"] == "Second ticket's independent document.\n"
    assert first_resolver.get_runtime_config("harness/crucible") == {
        "execution": {"endpoint_type": "kube"}
    }
    assert second_resolver.get_runtime_config("harness/crucible") == {
        "execution": {"endpoint_type": "podman"}
    }
    assert first_bootstrap["revision"] != second_bootstrap["revision"]


def test_startup_defers_git_until_ticket_pin_is_available(
    tmp_path: Path, monkeypatch
) -> None:
    import paths
    import providers.skills.gateway as gateway
    import providers.skills.git_source as git_source

    home = tmp_path / "service-home"
    monkeypatch.setattr(gateway, "AGENTIC_PERF_HOME", home)
    monkeypatch.setattr(paths, "CONFIG_PATH", tmp_path / "config.json")
    org_root = tmp_path / "organization"
    _write_package(
        org_root,
        note="Pinned Git note.\n",
        runtime={"execution": {"endpoint_type": "kube"}},
    )
    source = {
        "kind": "git",
        "url": "https://git.example.org/team/skills.git",
        "ref": "main",
        "auth": {
            "kind": "https-token",
            "secret_ref": "organization/git-token",
        },
    }
    _config_file(
        paths.CONFIG_PATH,
        {"sources": [{"id": "org", **source}]},
    )
    parsed = parse_git_source(source)
    revision = "a" * 40
    epoch = SourceEpoch(
        home / "skill-snapshots",
        ticket_id="PERF-OFFLINE-RESUME",
        attempt_id="initial",
        audit_emit=lambda _event: None,
    )
    epoch.get_or_capture(
        "git:org",
        parsed.identity,
        lambda: {"source_identity": parsed.identity, "revision": revision},
    )

    prepared_revisions = []

    async def offline_prepare(current_source, *, pinned_commit=None, **_kwargs):
        assert current_source == source
        assert pinned_commit == revision
        prepared_revisions.append(pinned_commit)
        return PreparedGitSource(org_root, revision, parsed.identity)

    monkeypatch.setattr(git_source, "prepare_git_source", offline_prepare)

    async def scenario():
        startup = await build_skill_provider_async(defer_organization_sources=True)
        ticket = await build_skill_provider_async(
            ticket_id="PERF-OFFLINE-RESUME",
            attempt_id="initial",
            skill_phase="benchmark",
            secrets_provider=object(),
        )
        return startup, ticket

    startup, ticket = asyncio.run(scenario())
    assert startup.organization_resolver.ticket_id == ""
    assert ticket.organization_resolver.ticket_id == "PERF-OFFLINE-RESUME"
    assert prepared_revisions == [revision]
    assert ticket.organization_resolver.bootstrap("harness/crucible")["revision"]
