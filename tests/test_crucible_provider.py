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
    assert template["harness"] == "crucible"
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
async def test_generate_runfile_harness_field(provider: CrucibleSkillProvider):
    result = await provider.generate_runfile("fio", {})
    assert result.template["harness"] == "crucible"
