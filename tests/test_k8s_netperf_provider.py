from __future__ import annotations

import pytest

from providers.skills.k8s_netperf import K8sNetperfSkillProvider


@pytest.fixture
def provider() -> K8sNetperfSkillProvider:
    return K8sNetperfSkillProvider()


@pytest.mark.asyncio
async def test_list_benchmarks(provider: K8sNetperfSkillProvider):
    benchmarks = await provider.list_benchmarks()
    names = [b.name for b in benchmarks]
    assert "k8s-netperf" in names
    assert len(benchmarks) == 1


@pytest.mark.asyncio
async def test_harness_field(provider: K8sNetperfSkillProvider):
    benchmarks = await provider.list_benchmarks()
    for b in benchmarks:
        assert b.harness == "k8s-netperf"


@pytest.mark.asyncio
async def test_endpoint_types_kube(provider: K8sNetperfSkillProvider):
    benchmarks = await provider.list_benchmarks()
    for b in benchmarks:
        assert "kube" in b.endpoint_types


@pytest.mark.asyncio
async def test_roles_and_min_hosts(provider: K8sNetperfSkillProvider):
    benchmarks = await provider.list_benchmarks()
    for b in benchmarks:
        assert b.roles == ["client"]
        assert b.min_hosts == 1


@pytest.mark.asyncio
async def test_get_benchmark_found(provider: K8sNetperfSkillProvider):
    b = await provider.get_benchmark("k8s-netperf")
    assert b is not None
    assert b.name == "k8s-netperf"
    assert b.harness == "k8s-netperf"


@pytest.mark.asyncio
async def test_get_benchmark_not_found(provider: K8sNetperfSkillProvider):
    b = await provider.get_benchmark("nonexistent")
    assert b is None


@pytest.mark.asyncio
async def test_resolve_benchmark_netperf(provider: K8sNetperfSkillProvider):
    result = await provider.resolve_benchmark(
        {"description": "netperf throughput test"}
    )
    assert result == "k8s-netperf"


@pytest.mark.asyncio
async def test_resolve_benchmark_network(provider: K8sNetperfSkillProvider):
    result = await provider.resolve_benchmark(
        {"description": "network latency measurement"}
    )
    assert result == "k8s-netperf"


@pytest.mark.asyncio
async def test_resolve_benchmark_iperf(provider: K8sNetperfSkillProvider):
    result = await provider.resolve_benchmark(
        {"description": "run iperf3 between pods"}
    )
    assert result == "k8s-netperf"


@pytest.mark.asyncio
async def test_resolve_benchmark_no_match(provider: K8sNetperfSkillProvider):
    result = await provider.resolve_benchmark(
        {"description": "pod density stress test"}
    )
    assert result is None


@pytest.mark.asyncio
async def test_generate_runfile_defaults(provider: K8sNetperfSkillProvider):
    result = await provider.generate_runfile("k8s-netperf", {})
    t = result.template
    assert t["harness"] == "k8s-netperf"
    assert t["driver"] == "netperf"
    assert "--netperf" in t["cli_flags"]
    assert "config" in t

    tests = t["config"]["tests"]
    assert len(tests) == 1
    test_config = next(iter(tests[0].values()))
    assert test_config["profile"] == "TCP_STREAM"
    assert test_config["duration"] == 30
    assert test_config["samples"] == 3
    assert test_config["messagesize"] == 1024
    assert test_config["parallelism"] == 1


@pytest.mark.asyncio
async def test_generate_runfile_custom_profiles(provider: K8sNetperfSkillProvider):
    result = await provider.generate_runfile(
        "k8s-netperf",
        {"profiles": ["TCP_STREAM", "TCP_RR", "UDP_STREAM"]},
    )
    t = result.template
    tests = t["config"]["tests"]
    assert len(tests) == 3
    profiles = [next(iter(test.values()))["profile"] for test in tests]
    assert "TCP_STREAM" in profiles
    assert "TCP_RR" in profiles
    assert "UDP_STREAM" in profiles


@pytest.mark.asyncio
async def test_generate_runfile_iperf3(provider: K8sNetperfSkillProvider):
    result = await provider.generate_runfile(
        "k8s-netperf", {"driver": "iperf3"}
    )
    t = result.template
    assert t["driver"] == "iperf3"
    assert "--iperf3" in t["cli_flags"]


@pytest.mark.asyncio
async def test_generate_runfile_uperf(provider: K8sNetperfSkillProvider):
    result = await provider.generate_runfile(
        "k8s-netperf", {"driver": "uperf"}
    )
    t = result.template
    assert t["driver"] == "uperf"
    assert "--uperf" in t["cli_flags"]


@pytest.mark.asyncio
async def test_generate_runfile_host_network(provider: K8sNetperfSkillProvider):
    result = await provider.generate_runfile(
        "k8s-netperf", {"hostNet": True}
    )
    assert "--hostNet" in result.template["cli_flags"]


@pytest.mark.asyncio
async def test_generate_runfile_local(provider: K8sNetperfSkillProvider):
    result = await provider.generate_runfile(
        "k8s-netperf", {"local": True}
    )
    assert "--local" in result.template["cli_flags"]


@pytest.mark.asyncio
async def test_generate_runfile_across(provider: K8sNetperfSkillProvider):
    result = await provider.generate_runfile(
        "k8s-netperf", {"across": True}
    )
    assert "--across" in result.template["cli_flags"]


@pytest.mark.asyncio
async def test_generate_runfile_service(provider: K8sNetperfSkillProvider):
    result = await provider.generate_runfile(
        "k8s-netperf", {"service": True}
    )
    tests = result.template["config"]["tests"]
    for test in tests:
        test_config = next(iter(test.values()))
        assert test_config["service"] is True


@pytest.mark.asyncio
async def test_generate_runfile_custom_params(provider: K8sNetperfSkillProvider):
    result = await provider.generate_runfile(
        "k8s-netperf",
        {
            "duration": 60,
            "samples": 5,
            "messagesize": 8192,
            "parallelism": 2,
        },
    )
    test_config = next(iter(result.template["config"]["tests"][0].values()))
    assert test_config["duration"] == 60
    assert test_config["samples"] == 5
    assert test_config["messagesize"] == 8192
    assert test_config["parallelism"] == 2


@pytest.mark.asyncio
async def test_generate_runfile_unknown_benchmark(provider: K8sNetperfSkillProvider):
    result = await provider.generate_runfile("nonexistent", {})
    assert result.template == {}


@pytest.mark.asyncio
async def test_get_benchmark_params(provider: K8sNetperfSkillProvider):
    params = await provider.get_benchmark_params("k8s-netperf")
    assert params is not None
    assert "driver" in params
    assert "profiles" in params
    assert "duration" in params
    assert "samples" in params
    assert "messagesize" in params
    assert "parallelism" in params
    assert params["driver"]["default"] == "netperf"
    assert params["duration"]["default"] == 30


@pytest.mark.asyncio
async def test_get_benchmark_params_not_found(provider: K8sNetperfSkillProvider):
    params = await provider.get_benchmark_params("nonexistent")
    assert params is None


@pytest.mark.asyncio
async def test_get_runfile_schema(provider: K8sNetperfSkillProvider):
    schema = await provider.get_runfile_schema()
    assert schema is not None
    assert schema["type"] == "object"
    assert "config" in schema["properties"]
    assert "driver" in schema["properties"]


@pytest.mark.asyncio
async def test_get_example_runfile(provider: K8sNetperfSkillProvider):
    example = await provider.get_example_runfile("k8s-netperf")
    assert example is not None
    assert example["harness"] == "k8s-netperf"
    assert "config" in example
    assert "driver" in example


@pytest.mark.asyncio
async def test_get_example_runfile_not_found(provider: K8sNetperfSkillProvider):
    example = await provider.get_example_runfile("nonexistent")
    assert example is None


@pytest.mark.asyncio
async def test_validate_runfile_valid(provider: K8sNetperfSkillProvider):
    result = await provider.generate_runfile("k8s-netperf", {})
    validation = await provider.validate_runfile(result.template)
    assert validation["valid"] is True
    assert validation["errors"] == []


@pytest.mark.asyncio
async def test_validate_runfile_missing_config(provider: K8sNetperfSkillProvider):
    validation = await provider.validate_runfile({"driver": "netperf"})
    assert validation["valid"] is False
    assert any("config" in e for e in validation["errors"])


@pytest.mark.asyncio
async def test_validate_runfile_missing_tests(provider: K8sNetperfSkillProvider):
    validation = await provider.validate_runfile({
        "config": {},
        "driver": "netperf",
    })
    assert validation["valid"] is False
    assert any("tests" in e for e in validation["errors"])


@pytest.mark.asyncio
async def test_validate_runfile_invalid_profile(provider: K8sNetperfSkillProvider):
    validation = await provider.validate_runfile({
        "config": {
            "tests": [{"bad_test": {"profile": "INVALID_PROFILE"}}],
        },
        "driver": "netperf",
    })
    assert validation["valid"] is False
    assert any("profile" in e for e in validation["errors"])


@pytest.mark.asyncio
async def test_validate_runfile_invalid_driver(provider: K8sNetperfSkillProvider):
    validation = await provider.validate_runfile({
        "config": {
            "tests": [{"test1": {"profile": "TCP_STREAM"}}],
        },
        "driver": "invalid_driver",
    })
    assert validation["valid"] is False
    assert any("driver" in e.lower() for e in validation["errors"])


@pytest.mark.asyncio
async def test_validate_runfile_driver_profile_mismatch(
    provider: K8sNetperfSkillProvider,
):
    validation = await provider.validate_runfile({
        "config": {
            "tests": [{"test1": {"profile": "TCP_CRR"}}],
        },
        "driver": "iperf3",
    })
    assert validation["valid"] is False
    assert any("not supported" in e for e in validation["errors"])


@pytest.mark.asyncio
async def test_validate_generated_runfile_passes(provider: K8sNetperfSkillProvider):
    result = await provider.generate_runfile("k8s-netperf", {})
    validation = await provider.validate_runfile(result.template)
    assert validation["valid"] is True, f"k8s-netperf: {validation['errors']}"


@pytest.mark.asyncio
async def test_validate_generated_multiprofile_passes(
    provider: K8sNetperfSkillProvider,
):
    result = await provider.generate_runfile(
        "k8s-netperf",
        {"profiles": ["TCP_STREAM", "TCP_RR", "UDP_STREAM"]},
    )
    validation = await provider.validate_runfile(result.template)
    assert validation["valid"] is True, f"multi-profile: {validation['errors']}"


@pytest.mark.asyncio
async def test_test_name_includes_messagesize(provider: K8sNetperfSkillProvider):
    result = await provider.generate_runfile(
        "k8s-netperf", {"messagesize": 8192}
    )
    test = result.template["config"]["tests"][0]
    test_name = next(iter(test.keys()))
    assert "8192" in test_name


@pytest.mark.asyncio
async def test_string_profile_coerced_to_list(provider: K8sNetperfSkillProvider):
    result = await provider.generate_runfile(
        "k8s-netperf", {"profiles": "TCP_RR"}
    )
    tests = result.template["config"]["tests"]
    assert len(tests) == 1
    test_config = next(iter(tests[0].values()))
    assert test_config["profile"] == "TCP_RR"
