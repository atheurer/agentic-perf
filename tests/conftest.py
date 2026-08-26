from __future__ import annotations

import atexit
import os as _os
import shutil as _shutil
import tempfile as _tempfile
import warnings as _warnings

_TEST_HOME = _tempfile.mkdtemp(prefix="agentic-perf-tests-")
_os.environ["AGENTIC_PERF_HOME"] = _TEST_HOME
_os.environ["AGENTIC_PERF_SECRETS"] = _os.path.join(_TEST_HOME, "secrets")
_os.environ["AGENTIC_PERF_SKILLS"] = _os.path.join(_TEST_HOME, "private-skills")
_os.environ["AGENTIC_PERF_ARTIFACTS"] = _os.path.join(_TEST_HOME, "artifacts")

import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from providers.secrets.base import SecretsProvider
from providers.skills.base import BenchmarkSuite, RunfileTemplate, SkillProvider

TEST_DEFS_YAML = textwrap.dedent("""\
    test_defs:
      test1:
        test_template: streams_template.yml
        test_name: streams
        test_description: STREAM memory bandwidth benchmark
        test_specific: "--iterations 5"

      test2:
        test_template: fio_template.yml
        test_name: fio
        test_description: straight fio
        archive_results: "yes"
        storage_required: "yes"
        test_specific: "--disks {{ dyn_data.storage }} --regression"

      test3:
        test_template: uperf_template.yml
        test_name: uperf
        test_description: uperf network benchmark
        archive_results: "yes"
        network_required: "yes"
        test_specific: "--client_ips {{ dyn_data.ct_uperf_server_ip }} --server_ips {{ dyn_data.ct_uperf_client_list }} --tests stream --time 60"

      test4:
        test_template: coremark_template.yml
        test_name: coremark
        test_description: coremark CPU test
        archive_results: "yes"
        test_specific: "--iterations 5"

      test5:
        test_template: specjbb_template.yml
        test_name: specjbb
        test_description: SPECjbb Java benchmark
        java_required: "yes"
        test_specific: "--java_version {{ config_info.java_version }}"

      test6:
        test_template: pyperf_template.yml
        test_name: pyperf
        test_description: Python pyperformance benchmark suite
        test_specific: "--benchmarks all"
""")


@pytest.fixture
def tmp_zathras_repo(tmp_path: Path) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "test_defs.yml").write_text(TEST_DEFS_YAML)
    return tmp_path


class MockSecretsProvider(SecretsProvider):
    def __init__(self, files: dict[str, str] | None = None) -> None:
        self._files = files or {}

    async def get_secret(self, path: str) -> str | None:
        if path in self._files:
            return "mock-secret-content"
        return None

    async def get_secret_file(self, path: str) -> Path | None:
        local = self._files.get(path)
        return Path(local) if local else None

    async def list_secrets(self, prefix: str = "") -> list[str]:
        return [k for k in self._files if k.startswith(prefix)]


class MockSkillProvider(SkillProvider):
    def __init__(
        self,
        benchmarks: list[BenchmarkSuite] | None = None,
        resolve_result: str | None = None,
        runfile_template: RunfileTemplate | None = None,
        private_config: dict[str, dict[str, Any]] | None = None,
        runfile_schema: dict[str, Any] | None = None,
        benchmark_params: dict[str, dict[str, Any]] | None = None,
        tool_params: dict[str, dict[str, Any]] | None = None,
        tool_metadata: dict[str, dict[str, Any]] | None = None,
        tools: list[str] | None = None,
        example_runfiles: dict[str, dict[str, Any]] | None = None,
        validation_result: dict[str, Any] | None = None,
    ) -> None:
        self._benchmarks = benchmarks or []
        self._resolve_result = resolve_result
        self._runfile_template = runfile_template or RunfileTemplate(benchmark="")
        self._private_config = private_config or {}
        self._runfile_schema = runfile_schema
        self._benchmark_params = benchmark_params or {}
        self._tool_params = tool_params or {}
        self._tool_metadata = tool_metadata or {}
        self._tools = tools or []
        self._example_runfiles = example_runfiles or {}
        self._validation_result = validation_result

    async def list_benchmarks(self) -> list[BenchmarkSuite]:
        return list(self._benchmarks)

    async def get_benchmark(self, name: str) -> BenchmarkSuite | None:
        for b in self._benchmarks:
            if b.name == name:
                return b
        return None

    async def resolve_benchmark(self, requirements: dict[str, Any]) -> str | None:
        return self._resolve_result

    async def generate_runfile(
        self, benchmark: str, params: dict[str, Any]
    ) -> RunfileTemplate:
        return RunfileTemplate(
            benchmark=benchmark,
            template={**self._runfile_template.template, "params_received": params},
        )

    async def get_runfile_schema(self) -> dict[str, Any] | None:
        return self._runfile_schema

    async def get_benchmark_params(self, benchmark: str) -> dict[str, Any] | None:
        return self._benchmark_params.get(benchmark)

    async def list_tools(self) -> list[str]:
        return list(self._tools)

    async def get_tool_params(self, tool: str) -> dict[str, Any] | None:
        return self._tool_params.get(tool)

    async def get_tool_metadata(self, tool: str) -> dict[str, Any] | None:
        return self._tool_metadata.get(tool)

    async def get_example_runfile(
        self, benchmark: str, endpoint_type: str = "remotehosts"
    ) -> dict[str, Any] | None:
        return self._example_runfiles.get(benchmark)

    async def get_private_config(self, suite_name: str, key: str) -> Any | None:
        return self._private_config.get(suite_name, {}).get(key)

    async def get_all_private_config(self, suite_name: str) -> dict[str, Any]:
        return dict(self._private_config.get(suite_name, {}))

    async def validate_runfile(
        self, run_file: dict[str, Any], harness: str | None = None
    ) -> dict[str, Any]:
        if self._validation_result is not None:
            return self._validation_result
        return {"valid": True, "errors": []}


@dataclass
class SSHResult:
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""


class MockSSHExecutor:
    def __init__(self, results: dict[str, SSHResult] | None = None) -> None:
        self._results = results or {}
        self._default = SSHResult(exit_code=0, stdout="ok")
        self.calls: list[dict[str, Any]] = []

    async def run(self, host: str, command: str, timeout: int = 300) -> SSHResult:
        self.calls.append({"method": "run", "host": host, "command": command})
        for pattern, result in self._results.items():
            if pattern in command:
                return result
        return self._default

    async def copy_from(
        self, host: str, remote_path: str, local_path: str, timeout: int = 120
    ) -> SSHResult:
        self.calls.append(
            {
                "method": "copy_from",
                "host": host,
                "remote_path": remote_path,
                "local_path": local_path,
            }
        )
        return self._default

    async def copy_to(
        self, host: str, local_path: str, remote_path: str, timeout: int = 60
    ) -> SSHResult:
        self.calls.append(
            {
                "method": "copy_to",
                "host": host,
                "local_path": local_path,
                "remote_path": remote_path,
            }
        )
        return self._default

    async def run_with_progress(
        self, host: str, command: str, progress_callback=None
    ) -> SSHResult:
        self.calls.append(
            {"method": "run_with_progress", "host": host, "command": command}
        )
        for pattern, result in self._results.items():
            if pattern in command:
                return result
        return self._default


def make_provisioning_handlers(
    ssh: MockSSHExecutor,
    skill_provider: SkillProvider | None = None,
    secrets_provider: SecretsProvider | None = None,
):
    """Return a dict-like accessor for agents.provisioning.server's @mcp.tool()
    functions, wired to the given mocks and bypassing _ensure_init().

    FastMCP's @mcp.tool() decorator returns the function unchanged and
    directly callable, so this just patches the module's lazily-initialized
    globals and JSON-decodes each tool's string return value into a dict,
    matching the calling convention the old mcp_server.py closure-based
    handlers used (``await handlers["tool_name"](**kwargs)`` -> dict).
    """
    import json

    import agents.provisioning.server as srv

    srv._ssh = ssh
    srv._skill_provider = skill_provider or MockSkillProvider()
    srv._secrets_provider = secrets_provider
    srv._initialized = True

    class _Handlers:
        def __getitem__(self, name: str):
            fn = getattr(srv, name)

            async def _wrapper(**kwargs):
                result = await fn(**kwargs)
                return json.loads(result) if isinstance(result, str) else result

            return _wrapper

    return _Handlers()


def make_benchmark_handlers(
    ssh: MockSSHExecutor,
    skill_provider: SkillProvider | None = None,
    repo_cache=None,
    request_clarification_fn=None,
):
    """Return a dict-like accessor for agents.benchmark.server's @mcp.tool()
    functions, wired to the given mocks and bypassing _ensure_init().

    Also provides ``present_runfile_for_approval`` (a local-only tool
    defined in agent.py) when *request_clarification_fn* is given, so
    tests that exercised it via the old create_benchmark_tool_handlers
    closure still work.
    """
    import json as _json

    import agents.benchmark.server as srv

    srv._ssh = ssh
    srv._skill_provider = skill_provider or MockSkillProvider()
    srv._repo_cache = repo_cache
    srv._initialized = True

    class _Handlers:
        def __getitem__(self, name: str):
            if name == "present_runfile_for_approval" and request_clarification_fn:

                async def _present(
                    run_file: dict,
                    benchmark: str | None = None,
                    summary: str | None = None,
                ) -> str:
                    bench_label = f" for {benchmark}" if benchmark else ""
                    summary_line = f"\n\n{summary}" if summary else ""
                    question = (
                        f"Please review this run-file{bench_label}"
                        f"{summary_line}\n\n"
                        f"```json\n{_json.dumps(run_file, indent=2)}\n```"
                        "\n\nDo you approve this configuration? "
                        "(approve / request changes / reject)"
                    )
                    await request_clarification_fn(question)
                    return "Execution paused for user approval."

                return _present

            fn = getattr(srv, name)

            async def _wrapper(**kwargs):
                result = await fn(**kwargs)
                return _json.loads(result) if isinstance(result, str) else result

            return _wrapper

        def __contains__(self, name: str) -> bool:
            if name == "present_runfile_for_approval":
                return request_clarification_fn is not None
            return hasattr(srv, name)

    return _Handlers()


def make_resource_handlers(
    ssh: MockSSHExecutor | None = None,
    registry=None,
    secrets_provider: SecretsProvider | None = None,
    instance_name: str | None = None,
):
    """Return a dict-like accessor for agents.resource.server's @mcp.tool()
    functions, wired to the given mocks and bypassing _ensure_init().
    """
    import json as _json

    import agents.resource.server as srv

    if registry is None and secrets_provider is not None:
        from providers.resource.registry import ResourceProviderRegistry

        registry = ResourceProviderRegistry(
            secrets_provider,
            instance_name=instance_name,
        )

    srv._ssh = ssh or MockSSHExecutor()
    srv._registry = registry
    srv._initialized = True

    class _Handlers:
        def __getitem__(self, name: str):
            fn = getattr(srv, name)

            async def _wrapper(**kwargs):
                result = await fn(**kwargs)
                return _json.loads(result) if isinstance(result, str) else result

            return _wrapper

    return _Handlers()


# ---------------------------------------------------------------------------
# Test sandbox cleanup
# ---------------------------------------------------------------------------


def _cleanup_test_home() -> None:
    """Best-effort removal of the per-session sandbox directory."""
    try:
        _shutil.rmtree(_TEST_HOME)
    except FileNotFoundError:
        pass
    except OSError as exc:
        _warnings.warn(
            f"Could not remove test sandbox {_TEST_HOME}: {exc}",
            stacklevel=1,
        )


atexit.register(_cleanup_test_home)


def pytest_sessionfinish(session, exitstatus):
    """Clean up the test sandbox directory at session end."""
    _cleanup_test_home()
