from __future__ import annotations

import json
import os

import pytest

from providers.skills.crucible import (
    CrucibleContextGateway as CrucibleSkillProvider,
)
from providers.skills.crucible import (
    CrucibleSourceResolver,
    select_crucible_context,
)
from providers.skills.local_context import LocalContextSource

CRUCIBLE_HOME = os.environ.get("CRUCIBLE_HOME", "/opt/crucible")
HAS_CRUCIBLE = os.path.isdir(os.path.join(CRUCIBLE_HOME, "subprojects", "benchmarks"))


@pytest.fixture
def provider() -> CrucibleSkillProvider:
    return CrucibleSkillProvider(CRUCIBLE_HOME)


@pytest.mark.skipif(not HAS_CRUCIBLE, reason="CRUCIBLE_HOME not available")
@pytest.mark.asyncio
async def test_harness_field_set(provider: CrucibleSkillProvider):
    benchmarks = await provider.list_benchmarks()
    assert len(benchmarks) > 0
    for b in benchmarks:
        assert b.harness == "crucible"


@pytest.mark.skipif(not HAS_CRUCIBLE, reason="CRUCIBLE_HOME not available")
@pytest.mark.asyncio
async def test_generate_runfile_with_endpoints(provider: CrucibleSkillProvider):
    result = await provider.generate_runfile(
        "fio",
        {
            "endpoints": [{"host": "10.0.0.1", "roles": ["client"]}],
            "userenv": "alma8",
            "osruntime": "podman",
        },
    )
    template = result.template
    assert "harness" not in template
    assert "endpoints" in template
    ep = template["endpoints"][0]
    assert ep["type"] == "remotehosts"
    assert ep["settings"]["userenv"] == "alma8"
    assert ep["remotes"][0]["config"]["host"] == "10.0.0.1"
    assert ep["remotes"][0]["config"]["settings"]["osruntime"] == "podman"


@pytest.mark.skipif(not HAS_CRUCIBLE, reason="CRUCIBLE_HOME not available")
@pytest.mark.asyncio
async def test_generate_runfile_with_tags(provider: CrucibleSkillProvider):
    result = await provider.generate_runfile(
        "fio",
        {
            "endpoints": [{"host": "10.0.0.1", "roles": ["client"]}],
            "tags": {"environment": "test", "ticket": "PERF-100"},
        },
    )
    assert result.template["tags"] == {"environment": "test", "ticket": "PERF-100"}


@pytest.mark.asyncio
async def test_generate_runfile_no_endpoints():
    # Isolate run-file generation from examples in the developer's Crucible
    # checkout, which may carry ticket-specific tags of its own.
    provider = CrucibleSkillProvider("/nonexistent")
    result = await provider.generate_runfile("fio", {})
    assert "endpoints" not in result.template
    assert result.template["tags"] == {}


@pytest.mark.asyncio
async def test_generate_runfile_defaults_tags_to_object():
    provider = CrucibleSkillProvider("/nonexistent")

    result = await provider.generate_runfile("uperf", {})

    assert result.template["tags"] == {}


@pytest.mark.asyncio
async def test_validate_runfile_requires_object_tags_without_controller_schema():
    provider = CrucibleSkillProvider("/nonexistent")

    missing = await provider.validate_runfile({"benchmarks": []})
    malformed = await provider.validate_runfile({"benchmarks": [], "tags": ["network"]})

    assert missing == {
        "valid": False,
        "errors": ["missing required top-level key: tags"],
    }
    assert malformed == {
        "valid": False,
        "errors": ["top-level tags must be an object"],
    }


@pytest.mark.asyncio
async def test_get_runfile_schema_requires_tags_without_mutating_source(monkeypatch):
    provider = CrucibleSkillProvider("/nonexistent")
    source_schema = {
        "type": "object",
        "properties": {"benchmarks": {"type": "array"}},
        "required": ["benchmarks"],
    }
    monkeypatch.setattr(provider, "_load_schema", lambda: source_schema)

    schema = await provider.get_runfile_schema()

    assert schema["properties"]["tags"] == {"type": "object", "default": {}}
    assert schema["required"] == ["benchmarks", "tags"]
    assert "tags" not in source_schema["properties"]
    assert source_schema["required"] == ["benchmarks"]


@pytest.mark.asyncio
async def test_validate_runfile_uses_required_tags_schema_copy(monkeypatch):
    provider = CrucibleSkillProvider("/nonexistent")
    source_schema = {
        "type": "object",
        "properties": {"benchmarks": {"type": "array"}},
        "required": ["benchmarks"],
        "additionalProperties": False,
    }
    monkeypatch.setattr(provider, "_load_schema", lambda: source_schema)

    validation = await provider.validate_runfile({"benchmarks": [], "tags": {}})

    assert validation["valid"] is True
    assert source_schema["required"] == ["benchmarks"]
    assert "tags" not in source_schema["properties"]


@pytest.mark.skipif(not HAS_CRUCIBLE, reason="CRUCIBLE_HOME not available")
@pytest.mark.asyncio
async def test_get_runfile_schema(provider: CrucibleSkillProvider):
    schema = await provider.get_runfile_schema()
    assert schema is not None
    assert "properties" in schema
    assert "benchmarks" in schema["properties"]


@pytest.mark.skipif(not HAS_CRUCIBLE, reason="CRUCIBLE_HOME not available")
@pytest.mark.asyncio
async def test_get_benchmark_params(provider: CrucibleSkillProvider):
    params = await provider.get_benchmark_params("uperf")
    if params is not None:
        assert isinstance(params, dict)


@pytest.mark.skipif(not HAS_CRUCIBLE, reason="CRUCIBLE_HOME not available")
@pytest.mark.asyncio
async def test_get_example_runfile(provider: CrucibleSkillProvider):
    example = await provider.get_example_runfile("fio")
    if example is not None:
        assert "benchmarks" in example


@pytest.mark.asyncio
async def test_get_runfile_schema_missing():
    provider = CrucibleSkillProvider("/nonexistent")
    schema = await provider.get_runfile_schema()
    assert schema is None


@pytest.mark.asyncio
async def test_get_benchmark_params_nonexistent():
    provider = CrucibleSkillProvider("/nonexistent")
    params = await provider.get_benchmark_params("fio")
    assert params is None


@pytest.mark.asyncio
async def test_get_tool_params_nonexistent():
    provider = CrucibleSkillProvider("/nonexistent")
    params = await provider.get_tool_params("sysstat")
    assert params is None


@pytest.mark.asyncio
async def test_get_tool_metadata_nonexistent():
    provider = CrucibleSkillProvider("/nonexistent")
    meta = await provider.get_tool_metadata("sysstat")
    assert meta is None


@pytest.mark.asyncio
async def test_list_tools_nonexistent():
    provider = CrucibleSkillProvider("/nonexistent")
    tools = await provider.list_tools()
    assert tools == []


@pytest.mark.asyncio
async def test_tool_params_and_metadata_discovery(tmp_path):
    tools_dir = tmp_path / "subprojects" / "tools" / "sysstat"
    tools_dir.mkdir(parents=True)
    multiplex_json = tools_dir / "multiplex.json"
    multiplex_json.write_text('{"presets": {"defaults": {"interval": "3"}}}')
    metadata_json = tools_dir / "tool-metadata.json"
    metadata_json.write_text('{"description": "sysstat profiler"}')

    provider = CrucibleSkillProvider(tmp_path)
    tools = await provider.list_tools()
    assert "sysstat" in tools

    params = await provider.get_tool_params("sysstat")
    assert params == {"presets": {"defaults": {"interval": "3"}}}

    meta = await provider.get_tool_metadata("sysstat")
    assert meta == {"description": "sysstat profiler"}


@pytest.mark.asyncio
async def test_source_catalog_discovers_benchmark_repositories(tmp_path):
    """The core Crucible repo is an index; benchmark metadata lives elsewhere."""
    source = tmp_path / "crucible"
    (source / "config").mkdir(parents=True)
    (source / "config" / "repos.json").write_text(
        json.dumps(
            {
                "official": [
                    {
                        "name": "perftest",
                        "type": "benchmark",
                        "repository": "https://example.test/bench-perftest",
                        "checkout": {"mode": "follow", "target": "main"},
                    }
                ],
                "unofficial": [],
            }
        )
    )
    bench = tmp_path / "bench-perftest"
    bench.mkdir()
    (bench / "multiplex.json").write_text(
        '{"presets": {"short": [{"arg": "duration", "vals": ["5"]}]}}'
    )
    (bench / "rickshaw.json").write_text(
        '{"benchmark": "perftest", "client": {}, "server": {}}'
    )

    provider = CrucibleSkillProvider(
        tmp_path / "missing-controller", source_repo=source
    )
    benchmarks = await provider.list_benchmarks()
    perftest = next(b for b in benchmarks if b.name == "perftest")
    assert perftest.roles == ["client", "server"]
    assert perftest.min_hosts == 2
    assert perftest.supported_params["presets"]["short"]
    assert perftest.source["repository"] == "https://example.test/bench-perftest"
    assert perftest.source["ref"] == "main"


@pytest.mark.asyncio
async def test_source_catalog_exact_match_and_no_generic_rdma_fallback(tmp_path):
    source = tmp_path / "crucible"
    (source / "config").mkdir(parents=True)
    (source / "config" / "repos.json").write_text(
        '{"official": [{"name": "perftest", "type": "benchmark", '
        '"repository": "https://example.test/bench-perftest", '
        '"checkout": {"mode": "follow", "target": "main"}}], '
        '"unofficial": []}'
    )
    provider = CrucibleSkillProvider(
        tmp_path / "missing-controller", source_repo=source
    )

    assert (
        await provider.resolve_benchmark(
            {"description": "run perftest for RDMA throughput"}
        )
        == "perftest"
    )

    without_source = CrucibleSkillProvider(tmp_path / "missing-controller")
    assert (
        await without_source.resolve_benchmark(
            {"description": "run ib_write_bw for RDMA throughput"}
        )
        is None
    )


@pytest.mark.asyncio
async def test_benchmark_context_retrieves_allowlisted_root_files(tmp_path):
    source = tmp_path / "crucible"
    (source / "config").mkdir(parents=True)
    (source / "config" / "repos.json").write_text(
        json.dumps(
            {
                "official": [
                    {
                        "name": "perftest",
                        "type": "benchmark",
                        "repository": "https://example.test/bench-perftest",
                        "checkout": {"mode": "follow", "target": "main"},
                    }
                ]
            }
        )
    )
    benchmark_repo = tmp_path / "fetched-perftest"
    benchmark_repo.mkdir()
    (benchmark_repo / "AGENTS.md").write_text("Use role=all for ifname.\n")
    (benchmark_repo / "multiplex.json").write_text('{"presets": {}}')
    (benchmark_repo / "outside.txt").write_text("must not be read")

    class Cache:
        def get_path(self, name):
            if name == "crucible-benchmark-perftest":
                return benchmark_repo
            return None

        def ensure_repo(self, name, url):
            assert name == "crucible-benchmark-perftest"
            assert url == "https://example.test/bench-perftest"
            return benchmark_repo

    provider = CrucibleSkillProvider(
        tmp_path / "missing-controller", source_repo=source, repo_cache=Cache()
    )
    result = await provider.get_benchmark_context("perftest")

    assert result["found"] is True
    assert result["effective_source"] == "github"
    assert result["repository"] == "https://example.test/bench-perftest"
    assert result["ref"] == "main"
    assert result["files"] == ["AGENTS.md", "multiplex.json"]
    assert result["context"]["AGENTS.md"].startswith("Use role=all")
    assert "outside.txt" not in result["context"]
    assert "README.md" in result["missing_files"]


@pytest.mark.asyncio
async def test_benchmark_context_reports_catalog_and_checkout_failures(tmp_path):
    source = tmp_path / "crucible"
    (source / "config").mkdir(parents=True)
    (source / "config" / "repos.json").write_text(
        '{"official": [{"name": "fio", "type": "benchmark", '
        '"repository": "https://example.test/bench-fio"}]}'
    )

    class Cache:
        def get_path(self, name):
            return None

        def ensure_repo(self, name, url):
            return tmp_path / "not-created"

    provider = CrucibleSkillProvider(
        tmp_path / "missing-controller", source_repo=source, repo_cache=Cache()
    )
    missing = await provider.get_benchmark_context("fio")
    unknown = await provider.get_benchmark_context("not-in-catalog")

    assert missing["found"] is False
    assert missing["reason"] == "benchmark_repository_unavailable"
    assert unknown == {
        "found": False,
        "benchmark": "not-in-catalog",
        "effective_source": None,
        "reason": "benchmark_not_in_catalog",
    }


@pytest.mark.asyncio
async def test_benchmark_context_mcp_tool_delegates_to_crucible_provider(
    tmp_path, monkeypatch
):
    source = tmp_path / "crucible"
    (source / "config").mkdir(parents=True)
    (source / "config" / "repos.json").write_text(
        '{"official": [{"name": "fio", "type": "benchmark", '
        '"repository": "https://example.test/bench-fio"}]}'
    )

    class Cache:
        def get_path(self, name):
            if name == "crucible-benchmark-fio":
                repo = tmp_path / "bench-fio"
                repo.mkdir()
                (repo / "README.md").write_text("fio guidance")
                return repo
            return None

        def ensure_repo(self, name, url):
            repo = tmp_path / "bench-fio"
            repo.mkdir()
            (repo / "README.md").write_text("fio guidance")
            return repo

    provider = CrucibleSkillProvider(
        tmp_path / "missing-controller", source_repo=source, repo_cache=Cache()
    )
    import agents.benchmark.server as server
    import paths

    monkeypatch.setattr(server, "_crucible_context", provider)
    monkeypatch.setattr(server, "_initialized", True)
    monkeypatch.setattr(paths, "TICKET_DIR", tmp_path / "tickets")
    monkeypatch.setenv("TICKET_ID", "PERF-CONTEXT")
    result = json.loads(await server.get_crucible_benchmark_context("fio"))

    assert result["found"] is True
    assert result["context"]["README.md"] == "fio guidance"
    assert result["context_manifest"]["document_count"] >= 1
    assert all("source" not in item for item in result["context_manifest"]["documents"])
    assert (
        tmp_path
        / "tickets"
        / "PERF-CONTEXT"
        / "workspace"
        / "context"
        / "sources"
        / "github"
        / "benchmarks"
        / "fio"
        / "README.md"
    ).read_text() == "fio guidance"


@pytest.mark.asyncio
async def test_benchmark_context_uses_controller_snapshot_when_policy_allows(
    tmp_path, monkeypatch
):
    source = tmp_path / "crucible"
    (source / "config").mkdir(parents=True)
    (source / "config" / "repos.json").write_text(
        '{"official": [{"name": "fio", "type": "benchmark", '
        '"repository": "https://example.test/bench-fio"}]}'
    )

    class Cache:
        def get_path(self, name):
            if name == "crucible-benchmark-fio":
                repo = tmp_path / "bench-fio"
                repo.mkdir(exist_ok=True)
                (repo / "README.md").write_text("github guidance")
                return repo
            return None

        def ensure_repo(self, name, url):
            repo = tmp_path / "bench-fio"
            repo.mkdir()
            (repo / "README.md").write_text("github guidance")
            return repo

    provider = CrucibleSkillProvider(
        tmp_path / "missing-controller", source_repo=source, repo_cache=Cache()
    )
    import agents.benchmark.server as server
    import paths
    from providers.workspace.manager import WorkspaceManager

    monkeypatch.setattr(paths, "TICKET_DIR", tmp_path / "tickets")
    monkeypatch.setenv("TICKET_ID", "PERF-CONTROLLER")
    manager = WorkspaceManager(ticket_id="PERF-CONTROLLER")
    manager.save_source_snapshot(
        "controller",
        {"commit": "controller-pin"},
        {"README.md": "controller guidance"},
        benchmark="fio",
    )
    monkeypatch.setattr(server, "_crucible_context", provider)
    monkeypatch.setattr(server, "_initialized", True)
    monkeypatch.setattr(
        server,
        "_ticket",
        {
            "custom_fields": {
                "crucible_update_policy": "no_update",
                "crucible_controller_context": {
                    "identified": True,
                    "reachable": True,
                    "crucible_installed": True,
                },
            }
        },
    )

    result = json.loads(await server.get_crucible_benchmark_context("fio"))

    assert result["context"]["README.md"] == "controller guidance"
    manifest = json.loads(
        (manager.workspace_dir / "context/effective-context.json").read_text()
    )
    assert "effective_source" not in manifest
    assert "sources" not in manifest
    assert "workspace_refs" not in manifest


@pytest.mark.asyncio
async def test_crucible_context_gateway_discovers_namespaces_and_reads_safe_paths(
    tmp_path,
):
    source = tmp_path / "crucible"
    (source / "config").mkdir(parents=True)
    (source / "docs").mkdir()
    (source / "docs" / "renamed-run-guide.md").write_text("run guidance")
    (source / "config" / "repos.json").write_text(
        '{"official": [{"name": "fio", "type": "benchmark", '
        '"repository": "https://example.test/bench-fio"}]}'
    )

    class Cache:
        def get_path(self, name):
            if name == "crucible-benchmark-fio":
                repo = tmp_path / "bench-fio"
                (repo / "docs").mkdir(parents=True, exist_ok=True)
                (repo / "docs" / "new-semantics.md").write_text("benchmark semantics")
                return repo
            return None

        def ensure_repo(self, name, url):
            repo = tmp_path / "bench-fio"
            (repo / "docs").mkdir(parents=True, exist_ok=True)
            (repo / "docs" / "new-semantics.md").write_text("benchmark semantics")
            return repo

    provider = CrucibleSkillProvider(
        tmp_path / "missing-controller",
        source_repo=source,
        repo_cache=Cache(),
        source_provenance={"commit": "core-pin", "ref": "main"},
    )
    listing = await provider.get_crucible_context(
        "fio", operation="list", subject_area="all"
    )
    refs = {item["path"] for item in listing["documents"]}
    assert "core/docs/renamed-run-guide.md" in refs
    assert "benchmark/fio/docs/new-semantics.md" in refs
    assert all(item["path"] == item["ref"] for item in listing["documents"])
    assert all("provenance" in item for item in listing["documents"])
    multi = await provider.get_crucible_context(
        "fio", operation="list", subject_area=["run-file", "benchmark"]
    )
    assert "benchmark/fio/docs/new-semantics.md" in {
        item["path"] for item in multi["documents"]
    }
    comma_compatible = await provider.get_crucible_context(
        "fio", operation="list", subject_area="run-file,benchmark"
    )
    assert comma_compatible["documents"] == multi["documents"]

    document = await provider.get_crucible_context(
        "fio",
        operation="read",
        namespace="benchmark/fio",
        path=next(
            item["path"]
            for item in listing["documents"]
            if item["path"].endswith("new-semantics.md")
        ),
    )
    assert document["document"]["content"] == "benchmark semantics"
    unsafe = await provider.get_crucible_context(
        "fio", operation="read", namespace="core", path="../secret.txt"
    )
    assert unsafe["found"] is False


@pytest.mark.asyncio
async def test_manifest_local_context_is_filtered_and_provenance_is_returned(tmp_path):
    source = tmp_path / "crucible"
    (source / "config").mkdir(parents=True)
    (source / "config" / "repos.json").write_text("{}")
    (tmp_path / "skills" / "crucible").mkdir(parents=True)
    (tmp_path / "skills" / "crucible" / "uperf-run-file.md").write_text(
        "uperf local guidance"
    )
    (tmp_path / "skills" / "crucible" / "perftest.md").write_text(
        "perftest local guidance"
    )
    manifest = tmp_path / "skills" / "context-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "entries": [
                    {
                        "id": "uperf-overlay",
                        "path": "skills/crucible/uperf-run-file.md",
                        "harness": "crucible",
                        "benchmark": "uperf",
                        "phase": "benchmark",
                        "agent": "benchmark-agent",
                        "subjects": ["run-file"],
                        "provenance": {"reason": "test overlay"},
                    },
                    {
                        "id": "perftest-overlay",
                        "path": "skills/crucible/perftest.md",
                        "harness": "crucible",
                        "benchmark": "perftest",
                        "phase": "benchmark",
                        "agent": "benchmark-agent",
                        "subjects": ["run-file"],
                    },
                ],
            }
        )
    )
    provider = CrucibleSkillProvider(
        tmp_path / "controller",
        source_repo=source,
        local_context_source=LocalContextSource(manifest, root=tmp_path),
    )

    uperf = await provider.get_crucible_context(
        "uperf",
        operation="list",
        namespace="benchmark/uperf",
        subject_area="run-file",
        phase="benchmark",
        agent="benchmark-agent",
    )
    local = [item for item in uperf["documents"] if item["source"] == "local"]
    assert [item["path"] for item in local] == [
        "local/skills/crucible/uperf-run-file.md"
    ]
    assert local[0]["provenance"]["entry_id"] == "uperf-overlay"
    assert local[0]["provenance"]["reason"] == "test overlay"

    read = await provider.get_crucible_context(
        "uperf",
        operation="read",
        namespace="benchmark/uperf",
        path="local/skills/crucible/uperf-run-file.md",
        phase="benchmark",
        agent="benchmark-agent",
    )
    assert read["document"]["content"] == "uperf local guidance"

    perftest = await provider.get_crucible_context(
        "perftest",
        operation="list",
        namespace="benchmark/perftest",
        subject_area="run-file",
        phase="benchmark",
        agent="benchmark-agent",
    )
    assert "local/skills/crucible/uperf-run-file.md" not in {
        item["path"] for item in perftest["documents"]
    }
    assert "local/skills/crucible/perftest.md" in {
        item["path"] for item in perftest["documents"]
    }

    wrong_phase = await provider.get_crucible_context(
        "uperf",
        operation="list",
        namespace="benchmark/uperf",
        phase="review",
        agent="benchmark-agent",
    )
    assert "local/skills/crucible/uperf-run-file.md" not in {
        item["path"] for item in wrong_phase["documents"]
    }

    wrong_agent = await provider.get_crucible_context(
        "uperf",
        operation="list",
        namespace="benchmark/uperf",
        phase="benchmark",
        agent="review-agent",
    )
    assert "local/skills/crucible/uperf-run-file.md" not in {
        item["path"] for item in wrong_agent["documents"]
    }

    wrong_subject = await provider.get_crucible_context(
        "uperf",
        operation="list",
        namespace="benchmark/uperf",
        subject_area="results",
        phase="benchmark",
        agent="benchmark-agent",
    )
    wrong_subject_doc = next(
        item
        for item in wrong_subject["documents"]
        if item["path"] == "local/skills/crucible/uperf-run-file.md"
    )
    assert wrong_subject_doc["subject_match"] is False


@pytest.mark.asyncio
async def test_local_context_gateway_persists_supplemental_source(
    tmp_path, monkeypatch
):
    source = tmp_path / "crucible"
    (source / "config").mkdir(parents=True)
    (source / "config" / "repos.json").write_text("{}")
    (tmp_path / "skills" / "crucible").mkdir(parents=True)
    (tmp_path / "skills" / "crucible" / "uperf-run-file.md").write_text(
        "uperf local guidance"
    )
    manifest = tmp_path / "skills" / "context-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "id": "uperf-overlay",
                        "path": "skills/crucible/uperf-run-file.md",
                        "harness": "crucible",
                        "benchmark": "uperf",
                        "phase": "benchmark",
                        "agent": "benchmark-agent",
                    }
                ]
            }
        )
    )
    provider = CrucibleSkillProvider(
        source,
        source_repo=source,
        local_context_source=LocalContextSource(manifest, root=tmp_path),
    )
    import paths
    from agents.server_utils import crucible_context_gateway

    monkeypatch.setattr(paths, "TICKET_DIR", tmp_path / "tickets")
    result = json.loads(
        await crucible_context_gateway(
            provider,
            ticket_id="PERF-LOCAL-GATEWAY",
            agent_name="benchmark-agent",
            phase="benchmark",
            benchmark="uperf",
            operation="read",
            namespace="benchmark/uperf",
            path="local/skills/crucible/uperf-run-file.md",
        )
    )
    assert "source" not in result["document"]
    assert "context_manifest" in result
    saved = (
        tmp_path
        / "tickets"
        / "PERF-LOCAL-GATEWAY"
        / "workspace"
        / "context"
        / "sources"
        / "local"
        / "benchmarks"
        / "uperf"
        / "skills"
        / "crucible"
        / "uperf-run-file.md"
    )
    assert saved.read_text() == "uperf local guidance"


@pytest.mark.asyncio
async def test_benchmark_metadata_is_available_via_context_without_legacy_tools(
    tmp_path, monkeypatch
):
    source = tmp_path / "crucible"
    (source / "config").mkdir(parents=True)
    (source / "config" / "repos.json").write_text(
        '{"official": [{"name": "fio", "type": "benchmark", '
        '"repository": "https://example.test/bench-fio"}]}'
    )

    class Cache:
        def get_path(self, name):
            if name == "crucible-benchmark-fio":
                repo = tmp_path / "bench-fio"
                repo.mkdir(exist_ok=True)
                (repo / "multiplex.json").write_text('{"presets": {"default": {}}}')
                (repo / "rickshaw.json").write_text(
                    '{"benchmark": "fio", "client": {}}'
                )
                return repo
            return None

        def ensure_repo(self, name, url):
            repo = tmp_path / "bench-fio"
            repo.mkdir(exist_ok=True)
            (repo / "multiplex.json").write_text('{"presets": {"default": {}}}')
            (repo / "rickshaw.json").write_text('{"benchmark": "fio", "client": {}}')
            return repo

    async def unavailable(*args, **kwargs):
        raise AssertionError("legacy benchmark discovery tool was called")

    monkeypatch.setattr(CrucibleSkillProvider, "get_benchmark_params", unavailable)
    monkeypatch.setattr(CrucibleSkillProvider, "get_example_runfile", unavailable)
    provider = CrucibleSkillProvider(
        tmp_path / "missing-controller", source_repo=source, repo_cache=Cache()
    )

    listing = await provider.get_crucible_context(
        "fio", operation="list", namespace="benchmark/fio", subject_area="benchmark"
    )
    paths = {item["path"] for item in listing["documents"]}
    assert "benchmark/fio/multiplex.json" in paths
    assert "benchmark/fio/rickshaw.json" in paths

    for metadata_path, expected in (
        ("benchmark/fio/multiplex.json", '"default"'),
        ("benchmark/fio/rickshaw.json", '"client"'),
    ):
        document = await provider.get_crucible_context(
            "fio",
            operation="read",
            namespace="benchmark/fio",
            path=metadata_path,
            subject_area="benchmark",
        )
        assert expected in document["document"]["content"]


@pytest.mark.asyncio
async def test_benchmark_inventory_never_hides_owned_readme_by_subject(tmp_path):
    source = tmp_path / "crucible"
    (source / "config").mkdir(parents=True)
    (source / "config" / "repos.json").write_text(
        '{"official": [{"name": "perftest", "type": "benchmark", '
        '"repository": "https://example.test/bench-perftest"}]}'
    )

    class Cache:
        def get_path(self, name):
            if name == "crucible-benchmark-perftest":
                repo = tmp_path / "bench-perftest"
                (repo / "docs").mkdir(parents=True, exist_ok=True)
                (repo / "README.md").write_text(
                    "client and server use the same benchmark instance ID"
                )
                (repo / "docs" / "pairing.md").write_text("role pairing guidance")
                (repo / "multiplex.json").write_text('{"validations": {}}')
                (repo / "rickshaw.json").write_text(
                    '{"benchmark": "perftest", "client": {}, "server": {}}'
                )
                return repo
            return None

        def ensure_repo(self, name, url):
            repo = tmp_path / "bench-perftest"
            (repo / "docs").mkdir(parents=True, exist_ok=True)
            (repo / "README.md").write_text(
                "client and server use the same benchmark instance ID"
            )
            (repo / "docs" / "pairing.md").write_text("role pairing guidance")
            (repo / "multiplex.json").write_text('{"validations": {}}')
            (repo / "rickshaw.json").write_text(
                '{"benchmark": "perftest", "client": {}, "server": {}}'
            )
            return repo

    provider = CrucibleSkillProvider(
        tmp_path / "missing-controller", source_repo=source, repo_cache=Cache()
    )
    result = await provider.get_crucible_context(
        "perftest",
        operation="list",
        namespace="benchmark/perftest",
        subject_area=["run-file", "benchmark"],
    )

    paths = {item["path"] for item in result["documents"]}
    assert paths == {
        "benchmark/perftest/README.md",
        "benchmark/perftest/docs/pairing.md",
        "benchmark/perftest/multiplex.json",
        "benchmark/perftest/rickshaw.json",
    }
    readme = next(
        item for item in result["documents"] if item["path"].endswith("README.md")
    )
    assert readme["entrypoint"] is True
    assert readme["subject_match"] is False
    assert result["inventory"]["complete"] is True
    assert result["inventory"]["excluded"] == 0


def test_context_inventory_excludes_hidden_nested_documents(tmp_path):
    repo = tmp_path / "bench"
    (repo / "docs" / ".private").mkdir(parents=True)
    (repo / "docs" / "public.md").write_text("public")
    (repo / "docs" / ".private" / "secret.md").write_text("secret")

    files, exclusions = CrucibleSkillProvider._discover_context_inventory(
        repo, benchmark_namespace=True
    )

    assert files == ["docs/public.md"]
    assert exclusions == {"hidden_or_repository_internal": 1}


@pytest.mark.asyncio
async def test_gateway_materializes_inventory_for_workspace_read_and_search(
    tmp_path, monkeypatch
):
    source = tmp_path / "crucible"
    (source / "config").mkdir(parents=True)
    (source / "config" / "repos.json").write_text(
        '{"official": [{"name": "perftest", "type": "benchmark", '
        '"repository": "https://example.test/bench-perftest"}]}'
    )

    class Cache:
        def get_path(self, name):
            if name == "crucible-benchmark-perftest":
                repo = tmp_path / "bench-perftest"
                repo.mkdir(exist_ok=True)
                (repo / "README.md").write_text(
                    "client-1 and server-1 form one perftest pair"
                )
                return repo
            return None

        def ensure_repo(self, name, url):
            repo = tmp_path / "bench-perftest"
            repo.mkdir(exist_ok=True)
            (repo / "README.md").write_text(
                "client-1 and server-1 form one perftest pair"
            )
            return repo

    provider = CrucibleSkillProvider(
        tmp_path / "missing-controller", source_repo=source, repo_cache=Cache()
    )
    import paths
    from agents.server_utils import crucible_context_gateway

    monkeypatch.setattr(paths, "TICKET_DIR", tmp_path / "tickets")
    listing = json.loads(
        await crucible_context_gateway(
            provider,
            ticket_id="PERF-WORKSPACE-CONTEXT",
            agent_name="benchmark-agent",
            phase="benchmark",
            benchmark="perftest",
            operation="list",
            namespace="benchmark/perftest",
            subject_area="benchmark",
        )
    )
    readme = next(
        item for item in listing["documents"] if item["ref"].endswith("README.md")
    )
    assert "workspace_ref" not in readme

    read = json.loads(
        await crucible_context_gateway(
            provider,
            ticket_id="PERF-WORKSPACE-CONTEXT",
            agent_name="benchmark-agent",
            phase="benchmark",
            benchmark="perftest",
            operation="read",
            namespace="benchmark/perftest",
            path=readme["ref"],
            max_bytes=8,
        )
    )
    assert read["document"]["content"] == "client-1"
    assert read["document"]["size_bytes"] == len(
        b"client-1 and server-1 form one perftest pair"
    )
    assert read["document"]["next_offset_bytes"] == 8
    continued_read = json.loads(
        await crucible_context_gateway(
            provider,
            ticket_id="PERF-WORKSPACE-CONTEXT",
            agent_name="benchmark-agent",
            phase="benchmark",
            benchmark="perftest",
            operation="read",
            namespace="benchmark/perftest",
            path=readme["ref"],
            max_bytes=64,
            offset_bytes=read["document"]["next_offset_bytes"],
        )
    )
    assert read["document"]["content"] + continued_read["document"]["content"] == (
        "client-1 and server-1 form one perftest pair"
    )
    assert continued_read["document"]["next_offset_bytes"] is None

    search = json.loads(
        await crucible_context_gateway(
            provider,
            ticket_id="PERF-WORKSPACE-CONTEXT",
            agent_name="benchmark-agent",
            phase="benchmark",
            benchmark="perftest",
            operation="search",
            namespace="benchmark/perftest",
            query="server-1",
        )
    )
    assert search["total_documents_matched"] == 1
    assert search["results"][0]["ref"] == readme["ref"]


@pytest.mark.asyncio
async def test_crucible_context_gateway_persists_phase_owned_effective_manifest(
    tmp_path, monkeypatch
):
    source = tmp_path / "crucible"
    (source / "docs").mkdir(parents=True)
    (source / "docs" / "execution.md").write_text("execution")
    (source / "config" / "repos.json").parent.mkdir()
    (source / "config" / "repos.json").write_text("{}")
    provider = CrucibleSkillProvider(source, source_repo=source)
    import paths
    from agents.server_utils import crucible_context_gateway

    monkeypatch.setattr(paths, "TICKET_DIR", tmp_path / "tickets")
    monkeypatch.setattr(paths, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(paths, "TRACE_DB_PATH", tmp_path / "trace.db")
    _ = json.loads(
        await crucible_context_gateway(
            provider,
            ticket_id="PERF-GATEWAY",
            agent_name="benchmark-agent",
            phase="benchmark",
            operation="read",
            namespace="core",
            path="core/docs/execution.md",
            subject_area="execution",
        )
    )
    manifest = json.loads(
        (
            tmp_path
            / "tickets"
            / "PERF-GATEWAY"
            / "workspace"
            / "context"
            / "effective-context.json"
        ).read_text()
    )
    assert manifest["phase"] == "benchmark"
    assert manifest["audience"] == "benchmark"
    assert manifest["agent"] == "benchmark-agent"
    assert "effective_source" not in manifest
    assert "workspace_refs" not in manifest
    from providers.events import EventBus

    bus = EventBus(log_dir=tmp_path / "logs")
    try:
        events = bus.get_events("PERF-GATEWAY", limit=100)
    finally:
        bus.close()
    resolution = next(
        event for event in events if event["event_type"] == "context_resolution"
    )
    assert resolution["data"]["tool"] == "get_skill_context"
    assert resolution["data"]["details"]["effective_source"] == "github"
    assert resolution["data"]["details"]["phase"] == "benchmark"


@pytest.mark.asyncio
async def test_benchmark_gateway_prefers_controller_checkout_and_params(
    tmp_path, monkeypatch
):
    github = tmp_path / "github-crucible"
    (github / "config").mkdir(parents=True)
    (github / "docs").mkdir()
    (github / "docs" / "execution.md").write_text("github execution")
    (github / "config" / "repos.json").write_text(
        '{"official": [{"name": "perftest", "type": "benchmark", '
        '"repository": "https://example.test/bench-perftest"}]}'
    )
    controller = tmp_path / "controller"
    (controller / "config").mkdir(parents=True)
    (controller / "repos" / "bench-perftest").mkdir(parents=True)
    (controller / "config" / "repos.json").write_text(
        '{"official": [{"name": "perftest", "type": "benchmark", '
        '"repository": "https://example.test/bench-perftest"}]}'
    )
    (controller / "repos" / "bench-perftest" / "README.md").write_text(
        "controller benchmark guidance"
    )
    (controller / "repos" / "bench-perftest" / "multiplex.json").write_text(
        '{"presets": {"default": {"size": "64K"}}}'
    )

    class Cache:
        def get_path(self, name):
            return None

        def ensure_repo(self, name, url):
            repo = tmp_path / "github-bench-perftest"
            repo.mkdir(exist_ok=True)
            (repo / "README.md").write_text("github benchmark guidance")
            return repo

    provider = CrucibleSkillProvider(
        controller,
        source_repo=github,
        repo_cache=Cache(),
        source_provenance={"core_catalog_commit": "github-pin"},
    )
    result = await provider.get_crucible_context(
        "perftest",
        operation="read",
        namespace="benchmark/perftest",
        path="benchmark/perftest/README.md",
        phase="benchmark",
        controller={"identified": True, "reachable": True, "crucible_installed": True},
        update_policy="no_update",
    )
    assert result["effective_source"] == "controller"
    assert result["document"]["content"] == "controller benchmark guidance"
    assert result["document"]["source"] == "controller"
    assert any(
        item["source"] == "controller" and item["selected"]
        for item in result["sources_considered"]
    )
    assert any(
        item["source"] == "github" and item["skipped_reason"]
        for item in result["sources_considered"]
    )
    params = await provider.get_benchmark_params("perftest")
    assert params["presets"]["default"]["size"] == "64K"

    import agents.benchmark.server as server
    import paths

    monkeypatch.setattr(paths, "TICKET_DIR", tmp_path / "tickets")
    monkeypatch.setenv("TICKET_ID", "PERF-A3D7B59F")
    monkeypatch.setattr(server, "_crucible_context", provider)
    monkeypatch.setattr(server, "_initialized", True)
    monkeypatch.setattr(
        server,
        "_ticket",
        {
            "custom_fields": {
                "crucible_update_policy": "no_update",
                "crucible_controller_context": {
                    "identified": True,
                    "reachable": True,
                    "crucible_installed": True,
                },
            }
        },
    )
    mcp_result = json.loads(
        await server.get_crucible_benchmark_context(
            "perftest",
            operation="read",
            namespace="benchmark/perftest",
            path="benchmark/perftest/README.md",
        )
    )
    assert mcp_result["document"]["content"] == "controller benchmark guidance"
    assert "source" not in mcp_result["document"]
    assert "selection" not in mcp_result


@pytest.mark.asyncio
async def test_source_gateway_resolves_core_and_tool_repositories_from_catalog(
    tmp_path,
):
    source = tmp_path / "crucible"
    (source / "config").mkdir(parents=True)
    (source / "config" / "repos.json").write_text(
        json.dumps(
            {
                "official": [
                    {
                        "name": "rickshaw",
                        "type": "core",
                        "repository": "https://example.test/rickshaw",
                    },
                    {
                        "name": "sysstat",
                        "type": "tool",
                        "repository": "https://example.test/tool-sysstat",
                    },
                ]
            }
        )
    )
    (tmp_path / "rickshaw" / "docs").mkdir(parents=True)
    (tmp_path / "rickshaw" / "docs" / "execution.md").write_text(
        "core execution guidance"
    )
    (tmp_path / "tool-sysstat").mkdir()
    (tmp_path / "tool-sysstat" / "multiplex.json").write_text(
        '{"presets": {"default": []}}'
    )

    provider = CrucibleSkillProvider(source, source_repo=source)
    core = await provider.get_crucible_context(
        operation="list",
        namespace="core/rickshaw",
        phase="benchmark",
    )
    tool = await provider.get_crucible_context(
        operation="read",
        namespace="tool/sysstat",
        path="tool/sysstat/multiplex.json",
        phase="benchmark",
    )
    search = await provider.get_crucible_context(
        operation="search",
        namespace="tool/sysstat",
        query="presets",
        phase="benchmark",
    )

    assert core["inventory"]["complete"] is True
    assert "core/rickshaw/docs/execution.md" in {
        item["ref"] for item in core["documents"]
    }
    assert tool["document"]["content"] == '{"presets": {"default": []}}'
    assert search["documents"][0]["path"] == "tool/sysstat/multiplex.json"


@pytest.mark.asyncio
async def test_controller_context_refresh_snapshots_remote_install(
    tmp_path, monkeypatch
):
    import agents.benchmark.server as server
    import paths
    from providers.ssh import SSHResult
    from providers.workspace.manager import WorkspaceManager

    class FakeSSH:
        files = {
            "/opt/crucible/AGENTS.md": "Crucible layout guidance",
            "/opt/crucible/subprojects/core/config/repos.json": (
                '{"official": [{"name": "perftest", "type": "benchmark"}]}'
            ),
            "/opt/crucible/subprojects/core/docs/how-run-files-work.md": "controller run-file guidance",
            "/opt/crucible/subprojects/benchmarks/bench-perftest/multiplex.json": '{"presets": {"basic": [{"arg": "ifname"}]}}',
            "/opt/crucible/subprojects/benchmarks/bench-perftest/README.md": "controller perftest guidance",
            "/opt/crucible/subprojects/benchmarks/bench-perftest/docs/pairing.md": "client and server use the same benchmark ID",
        }

        async def run(self, host, command, **kwargs):
            if command.startswith("for path in"):
                if "/opt/crucible/AGENTS.md" in command:
                    return SSHResult("/opt/crucible/AGENTS.md\n", "", 0)
                if "/opt/crucible/subprojects/core/config/repos.json" in command:
                    return SSHResult(
                        "/opt/crucible/subprojects/core/config/repos.json\n", "", 0
                    )
                if "/opt/crucible/subprojects/core/docs" in command:
                    return SSHResult("/opt/crucible/subprojects/core/docs\n", "", 0)
                if "/opt/crucible/subprojects/benchmarks/bench-perftest" in command:
                    return SSHResult(
                        "/opt/crucible/subprojects/benchmarks/bench-perftest\n", "", 0
                    )
                return SSHResult("", "", 0)
            if command.startswith("find /opt/crucible/subprojects/core/docs"):
                return SSHResult(
                    "/opt/crucible/subprojects/core/docs/how-run-files-work.md\n",
                    "",
                    0,
                )
            if command.startswith(
                "find /opt/crucible/subprojects/benchmarks/bench-perftest"
            ):
                return SSHResult(
                    "\n".join(
                        path
                        for path in self.files
                        if path.startswith(
                            "/opt/crucible/subprojects/benchmarks/bench-perftest/"
                        )
                    )
                    + "\n",
                    "",
                    0,
                )
            path = command.rsplit(" ", 1)[-1].strip("'")
            content = self.files.get(path)
            return SSHResult(
                content or "", "" if content else "missing", 0 if content else 1
            )

    monkeypatch.setattr(paths, "TICKET_DIR", tmp_path / "tickets")
    monkeypatch.setenv("TICKET_ID", "PERF-REMOTE-CONTEXT")
    monkeypatch.setattr(server, "_ssh", FakeSSH())
    monkeypatch.setattr(
        server,
        "_ticket",
        {
            "custom_fields": {
                "assigned_hardware_ips": {"controller": "controller.example.test"}
            }
        },
    )

    manager = WorkspaceManager(
        ticket_id="PERF-REMOTE-CONTEXT", agent_name="benchmark-agent", phase="benchmark"
    )
    bootstrap = await server._refresh_controller_bootstrap(manager)
    assert bootstrap["available"] is True
    assert bootstrap["document"]["ref"] == "core/AGENTS.md"
    assert manager.read_document("core/AGENTS.md")["content"] == (
        "Crucible layout guidance"
    )
    result = await server._refresh_controller_context("perftest", manager)

    assert result["available"] is True
    assert result["host"] == "controller.example.test"
    assert result["reason"] == "controller_refresh_succeeded"
    assert (
        manager.load_source_snapshot("controller")["files"][
            "docs/how-run-files-work.md"
        ]
        == "controller run-file guidance"
    )
    assert manager.load_source_snapshot("controller", "perftest")["files"][
        "multiplex.json"
    ].startswith('{"presets"')
    assert result["provenance"]["controller"] == "controller.example.test"
    documents, inventory = server._controller_snapshot_documents(
        manager,
        benchmark="perftest",
        namespace="benchmark/perftest",
        subject_area="benchmark",
        provenance=result["provenance"],
    )
    assert inventory["complete"] is True
    assert {item["ref"] for item in documents} == {
        "benchmark/perftest/README.md",
        "benchmark/perftest/docs/pairing.md",
        "benchmark/perftest/multiplex.json",
    }

    github = tmp_path / "github-crucible"
    (github / "config").mkdir(parents=True)
    (github / "bench-perftest").mkdir()
    (github / "config" / "repos.json").write_text(
        '{"official": [{"name": "perftest", "type": "benchmark", '
        '"repository": "https://example.test/bench-perftest"}]}'
    )
    (github / "bench-perftest" / "README.md").write_text("github benchmark guidance")
    from providers.skills.crucible import CrucibleContextGateway

    monkeypatch.setattr(
        server,
        "_crucible_context",
        CrucibleContextGateway(tmp_path / "missing-controller", source_repo=github),
    )
    monkeypatch.setattr(server, "_initialized", True)
    gateway_result = json.loads(
        await server.get_crucible_benchmark_context(
            "perftest",
            operation="list",
            namespace="benchmark/perftest",
            phase="benchmark",
        )
    )
    assert "effective_source" not in gateway_result
    assert "selection" not in gateway_result
    assert all("source" not in item for item in gateway_result["documents"])


@pytest.mark.asyncio
async def test_provisioning_does_not_expose_crucible_context_gateway():
    import agents.provisioning.server as server

    registered = await server.get_registered_tools()
    assert not any(tool.name == "get_crucible_benchmark_context" for tool in registered)


def test_crucible_source_resolver_uses_existing_local_checkout(tmp_path):
    source = tmp_path / "source"
    (source / "config").mkdir(parents=True)
    (source / "config" / "repos.json").write_text("{}")

    class Cache:
        def __init__(self):
            self.lookups = 0

        def get_path(self, name):
            self.lookups += 1
            return source if name == "crucible" else None

    cache = Cache()
    resolver = CrucibleSourceResolver(cache, "https://example.test/crucible.git")
    first = resolver.resolve()
    second = resolver.resolve()

    assert first is second
    assert cache.lookups == 1
    assert first.path == source
    assert first.provenance["effective_source"] == "local"
    assert first.provenance["source_reason"] == "local_checkout"
    assert first.provenance["refresh_attempted"] is False


def test_crucible_source_resolver_uses_explicit_local_fallback(tmp_path):
    fallback = tmp_path / "fallback"
    (fallback / "config").mkdir(parents=True)
    (fallback / "config" / "repos.json").write_text("{}")

    class Cache:
        def ensure_repo(self, name, url):
            raise RuntimeError("network unavailable")

        def get_path(self, name):
            return None

    result = CrucibleSourceResolver(
        Cache(), "https://example.test/crucible.git", local_fallback=fallback
    ).resolve()

    assert result.path == fallback
    assert result.provenance["effective_source"] == "local"
    assert result.provenance["source_reason"] == "local_checkout"
    assert result.provenance["refresh_attempted"] is False


def test_build_skill_provider_does_not_register_crucible(tmp_path, monkeypatch):
    from agents.server_utils import build_skill_provider

    provider = build_skill_provider(
        crucible_home=tmp_path / "controller", repo_cache=object()
    )
    assert provider.get_provider("crucible") is None


def test_build_skill_provider_never_resolves_crucible_source(tmp_path, monkeypatch):
    from agents.server_utils import build_skill_provider

    provider = build_skill_provider(
        crucible_home=tmp_path / "controller",
        repo_cache=object(),
        resolve_source=False,
    )
    assert provider.get_provider("crucible") is None


def test_build_skill_provider_catalog_only_registers_crucible_catalog(tmp_path):
    from agents.server_utils import build_skill_provider

    provider = build_skill_provider(
        crucible_home=tmp_path / "controller",
        repo_cache=object(),
        catalog_only=True,
    )
    catalog = provider.get_provider("crucible")
    assert catalog is not None
    assert catalog.__class__.__name__ == "CrucibleCatalogSkillProvider"


@pytest.mark.asyncio
async def test_triage_catalog_uses_bounded_files_without_local_checkout(tmp_path):
    class Fetcher:
        def read_json(self, path, *, repository=None, ref="main"):
            if path == "config/repos.json":
                return {
                    "official": [
                        {
                            "name": "perftest",
                            "type": "benchmark",
                            "repository": "https://github.com/example/bench-perftest.git",
                            "checkout": {"target": "main"},
                        }
                    ]
                }
            if path == "multiplex.json":
                assert repository.endswith("bench-perftest.git")
                return {"params": {"ifname": {"role": "all"}}}
            if path == "rickshaw.json":
                return {"client": {}, "server": {}}
            return None

    provider = CrucibleSkillProvider(
        tmp_path / "does-not-exist", catalog_fetcher=Fetcher(), catalog_only=True
    )

    benchmark = await provider.get_benchmark("perftest")
    resolved = await provider.resolve_benchmark(
        {"description": "RDMA throughput with perftest"}
    )

    assert benchmark is not None
    assert benchmark.roles == ["client", "server"]
    assert benchmark.min_hosts == 2
    assert benchmark.supported_params == {"params": {"ifname": {"role": "all"}}}
    assert resolved == "perftest"


@pytest.mark.asyncio
async def test_runtime_provider_does_not_fallback_to_remote_catalog(tmp_path):
    crucible_home = tmp_path / "crucible"
    crucible_home.mkdir()

    class Fetcher:
        def read_json(self, *args, **kwargs):
            raise AssertionError("runtime context must not fetch the remote catalog")

    provider = CrucibleSkillProvider(
        crucible_home,
        catalog_fetcher=Fetcher(),
    )

    assert await provider.get_benchmark("perftest") is None


@pytest.mark.asyncio
async def test_catalog_only_provider_ignores_local_crucible_tree(tmp_path):
    local = tmp_path / "crucible" / "subprojects" / "benchmarks" / "local-only"
    local.mkdir(parents=True)
    (local / "multiplex.json").write_text("{}")

    class Fetcher:
        def read_json(self, path, *, repository=None, ref="main"):
            if path == "config/repos.json":
                return {
                    "official": [
                        {
                            "name": "perftest",
                            "type": "benchmark",
                            "repository": "https://github.com/example/bench-perftest.git",
                        }
                    ]
                }
            return None

    provider = CrucibleSkillProvider(
        tmp_path / "crucible",
        catalog_fetcher=Fetcher(),
        catalog_only=True,
    )

    names = {benchmark.name for benchmark in await provider.list_benchmarks()}

    assert names == {"perftest"}


def test_crucible_catalog_fetcher_uses_crucible_default_branch():
    from providers.skills.crucible import CrucibleCatalogFetcher

    assert CrucibleCatalogFetcher._raw_url(
        "https://github.com/perftool-incubator/crucible.git",
        "config/repos.json",
    ) == (
        "https://raw.githubusercontent.com/perftool-incubator/crucible/"
        "master/config/repos.json"
    )


def test_crucible_context_policy_is_explicit_and_phase_specific():
    assert select_crucible_context(phase="triage")["effective_source"] == "github"
    controller = {
        "identified": True,
        "reachable": True,
        "crucible_installed": True,
        "snapshot_available": True,
    }
    selected = select_crucible_context(
        phase="benchmark", controller=controller, update_policy="no_update"
    )
    assert selected["effective_source"] == "controller"
    assert selected["source_assumption"] is False

    unknown = select_crucible_context(phase="benchmark", controller=controller)
    assert unknown["effective_source"] == "controller"
    assert unknown["source_assumption"] is True
    assert unknown["assumption"] == "no_update"

    planned = select_crucible_context(
        phase="benchmark", controller=controller, update_policy="update"
    )
    assert planned["effective_source"] == "github"
    assert planned["source_reason"] == "github_until_controller_refresh"


@pytest.mark.asyncio
async def test_get_example_runfile_missing():
    provider = CrucibleSkillProvider("/nonexistent")
    example = await provider.get_example_runfile("fio")
    assert example is None


@pytest.mark.skipif(not HAS_CRUCIBLE, reason="CRUCIBLE_HOME not available")
@pytest.mark.asyncio
async def test_generate_runfile_kube_endpoint(provider: CrucibleSkillProvider):
    """Kube endpoint with client+server roles produces correct flat structure."""
    result = await provider.generate_runfile(
        "uperf",
        {
            "endpoint_type": "kube",
            "endpoints": [
                {"host": "10.0.0.1", "roles": ["client"]},
                {"host": "10.0.0.2", "roles": ["server"]},
            ],
            "controller_ip": "10.0.0.1",
            "kube_host": "10.0.0.1",
            "userenv": "default",
        },
    )
    template = result.template
    assert "endpoints" in template
    ep = template["endpoints"][0]
    assert ep["type"] == "kube"
    assert ep["host"] == "10.0.0.1"
    assert ep["controller-ip-address"] == "10.0.0.1"
    assert ep["user"] == "root"
    assert "client" in ep["engines"]
    assert "server" in ep["engines"]
    assert "remotes" not in ep
    assert "settings" not in ep


@pytest.mark.skipif(not HAS_CRUCIBLE, reason="CRUCIBLE_HOME not available")
@pytest.mark.asyncio
async def test_generate_runfile_kube_single_role(provider: CrucibleSkillProvider):
    """Kube endpoint with only client role omits server from engines."""
    result = await provider.generate_runfile(
        "fio",
        {
            "endpoint_type": "kube",
            "endpoints": [{"host": "10.0.0.1", "roles": ["client"]}],
            "controller_ip": "10.0.0.1",
            "kube_host": "10.0.0.1",
        },
    )
    ep = result.template["endpoints"][0]
    assert ep["engines"] == {"client": "1"}
    assert "server" not in ep["engines"]


@pytest.mark.skipif(not HAS_CRUCIBLE, reason="CRUCIBLE_HOME not available")
@pytest.mark.asyncio
async def test_generate_runfile_remotehosts_unchanged(provider: CrucibleSkillProvider):
    """Remotehosts generation still works when endpoint_type is not specified."""
    result = await provider.generate_runfile(
        "fio",
        {
            "endpoints": [{"host": "10.0.0.1", "roles": ["client"]}],
            "userenv": "alma8",
            "osruntime": "podman",
        },
    )
    ep = result.template["endpoints"][0]
    assert ep["type"] == "remotehosts"
    assert "remotes" in ep
    assert ep["settings"]["userenv"] == "alma8"


@pytest.mark.skipif(not HAS_CRUCIBLE, reason="CRUCIBLE_HOME not available")
@pytest.mark.asyncio
async def test_generate_runfile_kube_has_config(provider: CrucibleSkillProvider):
    """Kube endpoint includes config array with userenv when non-default."""
    result = await provider.generate_runfile(
        "fio",
        {
            "endpoint_type": "kube",
            "endpoints": [{"host": "10.0.0.1", "roles": ["client"]}],
            "controller_ip": "10.0.0.1",
            "kube_host": "10.0.0.1",
            "userenv": "alma8",
        },
    )
    ep = result.template["endpoints"][0]
    assert ep["config"] == [{"targets": "default", "settings": {"userenv": "alma8"}}]


@pytest.mark.skipif(not HAS_CRUCIBLE, reason="CRUCIBLE_HOME not available")
@pytest.mark.asyncio
async def test_generate_runfile_kube_default_userenv_no_config(
    provider: CrucibleSkillProvider,
):
    """Kube endpoint omits config when userenv is 'default'."""
    result = await provider.generate_runfile(
        "fio",
        {
            "endpoint_type": "kube",
            "endpoints": [{"host": "10.0.0.1", "roles": ["client"]}],
            "controller_ip": "10.0.0.1",
            "kube_host": "10.0.0.1",
            "userenv": "default",
        },
    )
    ep = result.template["endpoints"][0]
    assert "config" not in ep


ALLOWED_RUNFILE_KEYS = {
    "benchmarks",
    "endpoints",
    "run-params",
    "schema",
    "tags",
    "tool-params",
}


@pytest.mark.skipif(not HAS_CRUCIBLE, reason="CRUCIBLE_HOME not available")
@pytest.mark.asyncio
async def test_generate_runfile_only_valid_keys(provider: CrucibleSkillProvider):
    """Run-file template must only contain keys that crucible's blockbreaker schema allows."""
    result = await provider.generate_runfile(
        "fio",
        {
            "endpoints": [{"host": "10.0.0.1", "roles": ["client"]}],
            "tags": {"env": "test"},
        },
    )
    extra = set(result.template.keys()) - ALLOWED_RUNFILE_KEYS
    assert not extra, f"Run-file contains keys rejected by crucible schema: {extra}"


@pytest.mark.skipif(not HAS_CRUCIBLE, reason="CRUCIBLE_HOME not available")
@pytest.mark.asyncio
async def test_generate_runfile_minimal_only_valid_keys(
    provider: CrucibleSkillProvider,
):
    """Even a minimal run-file (no endpoints/tags) must not have extra keys."""
    result = await provider.generate_runfile("fio", {})
    extra = set(result.template.keys()) - ALLOWED_RUNFILE_KEYS
    assert not extra, f"Run-file contains keys rejected by crucible schema: {extra}"


@pytest.mark.skipif(not HAS_CRUCIBLE, reason="CRUCIBLE_HOME not available")
@pytest.mark.asyncio
async def test_validate_runfile_passes_for_generated(provider: CrucibleSkillProvider):
    """A run-file from generate_runfile must pass schema validation."""
    result = await provider.generate_runfile(
        "fio",
        {
            "endpoints": [{"host": "10.0.0.1", "roles": ["client"]}],
            "userenv": "default",
            "osruntime": "podman",
        },
    )
    validation = await provider.validate_runfile(result.template)
    assert validation["valid"], (
        f"Generated run-file failed validation: {validation['errors']}"
    )


@pytest.mark.asyncio
async def test_validate_runfile_rejects_extra_keys(
    monkeypatch,
):
    """Run-file with extra top-level keys must fail validation."""
    provider = CrucibleSkillProvider("/nonexistent")
    monkeypatch.setattr(
        provider,
        "_load_schema",
        lambda: {
            "type": "object",
            "properties": {"benchmarks": {"type": "array"}},
            "required": ["benchmarks"],
            "additionalProperties": False,
        },
    )
    bad_runfile = {"benchmarks": [], "tags": {}, "harness": "crucible"}
    validation = await provider.validate_runfile(bad_runfile)
    assert not validation["valid"]
    assert any("harness" in e for e in validation["errors"])


class TestEndpointUserEnforcement:
    """Crucible requires root — non-root endpoint_user must be overridden."""

    def test_remotehosts_overrides_non_root(self):
        p = CrucibleSkillProvider("/nonexistent")
        template: dict = {}
        p._build_remotehosts_endpoints(
            template,
            {"endpoint_user": "ec2-user"},
            [{"host": "10.0.0.1", "roles": ["client"]}],
        )
        assert template["endpoints"][0]["settings"]["user"] == "root"

    def test_remotehosts_keeps_root(self):
        p = CrucibleSkillProvider("/nonexistent")
        template: dict = {}
        p._build_remotehosts_endpoints(
            template,
            {"endpoint_user": "root"},
            [{"host": "10.0.0.1", "roles": ["client"]}],
        )
        assert template["endpoints"][0]["settings"]["user"] == "root"

    def test_kube_overrides_non_root(self):
        p = CrucibleSkillProvider("/nonexistent")
        template: dict = {}
        p._build_kube_endpoints(
            template,
            {"endpoint_user": "ec2-user", "kube_host": "10.0.0.1"},
            [{"host": "10.0.0.1", "roles": ["client"]}],
            "fio",
        )
        assert template["endpoints"][0]["user"] == "root"

    def test_default_is_root(self):
        p = CrucibleSkillProvider("/nonexistent")
        template: dict = {}
        p._build_remotehosts_endpoints(
            template,
            {},
            [{"host": "10.0.0.1", "roles": ["client"]}],
        )
        assert template["endpoints"][0]["settings"]["user"] == "root"


@pytest.mark.asyncio
async def test_skill_context_mcp_schema_exposes_generic_request_fields():
    import agents.benchmark.server as benchmark_server
    import agents.provisioning.server as provisioning_server
    import agents.resource.server as resource_server
    import agents.review.server as review_server
    import agents.triage.server as triage_server

    for server in (
        benchmark_server,
        provisioning_server,
        resource_server,
        review_server,
        triage_server,
    ):
        tools = await server.mcp.list_tools()
        tool = next(item for item in tools if item.name == "get_skill_context")
        assert set(tool.parameters["properties"]) == {
            "subject",
            "operation",
            "ref",
            "path",
            "from_ref",
            "query",
            "max_bytes",
            "offset_bytes",
            "cursor",
        }
        assert tool.parameters["additionalProperties"] is False


@pytest.mark.asyncio
async def test_registered_skill_context_tool_includes_provider_runfile_contract(
    tmp_path, monkeypatch
):
    import agents.benchmark.server as benchmark_server

    provider = CrucibleSkillProvider(tmp_path / "missing-controller")
    monkeypatch.setattr(benchmark_server, "_initialized", True)
    monkeypatch.setattr(benchmark_server, "_crucible_context", provider)
    monkeypatch.setattr(benchmark_server, "_ssh", object())
    monkeypatch.setattr(
        benchmark_server,
        "_controller_host",
        lambda: "controller.example.test",
    )
    calls = []

    async def fake_skill_context_gateway(*_args, **kwargs):
        calls.append(kwargs)
        return json.dumps(
            {
                "found": True,
                "operation": "bootstrap",
                "document": {"ref": "AGENTS.md", "content": "controller guidance"},
            }
        )

    monkeypatch.setattr(
        benchmark_server,
        "skill_context_gateway",
        fake_skill_context_gateway,
    )

    tools = await benchmark_server.mcp.list_tools()
    assert any(tool.name == "get_skill_context" for tool in tools)

    response = json.loads(
        await benchmark_server.get_skill_context(
            subject="harness/crucible", operation="bootstrap"
        )
    )

    assert response["document"]["content"] == "controller guidance"
    assert response["runfile_contract"] == provider.get_runfile_contract()
    assert response["runfile_contract"]["required_top_level_fields"] == ["tags"]
    assert response["runfile_contract"]["properties"]["tags"] == {
        "type": "object",
        "default": {},
    }
    assert "Use {} when no tags apply" in response["runfile_contract"]["guidance"]
    assert calls[0]["operation"] == "bootstrap"
    assert calls[0]["subject"] == "harness/crucible"


@pytest.mark.asyncio
async def test_benchmark_search_budget_includes_runfile_contract(tmp_path, monkeypatch):
    import agents.benchmark.server as benchmark_server

    class LargeContractProvider:
        def get_runfile_contract(self):
            return {"guidance": "run-file contract " * 100}

    monkeypatch.setattr(benchmark_server, "_initialized", True)
    monkeypatch.setattr(benchmark_server, "_crucible_context", LargeContractProvider())
    calls = []

    async def too_small(*_args, **kwargs):
        calls.append(kwargs)
        return json.dumps(
            {
                "found": False,
                "operation": "search",
                "reason": "page_too_small",
                "message": "max_bytes must leave room for one result page.",
            },
            separators=(",", ":"),
        )

    monkeypatch.setattr(benchmark_server, "skill_context_gateway", too_small)
    response = await benchmark_server.get_skill_context(
        subject="harness/crucible",
        operation="search",
        query="README",
        max_bytes=512,
    )

    payload = json.loads(response)
    assert len(response.encode("utf-8")) <= 512
    assert payload["reason"] == "page_too_small"
    assert "runfile_contract" not in payload
    assert calls[0]["response_overhead_bytes"] > 512


@pytest.mark.asyncio
async def test_controller_context_gateway_follows_agent_supplied_paths(
    tmp_path, monkeypatch
):
    import paths
    from agents.server_utils import controller_context_gateway
    from providers.ssh import SSHResult
    from providers.workspace.manager import WorkspaceManager

    class FakeSSH:
        async def run(self, host, command, **kwargs):
            if "realpath -e" in command:
                if "perftest/README.md" in command:
                    return SSHResult("perftest guidance", "", 0)
                if "AGENTS.md" in command:
                    return SSHResult(
                        "Read subprojects/benchmarks/perftest/README.md next.",
                        "",
                        0,
                    )
                return SSHResult("", "missing", 1)
            if "python3 -c" in command:
                return SSHResult(
                    "CONTENT\t/opt/crucible/subprojects/benchmarks/perftest/README.md\t12\t"
                    "device guidance\n"
                    "NAME\tf\t17\t/opt/crucible/subprojects/benchmarks/perftest/README.md\n"
                    "NAME\td\t\t/opt/crucible/subprojects/benchmarks/perftest\n",
                    "",
                    0,
                )
            if "AGENTS.md" in command:
                return SSHResult(
                    "Read subprojects/benchmarks/perftest/README.md next.", "", 0
                )
            if "perftest/README.md" in command:
                return SSHResult("perftest guidance", "", 0)
            return SSHResult("", "missing", 1)

    monkeypatch.setattr(paths, "TICKET_DIR", tmp_path / "tickets")
    ticket_id = "PERF-DIRECTED-CONTEXT"
    bootstrap = json.loads(
        await controller_context_gateway(
            ssh=FakeSSH(),
            controller_host="controller.example.test",
            ticket_id=ticket_id,
            agent_name="benchmark-agent",
            phase="benchmark",
            operation="bootstrap",
        )
    )
    assert bootstrap["document"]["ref"] == "AGENTS.md"
    assert "source" not in bootstrap["document"]

    read = json.loads(
        await controller_context_gateway(
            ssh=FakeSSH(),
            controller_host="controller.example.test",
            ticket_id=ticket_id,
            agent_name="benchmark-agent",
            phase="benchmark",
            operation="read",
            path="subprojects/benchmarks/perftest/README.md",
            max_bytes=8,
        )
    )
    assert read["document"]["ref"] == "subprojects/benchmarks/perftest/README.md"
    assert read["document"]["content"] == "perftest"
    assert read["document"]["size_bytes"] == len(b"perftest guidance")
    assert read["document"]["next_offset_bytes"] == 8
    assert "content" not in read["documents"][0]
    continued = json.loads(
        await controller_context_gateway(
            ssh=FakeSSH(),
            controller_host="controller.example.test",
            ticket_id=ticket_id,
            agent_name="benchmark-agent",
            phase="benchmark",
            operation="read",
            path="subprojects/benchmarks/perftest/README.md",
            max_bytes=8,
            offset_bytes=read["document"]["next_offset_bytes"],
        )
    )
    assert continued["document"]["content"] == " guidanc"
    manager = WorkspaceManager(
        ticket_id=ticket_id, agent_name="benchmark-agent", phase="benchmark"
    )
    assert (
        manager.read_document("subprojects/benchmarks/perftest/README.md")["status"]
        == "ok"
    )

    search = json.loads(
        await controller_context_gateway(
            ssh=FakeSSH(),
            controller_host="controller.example.test",
            ticket_id=ticket_id,
            agent_name="benchmark-agent",
            phase="benchmark",
            operation="search",
            query="device|rdma",
        )
    )
    assert search["found"] is True
    assert search["results"][0]["ref"] == "subprojects/benchmarks/perftest/README.md"
    assert search["results"][0]["size_bytes"] == 17
    assert search["results"][0]["match_kinds"] == ["content", "name"]
    assert search["results"][0]["matches"][0]["kind"] == "content"
    assert search["total_files"] == 2
    assert search["total_matches"] == 3
    assert search["results"][1]["type"] == "directory"
    assert "source" not in search["results"][0]


@pytest.mark.asyncio
async def test_controller_context_gateway_rejects_symlink_escape(tmp_path, monkeypatch):
    import paths
    from agents.server_utils import controller_context_gateway
    from providers.ssh import SSHResult

    class EscapingSSH:
        async def run(self, host, command, **kwargs):
            if "realpath -e" in command:
                return SSHResult("", "path escapes controller root", 22)
            return SSHResult("", "unexpected command", 1)

    monkeypatch.setattr(paths, "TICKET_DIR", tmp_path / "tickets")
    result = json.loads(
        await controller_context_gateway(
            ssh=EscapingSSH(),
            controller_host="controller.example.test",
            ticket_id="PERF-SYMLINK-ESCAPE",
            agent_name="benchmark-agent",
            phase="benchmark",
            operation="read",
            path="subprojects/benchmarks/perftest/README.md",
        )
    )

    assert result["found"] is False
    assert result["reason"] == "controller_path_outside_root"


@pytest.mark.parametrize(
    ("exit_code", "reason", "path_type"),
    [
        (20, "controller_root_unavailable", "unknown"),
        (21, "controller_document_not_found", "missing"),
        (22, "controller_path_outside_root", "outside_root"),
        (23, "controller_path_is_directory", "directory"),
        (24, "controller_path_not_file", "unsupported"),
        (25, "controller_path_inaccessible", "inaccessible"),
        (26, "controller_read_failed", "file"),
        (27, "controller_path_excluded", "excluded"),
    ],
)
@pytest.mark.asyncio
async def test_controller_read_errors_are_structured_and_audited(
    tmp_path, monkeypatch, exit_code, reason, path_type
):
    import agents.server_utils as server_utils
    import paths
    from providers.ssh import SSHResult

    events = []
    monkeypatch.setattr(paths, "TICKET_DIR", tmp_path / "tickets")
    monkeypatch.setattr(
        server_utils,
        "emit_private_tool_audit_event",
        lambda *args, **kwargs: events.append(kwargs["data"]),
    )

    class ErrorSSH:
        async def run(self, host, command, **kwargs):
            return SSHResult("", "secret diagnostic must not escape", exit_code)

    result = json.loads(
        await server_utils.controller_context_gateway(
            ssh=ErrorSSH(),
            controller_host="controller.example.test",
            ticket_id=f"PERF-CONTROLLER-ERROR-{exit_code}",
            agent_name="benchmark-agent",
            phase="benchmark",
            operation="read",
            path="repos/example/README.md",
        )
    )

    assert result["reason"] == reason
    assert result["requested_ref"] == "repos/example/README.md"
    assert result["path_type"] == path_type
    assert result["next_action"]
    assert "secret diagnostic" not in json.dumps(result)
    assert events[-1]["outcome"] == "rejected"
    assert events[-1]["reason"] == reason
    assert "secret diagnostic" not in json.dumps(events)


@pytest.mark.asyncio
async def test_controller_transport_error_is_safe_and_audited(tmp_path, monkeypatch):
    import agents.server_utils as server_utils
    import paths

    events = []
    monkeypatch.setattr(paths, "TICKET_DIR", tmp_path / "tickets")
    monkeypatch.setattr(
        server_utils,
        "emit_private_tool_audit_event",
        lambda *args, **kwargs: events.append(kwargs["data"]),
    )

    class FailedSSH:
        async def run(self, host, command, **kwargs):
            raise RuntimeError("credential-bearing ssh failure")

    result = json.loads(
        await server_utils.controller_context_gateway(
            ssh=FailedSSH(),
            controller_host="controller.example.test",
            ticket_id="PERF-CONTROLLER-TRANSPORT-ERROR",
            agent_name="benchmark-agent",
            phase="benchmark",
            operation="read",
            path="README.md",
        )
    )

    assert result["reason"] == "controller_transport_error"
    assert result["requested_ref"] == "README.md"
    assert "credential-bearing" not in json.dumps(result)
    assert events[-1]["outcome"] == "rejected"
    assert "credential-bearing" not in json.dumps(events)


@pytest.mark.asyncio
async def test_controller_read_reports_only_validated_resolved_ref(
    tmp_path, monkeypatch
):
    import agents.server_utils as server_utils
    import paths
    from providers.ssh import SSHResult

    monkeypatch.setattr(paths, "TICKET_DIR", tmp_path / "tickets")

    class ResolvedSSH:
        async def run(self, host, command, **kwargs):
            return SSHResult(
                "safe controller guidance",
                "resolved_ref=canonical/README.md\nprivate transport detail",
                0,
            )

    result = json.loads(
        await server_utils.controller_context_gateway(
            ssh=ResolvedSSH(),
            controller_host="controller.example.test",
            ticket_id="PERF-CONTROLLER-RESOLVED-REF",
            agent_name="benchmark-agent",
            phase="benchmark",
            operation="read",
            path="alias/README.md",
        )
    )

    assert result["requested_ref"] == "alias/README.md"
    assert result["resolved_ref"] == "canonical/README.md"
    assert result["path_type"] == "file"
    assert "private transport detail" not in json.dumps(result)


@pytest.mark.asyncio
async def test_controller_gateway_distinguishes_invalid_and_missing_origins(
    tmp_path, monkeypatch
):
    import agents.server_utils as server_utils
    import paths
    from providers.ssh import SSHResult

    monkeypatch.setattr(paths, "TICKET_DIR", tmp_path / "tickets")

    class MissingOriginSSH:
        calls = 0

        async def run(self, host, command, **kwargs):
            self.calls += 1
            return SSHResult("", "not-found detail", 21)

    ssh = MissingOriginSSH()
    invalid = json.loads(
        await server_utils.controller_context_gateway(
            ssh=ssh,
            controller_host="controller.example.test",
            ticket_id="PERF-CONTROLLER-INVALID-ORIGIN",
            agent_name="benchmark-agent",
            phase="benchmark",
            operation="read",
            from_ref="../outside",
            path="README.md",
        )
    )
    missing = json.loads(
        await server_utils.controller_context_gateway(
            ssh=ssh,
            controller_host="controller.example.test",
            ticket_id="PERF-CONTROLLER-MISSING-ORIGIN",
            agent_name="benchmark-agent",
            phase="benchmark",
            operation="read",
            from_ref="repos/missing",
            path="README.md",
        )
    )

    assert invalid["reason"] == "invalid_origin"
    assert missing["reason"] == "controller_origin_not_found"
    assert missing["path_type"] == "missing"
    assert ssh.calls == 1
    assert "not-found detail" not in json.dumps(missing)

    class PointerSSH:
        calls = 0

        async def run(self, host, command, **kwargs):
            self.calls += 1
            return SSHResult("directory", "", 0)

    pointer_ssh = PointerSSH()
    invalid_pointer = json.loads(
        await server_utils.controller_context_gateway(
            ssh=pointer_ssh,
            controller_host="controller.example.test",
            ticket_id="PERF-CONTROLLER-INVALID-POINTER",
            agent_name="benchmark-agent",
            phase="benchmark",
            operation="search",
            query="README",
            from_ref="repos/safe",
            path="../../../outside",
        )
    )
    assert invalid_pointer["reason"] == "invalid_origin"
    assert invalid_pointer["path_type"] == "invalid_pointer"
    assert pointer_ssh.calls == 1


@pytest.mark.parametrize("exit_code", [-1, 255])
@pytest.mark.parametrize("operation", ["read", "list", "search"])
@pytest.mark.asyncio
async def test_controller_origin_inspection_transport_failures_are_retryable_and_safe(
    tmp_path, monkeypatch, exit_code, operation
):
    import agents.server_utils as server_utils
    import paths
    from providers.ssh import SSHResult

    events = []
    monkeypatch.setattr(paths, "TICKET_DIR", tmp_path / "tickets")
    monkeypatch.setattr(
        server_utils,
        "emit_private_tool_audit_event",
        lambda *args, **kwargs: events.append(kwargs["data"]),
    )

    class FailedInspectionSSH:
        calls = 0

        async def run(self, host, command, **kwargs):
            self.calls += 1
            return SSHResult("", "credential-bearing transport detail", exit_code)

    ssh = FailedInspectionSSH()
    result = json.loads(
        await server_utils.controller_context_gateway(
            ssh=ssh,
            controller_host="controller.example.test",
            ticket_id=f"PERF-CONTROLLER-ORIGIN-TRANSPORT-{operation}-{exit_code}",
            agent_name="benchmark-agent",
            phase="benchmark",
            operation=operation,
            from_ref="repos/safe",
            path="README.md" if operation == "read" else "",
            query="README" if operation == "search" else "",
        )
    )

    assert result["reason"] == "controller_transport_error"
    assert result["requested_ref"] == "repos/safe"
    assert "Retry" in result["next_action"]
    assert "credential-bearing" not in json.dumps(result)
    assert ssh.calls == 1
    assert events[-1]["outcome"] == "rejected"
    assert events[-1]["reason"] == "controller_transport_error"
    assert "credential-bearing" not in json.dumps(events)


@pytest.mark.asyncio
async def test_controller_listing_returns_typed_reusable_children(
    tmp_path, monkeypatch
):
    import agents.server_utils as server_utils
    import paths
    from providers.ssh import SSHResult

    monkeypatch.setattr(paths, "TICKET_DIR", tmp_path / "tickets")
    directory = "repos/https:github.com:perftool-incubator/bench-uperf"

    class ListingSSH:
        async def run(self, host, command, **kwargs):
            assert "os.path.realpath" in command
            return SSHResult(
                json.dumps(
                    {
                        "entries": [
                            {
                                "path": f"{directory}/README.md",
                                "type": "file",
                                "size_bytes": 18,
                            },
                            {
                                "path": f"{directory}/docs",
                                "type": "directory",
                                "size_bytes": None,
                            },
                        ],
                        "complete": True,
                        "resolved_path": directory,
                    }
                ),
                "",
                0,
            )

    result = json.loads(
        await server_utils.controller_context_gateway(
            ssh=ListingSSH(),
            controller_host="controller.example.test",
            ticket_id="PERF-CONTROLLER-LIST",
            agent_name="benchmark-agent",
            phase="benchmark",
            operation="list",
            path=directory,
        )
    )

    assert result["resolved_ref"] == directory
    assert result["path_type"] == "directory"
    assert [(item["path"], item["type"]) for item in result["results"]] == [
        (f"{directory}/README.md", "file"),
        (f"{directory}/docs", "directory"),
    ]
    assert all(item["uri"] == f"crucible://{item['ref']}" for item in result["results"])
    assert result["recursive"] is False


@pytest.mark.asyncio
async def test_software_listing_refs_are_reusable_and_directory_reads_explain_list(
    tmp_path, monkeypatch
):
    import agents.skill_gateway as skill_gateway
    import paths
    from providers.skills.gateway import OrganizationSkillResolver
    from providers.ssh import SSHResult

    monkeypatch.setattr(paths, "TICKET_DIR", tmp_path / "tickets")
    directory = "repos/https:github.com:perftool-incubator/bench-uperf"

    class Provider:
        organization_resolver = OrganizationSkillResolver.from_instance_config({})

    class ListingSSH:
        async def run(self, host, command, **kwargs):
            if command.startswith("python3 -c "):
                return SSHResult(
                    json.dumps(
                        {
                            "entries": [
                                {
                                    "path": f"{directory}/README.md",
                                    "type": "file",
                                    "size_bytes": 18,
                                },
                                {
                                    "path": f"{directory}/docs",
                                    "type": "directory",
                                    "size_bytes": None,
                                },
                            ],
                            "complete": True,
                            "resolved_path": directory,
                        }
                    ),
                    "",
                    0,
                )
            if "cat --" in command:
                return SSHResult("", "secret controller error", 23)
            raise AssertionError(f"unexpected controller command: {command}")

    listing = json.loads(
        await skill_gateway.skill_context_gateway(
            Provider(),
            ticket_id="PERF-SOFTWARE-LIST-REFS",
            agent_name="benchmark-agent",
            phase="benchmark",
            operation="list",
            ref=skill_gateway._software_ref(directory),
            ssh=ListingSSH(),
            controller_host="controller.example.test",
        )
    )
    source = next(item for item in listing["sources"] if item["scope"] == "software")
    assert [(item["path"], item["type"]) for item in source["results"]] == [
        (f"{directory}/README.md", "file"),
        (f"{directory}/docs", "directory"),
    ]
    assert all(
        item["ref"] == skill_gateway._software_ref(item["path"])
        for item in source["results"]
    )
    directory_ref = next(
        item["ref"] for item in source["results"] if item["type"] == "directory"
    )
    read = json.loads(
        await skill_gateway.skill_context_gateway(
            Provider(),
            ticket_id="PERF-SOFTWARE-LIST-REFS",
            agent_name="benchmark-agent",
            phase="benchmark",
            operation="read",
            ref=directory_ref,
            ssh=ListingSSH(),
            controller_host="controller.example.test",
        )
    )
    assert read["reason"] == "controller_path_is_directory"
    assert read["path_type"] == "directory"
    assert "operation='list'" in read["next_action"]
    assert "secret controller error" not in json.dumps(read)


@pytest.mark.asyncio
async def test_controller_search_uses_encoded_colon_directory_or_file_scope(
    tmp_path, monkeypatch
):
    import agents.skill_gateway as skill_gateway
    import paths
    from agents.skill_gateway import skill_context_gateway
    from providers.skills.gateway import OrganizationSkillResolver
    from providers.ssh import SSHResult

    monkeypatch.setattr(paths, "TICKET_DIR", tmp_path / "tickets")
    relative_directory = "repos/https:github.com:perftool-incubator/bench-uperf"
    expected_file = f"{relative_directory}/README.md"
    expected_ref = skill_gateway._software_ref(expected_file)
    calls = []

    class Provider:
        organization_resolver = OrganizationSkillResolver.from_instance_config({})

    class ScopedSSH:
        async def run(self, host, command, **kwargs):
            calls.append(command)
            if "realpath -e" in command:
                kind = "file" if "README.md" in command else "directory"
                return SSHResult(kind, "", 0)
            assert "MAX_DIRECTORIES" in command
            return SSHResult(
                f"NAME\tf\t18\t/opt/crucible/{expected_file}\n",
                "",
                0,
            )

    for is_directory in (True, False):
        origin = relative_directory if is_directory else expected_file
        origin_ref = skill_gateway._software_ref(origin)
        calls.clear()
        result = json.loads(
            await skill_context_gateway(
                Provider(),
                ticket_id=f"PERF-SCOPED-SEARCH-{is_directory}",
                agent_name="benchmark-agent",
                phase="benchmark",
                operation="search",
                query="README",
                from_ref=origin_ref,
                ssh=ScopedSSH(),
                controller_host="controller.example.test",
            )
        )
        search_command = next(
            command for command in calls if "MAX_DIRECTORIES" in command
        )
        assert relative_directory in search_command
        source = next(
            source for source in result["sources"] if source["scope"] == "software"
        )
        assert source["results"][0]["ref"] == expected_ref
        assert source["scope_ref"] == skill_gateway._software_ref(relative_directory)
        assert source["complete"] is True
