from __future__ import annotations

import os

import pytest

from providers.skills.crucible import CrucibleSkillProvider

CRUCIBLE_HOME = os.environ.get("CRUCIBLE_HOME", "/home/atheurer/swdev/repos/crucible")
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
