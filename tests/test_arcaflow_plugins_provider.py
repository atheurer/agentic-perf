from __future__ import annotations

from unittest.mock import patch

import pytest

from providers.skills.arcaflow_plugins import (
    PLUGIN_METADATA,
    ArcaflowPluginSkillProvider,
    _benchmark_to_repo_name,
    _plugin_name_to_benchmark,
)


@pytest.fixture
def provider(tmp_path) -> ArcaflowPluginSkillProvider:
    """Provider that uses local metadata only (no Quay/podman calls)."""
    p = ArcaflowPluginSkillProvider(
        schema_cache_dir=tmp_path / "schema-cache",
        discover_schemas=False,
    )
    # Pre-populate from local metadata to avoid Quay calls in tests
    p._build_from_local_metadata()
    return p


# --- Name conversion ---


def test_plugin_name_to_benchmark():
    assert _plugin_name_to_benchmark("arcaflow-plugin-stressng") == "arcaflow-stressng"
    assert (
        _plugin_name_to_benchmark("arcaflow-plugin-coremark-pro")
        == "arcaflow-coremark-pro"
    )


def test_benchmark_to_repo_name():
    assert _benchmark_to_repo_name("arcaflow-stressng") == "arcaflow-plugin-stressng"
    assert (
        _benchmark_to_repo_name("arcaflow-coremark-pro")
        == "arcaflow-plugin-coremark-pro"
    )


# --- Listing ---


@pytest.mark.asyncio
async def test_list_benchmarks(
    provider: ArcaflowPluginSkillProvider,
):
    benchmarks = await provider.list_benchmarks()
    assert len(benchmarks) == len(PLUGIN_METADATA)
    names = {b.name for b in benchmarks}
    assert "arcaflow-stressng" in names
    assert "arcaflow-fio" in names
    assert "arcaflow-sysbench" in names


@pytest.mark.asyncio
async def test_list_benchmarks_harness_field(
    provider: ArcaflowPluginSkillProvider,
):
    benchmarks = await provider.list_benchmarks()
    for b in benchmarks:
        assert b.harness == "arcaflow-plugins"


@pytest.mark.asyncio
async def test_list_benchmarks_endpoint_types(
    provider: ArcaflowPluginSkillProvider,
):
    benchmarks = await provider.list_benchmarks()
    for b in benchmarks:
        assert "remotehosts" in b.endpoint_types


@pytest.mark.asyncio
async def test_list_benchmarks_roles(
    provider: ArcaflowPluginSkillProvider,
):
    benchmarks = await provider.list_benchmarks()
    for b in benchmarks:
        assert b.roles == ["client"]
        assert b.min_hosts == 1


# --- Get benchmark ---


@pytest.mark.asyncio
async def test_get_benchmark_found(
    provider: ArcaflowPluginSkillProvider,
):
    b = await provider.get_benchmark("arcaflow-stressng")
    assert b is not None
    assert b.name == "arcaflow-stressng"
    assert "stress" in b.description.lower()
    assert b.harness == "arcaflow-plugins"


@pytest.mark.asyncio
async def test_get_benchmark_not_found(
    provider: ArcaflowPluginSkillProvider,
):
    b = await provider.get_benchmark("nonexistent-plugin")
    assert b is None


# --- Resolution ---


@pytest.mark.asyncio
async def test_resolve_benchmark_cpu_stress(
    provider: ArcaflowPluginSkillProvider,
):
    result = await provider.resolve_benchmark({"description": "run a CPU stress test"})
    assert result is not None
    assert "stressng" in result or "sysbench" in result


@pytest.mark.asyncio
async def test_resolve_benchmark_storage(
    provider: ArcaflowPluginSkillProvider,
):
    result = await provider.resolve_benchmark(
        {"description": "4K random read IOPS storage test"}
    )
    assert result == "arcaflow-fio"


@pytest.mark.asyncio
async def test_resolve_benchmark_network(
    provider: ArcaflowPluginSkillProvider,
):
    result = await provider.resolve_benchmark(
        {"description": "network throughput benchmark"}
    )
    assert result is not None
    assert "uperf" in result or "iperf" in result


@pytest.mark.asyncio
async def test_resolve_benchmark_no_match(
    provider: ArcaflowPluginSkillProvider,
):
    result = await provider.resolve_benchmark(
        {"description": "completely unrelated query about databases"}
    )
    assert result is None


@pytest.mark.asyncio
async def test_resolve_benchmark_wrong_harness(
    provider: ArcaflowPluginSkillProvider,
):
    result = await provider.resolve_benchmark(
        {
            "description": "run a CPU stress test",
            "harness": "crucible",
        }
    )
    assert result is None


@pytest.mark.asyncio
async def test_resolve_benchmark_explicit_arcaflow_harness(
    provider: ArcaflowPluginSkillProvider,
):
    result = await provider.resolve_benchmark(
        {
            "description": "cpu stress",
            "harness": "arcaflow-plugins",
        }
    )
    assert result is not None


# --- Runfile generation ---


@pytest.mark.asyncio
async def test_generate_runfile(
    provider: ArcaflowPluginSkillProvider,
):
    result = await provider.generate_runfile(
        "arcaflow-stressng",
        {"timeout": 30},
    )
    assert result.benchmark == "arcaflow-stressng"
    t = result.template
    assert t["harness"] == "arcaflow-plugins"
    assert "arcaflow-plugin-stressng" in t["plugin_image"]
    assert t["plugin_step"] == "workload"
    assert t["input"]["timeout"] == 30


@pytest.mark.asyncio
async def test_generate_runfile_preserves_example_defaults(
    provider: ArcaflowPluginSkillProvider,
):
    result = await provider.generate_runfile(
        "arcaflow-stressng",
        {"timeout": 120},
    )
    assert "stressors" in result.template["input"]
    assert result.template["input"]["timeout"] == 120


@pytest.mark.asyncio
async def test_generate_runfile_filters_harness_keys(
    provider: ArcaflowPluginSkillProvider,
):
    result = await provider.generate_runfile(
        "arcaflow-stressng",
        {
            "timeout": 30,
            "harness": "arcaflow-plugins",
            "endpoint_type": "remotehosts",
            "hosts": ["10.0.0.1"],
        },
    )
    plugin_input = result.template["input"]
    assert "harness" not in plugin_input
    assert "endpoint_type" not in plugin_input
    assert "hosts" not in plugin_input


@pytest.mark.asyncio
async def test_generate_runfile_image_has_version(
    provider: ArcaflowPluginSkillProvider,
):
    result = await provider.generate_runfile("arcaflow-stressng", {})
    image = result.template["plugin_image"]
    assert ":" in image
    # Local metadata fallback uses "latest" as version
    assert "arcaflow-plugin-stressng:" in image


# --- Params and examples ---


@pytest.mark.asyncio
async def test_get_benchmark_params(
    provider: ArcaflowPluginSkillProvider,
):
    params = await provider.get_benchmark_params("arcaflow-stressng")
    assert params is not None
    assert "params" in params
    assert "timeout" in params["params"]
    assert "stressors" in params["params"]


@pytest.mark.asyncio
async def test_get_benchmark_params_not_found(
    provider: ArcaflowPluginSkillProvider,
):
    params = await provider.get_benchmark_params("nonexistent")
    assert params is None


@pytest.mark.asyncio
async def test_get_example_runfile(
    provider: ArcaflowPluginSkillProvider,
):
    example = await provider.get_example_runfile("arcaflow-fio")
    assert example is not None
    assert example["harness"] == "arcaflow-plugins"
    assert "arcaflow-plugin-fio" in example["plugin_image"]
    assert "input" in example
    assert "jobs" in example["input"]


@pytest.mark.asyncio
async def test_get_example_runfile_not_found(
    provider: ArcaflowPluginSkillProvider,
):
    example = await provider.get_example_runfile("nonexistent")
    assert example is None


# --- Validation ---


@pytest.mark.asyncio
async def test_validate_runfile_valid(
    provider: ArcaflowPluginSkillProvider,
):
    validation = await provider.validate_runfile(
        {
            "plugin_image": ("quay.io/arcalot/arcaflow-plugin-stressng:0.8.1"),
            "input": {"timeout": 60, "stressors": []},
        }
    )
    assert validation["valid"] is True
    assert validation["errors"] == []


@pytest.mark.asyncio
async def test_validate_runfile_missing_image(
    provider: ArcaflowPluginSkillProvider,
):
    validation = await provider.validate_runfile({"input": {"timeout": 60}})
    assert validation["valid"] is False
    assert any("plugin_image" in e for e in validation["errors"])


@pytest.mark.asyncio
async def test_validate_runfile_missing_input(
    provider: ArcaflowPluginSkillProvider,
):
    validation = await provider.validate_runfile(
        {"plugin_image": ("quay.io/arcalot/arcaflow-plugin-stressng:0.8.1")}
    )
    assert validation["valid"] is False
    assert any("input" in e for e in validation["errors"])


@pytest.mark.asyncio
async def test_validate_runfile_bad_input_type(
    provider: ArcaflowPluginSkillProvider,
):
    validation = await provider.validate_runfile(
        {
            "plugin_image": ("quay.io/arcalot/arcaflow-plugin-stressng:0.8.1"),
            "input": "not a dict",
        }
    )
    assert validation["valid"] is False
    assert any("dictionary" in e for e in validation["errors"])


@pytest.mark.asyncio
async def test_validate_generated_runfile_passes(
    provider: ArcaflowPluginSkillProvider,
):
    result = await provider.generate_runfile(
        "arcaflow-stressng",
        {"timeout": 30},
    )
    validation = await provider.validate_runfile(result.template)
    assert validation["valid"] is True


# --- Catalog integrity ---


@pytest.mark.asyncio
async def test_all_metadata_entries_have_required_fields():
    for name, info in PLUGIN_METADATA.items():
        assert "description" in info, f"{name} missing 'description'"
        assert "step" in info, f"{name} missing 'step'"
        assert "keywords" in info, f"{name} missing 'keywords'"
        assert len(info["keywords"]) > 0, f"{name} has empty keywords"


@pytest.mark.asyncio
async def test_all_local_plugins_generate_valid_runfiles(
    provider: ArcaflowPluginSkillProvider,
):
    benchmarks = await provider.list_benchmarks()
    for b in benchmarks:
        result = await provider.generate_runfile(b.name, {})
        validation = await provider.validate_runfile(result.template)
        assert validation["valid"], (
            f"{b.name} generated invalid runfile: {validation['errors']}"
        )


# --- Quay discovery ---


@pytest.mark.asyncio
async def test_quay_discovery_populates_catalog():
    """Verify Quay discovery adds plugins beyond local metadata."""
    provider = ArcaflowPluginSkillProvider()

    async def mock_discover():
        return {
            "arcaflow-plugin-stressng": "0.8.1",
            "arcaflow-plugin-fio": "0.4.0",
            "arcaflow-plugin-canutils": "1.0.0",
        }

    with patch.object(provider, "_discover_from_quay", mock_discover):
        benchmarks = await provider.list_benchmarks()

    names = {b.name for b in benchmarks}
    assert "arcaflow-stressng" in names
    assert "arcaflow-fio" in names
    # Unknown plugin discovered from Quay should also appear
    assert "arcaflow-canutils" in names

    # Known plugin should have rich description
    stressng = next(b for b in benchmarks if b.name == "arcaflow-stressng")
    assert "stress-ng" in stressng.description.lower()

    # Unknown plugin should have basic description
    canutils = next(b for b in benchmarks if b.name == "arcaflow-canutils")
    assert "canutils" in canutils.description.lower()


@pytest.mark.asyncio
async def test_quay_discovery_gets_version_tags():
    """Verify that Quay discovery picks semver tags over latest."""
    provider = ArcaflowPluginSkillProvider()

    async def mock_discover():
        return {"arcaflow-plugin-stressng": "0.8.1"}

    with patch.object(provider, "_discover_from_quay", mock_discover):
        result = await provider.generate_runfile("arcaflow-stressng", {})

    assert result.template["plugin_image"].endswith(":0.8.1")


@pytest.mark.asyncio
async def test_quay_unreachable_falls_back_to_local():
    """When Quay is unreachable, local metadata is used."""
    provider = ArcaflowPluginSkillProvider()

    async def mock_discover():
        return {}

    with patch.object(provider, "_discover_from_quay", mock_discover):
        benchmarks = await provider.list_benchmarks()

    names = {b.name for b in benchmarks}
    assert "arcaflow-stressng" in names
    assert "arcaflow-fio" in names
    assert len(benchmarks) == len(PLUGIN_METADATA)


@pytest.mark.asyncio
async def test_cache_prevents_repeated_quay_calls():
    """Second call should use cache, not hit Quay again."""
    provider = ArcaflowPluginSkillProvider()
    provider._build_from_local_metadata()

    with patch.object(provider, "_discover_from_quay") as mock_discover:
        await provider.list_benchmarks()
        mock_discover.assert_not_called()


class TestGetRunfileSchema:
    @pytest.mark.asyncio
    async def test_returns_schema(self):
        """get_runfile_schema returns the envelope schema."""
        provider = ArcaflowPluginSkillProvider(discover_schemas=False)
        schema = await provider.get_runfile_schema()
        assert schema is not None
        assert schema["type"] == "object"
        assert "plugin_image" in schema["properties"]
        assert "plugin_step" in schema["properties"]
        assert "input" in schema["properties"]
        assert schema["required"] == ["plugin_image", "plugin_step", "input"]
        assert schema["additionalProperties"] is False


class TestLookupAliasing:
    @pytest.mark.asyncio
    async def test_direct_name(self):
        """_lookup finds by exact catalog key."""
        provider = ArcaflowPluginSkillProvider(discover_schemas=False)
        provider._catalog = {"arcaflow-stressng": {"image": "test"}}
        assert provider._lookup("arcaflow-stressng") is not None

    @pytest.mark.asyncio
    async def test_short_name(self):
        """_lookup finds 'stressng' via 'arcaflow-stressng'."""
        provider = ArcaflowPluginSkillProvider(discover_schemas=False)
        provider._catalog = {"arcaflow-stressng": {"image": "test"}}
        assert provider._lookup("stressng") is not None

    @pytest.mark.asyncio
    async def test_missing_name(self):
        """_lookup returns None for unknown benchmarks."""
        provider = ArcaflowPluginSkillProvider(discover_schemas=False)
        provider._catalog = {"arcaflow-stressng": {"image": "test"}}
        assert provider._lookup("unknown") is None
