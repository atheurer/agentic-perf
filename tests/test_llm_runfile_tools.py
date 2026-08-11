from __future__ import annotations

import pytest

from providers.skills.base import RunfileTemplate
from tests.conftest import MockSSHExecutor, MockSkillProvider, make_benchmark_handlers

MOCK_SCHEMA = {
    "type": "object",
    "properties": {
        "benchmarks": {"type": "array", "minItems": 1},
        "endpoints": {"type": "array"},
        "tags": {"type": "object"},
    },
    "required": ["benchmarks"],
    "additionalProperties": False,
}

MOCK_UPERF_PARAMS = {
    "presets": {
        "basic": [
            {"arg": "test-type", "vals": ["stream"]},
            {"arg": "protocol", "vals": ["tcp"]},
        ],
    },
    "validations": {
        "test-types": {
            "description": "all possible test-types",
            "args": ["test-type"],
            "vals": "^stream$|^crr$|^rr$",
        },
    },
}

MOCK_UPERF_EXAMPLE = {
    "benchmarks": [
        {
            "name": "uperf",
            "ids": "1",
            "mv-params": {
                "global-options": [
                    {
                        "name": "defaults",
                        "params": [
                            {"arg": "test-type", "vals": ["stream"], "role": "client"},
                            {"arg": "protocol", "vals": ["tcp"], "role": "client"},
                        ],
                    },
                ],
                "sets": [{"include": "defaults", "params": []}],
            },
        },
    ],
    "endpoints": [
        {
            "type": "remotehosts",
            "settings": {"user": "root", "userenv": "default"},
            "remotes": [],
        },
    ],
}


@pytest.fixture
def provider_with_schema() -> MockSkillProvider:
    return MockSkillProvider(
        private_config={
            "crucible": {
                "execution": {
                    "controller_required": True,
                    "run_command": "crucible run",
                    "endpoint_type": "remotehosts",
                    "endpoint_user": "root",
                    "default_userenv": "alma8",
                    "default_osruntime": "podman",
                    "pre_run": ["ssh_key_setup"],
                    "run_file_format": "json",
                },
            },
        },
        runfile_schema=MOCK_SCHEMA,
        benchmark_params={"uperf": MOCK_UPERF_PARAMS},
        example_runfiles={"uperf": MOCK_UPERF_EXAMPLE},
        runfile_template=RunfileTemplate(benchmark="uperf", template={}),
    )


@pytest.fixture
def provider_without_schema() -> MockSkillProvider:
    return MockSkillProvider(
        private_config={
            "crucible": {
                "execution": {
                    "controller_required": True,
                    "run_command": "crucible run",
                    "run_file_format": "json",
                },
            },
        },
        runfile_template=RunfileTemplate(benchmark="fio", template={}),
    )


@pytest.fixture
def handlers_with_schema(provider_with_schema):
    return make_benchmark_handlers(
        ssh=MockSSHExecutor(),
        skill_provider=provider_with_schema,
    )


@pytest.fixture
def handlers_without_schema(provider_without_schema):
    return make_benchmark_handlers(
        ssh=MockSSHExecutor(),
        skill_provider=provider_without_schema,
    )


@pytest.mark.asyncio
async def test_get_runfile_schema_found(handlers_with_schema):
    result = await handlers_with_schema["get_runfile_schema"]()
    assert result["found"] is True
    assert result["harness"] == "crucible"
    assert result["schema"] == MOCK_SCHEMA
    assert "benchmarks" in result["schema"]["properties"]


@pytest.mark.asyncio
async def test_get_runfile_schema_not_found(handlers_without_schema):
    result = await handlers_without_schema["get_runfile_schema"]()
    assert result["found"] is False


@pytest.mark.asyncio
async def test_get_benchmark_params_found(handlers_with_schema):
    result = await handlers_with_schema["get_benchmark_params"](benchmark="uperf")
    assert result["found"] is True
    assert result["benchmark"] == "uperf"
    assert result["harness"] == "crucible"
    assert "presets" in result["params"]
    assert "validations" in result["params"]


@pytest.mark.asyncio
async def test_get_benchmark_params_not_found(handlers_with_schema):
    result = await handlers_with_schema["get_benchmark_params"](benchmark="nonexistent")
    assert result["found"] is False


@pytest.mark.asyncio
async def test_get_example_runfile_found(handlers_with_schema):
    result = await handlers_with_schema["get_example_runfile"](benchmark="uperf")
    assert result["found"] is True
    assert result["benchmark"] == "uperf"
    assert "benchmarks" in result["run_file"]
    assert result["run_file"]["benchmarks"][0]["name"] == "uperf"


@pytest.mark.asyncio
async def test_get_example_runfile_not_found(handlers_with_schema):
    result = await handlers_with_schema["get_example_runfile"](benchmark="nonexistent")
    assert result["found"] is False


@pytest.mark.asyncio
async def test_present_runfile_for_approval():
    clarification_calls = []

    async def capture_clarification(q):
        clarification_calls.append(q)

    provider = MockSkillProvider(
        runfile_template=RunfileTemplate(benchmark="uperf", template={}),
    )
    h = make_benchmark_handlers(
        ssh=MockSSHExecutor(),
        skill_provider=provider,
        request_clarification_fn=capture_clarification,
    )

    run_file = {"benchmarks": [{"name": "uperf", "ids": "1"}]}
    result = await h["present_runfile_for_approval"](
        run_file=run_file,
        benchmark="uperf",
        summary="Run uperf TCP stream test",
    )

    assert "paused" in result.lower()
    assert len(clarification_calls) == 1
    assert "uperf" in clarification_calls[0]
    assert "Run uperf TCP stream test" in clarification_calls[0]
    assert '"benchmarks"' in clarification_calls[0]


@pytest.mark.asyncio
async def test_execute_benchmark_accepts_llm_constructed_runfile(handlers_with_schema):
    """execute_benchmark should pass the LLM's run-file directly to the controller."""
    llm_runfile = {
        "benchmarks": [{"name": "uperf", "ids": "1", "mv-params": {}}],
        "endpoints": [
            {
                "type": "remotehosts",
                "settings": {"user": "root", "userenv": "alma8"},
                "remotes": [
                    {
                        "engines": [{"role": "client", "ids": [1]}],
                        "config": {
                            "host": "10.0.0.1",
                            "settings": {"osruntime": "podman"},
                        },
                    },
                ],
            },
        ],
    }
    result = await handlers_with_schema["execute_benchmark"](
        controller="10.0.0.1",
        run_file=llm_runfile,
        harness="crucible",
        run_command="crucible run",
    )
    # May fail at SCP (no real SSH in tests) but must not be "rejected"
    # by any local validation — the controller is the single source of truth.
    assert result["status"] != "rejected"
