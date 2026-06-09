from __future__ import annotations

import pytest

from providers.skills.base import BenchmarkSuite, RunfileTemplate
from providers.skills.multi import MultiHarnessSkillProvider
from providers.skills.private import PrivateSkillProvider

from tests.conftest import MockSkillProvider


CRUCIBLE_BENCHMARKS = [
    BenchmarkSuite(name="fio", description="Crucible fio", roles=["client"], min_hosts=1, harness="crucible"),
    BenchmarkSuite(name="uperf", description="Crucible uperf", roles=["client", "server"], min_hosts=2, harness="crucible"),
    BenchmarkSuite(name="trafficgen", description="Crucible trafficgen", roles=["client", "server"], min_hosts=2, harness="crucible"),
]

ZATHRAS_BENCHMARKS = [
    BenchmarkSuite(name="fio", description="Zathras fio", roles=["client"], min_hosts=1, harness="zathras"),
    BenchmarkSuite(name="streams", description="Zathras streams", roles=["client"], min_hosts=1, harness="zathras"),
    BenchmarkSuite(name="linpack", description="Zathras linpack", roles=["client"], min_hosts=1, harness="zathras"),
]


@pytest.fixture
def mock_crucible() -> MockSkillProvider:
    return MockSkillProvider(
        benchmarks=CRUCIBLE_BENCHMARKS,
        resolve_result="fio",
        runfile_template=RunfileTemplate(benchmark="fio", template={"harness": "crucible"}),
    )


@pytest.fixture
def mock_zathras() -> MockSkillProvider:
    return MockSkillProvider(
        benchmarks=ZATHRAS_BENCHMARKS,
        resolve_result="streams",
        runfile_template=RunfileTemplate(benchmark="streams", template={"harness": "zathras"}),
    )


@pytest.fixture
def multi(mock_crucible, mock_zathras) -> MultiHarnessSkillProvider:
    return MultiHarnessSkillProvider(
        harnesses={"crucible": mock_crucible, "zathras": mock_zathras},
        default_harness="crucible",
    )


@pytest.mark.asyncio
async def test_list_benchmarks_aggregates(multi: MultiHarnessSkillProvider):
    benchmarks = await multi.list_benchmarks()
    names = [b.name for b in benchmarks]
    assert "trafficgen" in names
    assert "streams" in names
    assert "linpack" in names
    assert names.count("fio") == 2


@pytest.mark.asyncio
async def test_list_harnesses(multi: MultiHarnessSkillProvider):
    harnesses = multi.list_harnesses()
    assert "crucible" in harnesses
    assert "zathras" in harnesses


@pytest.mark.asyncio
async def test_get_benchmark_prefers_default(multi: MultiHarnessSkillProvider):
    result = await multi.get_benchmark("fio")
    assert result is not None
    assert result.harness == "crucible"


@pytest.mark.asyncio
async def test_get_benchmark_fallback(multi: MultiHarnessSkillProvider):
    result = await multi.get_benchmark("streams")
    assert result is not None
    assert result.harness == "zathras"


@pytest.mark.asyncio
async def test_get_benchmark_not_found(multi: MultiHarnessSkillProvider):
    result = await multi.get_benchmark("nonexistent")
    assert result is None


@pytest.mark.asyncio
async def test_resolve_benchmark_with_harness_pref(multi: MultiHarnessSkillProvider):
    result = await multi.resolve_benchmark({
        "description": "anything",
        "harness": "zathras",
    })
    assert result == "streams"


@pytest.mark.asyncio
async def test_resolve_benchmark_default_preference(multi: MultiHarnessSkillProvider):
    result = await multi.resolve_benchmark({"description": "storage"})
    assert result == "fio"


@pytest.mark.asyncio
async def test_resolve_benchmark_fallback_to_other(mock_zathras):
    crucible_no_match = MockSkillProvider(
        benchmarks=CRUCIBLE_BENCHMARKS,
        resolve_result=None,
    )
    multi = MultiHarnessSkillProvider(
        harnesses={"crucible": crucible_no_match, "zathras": mock_zathras},
        default_harness="crucible",
    )
    result = await multi.resolve_benchmark({"description": "memory"})
    assert result == "streams"


@pytest.mark.asyncio
async def test_generate_runfile_delegates_by_harness_param(multi: MultiHarnessSkillProvider):
    result = await multi.generate_runfile("fio", {"harness": "zathras"})
    assert result.template["harness"] == "zathras"


@pytest.mark.asyncio
async def test_generate_runfile_delegates_by_benchmark_lookup(multi: MultiHarnessSkillProvider):
    result = await multi.generate_runfile("streams", {})
    assert result.template["harness"] == "zathras"


@pytest.mark.asyncio
async def test_generate_runfile_defaults_to_default_harness(multi: MultiHarnessSkillProvider):
    result = await multi.generate_runfile("fio", {})
    assert result.template["harness"] == "crucible"


@pytest.mark.asyncio
async def test_find_capable_harnesses_both(multi: MultiHarnessSkillProvider):
    capable = await multi.find_capable_harnesses("fio")
    harness_names = [c["harness"] for c in capable]
    assert "crucible" in harness_names
    assert "zathras" in harness_names
    default_entry = next(c for c in capable if c["harness"] == "crucible")
    assert default_entry["is_default"] is True


@pytest.mark.asyncio
async def test_find_capable_harnesses_one(multi: MultiHarnessSkillProvider):
    capable = await multi.find_capable_harnesses("streams")
    assert len(capable) == 1
    assert capable[0]["harness"] == "zathras"


@pytest.mark.asyncio
async def test_find_capable_harnesses_none(multi: MultiHarnessSkillProvider):
    capable = await multi.find_capable_harnesses("nonexistent")
    assert capable == []


@pytest.mark.asyncio
async def test_private_config_delegation():
    private = PrivateSkillProvider(skills_dir="/nonexistent")
    multi = MultiHarnessSkillProvider(
        harnesses={"crucible": MockSkillProvider()},
        private=private,
        default_harness="crucible",
    )
    result = await multi.get_private_config("crucible", "execution")
    assert result is None
