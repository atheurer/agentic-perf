from __future__ import annotations

from pathlib import Path

import pytest

from providers.skills.zathras import ZathrasSkillProvider


@pytest.fixture
def provider(tmp_zathras_repo: Path) -> ZathrasSkillProvider:
    return ZathrasSkillProvider(tmp_zathras_repo)


@pytest.mark.asyncio
async def test_list_benchmarks(provider: ZathrasSkillProvider):
    benchmarks = await provider.list_benchmarks()
    names = [b.name for b in benchmarks]
    assert "streams" in names
    assert "fio" in names
    assert "uperf" in names
    assert "coremark" in names
    assert "specjbb" in names
    assert len(benchmarks) == 5


@pytest.mark.asyncio
async def test_list_benchmarks_harness_field(provider: ZathrasSkillProvider):
    benchmarks = await provider.list_benchmarks()
    for b in benchmarks:
        assert b.harness == "zathras"


@pytest.mark.asyncio
async def test_list_benchmarks_network_roles(provider: ZathrasSkillProvider):
    benchmarks = await provider.list_benchmarks()
    uperf = next(b for b in benchmarks if b.name == "uperf")
    assert uperf.roles == ["client", "server"]
    assert uperf.min_hosts == 2


@pytest.mark.asyncio
async def test_list_benchmarks_single_host_roles(provider: ZathrasSkillProvider):
    benchmarks = await provider.list_benchmarks()
    streams = next(b for b in benchmarks if b.name == "streams")
    assert streams.roles == ["client"]
    assert streams.min_hosts == 1


@pytest.mark.asyncio
async def test_list_benchmarks_supported_params(provider: ZathrasSkillProvider):
    benchmarks = await provider.list_benchmarks()
    fio = next(b for b in benchmarks if b.name == "fio")
    assert fio.supported_params.get("storage_required") is True

    specjbb = next(b for b in benchmarks if b.name == "specjbb")
    assert specjbb.supported_params.get("java_required") is True


@pytest.mark.asyncio
async def test_get_benchmark_found(provider: ZathrasSkillProvider):
    result = await provider.get_benchmark("streams")
    assert result is not None
    assert result.name == "streams"
    assert result.harness == "zathras"


@pytest.mark.asyncio
async def test_get_benchmark_not_found(provider: ZathrasSkillProvider):
    result = await provider.get_benchmark("nonexistent")
    assert result is None


@pytest.mark.asyncio
async def test_resolve_benchmark_memory(provider: ZathrasSkillProvider):
    result = await provider.resolve_benchmark(
        {"description": "memory bandwidth test", "workload_type": "memory"}
    )
    assert result == "streams"


@pytest.mark.asyncio
async def test_resolve_benchmark_storage(provider: ZathrasSkillProvider):
    result = await provider.resolve_benchmark(
        {"description": "disk io performance", "workload_type": "storage"}
    )
    assert result == "fio"


@pytest.mark.asyncio
async def test_resolve_benchmark_network(provider: ZathrasSkillProvider):
    result = await provider.resolve_benchmark(
        {"description": "network throughput", "workload_type": "network"}
    )
    assert result == "uperf"


@pytest.mark.asyncio
async def test_resolve_benchmark_no_match(provider: ZathrasSkillProvider):
    result = await provider.resolve_benchmark(
        {"description": "quantum entanglement", "workload_type": "physics"}
    )
    assert result is None


@pytest.mark.asyncio
async def test_generate_runfile_single_host(provider: ZathrasSkillProvider):
    result = await provider.generate_runfile("streams", {
        "endpoints": [{"host": "10.0.0.1", "roles": ["client"]}],
    })
    assert result.benchmark == "streams"
    template = result.template
    assert template["harness"] == "zathras"
    assert template["scenario"]["global"]["system_type"] == "local"
    assert template["scenario"]["systems"]["system1"]["tests"] == "streams"
    assert template["scenario"]["systems"]["system1"]["host_config"] == "10.0.0.1"
    assert template["local_config"] is None


@pytest.mark.asyncio
async def test_generate_runfile_multi_host_network(provider: ZathrasSkillProvider):
    result = await provider.generate_runfile("uperf", {
        "endpoints": [
            {"host": "10.0.0.1", "roles": ["client"]},
            {"host": "10.0.0.2", "roles": ["server"]},
        ],
    })
    template = result.template
    local_config = template["local_config"]
    assert local_config is not None
    assert local_config["server_ips"] == "10.0.0.2"
    assert local_config["client_ips"] == "10.0.0.1"


@pytest.mark.asyncio
async def test_generate_runfile_storage(provider: ZathrasSkillProvider):
    result = await provider.generate_runfile("fio", {
        "endpoints": [{"host": "10.0.0.1", "roles": ["client"]}],
        "storage": "/dev/nvme0n1,/dev/nvme1n1",
    })
    template = result.template
    assert template["local_config"]["storage"] == "/dev/nvme0n1,/dev/nvme1n1"


@pytest.mark.asyncio
async def test_missing_test_defs(tmp_path: Path):
    provider = ZathrasSkillProvider(tmp_path / "nonexistent")
    benchmarks = await provider.list_benchmarks()
    assert benchmarks == []

    result = await provider.resolve_benchmark({"description": "anything"})
    assert result is None
