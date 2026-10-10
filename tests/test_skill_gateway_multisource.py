from __future__ import annotations

import asyncio
import json
from pathlib import Path

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
async def test_resource_gateway_tool_does_not_initialize_cloud_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agents.resource.server as resource_server

    skills = tmp_path / "skills"
    aws = skills / "resource" / "aws"
    aws.mkdir(parents=True)
    (aws / "SKILL.md").write_text("AWS guidance stays context only.\n")
    resolver = OrganizationSkillResolver.from_instance_config({})

    class Provider:
        organization_resolver = resolver

    async def get_skill_provider():
        return Provider()

    async def unexpected_resource_init():
        raise AssertionError("context retrieval must not initialize providers")

    monkeypatch.setattr(resource_server, "_get_skill_provider", get_skill_provider)
    monkeypatch.setattr(resource_server, "_ensure_init", unexpected_resource_init)
    monkeypatch.setattr(resource_server, "SKILLS_DIR", skills)
    monkeypatch.setenv("TICKET_ID", "TEST-AWS-CONTEXT")

    result = json.loads(await resource_server.get_skill_context(subject="resource/aws"))

    assert result["found"] is True
    assert any(item["scope"] == "local" for item in result["sources"])


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
        item for item in bootstrap["documents"] if item["scope"] == "upstream"
    )
    assert local_doc["source"] == "agentic-perf"
    assert upstream_doc["source"].endswith("kube-burner.git")

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
                query="usage",
                from_ref=upstream_doc["ref"],
                local_skills_dir=skills,
                repo_cache=cache,
            )
        )
    )
    assert upstream_search["found"] is True
    assert upstream_search["sources"][0]["scope"] == "upstream"

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
