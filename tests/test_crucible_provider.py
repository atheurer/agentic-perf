from __future__ import annotations

import os

import pytest

from providers.skills.crucible import CrucibleSkillProvider

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
    result = await provider.generate_runfile("fio", {
        "endpoints": [{"host": "10.0.0.1", "roles": ["client"]}],
        "userenv": "alma8",
        "osruntime": "podman",
    })
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
    result = await provider.generate_runfile("fio", {
        "endpoints": [{"host": "10.0.0.1", "roles": ["client"]}],
        "tags": {"environment": "test", "ticket": "PERF-100"},
    })
    assert result.template["tags"] == {"environment": "test", "ticket": "PERF-100"}


@pytest.mark.skipif(not HAS_CRUCIBLE, reason="CRUCIBLE_HOME not available")
@pytest.mark.asyncio
async def test_generate_runfile_no_endpoints(provider: CrucibleSkillProvider):
    result = await provider.generate_runfile("fio", {})
    assert "endpoints" not in result.template


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
async def test_get_example_runfile_missing():
    provider = CrucibleSkillProvider("/nonexistent")
    example = await provider.get_example_runfile("fio")
    assert example is None


@pytest.mark.skipif(not HAS_CRUCIBLE, reason="CRUCIBLE_HOME not available")
@pytest.mark.asyncio
async def test_generate_runfile_kube_endpoint(provider: CrucibleSkillProvider):
    """Kube endpoint with client+server roles produces correct flat structure."""
    result = await provider.generate_runfile("uperf", {
        "endpoint_type": "kube",
        "endpoints": [
            {"host": "10.0.0.1", "roles": ["client"]},
            {"host": "10.0.0.2", "roles": ["server"]},
        ],
        "controller_ip": "10.0.0.1",
        "kube_host": "10.0.0.1",
        "userenv": "default",
    })
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
    result = await provider.generate_runfile("fio", {
        "endpoint_type": "kube",
        "endpoints": [{"host": "10.0.0.1", "roles": ["client"]}],
        "controller_ip": "10.0.0.1",
        "kube_host": "10.0.0.1",
    })
    ep = result.template["endpoints"][0]
    assert ep["engines"] == {"client": "1"}
    assert "server" not in ep["engines"]


@pytest.mark.skipif(not HAS_CRUCIBLE, reason="CRUCIBLE_HOME not available")
@pytest.mark.asyncio
async def test_generate_runfile_remotehosts_unchanged(provider: CrucibleSkillProvider):
    """Remotehosts generation still works when endpoint_type is not specified."""
    result = await provider.generate_runfile("fio", {
        "endpoints": [{"host": "10.0.0.1", "roles": ["client"]}],
        "userenv": "alma8",
        "osruntime": "podman",
    })
    ep = result.template["endpoints"][0]
    assert ep["type"] == "remotehosts"
    assert "remotes" in ep
    assert ep["settings"]["userenv"] == "alma8"


@pytest.mark.skipif(not HAS_CRUCIBLE, reason="CRUCIBLE_HOME not available")
@pytest.mark.asyncio
async def test_generate_runfile_kube_has_config(provider: CrucibleSkillProvider):
    """Kube endpoint includes config array with userenv when non-default."""
    result = await provider.generate_runfile("fio", {
        "endpoint_type": "kube",
        "endpoints": [{"host": "10.0.0.1", "roles": ["client"]}],
        "controller_ip": "10.0.0.1",
        "kube_host": "10.0.0.1",
        "userenv": "alma8",
    })
    ep = result.template["endpoints"][0]
    assert ep["config"] == [{"targets": "default", "settings": {"userenv": "alma8"}}]


@pytest.mark.skipif(not HAS_CRUCIBLE, reason="CRUCIBLE_HOME not available")
@pytest.mark.asyncio
async def test_generate_runfile_kube_default_userenv_no_config(provider: CrucibleSkillProvider):
    """Kube endpoint omits config when userenv is 'default'."""
    result = await provider.generate_runfile("fio", {
        "endpoint_type": "kube",
        "endpoints": [{"host": "10.0.0.1", "roles": ["client"]}],
        "controller_ip": "10.0.0.1",
        "kube_host": "10.0.0.1",
        "userenv": "default",
    })
    ep = result.template["endpoints"][0]
    assert "config" not in ep


ALLOWED_RUNFILE_KEYS = {"benchmarks", "endpoints", "run-params", "schema", "tags", "tool-params"}


@pytest.mark.skipif(not HAS_CRUCIBLE, reason="CRUCIBLE_HOME not available")
@pytest.mark.asyncio
async def test_generate_runfile_only_valid_keys(provider: CrucibleSkillProvider):
    """Run-file template must only contain keys that crucible's blockbreaker schema allows."""
    result = await provider.generate_runfile("fio", {
        "endpoints": [{"host": "10.0.0.1", "roles": ["client"]}],
        "tags": {"env": "test"},
    })
    extra = set(result.template.keys()) - ALLOWED_RUNFILE_KEYS
    assert not extra, f"Run-file contains keys rejected by crucible schema: {extra}"


@pytest.mark.skipif(not HAS_CRUCIBLE, reason="CRUCIBLE_HOME not available")
@pytest.mark.asyncio
async def test_generate_runfile_minimal_only_valid_keys(provider: CrucibleSkillProvider):
    """Even a minimal run-file (no endpoints/tags) must not have extra keys."""
    result = await provider.generate_runfile("fio", {})
    extra = set(result.template.keys()) - ALLOWED_RUNFILE_KEYS
    assert not extra, f"Run-file contains keys rejected by crucible schema: {extra}"


@pytest.mark.skipif(not HAS_CRUCIBLE, reason="CRUCIBLE_HOME not available")
@pytest.mark.asyncio
async def test_validate_runfile_passes_for_generated(provider: CrucibleSkillProvider):
    """A run-file from generate_runfile must pass schema validation."""
    result = await provider.generate_runfile("fio", {
        "endpoints": [{"host": "10.0.0.1", "roles": ["client"]}],
        "userenv": "default",
        "osruntime": "podman",
    })
    validation = await provider.validate_runfile(result.template)
    assert validation["valid"], f"Generated run-file failed validation: {validation['errors']}"


@pytest.mark.skipif(not HAS_CRUCIBLE, reason="CRUCIBLE_HOME not available")
@pytest.mark.asyncio
async def test_validate_runfile_rejects_extra_keys(provider: CrucibleSkillProvider):
    """Run-file with extra top-level keys must fail validation."""
    bad_runfile = {"benchmarks": [], "harness": "crucible"}
    validation = await provider.validate_runfile(bad_runfile)
    assert not validation["valid"]
    assert any("harness" in e for e in validation["errors"])
