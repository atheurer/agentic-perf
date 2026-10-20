from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from agents.review.prompts import REVIEW_SYSTEM_PROMPT
from agents.skill_gateway import SKILL_GATEWAY_TOOL_DESCRIPTION, skill_context_gateway
from providers.skills.gateway import OrganizationSkillResolver, SkillGatewayError
from providers.skills.k8s_netperf import K8sNetperfSkillProvider
from providers.skills.multi import MultiHarnessSkillProvider
from providers.skills.private import PrivateSkillProvider


def _write_package(
    repository: Path,
    *,
    namespace: str,
    name: str,
    subject: str,
    instruction: str,
    shared_note: str = "Shared note from maintainers.\n",
    runtime_config: dict | None = None,
    document_phases: dict[str, list[str]] | None = None,
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
    documents = []
    for path, entrypoint in (("SKILL.md", True), ("notes.md", False)):
        document = {"path": path, "entrypoint": entrypoint}
        if document_phases and path in document_phases:
            document["phases"] = document_phases[path]
        documents.append(document)
    manifest = {
        "schema_version": 1,
        "subject": subject,
        "documents": documents,
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
    assert "upstream context is a baseline" in SKILL_GATEWAY_TOOL_DESCRIPTION
    assert "user context (when available)" in SKILL_GATEWAY_TOOL_DESCRIPTION
    assert "mandatory organization policy" in SKILL_GATEWAY_TOOL_DESCRIPTION
    assert "call request_clarification before submitting" in REVIEW_SYSTEM_PROMPT
    assert "does not break ties between peer sources" in REVIEW_SYSTEM_PROMPT


def test_resource_aws_guidance_is_retrieved_through_local_gateway(
    tmp_path: Path,
) -> None:
    skills = tmp_path / "skills"
    resource = skills / "resource"
    aws = resource / "aws"
    aws.mkdir(parents=True)
    (aws / "SKILL.md").write_text("AWS resource allocation guidance.\n")
    resolver = OrganizationSkillResolver.from_instance_config({})

    class Provider:
        organization_resolver = resolver

    bootstrap = json.loads(
        asyncio.run(
            skill_context_gateway(
                Provider(),
                ticket_id="TEST-AWS-CONTEXT",
                agent_name="resource-agent",
                phase="resource",
                subject="resource/aws",
                local_skills_dir=skills,
            )
        )
    )

    assert bootstrap["found"] is True
    source = next(item for item in bootstrap["sources"] if item["scope"] == "local")
    assert source["source"] == "agentic-perf"
    assert source["status"] == "available"
    ref = next(item["ref"] for item in bootstrap["documents"])
    document = json.loads(
        asyncio.run(
            skill_context_gateway(
                Provider(),
                ticket_id="TEST-AWS-CONTEXT",
                agent_name="resource-agent",
                phase="resource",
                subject="resource/aws",
                operation="read",
                ref=ref,
                local_skills_dir=skills,
            )
        )
    )["document"]

    assert document["scope"] == "local"
    assert document["source"] == "agentic-perf"
    assert "AWS resource allocation guidance" in document["content"]


@pytest.mark.asyncio
async def test_local_directory_listing_and_directory_origin_reads_are_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agents.skill_gateway as gateway
    import paths

    monkeypatch.setattr(paths, "TICKET_DIR", tmp_path / "tickets")
    skills = tmp_path / "skills"
    docs = skills / "kube-burner" / "docs"
    nested = docs / "nested"
    nested.mkdir(parents=True)
    (docs / "alpha.md").write_text("alpha guidance")
    (nested / "nested.md").write_text("special nested guidance")
    (docs / "token-notes.md").write_text("credential-shaped name")
    private_docs = docs / "credentials"
    private_docs.mkdir()
    (private_docs / "key.md").write_text("must not be listed")
    outside = tmp_path / "outside.md"
    outside.write_text("outside source")
    (docs / "safe-link.md").symlink_to(docs / "alpha.md")
    (docs / "escape.md").symlink_to(outside)
    (docs / "credential-link").symlink_to(private_docs, target_is_directory=True)
    (docs / ".git").mkdir()

    class Provider:
        organization_resolver = OrganizationSkillResolver.from_instance_config({})

    directory_ref = gateway._source_ref("local", "harness/kube-burner", "docs")
    listing = json.loads(
        await skill_context_gateway(
            Provider(),
            ticket_id="PERF-LOCAL-LIST",
            agent_name="benchmark-agent",
            phase="benchmark",
            subject="harness/kube-burner",
            operation="list",
            ref=directory_ref,
            local_skills_dir=skills,
        )
    )

    entries = listing["sources"][0]["results"]
    assert [(item["path"], item["type"]) for item in entries] == [
        ("docs/alpha.md", "file"),
        ("docs/nested", "directory"),
        ("docs/safe-link.md", "file"),
    ]
    assert all(item["ref"].startswith("skill://local/") for item in entries)
    assert listing["sources"][0]["recursive"] is False

    nested_ref = next(item["ref"] for item in entries if item["type"] == "directory")
    read = json.loads(
        await skill_context_gateway(
            Provider(),
            ticket_id="PERF-LOCAL-LIST",
            agent_name="benchmark-agent",
            phase="benchmark",
            subject="harness/kube-burner",
            operation="read",
            from_ref=nested_ref,
            path="nested.md",
            local_skills_dir=skills,
        )
    )
    assert read["document"]["content"] == "special nested guidance"

    search = json.loads(
        await skill_context_gateway(
            Provider(),
            ticket_id="PERF-LOCAL-LIST",
            agent_name="benchmark-agent",
            phase="benchmark",
            subject="harness/kube-burner",
            operation="search",
            query="special",
            from_ref=nested_ref,
            local_skills_dir=skills,
        )
    )
    assert [item["path"] for item in search["sources"][0]["results"]] == [
        "docs/nested/nested.md"
    ]


@pytest.mark.asyncio
async def test_root_level_local_document_origin_searches_source_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import paths

    monkeypatch.setattr(paths, "TICKET_DIR", tmp_path / "tickets")
    skills = tmp_path / "skills"
    local = skills / "kube-burner"
    local.mkdir(parents=True)
    (local / "SKILL.md").write_text("root needle guidance")
    (local / "notes.md").write_text("sibling needle reference")

    class Provider:
        organization_resolver = OrganizationSkillResolver.from_instance_config({})

    result = json.loads(
        await skill_context_gateway(
            Provider(),
            ticket_id="PERF-ROOT-ORIGIN-SEARCH",
            agent_name="benchmark-agent",
            phase="benchmark",
            subject="harness/kube-burner",
            operation="search",
            query="needle",
            from_ref=("skill://local/harness/kube-burner/SKILL.md"),
            local_skills_dir=skills,
        )
    )

    source = result["sources"][0]
    assert source["scan_scope"] == "."
    assert {item["path"] for item in source["results"]} == {"SKILL.md", "notes.md"}


@pytest.mark.asyncio
async def test_local_directory_alias_searches_its_resolved_subtree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import paths

    monkeypatch.setattr(paths, "TICKET_DIR", tmp_path / "tickets")
    skills = tmp_path / "skills"
    docs = skills / "kube-burner" / "docs"
    real = docs / "real"
    real.mkdir(parents=True)
    (real / "needle.md").write_text("needle through a safe directory alias")
    (docs / "alias").symlink_to(real, target_is_directory=True)

    class Provider:
        organization_resolver = OrganizationSkillResolver.from_instance_config({})

    result = json.loads(
        await skill_context_gateway(
            Provider(),
            ticket_id="PERF-LOCAL-ALIAS-SEARCH",
            agent_name="benchmark-agent",
            phase="benchmark",
            subject="harness/kube-burner",
            operation="search",
            query="needle",
            from_ref="skill://local/harness/kube-burner/docs/alias",
            local_skills_dir=skills,
        )
    )

    source = result["sources"][0]
    assert source["found"] is True
    assert source["complete"] is True
    assert [item["path"] for item in source["results"]] == ["docs/real/needle.md"]


@pytest.mark.asyncio
async def test_upstream_symlinks_cannot_escape_exposed_docs_trees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import paths

    monkeypatch.setattr(paths, "TICKET_DIR", tmp_path / "tickets")
    repo_root = tmp_path / "cache" / "kube-burner"
    docs = repo_root / "docs"
    source = repo_root / "src"
    docs.mkdir(parents=True)
    source.mkdir()
    real_docs = docs / "real"
    real_docs.mkdir()
    (real_docs / "needle.md").write_text("needle through a safe directory alias")
    (docs / "visible.md").write_text("public docs guidance")
    (source / "secret.md").write_text("secretphrase out of scope")
    (docs / "safe-link.md").symlink_to(docs / "visible.md")
    (docs / "alias").symlink_to(real_docs, target_is_directory=True)
    (docs / "internal").symlink_to(source, target_is_directory=True)

    from providers.skills.repo_cache import RepoCache

    cache = RepoCache(cache_dir=repo_root.parent)

    class Provider:
        organization_resolver = OrganizationSkillResolver.from_instance_config({})

    unsafe_directory_ref = "skill://upstream/harness/kube-burner/docs/internal"
    unsafe_document_ref = "skill://upstream/harness/kube-burner/docs/internal/secret.md"
    safe_directory_ref = "skill://upstream/harness/kube-burner/docs"

    async def invoke(**kwargs: Any) -> dict[str, Any]:
        return json.loads(
            await skill_context_gateway(
                Provider(),
                ticket_id="PERF-UPSTREAM-SYMLINK-BOUNDARY",
                agent_name="benchmark-agent",
                phase="benchmark",
                subject="harness/kube-burner",
                repo_cache=cache,
                **kwargs,
            )
        )

    direct_read = await invoke(operation="read", ref=unsafe_document_ref)
    assert direct_read["reason"] == "invalid_document"

    relative_read = await invoke(
        operation="read", from_ref=unsafe_directory_ref, path="secret.md"
    )
    assert relative_read["reason"] == "invalid_document"

    pointer_read = await invoke(
        operation="read", from_ref=safe_directory_ref, path="internal/secret.md"
    )
    assert pointer_read["reason"] == "invalid_document"

    directory_list = await invoke(operation="list", ref=unsafe_directory_ref)
    assert directory_list["reason"] == "invalid_document"

    directory_read = await invoke(operation="read", ref=safe_directory_ref)
    assert directory_read["reason"] == "source_path_is_directory"
    assert "operation='list'" in directory_read["message"]

    pointer_list = await invoke(
        operation="list", from_ref=safe_directory_ref, path="internal"
    )
    assert pointer_list["reason"] == "invalid_document"

    scoped_search = await invoke(
        operation="search", query="secretphrase", from_ref=unsafe_directory_ref
    )
    assert scoped_search["reason"] == "invalid_document"

    pointer_search = await invoke(
        operation="search",
        query="secretphrase",
        from_ref=safe_directory_ref,
        path="internal",
    )
    assert pointer_search["reason"] == "invalid_document"

    bootstrap = await invoke(operation="bootstrap")
    upstream_paths = {
        item["path"] for item in bootstrap["documents"] if item["scope"] == "upstream"
    }
    assert "docs/internal/secret.md" not in upstream_paths
    assert "docs/safe-link.md" in upstream_paths

    safe_read = await invoke(
        operation="read",
        ref="skill://upstream/harness/kube-burner/docs/safe-link.md",
    )
    assert safe_read["found"] is True
    assert safe_read["document"]["content"] == "public docs guidance"

    safe_alias_search = await invoke(
        operation="search",
        query="needle",
        from_ref="skill://upstream/harness/kube-burner/docs/alias",
    )
    alias_source = safe_alias_search["sources"][0]
    assert alias_source["found"] is True
    assert alias_source["complete"] is True
    assert [item["path"] for item in alias_source["results"]] == ["docs/real/needle.md"]


@pytest.mark.asyncio
async def test_search_and_list_cursors_are_bounded_scoped_and_do_not_drop_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agents.skill_gateway as gateway
    import paths

    monkeypatch.setattr(paths, "TICKET_DIR", tmp_path / "tickets")
    skills = tmp_path / "skills"
    docs = skills / "kube-burner" / "docs"
    docs.mkdir(parents=True)
    for index in range(35):
        (docs / f"doc-{index:02}.md").write_text(f"needle {index}\n")

    class Provider:
        organization_resolver = OrganizationSkillResolver.from_instance_config({})

    directory_ref = gateway._source_ref("local", "harness/kube-burner", "docs")

    async def invoke(**kwargs: Any) -> tuple[str, dict[str, Any]]:
        raw = await skill_context_gateway(
            Provider(),
            ticket_id=kwargs.pop("ticket_id", "PERF-CURSOR"),
            agent_name="benchmark-agent",
            phase="benchmark",
            subject="harness/kube-burner",
            local_skills_dir=skills,
            max_bytes=1024,
            **kwargs,
        )
        return raw, json.loads(raw)

    listed_refs = []
    raw, result = await invoke(operation="list", ref=directory_ref)
    while True:
        assert len(raw.encode("utf-8")) <= 1024
        listed_refs.extend(item["ref"] for item in result["sources"][0]["results"])
        cursor = result["sources"][0].get("next_cursor")
        if not cursor:
            break
        raw, result = await invoke(operation="list", ref=directory_ref, cursor=cursor)
    assert len(listed_refs) == 35
    assert len(set(listed_refs)) == 35

    found_refs = []
    raw, result = await invoke(
        operation="search", query="needle", from_ref=directory_ref, path="."
    )
    first_cursor = result["sources"][0]["next_cursor"]
    while True:
        assert len(raw.encode("utf-8")) <= 1024
        found_refs.extend(item["ref"] for item in result["sources"][0]["results"])
        cursor = result["sources"][0].get("next_cursor")
        if not cursor:
            break
        raw, result = await invoke(operation="search", query="needle", cursor=cursor)
    assert len(found_refs) == 35
    assert len(set(found_refs)) == 35

    _raw, changed_query = await invoke(
        operation="search",
        query="different",
        from_ref=directory_ref,
        cursor=first_cursor,
    )
    assert changed_query["reason"] == "cursor_scope_mismatch"
    _raw, changed_scope = await invoke(
        operation="search",
        query="needle",
        from_ref=directory_ref,
        path="nested",
        cursor=first_cursor,
    )
    assert changed_scope["reason"] == "cursor_scope_mismatch"
    _raw, changed_ticket = await invoke(
        ticket_id="PERF-OTHER-TICKET",
        operation="search",
        query="needle",
        from_ref=directory_ref,
        cursor=first_cursor,
    )
    assert changed_ticket["reason"] == "invalid_cursor"
    _raw, tampered = await invoke(
        operation="search",
        query="needle",
        cursor=f"{first_cursor[:-1]}!",
    )
    assert tampered["reason"] == "invalid_cursor"


@pytest.mark.asyncio
async def test_search_page_uses_spare_global_budget_across_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import paths

    monkeypatch.setattr(paths, "TICKET_DIR", tmp_path / "tickets")
    organization = tmp_path / "organization"
    _write_package(
        organization,
        namespace="harness",
        name="kube-burner",
        subject="harness/kube-burner",
        instruction="General organization guidance.",
    )
    resolver = _resolver(("team", organization))
    skills = tmp_path / "skills"
    deep_parent = skills / "kube-burner" / "docs"
    deep_parent = deep_parent.joinpath(*(["nested-directory-name"] * 56))
    deep_parent.mkdir(parents=True)
    long_path = deep_parent / "needle.md"
    long_path.write_text("needle in one long source path")
    upstream = tmp_path / "cache" / "kube-burner" / "docs"
    upstream.mkdir(parents=True)
    (upstream / "reference.md").write_text("unrelated upstream reference")

    from providers.skills.repo_cache import RepoCache

    cache = RepoCache(cache_dir=upstream.parents[1])

    class Provider:
        organization_resolver = resolver

    raw = await skill_context_gateway(
        Provider(),
        ticket_id="PERF-GLOBAL-SPARE-BUDGET",
        agent_name="benchmark-agent",
        phase="benchmark",
        subject="harness/kube-burner",
        operation="search",
        query="needle",
        max_bytes=6000,
        local_skills_dir=skills,
        repo_cache=cache,
    )
    result = json.loads(raw)

    assert len(raw.encode("utf-8")) <= 6000
    assert result["found"] is True
    local_source = next(item for item in result["sources"] if item["scope"] == "local")
    assert [item["path"] for item in local_source["results"]] == [
        long_path.relative_to(skills / "kube-burner").as_posix()
    ]


@pytest.mark.asyncio
async def test_search_with_no_available_sources_is_incomplete_not_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import paths

    monkeypatch.setattr(paths, "TICKET_DIR", tmp_path / "tickets")

    class Provider:
        organization_resolver = OrganizationSkillResolver.from_instance_config({})

    result = json.loads(
        await skill_context_gateway(
            Provider(),
            ticket_id="PERF-NO-SEARCH-SOURCES",
            agent_name="triage-agent",
            phase="triage",
            subject="harness/crucible",
            operation="search",
            query="missing",
        )
    )

    assert result["sources"] == []
    assert result["found"] is False
    assert result["complete"] is False
    assert result["absence_confirmed"] is False


@pytest.mark.asyncio
async def test_organization_search_cursor_continues_without_dropping_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import paths

    monkeypatch.setattr(paths, "TICKET_DIR", tmp_path / "tickets")
    organization = tmp_path / "organization"
    _write_package(
        organization,
        namespace="harness",
        name="crucible",
        subject="harness/crucible",
        instruction="General Crucible organization guidance.",
    )
    package = organization / "skills" / "harness" / "crucible"
    manifest_path = package / "skill.json"
    manifest = json.loads(manifest_path.read_text())
    documents = package / "references"
    documents.mkdir()
    for index in range(21):
        relative = f"references/reference-{index:02}.md"
        (package / relative).write_text(f"needle in reference {index:02}\n")
        manifest["documents"].append({"path": relative, "entrypoint": False})
    manifest_path.write_text(json.dumps(manifest))
    resolver = _resolver(("team", organization))

    class Provider:
        organization_resolver = resolver

    origin = resolver.bootstrap("harness/crucible")["documents"][0]["ref"]

    async def invoke(cursor: str = "") -> tuple[str, dict[str, Any]]:
        raw = await skill_context_gateway(
            Provider(),
            ticket_id="PERF-ORGANIZATION-CURSOR",
            agent_name="triage-agent",
            phase="triage",
            subject="harness/crucible",
            operation="search",
            query="needle",
            from_ref=origin,
            max_bytes=8192,
            cursor=cursor,
        )
        return raw, json.loads(raw)

    refs: list[str] = []
    raw, result = await invoke()
    while True:
        assert len(raw.encode("utf-8")) <= 8192
        source = result["sources"][0]
        refs.extend(item["ref"] for item in source["matches"])
        cursor = source.get("next_cursor")
        if not cursor:
            break
        raw, result = await invoke(cursor)

    assert len(refs) == 21
    assert len(set(refs)) == 21


@pytest.mark.asyncio
async def test_controller_search_cursor_continues_gateway_result_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agents.server_utils as server_utils
    import paths

    monkeypatch.setattr(paths, "TICKET_DIR", tmp_path / "tickets")
    calls = []
    search_results = [
        {
            "path": f"repos/bench/docs/reference-{index:02}.md",
            "type": "file",
            "size_bytes": 32,
            "match_kinds": ["content"],
            "matches": [{"kind": "content", "line_number": 1, "content": "needle"}],
            "match_count": 1,
            "matches_truncated": False,
        }
        for index in range(12)
    ]

    async def fake_controller_context_gateway(**kwargs: Any) -> str:
        calls.append(kwargs)
        return json.dumps(
            {
                "found": True,
                "operation": "search",
                "results": search_results,
                "total_files": len(search_results),
                "total_matches": len(search_results),
                "complete": True,
                "scan_scope": "repos/bench",
                "scope_semantics": "recursive subtree",
            }
        )

    monkeypatch.setattr(
        server_utils, "controller_context_gateway", fake_controller_context_gateway
    )

    class Provider:
        organization_resolver = OrganizationSkillResolver.from_instance_config({})

    origin = "skill://software/controller/harness/crucible/repos/bench"

    async def invoke(cursor: str = "") -> tuple[str, dict[str, Any]]:
        raw = await skill_context_gateway(
            Provider(),
            ticket_id="PERF-CONTROLLER-CURSOR",
            agent_name="benchmark-agent",
            phase="benchmark",
            subject="harness/crucible",
            operation="search",
            query="needle",
            from_ref=origin,
            max_bytes=1200,
            cursor=cursor,
        )
        return raw, json.loads(raw)

    refs: list[str] = []
    raw, result = await invoke()
    while True:
        assert len(raw.encode("utf-8")) <= 1200
        source = result["sources"][0]
        refs.extend(item["ref"] for item in source["results"])
        cursor = source.get("next_cursor")
        if not cursor:
            break
        raw, result = await invoke(cursor)

    assert len(refs) == 12
    assert len(set(refs)) == 12
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_incomplete_local_scan_does_not_confirm_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agents.skill_gateway as gateway
    import paths

    monkeypatch.setattr(paths, "TICKET_DIR", tmp_path / "tickets")
    skills = tmp_path / "skills"
    docs = skills / "kube-burner" / "docs"
    docs.mkdir(parents=True)
    (docs / "large.md").write_text("content without the query")
    monkeypatch.setattr(gateway, "_MAX_SOURCE_SEARCH_BYTES", 1)

    class Provider:
        organization_resolver = OrganizationSkillResolver.from_instance_config({})

    result = json.loads(
        await skill_context_gateway(
            Provider(),
            ticket_id="PERF-INCOMPLETE-SEARCH",
            agent_name="benchmark-agent",
            phase="benchmark",
            subject="harness/kube-burner",
            operation="search",
            query="absent",
            from_ref=gateway._source_ref(
                "local", "harness/kube-burner", "docs/large.md"
            ),
            local_skills_dir=skills,
        )
    )
    source = result["sources"][0]
    assert source["found"] is False
    assert source["complete"] is False
    assert source["absence_confirmed"] is False


@pytest.mark.asyncio
async def test_gateway_reports_unsupported_organization_directory_listing(
    tmp_path: Path,
):
    _write_package(
        tmp_path / "organization",
        namespace="harness",
        name="crucible",
        subject="harness/crucible",
        instruction="Crucible guidance.",
    )
    resolver = _resolver(("team", tmp_path / "organization"))

    class Provider:
        organization_resolver = resolver

    result = json.loads(
        await skill_context_gateway(
            Provider(),
            ticket_id="PERF-ORG-LIST",
            agent_name="benchmark-agent",
            phase="benchmark",
            subject="harness/crucible",
            operation="list",
            from_ref=resolver.bootstrap("harness/crucible")["documents"][0]["ref"],
        )
    )
    assert result["sources"][0]["capability"] == "unsupported"
    assert result["sources"][0]["reason"] == "directory_listing_unsupported"


@pytest.mark.asyncio
async def test_controller_listing_keeps_existing_phase_restrictions() -> None:
    import agents.skill_gateway as gateway

    class Provider:
        organization_resolver = OrganizationSkillResolver.from_instance_config({})

    class ForbiddenSSH:
        async def run(self, host, command, **kwargs):
            raise AssertionError("controller access is forbidden in triage")

    result = json.loads(
        await skill_context_gateway(
            Provider(),
            ticket_id="PERF-CONTROLLER-LIST-PHASE",
            agent_name="triage-agent",
            phase="triage",
            operation="list",
            ref=gateway._software_ref("repos/example"),
            ssh=ForbiddenSSH(),
            controller_host="controller.example.test",
        )
    )
    assert result["reason"] == "source_unavailable"


@pytest.mark.asyncio
async def test_resource_gateway_tool_does_not_initialize_cloud_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agents.resource.server as resource_server
    import agents.server_utils as server_utils
    import paths
    import providers.skills.git_source as git_source

    skills = tmp_path / "skills"
    aws = skills / "resource" / "aws"
    aws.mkdir(parents=True)
    (aws / "SKILL.md").write_text("AWS guidance stays context only.\n")
    organization = tmp_path / "organization"
    _write_package(
        organization,
        namespace="resource",
        name="aws",
        subject="resource/aws",
        instruction="Organization AWS allocation guidance.",
        document_phases={"SKILL.md": ["resource"], "notes.md": ["*"]},
    )
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "skill_gateway": {
                    "organization": {
                        "source": {
                            "kind": "git",
                            "url": "https://git.example.org/skills.git",
                            "ref": "main",
                            "auth": {
                                "kind": "https-token",
                                "secret_ref": "org/skills-token",
                            },
                        }
                    }
                }
            }
        )
    )

    class OrgSkillSecrets:
        calls: list[str] = []

        async def get_secret(self, name: str) -> str | None:
            self.calls.append(name)
            if name == "aws/config.json":
                raise AssertionError("AWS service credentials are not context")
            return "org-token" if name == "org/skills-token" else None

        async def secret_file(self, name: str):
            self.calls.append(name)
            if name == "aws/config.json":
                raise AssertionError("AWS service credentials are not context")
            assert name == "org/skills-token"

            class _SecretFile:
                async def __aenter__(self):
                    return tmp_path / "org-token"

                async def __aexit__(self, *_exc_info):
                    return None

            return _SecretFile()

    secrets = OrgSkillSecrets()

    async def prepare_org_source(source, *, secrets_provider=None, **_kwargs):
        assert source["auth"]["secret_ref"] == "org/skills-token"
        async with await secrets_provider.secret_file("org/skills-token"):
            pass
        return git_source.PreparedGitSource(
            root=organization,
            commit="a" * 40,
            identity="org-source-identity",
        )

    async def unexpected_resource_init():
        raise AssertionError("context retrieval must not initialize providers")

    monkeypatch.setattr(paths, "CONFIG_PATH", config_path)
    monkeypatch.setattr(server_utils, "build_secrets_provider", lambda: secrets)
    monkeypatch.setattr(git_source, "prepare_git_source", prepare_org_source)
    monkeypatch.setattr(resource_server, "_skill_provider", None)
    monkeypatch.setattr(resource_server, "_ensure_init", unexpected_resource_init)
    monkeypatch.setattr(resource_server, "SKILLS_DIR", skills)
    monkeypatch.setenv("TICKET_ID", "TEST-AWS-CONTEXT")

    result = json.loads(await resource_server.get_skill_context(subject="resource/aws"))

    assert result["found"] is True
    assert any(item["scope"] == "local" for item in result["sources"])
    assert secrets.calls == ["org/skills-token"]
    resolver = resource_server._skill_provider.organization_resolver
    assert (resolver.ticket_id, resolver.attempt_id, resolver.phase) == (
        "TEST-AWS-CONTEXT",
        "initial",
        "resource",
    )
    organization_docs = [
        item for item in result["documents"] if item["scope"] == "organization"
    ]
    assert any(item["path"].endswith("SKILL.md") for item in organization_docs)
    assert any(item["path"].endswith("notes.md") for item in organization_docs)
    skill_ref = next(
        item["ref"] for item in organization_docs if item["path"].endswith("SKILL.md")
    )
    skill_document = json.loads(
        await resource_server.get_skill_context(
            subject="resource/aws", operation="read", ref=skill_ref
        )
    )["document"]
    assert "Organization AWS allocation guidance" in skill_document["content"]


def test_kube_burner_gateway_exposes_local_and_cached_upstream_docs(
    tmp_path: Path,
) -> None:
    skills = tmp_path / "skills"
    local = skills / "kube-burner"
    local.mkdir(parents=True)
    (local / "config-guide.md").write_text("Use lowercase create jobs.\n")
    cache_root = tmp_path / "cache"
    upstream = cache_root / "kube-burner" / "docs"
    upstream.mkdir(parents=True)
    (upstream / "getting-started.md").write_text("Upstream usage reference.\n")
    (upstream / "advanced.md").write_text(
        "This separate document describes the specialmode operation.\n"
    )
    git_config = cache_root / "kube-burner" / ".git" / "config"
    git_config.parent.mkdir()
    git_config.write_text(
        '[remote "origin"]\n\turl = https://gitlab.example.org/team/kube-burner.git\n'
    )

    from providers.skills.repo_cache import RepoCache

    cache = RepoCache(cache_dir=cache_root)
    resolver = OrganizationSkillResolver.from_instance_config({})

    class Provider:
        organization_resolver = resolver

    bootstrap = json.loads(
        asyncio.run(
            skill_context_gateway(
                Provider(),
                ticket_id="TEST-KUBE-BURNER-CONTEXT",
                agent_name="benchmark-agent",
                phase="benchmark",
                subject="harness/kube-burner",
                local_skills_dir=skills,
                repo_cache=cache,
            )
        )
    )

    assert bootstrap["found"] is True
    assert {(item["scope"], item["status"]) for item in bootstrap["sources"]} >= {
        ("local", "available"),
        ("upstream", "available"),
    }
    local_doc = next(
        item for item in bootstrap["documents"] if item["scope"] == "local"
    )
    upstream_doc = next(
        item
        for item in bootstrap["documents"]
        if item["scope"] == "upstream" and item["path"] == "docs/getting-started.md"
    )
    assert local_doc["source"] == "agentic-perf"
    assert upstream_doc["source"] == "https://gitlab.example.org/team/kube-burner.git"
    assert upstream_doc["provenance_status"] == "configured-cache-origin-unverified"

    upstream_read = json.loads(
        asyncio.run(
            skill_context_gateway(
                Provider(),
                ticket_id="TEST-KUBE-BURNER-CONTEXT",
                agent_name="benchmark-agent",
                phase="benchmark",
                subject="harness/kube-burner",
                operation="read",
                ref=upstream_doc["ref"],
                local_skills_dir=skills,
                repo_cache=cache,
            )
        )
    )
    assert upstream_read["document"]["scope"] == "upstream"
    assert "Upstream usage reference" in upstream_read["document"]["content"]
    upstream_search = json.loads(
        asyncio.run(
            skill_context_gateway(
                Provider(),
                ticket_id="TEST-KUBE-BURNER-CONTEXT",
                agent_name="benchmark-agent",
                phase="benchmark",
                subject="harness/kube-burner",
                operation="search",
                query="specialmode",
                from_ref=upstream_doc["ref"],
                local_skills_dir=skills,
                repo_cache=cache,
            )
        )
    )
    assert upstream_search["found"] is True
    assert upstream_search["sources"][0]["scope"] == "upstream"
    assert [item["path"] for item in upstream_search["sources"][0]["results"]] == [
        "docs/advanced.md"
    ]
    default_upstream_search = json.loads(
        asyncio.run(
            skill_context_gateway(
                Provider(),
                ticket_id="TEST-KUBE-BURNER-CONTEXT",
                agent_name="benchmark-agent",
                phase="benchmark",
                subject="harness/kube-burner",
                operation="search",
                query="specialmode",
                local_skills_dir=skills,
                repo_cache=cache,
            )
        )
    )
    assert default_upstream_search["found"] is True
    assert [item["scope"] for item in default_upstream_search["sources"]] == [
        "local",
        "upstream",
    ]
    assert default_upstream_search["sources"][1]["found"] is True
    upstream_only_search = json.loads(
        asyncio.run(
            skill_context_gateway(
                Provider(),
                ticket_id="TEST-KUBE-BURNER-CONTEXT",
                agent_name="benchmark-agent",
                phase="benchmark",
                subject="harness/kube-burner",
                operation="search",
                query="specialmode",
                local_skills_dir=tmp_path / "no-local-skills",
                repo_cache=cache,
            )
        )
    )
    assert upstream_only_search["found"] is True
    assert any(
        item["scope"] == "upstream" and item["found"]
        for item in upstream_only_search["sources"]
    )

    unlisted_upstream_read = json.loads(
        asyncio.run(
            skill_context_gateway(
                Provider(),
                ticket_id="TEST-KUBE-BURNER-CONTEXT",
                agent_name="benchmark-agent",
                phase="benchmark",
                subject="harness/kube-burner",
                operation="read",
                ref="skill://upstream/harness/kube-burner/README.md",
                local_skills_dir=skills,
                repo_cache=cache,
            )
        )
    )
    assert unlisted_upstream_read["found"] is False
    assert unlisted_upstream_read["reason"] == "invalid_document"


def test_k8s_netperf_gateway_exposes_local_and_cached_upstream_docs(
    tmp_path: Path,
) -> None:
    skills = tmp_path / "skills"
    local = skills / "k8s-netperf"
    local.mkdir(parents=True)
    (local / "config-guide.md").write_text("Local execution contract.\n")
    cache_root = tmp_path / "cache"
    upstream = cache_root / "k8s-netperf" / "docs"
    upstream.mkdir(parents=True)
    (upstream / "README.md").write_text("Upstream k8s-netperf reference.\n")
    git_config = cache_root / "k8s-netperf" / ".git" / "config"
    git_config.parent.mkdir()
    git_config.write_text(
        '[remote "origin"]\n\turl = https://github.com/cloud-bulldozer/k8s-netperf.git\n'
    )

    from providers.skills.repo_cache import RepoCache

    cache = RepoCache(cache_dir=cache_root)
    resolver = OrganizationSkillResolver.from_instance_config({})

    class Provider:
        organization_resolver = resolver

    bootstrap = json.loads(
        asyncio.run(
            skill_context_gateway(
                Provider(),
                ticket_id="TEST-K8S-NETPERF-CONTEXT",
                agent_name="benchmark-agent",
                phase="benchmark",
                subject="harness/k8s-netperf",
                local_skills_dir=skills,
                repo_cache=cache,
            )
        )
    )

    assert bootstrap["found"] is True
    assert {(item["scope"], item["status"]) for item in bootstrap["sources"]} >= {
        ("local", "available"),
        ("upstream", "available"),
    }
    assert any(
        item["path"] == "config-guide.md" and item["scope"] == "local"
        for item in bootstrap["documents"]
    )
    upstream_doc = next(
        item for item in bootstrap["documents"] if item["scope"] == "upstream"
    )
    assert (
        upstream_doc["source"] == "https://github.com/cloud-bulldozer/k8s-netperf.git"
    )


def test_repo_cache_origin_redacts_credentials(tmp_path: Path) -> None:
    from providers.skills.repo_cache import RepoCache

    git_config = tmp_path / "kube-burner" / ".git" / "config"
    git_config.parent.mkdir(parents=True)
    git_config.write_text(
        '[remote "origin"]\n'
        "\turl = https://reader:top-secret@git.example.org/group/repo.git\n"
    )

    assert RepoCache(cache_dir=tmp_path).get_origin("kube-burner") == (
        "https://git.example.org/group/repo.git"
    )


@pytest.mark.asyncio
async def test_kube_burner_provenance_uses_harness_repos_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agents.server_utils as server_utils
    import providers.skills.repo_cache as repo_cache_module

    override = "https://gitlab.example.org/perf/kube-burner-fork.git"
    cache_root = tmp_path / "cache"

    class OfflineRepoCache(repo_cache_module.RepoCache):
        requested: dict[str, str] = {}

        def __init__(self):
            super().__init__(cache_dir=cache_root)

        async def ensure_repo(self, name: str, url: str) -> Path:
            self.requested[name] = url
            if name == "kube-burner":
                repo = cache_root / name
                (repo / "docs").mkdir(parents=True)
                (repo / ".git").mkdir()
                (repo / "docs" / "usage.md").write_text("Fork usage docs.\n")
                (repo / ".git" / "config").write_text(
                    f'[remote "origin"]\n\turl = {url}\n'
                )
            return cache_root / name

    monkeypatch.setattr(repo_cache_module, "RepoCache", OfflineRepoCache)
    monkeypatch.setenv("HARNESS_REPOS", json.dumps({"kube-burner": override}))
    cache = await server_utils.build_repo_cache()

    assert cache.requested["kube-burner"] == override
    assert cache.get_origin("kube-burner") == override

    resolver = OrganizationSkillResolver.from_instance_config({})

    class Provider:
        organization_resolver = resolver

    result = json.loads(
        await skill_context_gateway(
            Provider(),
            ticket_id="TEST-KUBE-BURNER-OVERRIDE",
            agent_name="benchmark-agent",
            phase="benchmark",
            subject="harness/kube-burner",
            repo_cache=cache,
        )
    )
    upstream = next(
        source for source in result["sources"] if source["scope"] == "upstream"
    )
    assert upstream["source"] == override
    assert upstream["provenance_status"] == "configured-cache-origin-unverified"


@pytest.mark.asyncio
async def test_docs_only_org_subject_preserves_other_harness_config_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "organization"
    _write_package(
        repository,
        namespace="harness",
        name="k8s-netperf",
        subject="harness/k8s-netperf",
        instruction="Organization context about k8s-netperf.",
    )
    resolver = _resolver(("org", repository))
    assert resolver.has_subject("harness/k8s-netperf")
    assert not resolver.has_runtime_config("harness/k8s-netperf")
    assert not resolver.uses_organization_config("harness/k8s-netperf")

    private_dir = tmp_path / "private-skills"
    private_dir.mkdir()
    (private_dir / "k8s-netperf.json").write_text(
        json.dumps({"execution": {"endpoint_user": "benchmark-user"}})
    )
    provider = MultiHarnessSkillProvider(
        {"k8s-netperf": K8sNetperfSkillProvider()},
        private=PrivateSkillProvider(private_dir, resolver=resolver),
    )

    import agents.benchmark.server as benchmark_server

    monkeypatch.setattr(benchmark_server, "_ensure_init", lambda: asyncio.sleep(0))
    monkeypatch.setattr(benchmark_server, "_skill_provider", provider)
    result = json.loads(await benchmark_server.get_execution_config("k8s-netperf"))

    assert result["found"] is True
    assert result["endpoint_type"] == "kube"
    assert result["endpoint_user"] == "benchmark-user"
    assert result["run_file_format"] == "yaml_config"


@pytest.mark.asyncio
async def test_resource_service_config_is_gateway_only_and_not_model_readable(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "organization"
    config = {
        "connection": {
            "api_host": "quads-internal.example.test",
            "api_scheme": "https",
        }
    }
    _write_package(
        repository,
        namespace="resource",
        name="quads",
        subject="resource/quads",
        instruction="Use the QUADS resource provider for managed bare-metal hosts.",
        runtime_config=config,
    )
    resolver = _resolver(("org-operations", repository))

    class Provider:
        organization_resolver = resolver

    result = json.loads(
        await skill_context_gateway(
            Provider(),
            ticket_id="TEST-QUADS-CONFIG",
            agent_name="resource-agent",
            phase="resource",
            subject="resource/quads",
        )
    )

    assert result["found"] is True
    assert result["context_conflicts"]["runtime_configuration_conflict"] is False
    assert "quads-internal.example.test" not in json.dumps(result)
    assert resolver.get_runtime_config("resource/quads") == config
