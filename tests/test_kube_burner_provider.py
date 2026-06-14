from __future__ import annotations

import pytest

from providers.skills.kube_burner import KubeBurnerSkillProvider


@pytest.fixture
def provider() -> KubeBurnerSkillProvider:
    return KubeBurnerSkillProvider()


@pytest.mark.asyncio
async def test_list_benchmarks(provider: KubeBurnerSkillProvider):
    benchmarks = await provider.list_benchmarks()
    names = [b.name for b in benchmarks]
    assert "node-density" in names
    assert "cluster-density" in names
    assert len(benchmarks) == 2


@pytest.mark.asyncio
async def test_harness_field(provider: KubeBurnerSkillProvider):
    benchmarks = await provider.list_benchmarks()
    for b in benchmarks:
        assert b.harness == "kube-burner"


@pytest.mark.asyncio
async def test_endpoint_types_kube(provider: KubeBurnerSkillProvider):
    benchmarks = await provider.list_benchmarks()
    for b in benchmarks:
        assert "kube" in b.endpoint_types


@pytest.mark.asyncio
async def test_roles_and_min_hosts(provider: KubeBurnerSkillProvider):
    benchmarks = await provider.list_benchmarks()
    for b in benchmarks:
        assert b.roles == ["client"]
        assert b.min_hosts == 1


@pytest.mark.asyncio
async def test_get_benchmark_found(provider: KubeBurnerSkillProvider):
    b = await provider.get_benchmark("node-density")
    assert b is not None
    assert b.name == "node-density"
    assert b.harness == "kube-burner"


@pytest.mark.asyncio
async def test_get_benchmark_not_found(provider: KubeBurnerSkillProvider):
    b = await provider.get_benchmark("nonexistent")
    assert b is None


@pytest.mark.asyncio
async def test_resolve_benchmark_density(provider: KubeBurnerSkillProvider):
    result = await provider.resolve_benchmark({"description": "pod density test"})
    assert result == "node-density"


@pytest.mark.asyncio
async def test_resolve_benchmark_stress(provider: KubeBurnerSkillProvider):
    result = await provider.resolve_benchmark({"description": "stress test the api"})
    assert result == "cluster-density"


@pytest.mark.asyncio
async def test_resolve_benchmark_kubernetes(provider: KubeBurnerSkillProvider):
    result = await provider.resolve_benchmark({"description": "kubernetes scale testing"})
    assert result is not None
    assert result in ("node-density", "cluster-density")


@pytest.mark.asyncio
async def test_resolve_benchmark_no_match(provider: KubeBurnerSkillProvider):
    result = await provider.resolve_benchmark({"description": "network throughput"})
    assert result is None


@pytest.mark.asyncio
async def test_generate_runfile_node_density(provider: KubeBurnerSkillProvider):
    result = await provider.generate_runfile("node-density", {})
    t = result.template
    assert t["harness"] == "kube-burner"
    assert "config" in t
    assert "templates" in t
    assert "pod.yml" in t["templates"]

    config = t["config"]
    assert "global" in config
    assert "jobs" in config
    assert len(config["jobs"]) == 1
    job = config["jobs"][0]
    assert job["name"] == "node-density"
    assert job["jobType"] == "create"
    assert job["jobIterations"] == 50


@pytest.mark.asyncio
async def test_generate_runfile_cluster_density(provider: KubeBurnerSkillProvider):
    result = await provider.generate_runfile("cluster-density", {"jobIterations": 5})
    t = result.template
    assert "deployment.yml" in t["templates"]
    assert "service.yml" in t["templates"]
    assert "configmap.yml" in t["templates"]
    assert "secret.yml" in t["templates"]

    job = t["config"]["jobs"][0]
    assert job["jobIterations"] == 5
    assert len(job["objects"]) == 4


@pytest.mark.asyncio
async def test_generate_runfile_custom_params(provider: KubeBurnerSkillProvider):
    result = await provider.generate_runfile(
        "node-density",
        {"jobIterations": 100, "qps": 50, "burst": 50, "gc": False},
    )
    t = result.template
    config = t["config"]
    assert config["global"]["gc"] is False
    job = config["jobs"][0]
    assert job["jobIterations"] == 100
    assert job["qps"] == 50
    assert job["burst"] == 50


@pytest.mark.asyncio
async def test_generate_runfile_unknown_benchmark(provider: KubeBurnerSkillProvider):
    result = await provider.generate_runfile("nonexistent", {})
    assert result.template == {}


@pytest.mark.asyncio
async def test_get_benchmark_params(provider: KubeBurnerSkillProvider):
    params = await provider.get_benchmark_params("node-density")
    assert params is not None
    assert "jobIterations" in params
    assert "qps" in params
    assert "burst" in params
    assert params["jobIterations"]["default"] == 50


@pytest.mark.asyncio
async def test_get_benchmark_params_not_found(provider: KubeBurnerSkillProvider):
    params = await provider.get_benchmark_params("nonexistent")
    assert params is None


@pytest.mark.asyncio
async def test_get_runfile_schema(provider: KubeBurnerSkillProvider):
    schema = await provider.get_runfile_schema()
    assert schema is not None
    assert schema["type"] == "object"
    assert "config" in schema["properties"]
    assert "templates" in schema["properties"]


@pytest.mark.asyncio
async def test_get_example_runfile(provider: KubeBurnerSkillProvider):
    example = await provider.get_example_runfile("node-density")
    assert example is not None
    assert example["harness"] == "kube-burner"
    assert "config" in example
    assert "templates" in example


@pytest.mark.asyncio
async def test_get_example_runfile_not_found(provider: KubeBurnerSkillProvider):
    example = await provider.get_example_runfile("nonexistent")
    assert example is None


@pytest.mark.asyncio
async def test_validate_runfile_valid(provider: KubeBurnerSkillProvider):
    result = await provider.generate_runfile("node-density", {})
    validation = await provider.validate_runfile(result.template)
    assert validation["valid"] is True
    assert validation["errors"] == []


@pytest.mark.asyncio
async def test_validate_runfile_missing_config(provider: KubeBurnerSkillProvider):
    validation = await provider.validate_runfile({"templates": {}})
    assert validation["valid"] is False
    assert any("config" in e for e in validation["errors"])


@pytest.mark.asyncio
async def test_validate_runfile_missing_jobs(provider: KubeBurnerSkillProvider):
    validation = await provider.validate_runfile({
        "config": {"global": {}},
        "templates": {},
    })
    assert validation["valid"] is False
    assert any("jobs" in e for e in validation["errors"])


@pytest.mark.asyncio
async def test_validate_runfile_invalid_job_type(provider: KubeBurnerSkillProvider):
    validation = await provider.validate_runfile({
        "config": {
            "global": {},
            "jobs": [{"name": "test", "jobType": "Invalid", "objects": []}],
        },
        "templates": {},
    })
    assert validation["valid"] is False
    assert any("jobType" in e for e in validation["errors"])


@pytest.mark.asyncio
async def test_validate_runfile_missing_template_reference(provider: KubeBurnerSkillProvider):
    validation = await provider.validate_runfile({
        "config": {
            "global": {},
            "jobs": [{
                "name": "test",
                "jobType": "create",
                "objects": [{"objectTemplate": "missing.yml"}],
            }],
        },
        "templates": {},
    })
    assert validation["valid"] is False
    assert any("missing.yml" in e for e in validation["errors"])


@pytest.mark.asyncio
async def test_validate_generated_runfile_passes(provider: KubeBurnerSkillProvider):
    for benchmark in ("node-density", "cluster-density"):
        result = await provider.generate_runfile(benchmark, {})
        validation = await provider.validate_runfile(result.template)
        assert validation["valid"] is True, f"{benchmark}: {validation['errors']}"
