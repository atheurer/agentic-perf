from __future__ import annotations

import pytest

from providers.skills.clusterbuster import ClusterbusterSkillProvider


@pytest.fixture
def provider() -> ClusterbusterSkillProvider:
    return ClusterbusterSkillProvider()


@pytest.mark.asyncio
async def test_list_benchmarks(provider: ClusterbusterSkillProvider):
    benchmarks = await provider.list_benchmarks()
    names = [b.name for b in benchmarks]
    assert len(benchmarks) == 8
    assert "cb-cpusoaker" in names
    assert "cb-fio" in names
    assert "cb-uperf" in names
    assert "cb-sysbench" in names
    assert "cb-memory" in names
    assert "cb-files" in names
    assert "cb-hammerdb" in names
    assert "cb-server" in names


@pytest.mark.asyncio
async def test_harness_field(provider: ClusterbusterSkillProvider):
    benchmarks = await provider.list_benchmarks()
    for b in benchmarks:
        assert b.harness == "clusterbuster"


@pytest.mark.asyncio
async def test_endpoint_types_kube(provider: ClusterbusterSkillProvider):
    benchmarks = await provider.list_benchmarks()
    for b in benchmarks:
        assert "kube" in b.endpoint_types


@pytest.mark.asyncio
async def test_get_benchmark_found(provider: ClusterbusterSkillProvider):
    b = await provider.get_benchmark("cb-cpusoaker")
    assert b is not None
    assert b.name == "cb-cpusoaker"
    assert b.harness == "clusterbuster"


@pytest.mark.asyncio
async def test_get_benchmark_not_found(provider: ClusterbusterSkillProvider):
    b = await provider.get_benchmark("nonexistent")
    assert b is None


@pytest.mark.asyncio
async def test_resolve_benchmark_clusterbuster(provider: ClusterbusterSkillProvider):
    result = await provider.resolve_benchmark(
        {"description": "clusterbuster cpu stress"}
    )
    assert result == "cb-cpusoaker"


@pytest.mark.asyncio
async def test_resolve_benchmark_hammerdb(provider: ClusterbusterSkillProvider):
    result = await provider.resolve_benchmark(
        {"description": "hammerdb database tpc-c benchmark"}
    )
    assert result == "cb-hammerdb"


@pytest.mark.asyncio
async def test_resolve_benchmark_sysbench(provider: ClusterbusterSkillProvider):
    result = await provider.resolve_benchmark(
        {"description": "sysbench cpu test"}
    )
    assert result == "cb-sysbench"


@pytest.mark.asyncio
async def test_resolve_benchmark_no_match(provider: ClusterbusterSkillProvider):
    result = await provider.resolve_benchmark(
        {"description": "network throughput with netperf"}
    )
    assert result is None


@pytest.mark.asyncio
async def test_generate_runfile_cpusoaker(provider: ClusterbusterSkillProvider):
    result = await provider.generate_runfile("cb-cpusoaker", {})
    t = result.template
    assert t["harness"] == "clusterbuster"
    assert "job_file" in t
    options = t["job_file"]["options"]
    assert options["workload"] == "cpusoaker"
    assert options["workloadruntime"] == 10
    assert options["cleanup"] is True
    assert options["precleanup"] is True
    assert options["exit-at-end"] is True
    assert options["deps-per-namespace"] == 8
    assert options["processes"] == 3
    assert options["namespaces"] == 1


@pytest.mark.asyncio
async def test_generate_runfile_uperf(provider: ClusterbusterSkillProvider):
    result = await provider.generate_runfile("cb-uperf", {})
    options = result.template["job_file"]["options"]
    assert options["workload"] == "uperf"
    assert options["uperf-msg-size"] == 8192
    assert options["uperf-test-type"] == "stream"
    assert options["uperf-proto"] == "tcp"
    assert options["antiaffinity"] is True


@pytest.mark.asyncio
async def test_generate_runfile_sysbench(provider: ClusterbusterSkillProvider):
    result = await provider.generate_runfile(
        "cb-sysbench", {"sysbench_workload": "memory"}
    )
    options = result.template["job_file"]["options"]
    assert options["workload"] == "sysbench"
    assert options["sysbench-workload"] == "memory"


@pytest.mark.asyncio
async def test_generate_runfile_hammerdb(provider: ClusterbusterSkillProvider):
    result = await provider.generate_runfile("cb-hammerdb", {})
    options = result.template["job_file"]["options"]
    assert options["workload"] == "hammerdb"
    assert options["hammerdb-driver"] == "pg"
    assert options["hammerdb-benchmark"] == "tpcc"
    assert options["hammerdb-virtual-users"] == 4
    assert options["workloadruntime"] == 180


@pytest.mark.asyncio
async def test_generate_runfile_custom_params(provider: ClusterbusterSkillProvider):
    result = await provider.generate_runfile(
        "cb-cpusoaker",
        {"workloadruntime": 60, "namespaces": 2, "deps_per_namespace": 4, "processes": 5},
    )
    options = result.template["job_file"]["options"]
    assert options["workloadruntime"] == 60
    assert options["namespaces"] == 2
    assert options["deps-per-namespace"] == 4
    assert options["processes"] == 5


@pytest.mark.asyncio
async def test_generate_runfile_unknown(provider: ClusterbusterSkillProvider):
    result = await provider.generate_runfile("nonexistent", {})
    assert result.template == {}


@pytest.mark.asyncio
async def test_get_benchmark_params(provider: ClusterbusterSkillProvider):
    params = await provider.get_benchmark_params("cb-cpusoaker")
    assert params is not None
    assert "workloadruntime" in params
    assert "namespaces" in params
    assert "deps_per_namespace" in params
    assert "processes" in params


@pytest.mark.asyncio
async def test_get_benchmark_params_not_found(provider: ClusterbusterSkillProvider):
    params = await provider.get_benchmark_params("nonexistent")
    assert params is None


@pytest.mark.asyncio
async def test_get_runfile_schema(provider: ClusterbusterSkillProvider):
    schema = await provider.get_runfile_schema()
    assert schema is not None
    assert schema["type"] == "object"
    assert "job_file" in schema["properties"]


@pytest.mark.asyncio
async def test_get_example_runfile(provider: ClusterbusterSkillProvider):
    example = await provider.get_example_runfile("cb-cpusoaker")
    assert example is not None
    assert example["harness"] == "clusterbuster"
    assert "job_file" in example


@pytest.mark.asyncio
async def test_get_example_runfile_not_found(provider: ClusterbusterSkillProvider):
    example = await provider.get_example_runfile("nonexistent")
    assert example is None


@pytest.mark.asyncio
async def test_validate_runfile_valid(provider: ClusterbusterSkillProvider):
    result = await provider.generate_runfile("cb-cpusoaker", {})
    validation = await provider.validate_runfile(result.template)
    assert validation["valid"] is True
    assert validation["errors"] == []


@pytest.mark.asyncio
async def test_validate_runfile_missing_job_file(provider: ClusterbusterSkillProvider):
    validation = await provider.validate_runfile({"harness": "clusterbuster"})
    assert validation["valid"] is False
    assert any("job_file" in e for e in validation["errors"])


@pytest.mark.asyncio
async def test_validate_runfile_missing_workload(provider: ClusterbusterSkillProvider):
    validation = await provider.validate_runfile({
        "job_file": {"options": {"workloadruntime": 10}},
    })
    assert validation["valid"] is False
    assert any("workload" in e for e in validation["errors"])


@pytest.mark.asyncio
async def test_validate_runfile_invalid_workload(provider: ClusterbusterSkillProvider):
    validation = await provider.validate_runfile({
        "job_file": {"options": {"workload": "invalid_wl", "workloadruntime": 10}},
    })
    assert validation["valid"] is False
    assert any("invalid_wl" in e for e in validation["errors"])


@pytest.mark.asyncio
async def test_validate_runfile_bad_runtime(provider: ClusterbusterSkillProvider):
    validation = await provider.validate_runfile({
        "job_file": {"options": {"workload": "cpusoaker", "workloadruntime": 0}},
    })
    assert validation["valid"] is False
    assert any("workloadruntime" in e for e in validation["errors"])


@pytest.mark.asyncio
async def test_validate_generated_runfile_passes(provider: ClusterbusterSkillProvider):
    for benchmark in (
        "cb-cpusoaker", "cb-fio", "cb-uperf", "cb-sysbench",
        "cb-memory", "cb-files", "cb-hammerdb", "cb-server",
    ):
        result = await provider.generate_runfile(benchmark, {})
        validation = await provider.validate_runfile(result.template)
        assert validation["valid"] is True, f"{benchmark}: {validation['errors']}"


@pytest.mark.asyncio
async def test_get_default_config(provider: ClusterbusterSkillProvider):
    config = await provider.get_default_config()
    assert "provisioning" in config
    assert "execution" in config
    prov = config["provisioning"]
    assert prov["install_method"] == "git_clone"
    assert "clusterbuster" in prov["git_url"]
    assert prov["verify_command"] == "clusterbuster --help"
    exe = config["execution"]
    assert exe["controller_required"] is True
    assert exe["endpoint_type"] == "kube"
