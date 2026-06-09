from __future__ import annotations

import os
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

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
""")


@pytest.fixture
def tmp_zathras_repo(tmp_path: Path) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "test_defs.yml").write_text(TEST_DEFS_YAML)
    return tmp_path


class MockSkillProvider(SkillProvider):
    def __init__(
        self,
        benchmarks: list[BenchmarkSuite] | None = None,
        resolve_result: str | None = None,
        runfile_template: RunfileTemplate | None = None,
        private_config: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._benchmarks = benchmarks or []
        self._resolve_result = resolve_result
        self._runfile_template = runfile_template or RunfileTemplate(benchmark="")
        self._private_config = private_config or {}

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

    async def get_private_config(self, suite_name: str, key: str) -> Any | None:
        return self._private_config.get(suite_name, {}).get(key)

    async def get_all_private_config(self, suite_name: str) -> dict[str, Any]:
        return dict(self._private_config.get(suite_name, {}))


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

    async def run(
        self, host: str, command: str, timeout: int = 300
    ) -> SSHResult:
        self.calls.append({"method": "run", "host": host, "command": command})
        for pattern, result in self._results.items():
            if pattern in command:
                return result
        return self._default

    async def copy_to(
        self, host: str, local_path: str, remote_path: str, timeout: int = 60
    ) -> SSHResult:
        self.calls.append({
            "method": "copy_to",
            "host": host,
            "local_path": local_path,
            "remote_path": remote_path,
        })
        return self._default
