from __future__ import annotations

import pytest

from providers.skills.vstorm import VstormSkillProvider


@pytest.fixture
def provider() -> VstormSkillProvider:
    return VstormSkillProvider()


@pytest.mark.asyncio
async def test_list_benchmarks(provider: VstormSkillProvider):
    benchmarks = await provider.list_benchmarks()
    names = [b.name for b in benchmarks]
    assert len(benchmarks) == 3
    assert "vstorm-containerdisk" in names
    assert "vstorm-stress-ng" in names
    assert "vstorm-dirty-pages" in names


@pytest.mark.asyncio
async def test_harness_field(provider: VstormSkillProvider):
    benchmarks = await provider.list_benchmarks()
    for b in benchmarks:
        assert b.harness == "vstorm"


@pytest.mark.asyncio
async def test_endpoint_types_kube(provider: VstormSkillProvider):
    benchmarks = await provider.list_benchmarks()
    for b in benchmarks:
        assert "kube" in b.endpoint_types


@pytest.mark.asyncio
async def test_get_benchmark_found(provider: VstormSkillProvider):
    b = await provider.get_benchmark("vstorm-containerdisk")
    assert b is not None
    assert b.name == "vstorm-containerdisk"
    assert b.harness == "vstorm"


@pytest.mark.asyncio
async def test_get_benchmark_not_found(provider: VstormSkillProvider):
    b = await provider.get_benchmark("nonexistent")
    assert b is None


@pytest.mark.asyncio
async def test_resolve_benchmark_vstorm(provider: VstormSkillProvider):
    result = await provider.resolve_benchmark(
        {"description": "vstorm vm boot test"}
    )
    assert result is not None
    assert result.startswith("vstorm-")


@pytest.mark.asyncio
async def test_resolve_benchmark_containerdisk(provider: VstormSkillProvider):
    result = await provider.resolve_benchmark(
        {"description": "containerdisk vm scale"}
    )
    assert result == "vstorm-containerdisk"


@pytest.mark.asyncio
async def test_resolve_benchmark_dirty_pages(provider: VstormSkillProvider):
    result = await provider.resolve_benchmark(
        {"description": "dirty pages live migration test"}
    )
    assert result == "vstorm-dirty-pages"


@pytest.mark.asyncio
async def test_resolve_benchmark_no_match(provider: VstormSkillProvider):
    result = await provider.resolve_benchmark(
        {"description": "network throughput netperf"}
    )
    assert result is None


@pytest.mark.asyncio
async def test_generate_runfile_containerdisk(provider: VstormSkillProvider):
    result = await provider.generate_runfile("vstorm-containerdisk", {})
    t = result.template
    assert t["harness"] == "vstorm"
    assert "cli_args" in t
    args = t["cli_args"]
    assert "--containerdisk" in args
    assert "--vms=4" in args
    assert "--namespaces=1" in args
    assert "--cores=1" in args
    assert "--memory=1Gi" in args
    assert "--wait" in args
    assert not any(a.startswith("--cloudinit=") for a in args)


@pytest.mark.asyncio
async def test_generate_runfile_stress_ng(provider: VstormSkillProvider):
    result = await provider.generate_runfile("vstorm-stress-ng", {})
    args = result.template["cli_args"]
    assert "--containerdisk" in args
    assert any(a.startswith("--cloudinit=") for a in args)
    assert any("stress-ng" in a for a in args)
    assert "--env=WORKLOAD_TYPE=memory-heavy" in args


@pytest.mark.asyncio
async def test_generate_runfile_stress_ng_cpu_heavy(provider: VstormSkillProvider):
    result = await provider.generate_runfile(
        "vstorm-stress-ng", {"workload_type": "cpu-heavy"}
    )
    args = result.template["cli_args"]
    assert "--env=WORKLOAD_TYPE=cpu-heavy" in args


@pytest.mark.asyncio
async def test_generate_runfile_dirty_pages(provider: VstormSkillProvider):
    result = await provider.generate_runfile("vstorm-dirty-pages", {})
    args = result.template["cli_args"]
    assert "--containerdisk" in args
    assert any(a.startswith("--cloudinit=") for a in args)
    assert any("dirty-mem-pages" in a for a in args)
    assert "--env=DIRTY_RATE_FRACTION=0.5" in args


@pytest.mark.asyncio
async def test_generate_runfile_custom_params(provider: VstormSkillProvider):
    result = await provider.generate_runfile(
        "vstorm-containerdisk",
        {"vms": 10, "namespaces": 2, "cores": 2, "memory": "2Gi"},
    )
    args = result.template["cli_args"]
    assert "--vms=10" in args
    assert "--namespaces=2" in args
    assert "--cores=2" in args
    assert "--memory=2Gi" in args


@pytest.mark.asyncio
async def test_generate_runfile_no_wait(provider: VstormSkillProvider):
    result = await provider.generate_runfile(
        "vstorm-containerdisk", {"wait": False}
    )
    args = result.template["cli_args"]
    assert "--wait" not in args


@pytest.mark.asyncio
async def test_generate_runfile_unknown(provider: VstormSkillProvider):
    result = await provider.generate_runfile("nonexistent", {})
    assert result.template == {}


@pytest.mark.asyncio
async def test_get_benchmark_params(provider: VstormSkillProvider):
    params = await provider.get_benchmark_params("vstorm-containerdisk")
    assert params is not None
    assert "vms" in params
    assert "namespaces" in params
    assert "cores" in params
    assert "memory" in params


@pytest.mark.asyncio
async def test_get_benchmark_params_not_found(provider: VstormSkillProvider):
    params = await provider.get_benchmark_params("nonexistent")
    assert params is None


@pytest.mark.asyncio
async def test_get_runfile_schema(provider: VstormSkillProvider):
    schema = await provider.get_runfile_schema()
    assert schema is not None
    assert schema["type"] == "object"
    assert "cli_args" in schema["properties"]


@pytest.mark.asyncio
async def test_get_example_runfile(provider: VstormSkillProvider):
    example = await provider.get_example_runfile("vstorm-containerdisk")
    assert example is not None
    assert example["harness"] == "vstorm"
    assert "cli_args" in example


@pytest.mark.asyncio
async def test_get_example_runfile_not_found(provider: VstormSkillProvider):
    example = await provider.get_example_runfile("nonexistent")
    assert example is None


@pytest.mark.asyncio
async def test_validate_runfile_valid(provider: VstormSkillProvider):
    result = await provider.generate_runfile("vstorm-containerdisk", {})
    validation = await provider.validate_runfile(result.template)
    assert validation["valid"] is True
    assert validation["errors"] == []


@pytest.mark.asyncio
async def test_validate_runfile_missing_cli_args(provider: VstormSkillProvider):
    validation = await provider.validate_runfile({"harness": "vstorm"})
    assert validation["valid"] is False
    assert any("cli_args" in e for e in validation["errors"])


@pytest.mark.asyncio
async def test_validate_runfile_missing_vms(provider: VstormSkillProvider):
    validation = await provider.validate_runfile({
        "cli_args": ["--containerdisk", "--namespaces=1"],
    })
    assert validation["valid"] is False
    assert any("vms" in e.lower() for e in validation["errors"])


@pytest.mark.asyncio
async def test_validate_runfile_bad_vms(provider: VstormSkillProvider):
    validation = await provider.validate_runfile({
        "cli_args": ["--containerdisk", "--vms=0"],
    })
    assert validation["valid"] is False


@pytest.mark.asyncio
async def test_validate_generated_runfile_passes(provider: VstormSkillProvider):
    for benchmark in ("vstorm-containerdisk", "vstorm-stress-ng", "vstorm-dirty-pages"):
        result = await provider.generate_runfile(benchmark, {})
        validation = await provider.validate_runfile(result.template)
        assert validation["valid"] is True, f"{benchmark}: {validation['errors']}"


@pytest.mark.asyncio
async def test_get_default_config(provider: VstormSkillProvider):
    config = await provider.get_default_config()
    assert "provisioning" in config
    assert "execution" in config
    prov = config["provisioning"]
    assert prov["install_method"] == "git_clone"
    assert "vstorm" in prov["git_url"]
    assert prov["verify_command"] == "/opt/vstorm/vstorm -h"
    exe = config["execution"]
    assert exe["controller_required"] is True
    assert exe["endpoint_type"] == "kube"
