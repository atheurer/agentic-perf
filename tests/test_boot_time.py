"""Tests for boot-time analysis benchmark tool."""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from providers.execution import AuditedSubprocessRunner
from providers.tracing import bind_trace_context, new_trace_context, reset_trace_context


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


class TestBootTimeJumpstarterRecovery:
    async def test_retries_after_three_power_cycles(self, tmp_path):
        from agents.benchmark import server

        (tmp_path / "boot-timings-test.sh").write_text("#!/bin/bash\n")
        mock_cache = MagicMock()
        mock_cache.get_path.return_value = tmp_path
        mock_process = MagicMock()
        mock_process.returncode = 0
        mock_process.communicate = AsyncMock(return_value=(b"", b""))
        mock_runner = MagicMock()
        mock_runner.start = AsyncMock(return_value=mock_process)
        ticket = {
            "custom_fields": {
                "resource_provider": "jumpstarter",
                "resource_provider_metadata": {"lease_id": "lease-123"},
            }
        }

        with (
            patch.object(server, "_initialized", True),
            patch.object(server, "_repo_cache", mock_cache),
            patch.object(server, "_ticket", ticket),
            patch("socket.create_connection", side_effect=OSError("not ready")) as ssh,
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch.object(server, "AuditedSubprocessRunner", return_value=mock_runner),
        ):
            result = json.loads(
                await server.execute_boot_time_test(
                    sut_host="192.168.1.100",
                    samples=1,
                )
            )

        assert result["status"] == "failed"
        assert "3 Jumpstarter power-cycle attempt(s)" in result["error"]
        assert "4 SSH polling window(s)" in result["error"]
        assert ssh.call_count == 4 * 12
        assert mock_runner.start.call_count == 3
        assert all(
            call.kwargs["mutating"] is True
            and call.args[0]
            == [
                "jmp",
                "shell",
                "--lease",
                "lease-123",
                "--",
                "j",
                "power",
                "cycle",
            ]
            for call in mock_runner.start.call_args_list
        )


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
            "id": "PERF-BOOT",
            "custom_fields": {
                "ssh_user": "root",
                "ssh_password": "password",
            },
        }

        http_response = MagicMock()
        http_response.json.return_value = {"custom_fields": {"output_dirs": []}}
        update_response = MagicMock()
        http_client = MagicMock()
        http_client.get = AsyncMock(return_value=http_response)
        http_client.patch = AsyncMock(return_value=update_response)
        http_context = AsyncMock()
        http_context.__aenter__.return_value = http_client
        http_context.__aexit__.return_value = False

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

        async def emit(_event):
            """Keep this unit test local while preserving a ticket trace."""

        token = bind_trace_context(new_trace_context(ticket_id="PERF-BOOT"))
        runner = AuditedSubprocessRunner(emit)

        try:
            with (
                patch.object(server, "_initialized", True),
                patch.object(server, "_repo_cache", mock_cache),
                patch.object(server, "_ticket", mock_ticket),
                patch.object(server, "_ssh", MagicMock()),
                patch.object(server, "AuditedSubprocessRunner", return_value=runner),
                patch(
                    "asyncio.create_subprocess_exec",
                    side_effect=mock_subprocess_exec,
                ),
                patch("tempfile.mkdtemp", return_value=str(tmp_path)),
                patch("socket.create_connection", return_value=MagicMock()),
                patch(
                    "providers.execution.AuditedAsyncHTTPClient",
                    return_value=http_context,
                ),
            ):
                result = json.loads(
                    await server.execute_boot_time_test(
                        sut_host="192.168.1.100",
                        samples=3,
                        description="test run",
                    )
                )
        finally:
            reset_trace_context(token)

        assert result["status"] == "completed"
        assert result["harness"] == "boot-time"
        assert result["samples_collected"] == 3
        assert "kpis" in result
        kpis = result["kpis"]
        assert kpis["sample_count"] == 3
        assert kpis["avg_kernel_s"] == 0.21
        assert kpis["avg_total_boot_s"] == 10.51
        saved_fields = http_client.patch.await_args.kwargs["json"]["fields"]
        assert saved_fields["output_dir"] == result["output_dir"]
        assert saved_fields["output_dirs"] == [result["output_dir"]]
        assert saved_fields["samples_collected"] == 3
        assert saved_fields["benchmark_kpis"] == kpis


class TestBootTimeDiagnostics:
    """Stall diagnostics must report actual boot samples, not all artifacts."""

    def test_counts_only_boot_time_log_artifacts(self, tmp_path):
        from agents.benchmark.server import _count_boot_time_samples

        results_dir = tmp_path / "results-2026-09-21"
        results_dir.mkdir()
        (tmp_path / "serial-capture.log").write_text("serial output")
        (tmp_path / "metadata.json").write_text("{}")
        (results_dir / "host_boot_time_logs.json").write_text("{}")

        assert _count_boot_time_samples(tmp_path) == 1


class TestBootTimeSerialDiagnostics:
    def test_read_stays_bounded_when_serial_log_grows(self, monkeypatch):
        from agents.benchmark.server import (
            _MAX_BOOT_SERIAL_READ,
            _boot_serial_diagnostics,
        )

        class GrowingSerial(io.BytesIO):
            requested_size = None

            def read(self, size=-1):
                self.requested_size = size
                position = self.tell()
                self.seek(0, io.SEEK_END)
                self.write(b"x" * (2 * _MAX_BOOT_SERIAL_READ))
                self.seek(position)
                return super().read(size)

        stream = GrowingSerial(b"boot\n")
        monkeypatch.setattr(Path, "open", lambda _self, _mode: stream)

        diagnostics = _boot_serial_diagnostics(Path("serial-capture.log"))

        assert stream.requested_size == _MAX_BOOT_SERIAL_READ
        assert diagnostics["serial_log_bytes"] == 5
        assert len(diagnostics["serial_tail"]) == 2000

    def test_uboot_banner_is_not_called_a_prompt(self, tmp_path):
        from agents.benchmark.server import _boot_serial_diagnostics

        serial_log = tmp_path / "serial-capture.log"
        serial_log.write_text("U-Boot 2024.01\nHit any key to stop autoboot: 0\n")

        diagnostics = _boot_serial_diagnostics(serial_log)

        assert "uboot_banner" in diagnostics["serial_indicators"]
        assert "uboot_autoboot" in diagnostics["serial_indicators"]
        assert "uboot_prompt" not in diagnostics["serial_indicators"]

    @pytest.mark.parametrize("failure", ["start", "cancel"])
    async def test_serial_capture_is_cleaned_up_after_benchmark_failure(
        self, tmp_path, monkeypatch, failure
    ):
        from agents.benchmark import server

        monkeypatch.delenv("TICKET_ID", raising=False)
        (tmp_path / "boot-timings-test.sh").write_text("#!/bin/bash\n")
        mock_cache = MagicMock()
        mock_cache.get_path.return_value = tmp_path
        ticket = {
            "custom_fields": {
                "resource_provider": "jumpstarter",
                "resource_provider_metadata": {"lease_id": "lease-123"},
                "directives": {"serial_capture": True},
            }
        }
        serial_proc = MagicMock(pid=123, returncode=None)
        serial_proc.wait = AsyncMock(return_value=0)
        benchmark_proc = MagicMock()
        benchmark_proc.communicate = AsyncMock(side_effect=asyncio.CancelledError())

        async def start(argv, **_kwargs):
            if argv[0] == "jmp":
                return serial_proc
            if failure == "start":
                raise RuntimeError("benchmark start failed")
            return benchmark_proc

        runner = MagicMock()
        runner.start = AsyncMock(side_effect=start)
        original_open_stream = server.AuditedFilesystem.open_stream
        opened_streams = []

        def track_open_stream(filesystem, *args, **kwargs):
            stream = original_open_stream(filesystem, *args, **kwargs)
            opened_streams.append(stream)
            return stream

        expected_error = RuntimeError if failure == "start" else asyncio.CancelledError
        with (
            patch.object(server, "_initialized", True),
            patch.object(server, "_repo_cache", mock_cache),
            patch.object(server, "_ticket", ticket),
            patch("paths.create_artifact_dir", return_value=tmp_path),
            patch("socket.create_connection", return_value=MagicMock()),
            patch.object(server, "AuditedSubprocessRunner", return_value=runner),
            patch.object(server.AuditedFilesystem, "open_stream", track_open_stream),
            pytest.raises(expected_error),
        ):
            await server.execute_boot_time_test(sut_host="192.0.2.10", samples=1)

        assert runner.start.await_count == 2
        serial_proc.terminate.assert_called_once()
        serial_proc.wait.assert_awaited_once_with(timeout=10)
        assert len(opened_streams) == 1
        assert opened_streams[0].closed

    async def test_serial_tail_remains_in_artifact_but_not_service_log(
        self, tmp_path, monkeypatch, caplog
    ):
        from agents.benchmark import server

        monkeypatch.delenv("TICKET_ID", raising=False)
        (tmp_path / "boot-timings-test.sh").write_text("#!/bin/bash\n")
        mock_cache = MagicMock()
        mock_cache.get_path.return_value = tmp_path
        ticket = {
            "custom_fields": {
                "resource_provider": "jumpstarter",
                "resource_provider_metadata": {"lease_id": "lease-123"},
                "directives": {"serial_capture": True},
            }
        }
        serial_proc = MagicMock(pid=123, returncode=None)
        serial_proc.wait = AsyncMock(return_value=0)
        benchmark_proc = MagicMock(returncode=1)
        benchmark_proc.communicate = AsyncMock(return_value=(b"", b"failed"))

        async def start(argv, **kwargs):
            if argv[0] == "jmp":
                kwargs["stdout"].write(b"U-Boot 2024.01\nPRIVATE SERIAL TEXT\n")
                kwargs["stdout"].flush()
                return serial_proc
            return benchmark_proc

        runner = MagicMock()
        runner.start = AsyncMock(side_effect=start)
        ssh = MagicMock()
        ssh.run = AsyncMock(return_value=MagicMock(exit_code=0, stdout="ALIVE"))

        with (
            patch.object(server, "_initialized", True),
            patch.object(server, "_repo_cache", mock_cache),
            patch.object(server, "_ticket", ticket),
            patch.object(server, "_ssh", ssh),
            patch("paths.create_artifact_dir", return_value=tmp_path),
            patch("socket.create_connection", return_value=MagicMock()),
            patch.object(server, "AuditedSubprocessRunner", return_value=runner),
            caplog.at_level("INFO", logger="agents.benchmark.server"),
        ):
            result = json.loads(
                await server.execute_boot_time_test(sut_host="192.0.2.10", samples=1)
            )

        assert "PRIVATE SERIAL TEXT" in result["stall_diagnostics"]["serial_tail"]
        artifact = json.loads((tmp_path / "stall-diagnostics.json").read_text())
        assert "PRIVATE SERIAL TEXT" in artifact["serial_tail"]
        assert "PRIVATE SERIAL TEXT" not in caplog.text


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
