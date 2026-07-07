"""Tests for boot-time analysis benchmark tool."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_boot_time_guard():
    """Reset the one-execution-per-session guard between tests."""
    import agents.benchmark.server as srv

    srv._boot_time_executed = False
    yield
    srv._boot_time_executed = False


class TestSelfHostGuard:
    """The _is_self_host guardrail must reject localhost variants."""

    def _import_guard(self):
        import importlib
        import sys

        # Force re-import to get the function
        mod_name = "agents.benchmark.server"
        if mod_name in sys.modules:
            del sys.modules[mod_name]
        mod = importlib.import_module(mod_name)
        return mod._is_self_host

    def test_localhost(self):
        is_self = self._import_guard()
        assert is_self("localhost") is True

    def test_127_0_0_1(self):
        is_self = self._import_guard()
        assert is_self("127.0.0.1") is True

    def test_ipv6_loopback(self):
        is_self = self._import_guard()
        assert is_self("::1") is True

    def test_zero_address(self):
        is_self = self._import_guard()
        assert is_self("0.0.0.0") is True

    def test_case_insensitive(self):
        is_self = self._import_guard()
        assert is_self("LOCALHOST") is True
        assert is_self("LocalHost") is True

    def test_remote_host_allowed(self):
        is_self = self._import_guard()
        assert is_self("192.168.1.100") is False
        assert is_self("10.0.0.5") is False

    def test_own_hostname(self):
        import socket

        is_self = self._import_guard()
        hostname = socket.gethostname()
        assert is_self(hostname) is True

    def test_own_fqdn(self):
        import socket

        is_self = self._import_guard()
        fqdn = socket.getfqdn()
        assert is_self(fqdn) is True


class TestBootTimeToolGuardrail:
    """execute_boot_time_test must reject self-host targets."""

    async def test_rejects_localhost(self):
        from agents.benchmark.server import execute_boot_time_test

        # Mock _ensure_init to avoid real initialization
        with patch(
            "agents.benchmark.server._ensure_init",
            new_callable=AsyncMock,
        ):
            result = json.loads(
                await execute_boot_time_test(
                    sut_host="localhost",
                    samples=1,
                )
            )
        assert result["status"] == "rejected"
        assert "SAFETY" in result["error"]

    async def test_rejects_127_0_0_1(self):
        from agents.benchmark.server import execute_boot_time_test

        with patch(
            "agents.benchmark.server._ensure_init",
            new_callable=AsyncMock,
        ):
            result = json.loads(
                await execute_boot_time_test(
                    sut_host="127.0.0.1",
                    samples=1,
                )
            )
        assert result["status"] == "rejected"

    async def test_rejects_ipv6_loopback(self):
        from agents.benchmark.server import execute_boot_time_test

        with patch(
            "agents.benchmark.server._ensure_init",
            new_callable=AsyncMock,
        ):
            result = json.loads(
                await execute_boot_time_test(
                    sut_host="::1",
                    samples=1,
                )
            )
        assert result["status"] == "rejected"


class TestBootTimeRepoLookup:
    """Tool must fail gracefully when repo is not cached."""

    async def test_no_repo_cache(self):
        from agents.benchmark import server

        with (
            patch.object(server, "_initialized", True),
            patch.object(server, "_repo_cache", None),
        ):
            result = json.loads(
                await server.execute_boot_time_test(
                    sut_host="192.168.1.100",
                    samples=1,
                )
            )
        assert result["status"] == "failed"
        assert "not found" in result["error"]

    async def test_repo_cache_missing_repo(self):
        from agents.benchmark import server

        mock_cache = MagicMock()
        mock_cache.get_path.return_value = None

        with (
            patch.object(server, "_initialized", True),
            patch.object(server, "_repo_cache", mock_cache),
        ):
            result = json.loads(
                await server.execute_boot_time_test(
                    sut_host="192.168.1.100",
                    samples=1,
                )
            )
        assert result["status"] == "failed"
        assert "not found" in result["error"]
        mock_cache.get_path.assert_called_once_with("boot-time-analysis-scripts")


class TestBootTimeKPIExtraction:
    """KPI extraction from merged boot-time results."""

    async def test_extracts_kpis_from_summary_files(self, tmp_path):
        """Simulate a successful run with mock output files."""
        from agents.benchmark import server

        # Create mock results directory structure
        results_dir = tmp_path / "results-2025-01-01-00-00-00"
        results_dir.mkdir()

        # Create mock boot_time_logs files (needed for
        # samples_collected count)
        for i in range(3):
            log_file = results_dir / f"host_{i}_boot_time_logs.json"
            log_file.write_text(json.dumps({"metadata": {}, "boot_logs": []}))

        # Create mock summary files (KPI source)
        for i, (k, ini, us, tot) in enumerate(
            [
                (0.2, 2.0, 8.0, 10.2),
                (0.21, 2.1, 8.2, 10.51),
                (0.22, 2.2, 8.4, 10.82),
            ]
        ):
            sf = results_dir / f"host_{i}_summary.json"
            sf.write_text(
                json.dumps(
                    {
                        "satime": {
                            "kernel": k,
                            "initrd": ini,
                            "userspace": us,
                            "total": tot,
                        },
                    }
                )
            )

        # Mock merged results (not used for KPIs anymore
        # but merge script is still called)
        merged_data = {
            "boot_time": [
                {
                    "satime": {
                        "kernel": 0.21,
                        "initrd": 2.1,
                        "userspace": 8.2,
                        "total": 10.51,
                    },
                },
                {
                    "satime": {
                        "kernel": 0.22,
                        "initrd": 2.2,
                        "userspace": 8.4,
                        "total": 10.82,
                    },
                },
            ],
        }

        mock_cache = MagicMock()
        mock_cache.get_path.return_value = tmp_path

        # Create the expected scripts (just need to exist)
        (tmp_path / "boot-timings-test.sh").write_text("#!/bin/bash\n")
        (tmp_path / "boot-timings-test.sh").chmod(0o755)
        # No install script — skip install step
        (tmp_path / "boot-time-merge.py").write_text("")

        mock_ticket = {
            "custom_fields": {
                "ssh_user": "root",
                "ssh_password": "password",
            },
        }

        async def mock_subprocess_exec(*args, **kwargs):
            proc = MagicMock()
            proc.returncode = 0
            # For the test script
            if "boot-timings-test.sh" in str(args):
                proc.communicate = AsyncMock(return_value=(b"OK", b""))
            # For the merge script
            else:
                proc.communicate = AsyncMock(
                    return_value=(
                        json.dumps(merged_data).encode(),
                        b"",
                    )
                )
            return proc

        with (
            patch.object(server, "_initialized", True),
            patch.object(server, "_repo_cache", mock_cache),
            patch.object(server, "_ticket", mock_ticket),
            patch.object(server, "_ssh", MagicMock()),
            patch(
                "asyncio.create_subprocess_exec",
                side_effect=mock_subprocess_exec,
            ),
            patch("tempfile.mkdtemp", return_value=str(tmp_path)),
        ):
            result = json.loads(
                await server.execute_boot_time_test(
                    sut_host="192.168.1.100",
                    samples=3,
                    description="test run",
                )
            )

        assert result["status"] == "completed"
        assert result["harness"] == "boot-time"
        assert result["samples_collected"] == 3
        assert "kpis" in result
        kpis = result["kpis"]
        assert kpis["sample_count"] == 3
        assert kpis["avg_kernel_s"] == 0.21
        assert kpis["avg_total_boot_s"] == 10.51


class TestRepoRegistration:
    """boot-time-analysis-scripts must be in default repo lists."""

    def test_in_server_utils(self):
        import inspect

        from agents.server_utils import build_repo_cache

        source = inspect.getsource(build_repo_cache)
        assert "boot-time-analysis-scripts" in source

    def test_in_orchestrator_config(self):
        from orchestrator.config import OrchestratorConfig

        config = OrchestratorConfig()
        assert "boot-time-analysis-scripts" in config.harness_repos
