from __future__ import annotations

import pytest

from providers.skills.benchmark_runner import BenchmarkRunnerSkillProvider


@pytest.fixture
def provider() -> BenchmarkRunnerSkillProvider:
    return BenchmarkRunnerSkillProvider()


@pytest.mark.asyncio
async def test_list_benchmarks(provider: BenchmarkRunnerSkillProvider):
    benchmarks = await provider.list_benchmarks()
    names = [b.name for b in benchmarks]
    assert "stressng_pod" in names
    assert "fio_pod" in names
    assert "uperf_pod" in names
    assert "sysbench_pod" in names
    assert len(benchmarks) == 7


@pytest.mark.asyncio
async def test_harness_field(provider: BenchmarkRunnerSkillProvider):
    benchmarks = await provider.list_benchmarks()
    for b in benchmarks:
        assert b.harness == "benchmark-runner"


@pytest.mark.asyncio
async def test_endpoint_types_kube(provider: BenchmarkRunnerSkillProvider):
    benchmarks = await provider.list_benchmarks()
    for b in benchmarks:
        assert "kube" in b.endpoint_types


@pytest.mark.asyncio
async def test_roles(provider: BenchmarkRunnerSkillProvider):
    benchmarks = await provider.list_benchmarks()
    by_name = {b.name: b for b in benchmarks}
    assert by_name["stressng_pod"].roles == ["client"]
    assert by_name["uperf_pod"].roles == ["client", "server"]
    assert by_name["fio_pod"].roles == ["client"]


@pytest.mark.asyncio
async def test_get_benchmark_found(provider: BenchmarkRunnerSkillProvider):
    b = await provider.get_benchmark("stressng_pod")
    assert b is not None
    assert b.name == "stressng_pod"
    assert b.harness == "benchmark-runner"


@pytest.mark.asyncio
async def test_get_benchmark_not_found(provider: BenchmarkRunnerSkillProvider):
    b = await provider.get_benchmark("nonexistent")
    assert b is None


@pytest.mark.asyncio
async def test_resolve_benchmark_stress(provider: BenchmarkRunnerSkillProvider):
    result = await provider.resolve_benchmark({"description": "cpu stress test"})
    assert result == "stressng_pod"


@pytest.mark.asyncio
async def test_resolve_benchmark_storage(provider: BenchmarkRunnerSkillProvider):
    result = await provider.resolve_benchmark({"description": "storage io test"})
    assert result in ("fio_pod", "vdbench_pod")


@pytest.mark.asyncio
async def test_resolve_benchmark_network(provider: BenchmarkRunnerSkillProvider):
    result = await provider.resolve_benchmark({"description": "network throughput"})
    assert result == "uperf_pod"


@pytest.mark.asyncio
async def test_resolve_benchmark_database(provider: BenchmarkRunnerSkillProvider):
    result = await provider.resolve_benchmark({"description": "database performance"})
    assert result is not None
    assert "hammerdb" in result


@pytest.mark.asyncio
async def test_resolve_benchmark_no_match(provider: BenchmarkRunnerSkillProvider):
    result = await provider.resolve_benchmark({"description": "quantum computing"})
    assert result is None


@pytest.mark.asyncio
async def test_generate_runfile_stressng(provider: BenchmarkRunnerSkillProvider):
    result = await provider.generate_runfile("stressng_pod", {})
    t = result.template
    assert t["harness"] == "benchmark-runner"
    assert "container_image" in t
    assert t["env_vars"]["WORKLOAD"] == "stressng_pod"
    assert t["env_vars"]["CLUSTER"] == "openshift"
    assert t["env_vars"]["RUN_TYPE"] == "func_ci"
    assert t["env_vars"]["SAVE_ARTIFACTS_LOCAL"] == "True"


@pytest.mark.asyncio
async def test_generate_runfile_custom_params(provider: BenchmarkRunnerSkillProvider):
    result = await provider.generate_runfile(
        "fio_pod", {"run_type": "perf_ci", "timeout": 1200, "scale": 4}
    )
    t = result.template
    assert t["env_vars"]["RUN_TYPE"] == "perf_ci"
    assert t["env_vars"]["TIMEOUT"] == "1200"
    assert t["env_vars"]["SCALE"] == "4"


@pytest.mark.asyncio
async def test_generate_runfile_unknown(provider: BenchmarkRunnerSkillProvider):
    result = await provider.generate_runfile("nonexistent", {})
    assert result.template == {}


@pytest.mark.asyncio
async def test_get_benchmark_params(provider: BenchmarkRunnerSkillProvider):
    params = await provider.get_benchmark_params("stressng_pod")
    assert params is not None
    assert "run_type" in params
    assert "timeout" in params


@pytest.mark.asyncio
async def test_get_benchmark_params_not_found(provider: BenchmarkRunnerSkillProvider):
    params = await provider.get_benchmark_params("nonexistent")
    assert params is None


@pytest.mark.asyncio
async def test_get_runfile_schema(provider: BenchmarkRunnerSkillProvider):
    schema = await provider.get_runfile_schema()
    assert schema is not None
    assert "env_vars" in schema["properties"]
    assert "container_image" in schema["properties"]


@pytest.mark.asyncio
async def test_get_example_runfile(provider: BenchmarkRunnerSkillProvider):
    example = await provider.get_example_runfile("stressng_pod")
    assert example is not None
    assert example["harness"] == "benchmark-runner"
    assert example["env_vars"]["WORKLOAD"] == "stressng_pod"


@pytest.mark.asyncio
async def test_get_example_runfile_not_found(provider: BenchmarkRunnerSkillProvider):
    example = await provider.get_example_runfile("nonexistent")
    assert example is None


@pytest.mark.asyncio
async def test_validate_runfile_valid(provider: BenchmarkRunnerSkillProvider):
    result = await provider.generate_runfile("stressng_pod", {})
    validation = await provider.validate_runfile(result.template)
    assert validation["valid"] is True
    assert validation["errors"] == []


@pytest.mark.asyncio
async def test_validate_runfile_missing_workload(provider: BenchmarkRunnerSkillProvider):
    validation = await provider.validate_runfile({
        "container_image": "quay.io/test",
        "env_vars": {},
    })
    assert validation["valid"] is False
    assert any("WORKLOAD" in e for e in validation["errors"])


@pytest.mark.asyncio
async def test_validate_runfile_unknown_workload(provider: BenchmarkRunnerSkillProvider):
    validation = await provider.validate_runfile({
        "container_image": "quay.io/test",
        "env_vars": {"WORKLOAD": "unknown_thing"},
    })
    assert validation["valid"] is False
    assert any("unknown_thing" in e for e in validation["errors"])


@pytest.mark.asyncio
async def test_validate_runfile_missing_image(provider: BenchmarkRunnerSkillProvider):
    validation = await provider.validate_runfile({
        "env_vars": {"WORKLOAD": "stressng_pod"},
    })
    assert validation["valid"] is False
    assert any("container_image" in e for e in validation["errors"])


@pytest.mark.asyncio
async def test_validate_runfile_invalid_cluster(provider: BenchmarkRunnerSkillProvider):
    validation = await provider.validate_runfile({
        "container_image": "quay.io/test",
        "env_vars": {"WORKLOAD": "stressng_pod", "CLUSTER": "invalid"},
    })
    assert validation["valid"] is False
    assert any("CLUSTER" in e for e in validation["errors"])


@pytest.mark.asyncio
async def test_validate_generated_runfiles_pass(provider: BenchmarkRunnerSkillProvider):
    for benchmark in ("stressng_pod", "fio_pod", "uperf_pod", "sysbench_pod"):
        result = await provider.generate_runfile(benchmark, {})
        validation = await provider.validate_runfile(result.template)
        assert validation["valid"] is True, f"{benchmark}: {validation['errors']}"
