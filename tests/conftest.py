from __future__ import annotations

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
        example_runfiles: dict[str, dict[str, Any]] | None = None,
        validation_result: dict[str, Any] | None = None,
    ) -> None:
        self._benchmarks = benchmarks or []
        self._resolve_result = resolve_result
        self._runfile_template = runfile_template or RunfileTemplate(benchmark="")
        self._private_config = private_config or {}
        self._runfile_schema = runfile_schema
        self._benchmark_params = benchmark_params or {}
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
