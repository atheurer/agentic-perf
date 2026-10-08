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


def _make_mock_stream(data: bytes = b"") -> MagicMock:
    """Create a mock async stream reader that yields data then EOF."""
    returns = [data[i : i + 65536] for i in range(0, len(data), 65536)] + [b""]
    stream = MagicMock()
    stream.read = AsyncMock(side_effect=returns)
    return stream


def _make_mock_process(
    returncode: int = 0,
    stdout_data: bytes = b"",
    stderr_data: bytes = b"",
) -> MagicMock:
    """Create a mock subprocess with async stream readers.

    Provides both stream-based and communicate() interfaces since different
    code paths in the benchmark server use each pattern.
    """
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = _make_mock_stream(stdout_data)
    proc.stderr = _make_mock_stream(stderr_data)
    proc.wait = AsyncMock(return_value=returncode)
    proc.communicate = AsyncMock(return_value=(stdout_data, stderr_data))
    proc.finish_streamed_output = AsyncMock()
    proc.pid = 12345
    return proc


async def _run_mock_benchmark(tmp_path: Path, process: MagicMock) -> dict:
    from agents.benchmark import server

    (tmp_path / "boot-timings-test.sh").write_text("#!/bin/bash\n")
    cache = MagicMock()
    cache.get_path.return_value = tmp_path
    runner = MagicMock()
    runner.start = AsyncMock(return_value=process)
    with (
        patch.object(server, "_initialized", True),
        patch.object(server, "_repo_cache", cache),
        patch.object(server, "_ticket", None),
        patch.object(server, "_BOOT_TIME_DRAIN_TIMEOUT", 0.05),
        patch("paths.create_artifact_dir", return_value=tmp_path),
        patch("socket.create_connection", return_value=MagicMock()),
        patch.object(server, "AuditedSubprocessRunner", return_value=runner),
    ):
        return json.loads(
            await server.execute_boot_time_test(sut_host="192.0.2.10", samples=1)
        )


class TestBootTimeOutputDrain:
    async def test_long_unterminated_output_is_logged_and_audited(self, tmp_path):
        payload = b"x" * 70000
        stream = asyncio.StreamReader()
        stream.feed_data(payload)
        stream.feed_eof()
        process = _make_mock_process()
        process.stdout = stream

        result = await _run_mock_benchmark(tmp_path, process)

        assert result["status"] == "completed"
        assert (tmp_path / "harness-output.log").read_bytes() == payload
        process.finish_streamed_output.assert_awaited_once_with(
            payload,
            b"",
            drain_complete=True,
            timed_out=False,
            cancelled=False,
            failed=False,
        )

    async def test_stderr_lines_keep_one_prefix_across_read_boundaries(self, tmp_path):
        long_line = b"x" * (65536 - len(b"first\n"))
        stderr = b"first\n" + long_line + b"\nthird\n\nlast"
        stream = asyncio.StreamReader()
        stream.feed_data(stderr)
        stream.feed_eof()
        process = _make_mock_process()
        process.stderr = stream

        result = await _run_mock_benchmark(tmp_path, process)

        assert result["status"] == "completed"
        assert (tmp_path / "harness-output.log").read_bytes() == (
            b"STDERR: first\n"
            + b"STDERR: "
            + long_line
            + b"\nSTDERR: third\nSTDERR: \nSTDERR: last"
        )
        assert process.finish_streamed_output.await_args.args[1] == stderr

    async def test_inherited_pipe_fails_without_discarding_partial_output(
        self, tmp_path
    ):
        stream = asyncio.StreamReader()
        stream.feed_data(b"partial output")
        process = _make_mock_process(returncode=0)
        process.stdout = stream

        result = await _run_mock_benchmark(tmp_path, process)

        assert result["status"] == "failed"
        assert result["exit_code"] != 0
        assert "remained open" in result["error"]
        assert (tmp_path / "harness-output.log").read_bytes() == b"partial output"
        process.finish_streamed_output.assert_awaited_once_with(
            b"partial output",
            b"",
            drain_complete=False,
            timed_out=False,
            cancelled=False,
            failed=False,
        )

    async def test_read_failure_fails_and_records_partial_output(self, tmp_path):
        process = _make_mock_process(returncode=0)
        process.stdout.read = AsyncMock(side_effect=[b"partial", OSError("bad pipe")])

        result = await _run_mock_benchmark(tmp_path, process)

        assert result["status"] == "failed"
        assert "read failed: bad pipe" in result["error"]
        process.finish_streamed_output.assert_awaited_once_with(
            b"partial",
            b"",
            drain_complete=False,
            timed_out=False,
            cancelled=False,
            failed=False,
        )

    async def test_reader_failure_cancels_sibling_before_log_close(self, tmp_path):
        from agents.benchmark import server

        sibling_started = asyncio.Event()
        sibling_cancelled = asyncio.Event()

        async def blocked_read(_size):
            sibling_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                sibling_cancelled.set()
                raise

        async def failing_read(_size):
            await sibling_started.wait()
            raise OSError("bad stdout pipe")

        process = _make_mock_process(returncode=0)
        process.stdout.read = AsyncMock(side_effect=failing_read)
        process.stderr.read = AsyncMock(side_effect=blocked_read)
        log = MagicMock()
        cancelled_at_close: list[bool] = []
        log.close.side_effect = lambda: cancelled_at_close.append(
            sibling_cancelled.is_set()
        )

        with patch.object(server.AuditedFilesystem, "open_stream", return_value=log):
            result = await _run_mock_benchmark(tmp_path, process)

        assert result["status"] == "failed"
        assert "read failed: bad stdout pipe" in result["error"]
        assert sibling_cancelled.is_set()
        assert cancelled_at_close == [True]

    async def test_log_open_failure_keeps_benchmark_result(self, tmp_path, caplog):
        from agents.benchmark import server

        process = _make_mock_process(stdout_data=b"success")
        with patch.object(
            server.AuditedFilesystem,
            "open_stream",
            side_effect=OSError("disk unavailable"),
        ):
            result = await _run_mock_benchmark(tmp_path, process)

        assert result["status"] == "completed"
        assert "Cannot open harness output log: disk unavailable" in caplog.text

    async def test_log_write_and_close_failures_keep_benchmark_result(
        self, tmp_path, caplog
    ):
        from agents.benchmark import server

        log = MagicMock()
        log.write.side_effect = OSError("write failed")
        log.close.side_effect = OSError("close failed")
        process = _make_mock_process(stdout_data=b"success")
        with patch.object(server.AuditedFilesystem, "open_stream", return_value=log):
            result = await _run_mock_benchmark(tmp_path, process)

        assert result["status"] == "completed"
        assert "Cannot write harness output log: write failed" in caplog.text
        assert "Cannot close harness output log: close failed" in caplog.text


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
        mock_process = _make_mock_process()
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


class TestBootTimePassiveSerialDefaults:
    @pytest.mark.parametrize(
        ("resource_provider", "directives", "expect_passive"),
        [
            ("jumpstarter", {}, True),
            ("quads", {}, False),
            ("jumpstarter", {"serial_capture": False}, False),
            ("jumpstarter", {"jumpstarter_serial": True}, False),
        ],
        ids=[
            "jumpstarter-default-on",
            "other-provider-default-off",
            "explicit-false-disables",
            "active-serial-suppresses-passive",
        ],
    )
    async def test_passive_capture_default_and_overrides(
        self, tmp_path, monkeypatch, resource_provider, directives, expect_passive
    ):
        from agents.benchmark import server

        monkeypatch.delenv("TICKET_ID", raising=False)
        (tmp_path / "boot-timings-test.sh").write_text("#!/bin/bash\n")
        mock_cache = MagicMock()
        mock_cache.get_path.return_value = tmp_path
        ticket = {
            "custom_fields": {
                "resource_provider": resource_provider,
                "resource_provider_metadata": {"lease_id": "lease-123"},
                "directives": directives,
            }
        }
        serial_proc = MagicMock(pid=123, returncode=None)
        serial_proc.wait = AsyncMock(return_value=0)
        benchmark_proc = _make_mock_process()

        async def start(argv, **_kwargs):
            return serial_proc if argv[0] == "jmp" else benchmark_proc

        runner = MagicMock()
        runner.start = AsyncMock(side_effect=start)

        with (
            patch.object(server, "_initialized", True),
            patch.object(server, "_repo_cache", mock_cache),
            patch.object(server, "_ticket", ticket),
            patch("paths.create_artifact_dir", return_value=tmp_path),
            patch("socket.create_connection", return_value=MagicMock()),
            patch.object(server, "AuditedSubprocessRunner", return_value=runner),
        ):
            await server.execute_boot_time_test(sut_host="192.0.2.10", samples=1)

        calls = runner.start.call_args_list
        passive_calls = [call for call in calls if call.args[0][0] == "jmp"]
        assert bool(passive_calls) is expect_passive
        if expect_passive:
            assert passive_calls[0].args[0] == [
                "jmp",
                "shell",
                "--lease=lease-123",
                "--",
                "j",
                "serial",
                "pipe",
            ]
            serial_proc.terminate.assert_called_once()
            serial_proc.wait.assert_awaited_once_with(timeout=10)
        else:
            serial_proc.terminate.assert_not_called()
        if directives.get("jumpstarter_serial"):
            benchmark_call = next(call for call in calls if call.args[0][0] != "jmp")
            assert "--jumpstarter-serial" in benchmark_call.args[0]


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
            # For the test script
            if "boot-timings-test.sh" in str(args):
                return _make_mock_process(stdout_data=b"OK")
            # For the merge script
            else:
                return _make_mock_process(
                    stdout_data=json.dumps(merged_data).encode(),
                )

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
        benchmark_proc = _make_mock_process()
        if failure == "cancel":
            benchmark_proc.stdout.read = AsyncMock(side_effect=asyncio.CancelledError())

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
        if failure == "cancel":
            benchmark_proc.finish_streamed_output.assert_awaited_once()
            assert benchmark_proc.finish_streamed_output.await_args.kwargs["cancelled"]
        # open_stream is called for both serial-capture.log
        # and harness-output.log
        assert len(opened_streams) == 2
        assert all(s.closed for s in opened_streams)

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
        benchmark_proc = _make_mock_process(returncode=1, stderr_data=b"failed")

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
