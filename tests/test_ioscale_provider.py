from __future__ import annotations

import pytest

from providers.skills.ioscale import IoscaleSkillProvider


@pytest.fixture
def provider() -> IoscaleSkillProvider:
    return IoscaleSkillProvider()


@pytest.mark.asyncio
async def test_list_benchmarks(provider: IoscaleSkillProvider):
    benchmarks = await provider.list_benchmarks()
    names = [b.name for b in benchmarks]
    assert len(benchmarks) == 3
    assert "ioscale-fio" in names
    assert "ioscale-mariadb" in names
    assert "ioscale-postgresql" in names


@pytest.mark.asyncio
async def test_harness_field(provider: IoscaleSkillProvider):
    benchmarks = await provider.list_benchmarks()
    for b in benchmarks:
        assert b.harness == "ioscale"


@pytest.mark.asyncio
async def test_endpoint_types_kube(provider: IoscaleSkillProvider):
    benchmarks = await provider.list_benchmarks()
    for b in benchmarks:
        assert "kube" in b.endpoint_types


@pytest.mark.asyncio
async def test_get_benchmark_found(provider: IoscaleSkillProvider):
    b = await provider.get_benchmark("ioscale-fio")
    assert b is not None
    assert b.name == "ioscale-fio"
    assert b.harness == "ioscale"


@pytest.mark.asyncio
async def test_get_benchmark_not_found(provider: IoscaleSkillProvider):
    b = await provider.get_benchmark("nonexistent")
    assert b is None


@pytest.mark.asyncio
async def test_resolve_benchmark_fio(provider: IoscaleSkillProvider):
    result = await provider.resolve_benchmark(
        {"description": "fio vm storage test"}
    )
    assert result == "ioscale-fio"


@pytest.mark.asyncio
async def test_resolve_benchmark_mariadb(provider: IoscaleSkillProvider):
    result = await provider.resolve_benchmark(
        {"description": "mariadb vm database benchmark"}
    )
    assert result == "ioscale-mariadb"


@pytest.mark.asyncio
async def test_resolve_benchmark_no_match(provider: IoscaleSkillProvider):
    result = await provider.resolve_benchmark(
        {"description": "network throughput pod test"}
    )
    assert result is None


@pytest.mark.asyncio
async def test_generate_runfile_fio(provider: IoscaleSkillProvider):
    result = await provider.generate_runfile("ioscale-fio", {})
    t = result.template
    assert t["harness"] == "ioscale"
    assert t["test_type"] == "fio"
    assert "vm_config" in t
    assert "test_config" in t
    assert "fio" in t["test_config"]
    fio = t["test_config"]["fio"]
    assert fio["test_size"] == "1G"
    assert fio["runtime"] == 300
    assert fio["numjobs"] == 1
    assert fio["iodepth"] == 16


@pytest.mark.asyncio
async def test_generate_runfile_mariadb(provider: IoscaleSkillProvider):
    result = await provider.generate_runfile("ioscale-mariadb", {})
    t = result.template
    assert t["test_type"] == "mariadb"
    assert "database" in t["test_config"]
    db = t["test_config"]["database"]
    assert db["warehouse_count"] == 50
    assert db["test_duration"] == 15
    assert db["user_count"] == "1 5 10"


@pytest.mark.asyncio
async def test_generate_runfile_postgresql(provider: IoscaleSkillProvider):
    result = await provider.generate_runfile("ioscale-postgresql", {})
    t = result.template
    assert t["test_type"] == "postgresql"
    assert "database" in t["test_config"]


@pytest.mark.asyncio
async def test_generate_runfile_custom_storage(provider: IoscaleSkillProvider):
    result = await provider.generate_runfile(
        "ioscale-fio", {"storage_class": "lvms-vg1", "storage_size": "50Gi"}
    )
    vm = result.template["vm_config"]
    assert vm["storage_class"] == "lvms-vg1"
    assert vm["storage_size"] == "50Gi"


@pytest.mark.asyncio
async def test_generate_runfile_custom_fio_params(provider: IoscaleSkillProvider):
    result = await provider.generate_runfile(
        "ioscale-fio",
        {"test_size": "10G", "runtime": 600, "block_sizes": "4k 8k",
         "io_patterns": "randread randwrite", "numjobs": 4},
    )
    fio = result.template["test_config"]["fio"]
    assert fio["test_size"] == "10G"
    assert fio["runtime"] == 600
    assert fio["block_sizes"] == "4k 8k"
    assert fio["numjobs"] == 4


@pytest.mark.asyncio
async def test_generate_runfile_unknown(provider: IoscaleSkillProvider):
    result = await provider.generate_runfile("nonexistent", {})
    assert result.template == {}


@pytest.mark.asyncio
async def test_get_benchmark_params(provider: IoscaleSkillProvider):
    params = await provider.get_benchmark_params("ioscale-fio")
    assert params is not None
    assert "test_size" in params
    assert "runtime" in params
    assert "block_sizes" in params
    assert "storage_class" in params
    assert "vm_cores" in params


@pytest.mark.asyncio
async def test_get_benchmark_params_not_found(provider: IoscaleSkillProvider):
    params = await provider.get_benchmark_params("nonexistent")
    assert params is None


@pytest.mark.asyncio
async def test_get_runfile_schema(provider: IoscaleSkillProvider):
    schema = await provider.get_runfile_schema()
    assert schema is not None
    assert schema["type"] == "object"
    assert "vm_config" in schema["properties"]
    assert "test_type" in schema["properties"]


@pytest.mark.asyncio
async def test_get_example_runfile(provider: IoscaleSkillProvider):
    example = await provider.get_example_runfile("ioscale-fio")
    assert example is not None
    assert example["harness"] == "ioscale"
    assert "vm_config" in example


@pytest.mark.asyncio
async def test_get_example_runfile_not_found(provider: IoscaleSkillProvider):
    example = await provider.get_example_runfile("nonexistent")
    assert example is None


@pytest.mark.asyncio
async def test_validate_runfile_valid(provider: IoscaleSkillProvider):
    result = await provider.generate_runfile("ioscale-fio", {})
    validation = await provider.validate_runfile(result.template)
    assert validation["valid"] is True
    assert validation["errors"] == []


@pytest.mark.asyncio
async def test_validate_runfile_missing_test_type(provider: IoscaleSkillProvider):
    validation = await provider.validate_runfile({
        "vm_config": {}, "test_config": {},
    })
    assert validation["valid"] is False
    assert any("test_type" in e for e in validation["errors"])


@pytest.mark.asyncio
async def test_validate_runfile_invalid_test_type(provider: IoscaleSkillProvider):
    validation = await provider.validate_runfile({
        "test_type": "invalid", "vm_config": {}, "test_config": {},
    })
    assert validation["valid"] is False
    assert any("invalid" in e for e in validation["errors"])


@pytest.mark.asyncio
async def test_validate_runfile_fio_missing_section(provider: IoscaleSkillProvider):
    validation = await provider.validate_runfile({
        "test_type": "fio", "vm_config": {}, "test_config": {},
    })
    assert validation["valid"] is False
    assert any("fio" in e for e in validation["errors"])


@pytest.mark.asyncio
async def test_validate_runfile_db_missing_section(provider: IoscaleSkillProvider):
    validation = await provider.validate_runfile({
        "test_type": "mariadb", "vm_config": {}, "test_config": {},
    })
    assert validation["valid"] is False
    assert any("database" in e for e in validation["errors"])


@pytest.mark.asyncio
async def test_validate_generated_runfile_passes(provider: IoscaleSkillProvider):
    for benchmark in ("ioscale-fio", "ioscale-mariadb", "ioscale-postgresql"):
        result = await provider.generate_runfile(benchmark, {})
        validation = await provider.validate_runfile(result.template)
        assert validation["valid"] is True, f"{benchmark}: {validation['errors']}"


@pytest.mark.asyncio
async def test_get_default_config(provider: IoscaleSkillProvider):
    config = await provider.get_default_config()
    assert "provisioning" in config
    assert "execution" in config
    prov = config["provisioning"]
    assert prov["install_method"] == "git_clone"
    assert "ioscale" in prov["git_url"]
    exe = config["execution"]
    assert exe["controller_required"] is True
    assert exe["endpoint_type"] == "kube"


@pytest.mark.asyncio
async def test_vm_config_has_image_url(provider: IoscaleSkillProvider):
    result = await provider.generate_runfile("ioscale-fio", {})
    vm = result.template["vm_config"]
    assert "image_url" in vm
    assert "fedora" in vm["image_url"].lower()
