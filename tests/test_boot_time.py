"""Tests for boot-time analysis benchmark tool."""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from agents.benchmark.harnesses import boot_time as harness
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
        patch.object(server, "_ticket", _boot_time_ticket()),
        patch.object(harness, "_BOOT_TIME_DRAIN_TIMEOUT", 0.05),
        patch("paths.create_artifact_dir", return_value=tmp_path),
        patch("socket.create_connection", return_value=MagicMock()),
        patch.object(harness, "AuditedSubprocessRunner", return_value=runner),
    ):
        return json.loads(
            await server.execute_boot_time_test(sut_host="192.0.2.10", samples=1)
        )


def _boot_time_ticket(
    custom_fields: dict | None = None,
    *,
    ticket_id: str = "PERF-BOOT-TEST",
) -> dict:
    fields = dict(custom_fields or {})
    directives = dict(fields.get("directives", {}))
    directives.setdefault("harness", "boot-time")
    fields["directives"] = directives
    return {
        "id": ticket_id,
        "status": "executing_benchmark",
        "custom_fields": fields,
    }


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

        with patch.object(harness.AuditedFilesystem, "open_stream", return_value=log):
            result = await _run_mock_benchmark(tmp_path, process)

        assert result["status"] == "failed"
        assert "read failed: bad stdout pipe" in result["error"]
        assert sibling_cancelled.is_set()
        assert cancelled_at_close == [True]

    async def test_log_open_failure_keeps_benchmark_result(self, tmp_path, caplog):
        process = _make_mock_process(stdout_data=b"success")
        with patch.object(
            harness.AuditedFilesystem,
            "open_stream",
            side_effect=OSError("disk unavailable"),
        ):
            result = await _run_mock_benchmark(tmp_path, process)

        assert result["status"] == "completed"
        assert "Cannot open harness output log: disk unavailable" in caplog.text

    async def test_log_write_and_close_failures_keep_benchmark_result(
        self, tmp_path, caplog
    ):
        log = MagicMock()
        log.write.side_effect = OSError("write failed")
        log.close.side_effect = OSError("close failed")
        process = _make_mock_process(stdout_data=b"success")
        with patch.object(harness.AuditedFilesystem, "open_stream", return_value=log):
            result = await _run_mock_benchmark(tmp_path, process)

        assert result["status"] == "completed"
        assert "Cannot write harness output log: write failed" in caplog.text
        assert "Cannot close harness output log: close failed" in caplog.text


@pytest.fixture(autouse=True)
def _reset_boot_time_guard(monkeypatch):
    """Reset the one-execution-per-session guard between tests."""
    import agents.benchmark.harnesses.boot_time as harness_mod
    import agents.benchmark.server as srv

    harness_mod._boot_time_executed = False
    monkeypatch.setattr(srv, "_ticket", _boot_time_ticket())
    yield
    harness_mod._boot_time_executed = False


class TestSelfHostGuard:
    """The _is_self_host guardrail must reject localhost variants."""

    def _import_guard(self):
        import agents.benchmark.server as mod

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
        ticket = _boot_time_ticket(
            {
                "resource_provider": "jumpstarter",
                "resource_provider_metadata": {"lease_id": "lease-123"},
            }
        )

        with (
            patch.object(server, "_initialized", True),
            patch.object(server, "_repo_cache", mock_cache),
            patch.object(server, "_ticket", ticket),
            patch("socket.create_connection", side_effect=OSError("not ready")) as ssh,
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch.object(harness, "AuditedSubprocessRunner", return_value=mock_runner),
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
        ticket = _boot_time_ticket(
            {
                "resource_provider": resource_provider,
                "resource_provider_metadata": {"lease_id": "lease-123"},
                "directives": directives,
            }
        )
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
            patch.object(harness, "AuditedSubprocessRunner", return_value=runner),
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
                "--exporter-logs",
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

        mock_ticket = _boot_time_ticket(
            {
                "ssh_user": "root",
                "ssh_password": "password",
            },
            ticket_id="PERF-BOOT",
        )

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
                patch.object(harness, "AuditedSubprocessRunner", return_value=runner),
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
        from agents.benchmark.harnesses.boot_time import _count_boot_time_samples

        results_dir = tmp_path / "results-2026-09-21"
        results_dir.mkdir()
        (tmp_path / "serial-capture.log").write_text("serial output")
        (tmp_path / "metadata.json").write_text("{}")
        (results_dir / "host_boot_time_logs.json").write_text("{}")

        assert _count_boot_time_samples(tmp_path) == 1


class TestBootTimeSerialDiagnostics:
    def test_read_stays_bounded_when_serial_log_grows(self, monkeypatch):
        from agents.benchmark.harnesses.boot_time import (
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
        from agents.benchmark.harnesses.boot_time import _boot_serial_diagnostics

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
        ticket = _boot_time_ticket(
            {
                "resource_provider": "jumpstarter",
                "resource_provider_metadata": {"lease_id": "lease-123"},
                "directives": {"serial_capture": True},
            }
        )
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
        original_open_stream = harness.AuditedFilesystem.open_stream
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
            patch.object(harness, "AuditedSubprocessRunner", return_value=runner),
            patch.object(harness.AuditedFilesystem, "open_stream", track_open_stream),
            pytest.raises(expected_error),
        ):
            await server.execute_boot_time_test(sut_host="192.0.2.10", samples=1)

        assert runner.start.await_count == 2
        serial_proc.terminate.assert_called_once()
        serial_proc.wait.assert_awaited_once_with(timeout=10)
        if failure == "cancel":
            benchmark_proc.finish_streamed_output.assert_awaited_once()
            assert benchmark_proc.finish_streamed_output.await_args.kwargs["cancelled"]
        # open_stream is called for serial-capture.log, exporter-logs.log,
        # and harness-output.log
        assert len(opened_streams) == 3
        assert all(s.closed for s in opened_streams)

    async def test_serial_tail_remains_in_artifact_but_not_service_log(
        self, tmp_path, monkeypatch, caplog
    ):
        from agents.benchmark import server

        monkeypatch.delenv("TICKET_ID", raising=False)
        (tmp_path / "boot-timings-test.sh").write_text("#!/bin/bash\n")
        mock_cache = MagicMock()
        mock_cache.get_path.return_value = tmp_path
        ticket = _boot_time_ticket(
            {
                "resource_provider": "jumpstarter",
                "resource_provider_metadata": {"lease_id": "lease-123"},
                "directives": {"serial_capture": True},
            }
        )
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
            patch.object(harness, "AuditedSubprocessRunner", return_value=runner),
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


class TestOutputDirPersistence:
    """Verify output_dir is written to custom_fields early."""

    @pytest.mark.parametrize("get_status", [200, 503])
    async def test_early_persistence_precedes_subprocess_and_is_best_effort(
        self, tmp_path, monkeypatch, get_status
    ):
        from agents.benchmark import server

        monkeypatch.setenv("TICKET_ID", "PERF-BOOT")
        (tmp_path / "boot-timings-test.sh").write_text("#!/bin/bash\n")
        cache = MagicMock()
        cache.get_path.return_value = tmp_path
        client = _FakeClient(
            _FakeResponse(
                {"custom_fields": {"output_dirs": []}}, status_code=get_status
            )
        )
        start_observations = []

        async def start(*_args, **_kwargs):
            start_observations.append(list(client.calls))
            return _make_mock_process(stdout_data=b"OK")

        def emit(_event):
            pass

        runner = MagicMock()
        runner.start = AsyncMock(side_effect=start)
        token = bind_trace_context(new_trace_context(ticket_id="PERF-BOOT"))
        try:
            with (
                patch.object(server, "_initialized", True),
                patch.object(server, "_repo_cache", cache),
                patch.object(server, "_ticket", _boot_time_ticket()),
                patch.object(harness, "durable_filesystem_emitter", return_value=emit),
                patch.object(harness, "AuditedSubprocessRunner", return_value=runner),
                patch("paths.create_artifact_dir", return_value=tmp_path),
                patch("socket.create_connection", return_value=MagicMock()),
                patch(
                    "providers.execution.AuditedAsyncHTTPClient", return_value=client
                ),
                patch("agents.server_utils.ticket_state_headers", return_value={}),
            ):
                result = json.loads(
                    await server.execute_boot_time_test(
                        sut_host="192.0.2.10", samples=1
                    )
                )
        finally:
            reset_trace_context(token)

        assert result["status"] == "completed"
        assert len(start_observations) == 1
        assert [call[0] for call in start_observations[0]] == (
            ["GET", "PATCH"] if get_status == 200 else ["GET"]
        )
        if get_status == 200:
            assert start_observations[0][1][2]["fields"] == {
                "output_dir": str(tmp_path),
                "output_dirs": [str(tmp_path)],
            }


# ---------------------------------------------------------------------------
# _append_output_dir helper tests
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Minimal response stub for _append_output_dir tests."""

    def __init__(self, payload: dict | None = None, *, status_code: int = 200):
        self._payload = payload or {}
        self._status_code = status_code

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self._status_code >= 400:
            raise RuntimeError(f"HTTP {self._status_code}")


class _FakeClient:
    """Async-context-manager stub that records GET/PATCH calls."""

    def __init__(
        self, get_response: _FakeResponse, patch_response: _FakeResponse | None = None
    ):
        self.get_response = get_response
        self.patch_response = patch_response or _FakeResponse()
        self.calls: list[tuple[str, str, dict | None]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url: str, **kw):
        self.calls.append(("GET", url, None))
        return self.get_response

    async def patch(self, url: str, *, json: dict | None = None, **kw):
        self.calls.append(("PATCH", url, json))
        return self.patch_response


class TestAppendOutputDir:
    """Unit tests for the _append_output_dir helper."""

    @pytest.mark.asyncio
    async def test_append_output_dir_appends_to_empty_list(self, monkeypatch):
        fake_client = _FakeClient(
            get_response=_FakeResponse({"custom_fields": {}}),
        )
        monkeypatch.setenv("STATE_STORE_URL", "http://fake:9090")
        monkeypatch.setenv("TICKET_ID", "T-1")

        import agents.benchmark.harnesses.boot_time as srv

        monkeypatch.setattr(
            "providers.execution.AuditedAsyncHTTPClient",
            lambda **kw: fake_client,
        )
        monkeypatch.setattr(
            "agents.server_utils.ticket_state_headers",
            lambda: {"x-test": "1"},
        )

        await srv._append_output_dir("T-1", "/data/run-1")

        _, _, patch_payload = fake_client.calls[1]
        assert patch_payload == {
            "fields": {
                "output_dir": "/data/run-1",
                "output_dirs": ["/data/run-1"],
            }
        }

    @pytest.mark.asyncio
    async def test_append_output_dir_null_list_is_empty(self, monkeypatch):
        import agents.benchmark.harnesses.boot_time as srv

        fake_client = _FakeClient(
            _FakeResponse({"custom_fields": {"output_dirs": None}})
        )
        monkeypatch.setattr(
            "providers.execution.AuditedAsyncHTTPClient", lambda **_kw: fake_client
        )
        monkeypatch.setattr("agents.server_utils.ticket_state_headers", lambda: {})
        await srv._append_output_dir("T-NULL", "/data/run-1")
        assert fake_client.calls[1][2]["fields"]["output_dirs"] == ["/data/run-1"]

    @pytest.mark.parametrize(
        ("payload", "message"),
        [
            ({}, "custom_fields"),
            ({"custom_fields": None}, "custom_fields"),
            ({"custom_fields": ["bad"]}, "custom_fields"),
            ({"custom_fields": "bad"}, "custom_fields"),
            ({"custom_fields": {"output_dirs": "old-path"}}, "output_dirs"),
            ({"custom_fields": {"output_dirs": {"old": "path"}}}, "output_dirs"),
        ],
    )
    async def test_malformed_ticket_fields_do_not_patch(
        self, monkeypatch, payload, message
    ):
        import agents.benchmark.harnesses.boot_time as srv

        fake_client = _FakeClient(_FakeResponse(payload))
        monkeypatch.setattr(
            "providers.execution.AuditedAsyncHTTPClient", lambda **_kw: fake_client
        )
        monkeypatch.setattr("agents.server_utils.ticket_state_headers", lambda: {})
        with pytest.raises(ValueError, match=message):
            await srv._append_output_dir("T-BAD", "/data/run-1")
        assert [call[0] for call in fake_client.calls] == ["GET"]

    async def test_ticket_read_error_does_not_parse_or_patch(self, monkeypatch):
        import agents.benchmark.harnesses.boot_time as srv

        class ErrorResponse(_FakeResponse):
            def json(self):
                raise AssertionError("error response must not be parsed")

        fake_client = _FakeClient(ErrorResponse(status_code=503))
        monkeypatch.setattr(
            "providers.execution.AuditedAsyncHTTPClient", lambda **_kw: fake_client
        )
        monkeypatch.setattr("agents.server_utils.ticket_state_headers", lambda: {})
        with pytest.raises(RuntimeError, match="HTTP 503"):
            await srv._append_output_dir("T-ERROR", "/data/run-1")
        assert [call[0] for call in fake_client.calls] == ["GET"]

    async def test_audited_update_preserves_ticket_and_fence_headers(self, monkeypatch):
        import agents.benchmark.harnesses.boot_time as srv
        from providers.execution import AuditedAsyncHTTPClient

        requests = []
        audit_events = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.method == "GET":
                return httpx.Response(
                    200, json={"custom_fields": {"output_dirs": ["/data/old"]}}
                )
            return httpx.Response(200, json={})

        async def emit(event):
            audit_events.append(event)

        def client_factory(**kwargs):
            client = httpx.AsyncClient(
                transport=httpx.MockTransport(handler), headers=kwargs["headers"]
            )
            return AuditedAsyncHTTPClient(client=client, emit=emit)

        headers = {
            "X-Agentic-Perf-Orchestrator-Session": "session-1",
            "X-Agentic-Perf-Orchestrator-Epoch": "7",
            "X-Agentic-Perf-Claim-Id": "claim-1",
        }
        monkeypatch.setattr("agents.server_utils.ticket_state_headers", lambda: headers)
        monkeypatch.setattr(
            "providers.execution.AuditedAsyncHTTPClient", client_factory
        )
        token = bind_trace_context(
            new_trace_context(ticket_id="T-1", agent_id="benchmark")
        )
        try:
            await srv._append_output_dir("T-1", "/data/run-1")
        finally:
            reset_trace_context(token)

        assert [request.method for request in requests] == ["GET", "PATCH"]
        assert all(
            request.headers["x-agentic-perf-ticket-id"] == "T-1" for request in requests
        )
        assert all(
            all(request.headers[key] == value for key, value in headers.items())
            for request in requests
        )
        assert json.loads(requests[1].content)["fields"]["output_dirs"] == [
            "/data/old",
            "/data/run-1",
        ]
        assert audit_events
        assert {event.ticket_id for event in audit_events} == {"T-1"}

    @pytest.mark.asyncio
    async def test_append_output_dir_appends_to_existing(self, monkeypatch):
        fake_client = _FakeClient(
            get_response=_FakeResponse(
                {"custom_fields": {"output_dirs": ["/data/run-0"]}}
            ),
        )
        monkeypatch.setenv("STATE_STORE_URL", "http://fake:9090")

        import agents.benchmark.harnesses.boot_time as srv

        monkeypatch.setattr(
            "providers.execution.AuditedAsyncHTTPClient",
            lambda **kw: fake_client,
        )
        monkeypatch.setattr(
            "agents.server_utils.ticket_state_headers",
            lambda: {},
        )

        await srv._append_output_dir("T-2", "/data/run-1")

        _, _, patch_payload = fake_client.calls[1]
        assert patch_payload["fields"]["output_dirs"] == [
            "/data/run-0",
            "/data/run-1",
        ]

    @pytest.mark.asyncio
    async def test_append_output_dir_skips_duplicate(self, monkeypatch):
        fake_client = _FakeClient(
            get_response=_FakeResponse(
                {"custom_fields": {"output_dirs": ["/data/run-1"]}}
            ),
        )
        monkeypatch.setenv("STATE_STORE_URL", "http://fake:9090")

        import agents.benchmark.harnesses.boot_time as srv

        monkeypatch.setattr(
            "providers.execution.AuditedAsyncHTTPClient",
            lambda **kw: fake_client,
        )
        monkeypatch.setattr(
            "agents.server_utils.ticket_state_headers",
            lambda: {},
        )

        await srv._append_output_dir("T-3", "/data/run-1")

        _, _, patch_payload = fake_client.calls[1]
        assert patch_payload["fields"]["output_dirs"] == ["/data/run-1"]

    @pytest.mark.asyncio
    async def test_append_output_dir_raises_on_http_error(self, monkeypatch):
        fake_client = _FakeClient(
            get_response=_FakeResponse({"custom_fields": {}}),
            patch_response=_FakeResponse(status_code=500),
        )
        monkeypatch.setenv("STATE_STORE_URL", "http://fake:9090")

        import agents.benchmark.harnesses.boot_time as srv

        monkeypatch.setattr(
            "providers.execution.AuditedAsyncHTTPClient",
            lambda **kw: fake_client,
        )
        monkeypatch.setattr(
            "agents.server_utils.ticket_state_headers",
            lambda: {},
        )

        with pytest.raises(RuntimeError, match="HTTP 500"):
            await srv._append_output_dir("T-4", "/data/run-1")


class TestToolDispatch:
    """The MCP tool in server.py must only bind server state and dispatch."""

    @pytest.mark.asyncio
    async def test_execute_boot_time_test_dispatches_with_server_state(
        self, monkeypatch
    ):
        import agents.benchmark.server as server

        captured = {}

        async def fake_run(ctx, sut_host, **kwargs):
            captured["ctx"] = ctx
            captured["sut_host"] = sut_host
            captured["kwargs"] = kwargs
            return json.dumps({"status": "ok"})

        ownership_checks = []

        async def fake_require_tool_owner(owner_harness):
            ownership_checks.append(owner_harness)

        monkeypatch.setattr(server, "run_boot_time_test", fake_run)
        monkeypatch.setattr(server, "_require_tool_owner", fake_require_tool_owner)
        monkeypatch.setattr(server, "_initialized", True)
        monkeypatch.setattr(server, "_ssh", "ssh-sentinel")
        monkeypatch.setattr(server, "_repo_cache", "cache-sentinel")
        monkeypatch.setattr(server, "_ticket", {"id": "T-1"})

        result = await server.execute_boot_time_test(
            sut_host="192.0.2.10", samples=3, kpi_pattern="boot"
        )

        assert json.loads(result) == {"status": "ok"}
        assert captured["sut_host"] == "192.0.2.10"
        assert captured["kwargs"] == {
            "samples": 3,
            "kpi_pattern": "boot",
            "clean_journal": False,
            "description": "",
        }
        ctx = captured["ctx"]
        assert ctx.ssh == "ssh-sentinel"
        assert ctx.repo_cache == "cache-sentinel"
        assert ctx.ticket == {"id": "T-1"}

        await ctx.require_tool_owner()
        assert ownership_checks == ["boot-time"]


class TestBootTimeUBootConfig:
    """U-Boot bootdelay configuration injected before the test loop."""

    def _make_s32g_ticket(self, directives: dict | None = None) -> dict:
        return _boot_time_ticket(
            {
                "resource_provider": "jumpstarter",
                "resource_provider_metadata": {
                    "lease_id": "lease-s32g",
                    "board_target": "s32g_vnp_rdb3",
                    "selector": "board-type=nxp-s32g-vnp-rdb3,enabled=true",
                },
                "directives": directives or {},
            }
        )

    def _make_runner(self, uboot_rc: int = 0) -> tuple[MagicMock, list]:
        """
        Return (runner mock, call_log) where call_log accumulates every argv
        passed to runner.start().  The first jmp-shell call is the U-Boot
        config; subsequent jmp-shell calls are serial/power; the non-jmp call
        is the benchmark subprocess.
        """
        call_log: list[list[str]] = []
        benchmark_proc = _make_mock_process()
        uboot_proc = _make_mock_process(returncode=uboot_rc)
        serial_proc = MagicMock(pid=99, returncode=None)
        serial_proc.wait = AsyncMock(return_value=0)

        async def start(argv, **_kwargs):
            call_log.append(list(argv))
            # U-Boot config: jmp shell ... set-uboot
            if argv[0] == "jmp" and "set-uboot" in argv:
                return uboot_proc
            # Passive serial capture
            if argv[0] == "jmp":
                return serial_proc
            return benchmark_proc

        runner = MagicMock()
        runner.start = AsyncMock(side_effect=start)
        return runner, call_log

    async def test_s32g_triggers_uboot_config_by_default(self, tmp_path, monkeypatch):
        """An S32G board with no directive gets bootdelay=0 configured."""
        import sys

        from agents.benchmark import server

        monkeypatch.delenv("TICKET_ID", raising=False)
        (tmp_path / "boot-timings-test.sh").write_text("#!/bin/bash\n")
        (tmp_path / "jumpstarter-serial-helper.py").write_text("# stub\n")
        mock_cache = MagicMock()
        mock_cache.get_path.return_value = tmp_path
        runner, call_log = self._make_runner(uboot_rc=0)

        with (
            patch.object(server, "_initialized", True),
            patch.object(server, "_repo_cache", mock_cache),
            patch.object(server, "_ticket", self._make_s32g_ticket()),
            patch("paths.create_artifact_dir", return_value=tmp_path),
            patch("socket.create_connection", return_value=MagicMock()),
            patch.object(harness, "AuditedSubprocessRunner", return_value=runner),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = json.loads(
                await server.execute_boot_time_test(sut_host="192.0.2.10", samples=1)
            )

        uboot_calls = [c for c in call_log if "set-uboot" in c]
        assert len(uboot_calls) == 1, "Expected exactly one set-uboot call"
        cmd = uboot_calls[0]
        assert cmd[:4] == ["jmp", "shell", "--lease", "lease-s32g"]
        assert cmd[4] == "--"
        assert cmd[5] == sys.executable
        assert cmd[7] == "set-uboot"
        assert "-s" in cmd
        idx = cmd.index("-s")
        assert cmd[idx + 1] == "bootdelay"
        assert cmd[idx + 2] == "0"

        assert result.get("uboot_config_applied") == ["bootdelay=0"]
        assert "bootdelay=0" in (result.get("notes") or "")
        assert "configure_bootdelay=false" in (result.get("notes") or "")

    async def test_non_s32g_board_skips_uboot_config(self, tmp_path, monkeypatch):
        """A non-S32G Jumpstarter board must not receive U-Boot config."""
        from agents.benchmark import server

        monkeypatch.delenv("TICKET_ID", raising=False)
        (tmp_path / "boot-timings-test.sh").write_text("#!/bin/bash\n")
        (tmp_path / "jumpstarter-serial-helper.py").write_text("# stub\n")
        mock_cache = MagicMock()
        mock_cache.get_path.return_value = tmp_path
        ticket = _boot_time_ticket(
            {
                "resource_provider": "jumpstarter",
                "resource_provider_metadata": {
                    "lease_id": "lease-qc8775",
                    "board_target": "qc8775",
                    "selector": "board-type=qc8775",
                },
            }
        )
        runner, call_log = self._make_runner()

        with (
            patch.object(server, "_initialized", True),
            patch.object(server, "_repo_cache", mock_cache),
            patch.object(server, "_ticket", ticket),
            patch("paths.create_artifact_dir", return_value=tmp_path),
            patch("socket.create_connection", return_value=MagicMock()),
            patch.object(harness, "AuditedSubprocessRunner", return_value=runner),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = json.loads(
                await server.execute_boot_time_test(sut_host="192.0.2.10", samples=1)
            )

        uboot_calls = [c for c in call_log if "set-uboot" in c]
        assert uboot_calls == [], "Non-S32G board should not trigger U-Boot config"
        assert "uboot_config_applied" not in result

    async def test_configure_bootdelay_false_opts_out(self, tmp_path, monkeypatch):
        """Directive configure_bootdelay=false suppresses config on S32G."""
        from agents.benchmark import server

        monkeypatch.delenv("TICKET_ID", raising=False)
        (tmp_path / "boot-timings-test.sh").write_text("#!/bin/bash\n")
        (tmp_path / "jumpstarter-serial-helper.py").write_text("# stub\n")
        mock_cache = MagicMock()
        mock_cache.get_path.return_value = tmp_path
        runner, call_log = self._make_runner()

        with (
            patch.object(server, "_initialized", True),
            patch.object(server, "_repo_cache", mock_cache),
            patch.object(
                server,
                "_ticket",
                self._make_s32g_ticket({"configure_bootdelay": False}),
            ),
            patch("paths.create_artifact_dir", return_value=tmp_path),
            patch("socket.create_connection", return_value=MagicMock()),
            patch.object(harness, "AuditedSubprocessRunner", return_value=runner),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = json.loads(
                await server.execute_boot_time_test(sut_host="192.0.2.10", samples=1)
            )

        uboot_calls = [c for c in call_log if "set-uboot" in c]
        assert uboot_calls == [], "configure_bootdelay=false must suppress config"
        assert "uboot_config_applied" not in result

    async def test_configure_bootdelay_true_forces_non_s32g(
        self, tmp_path, monkeypatch
    ):
        """Directive configure_bootdelay=true forces U-Boot config even on non-S32G."""

        from agents.benchmark import server

        monkeypatch.delenv("TICKET_ID", raising=False)
        (tmp_path / "boot-timings-test.sh").write_text("#!/bin/bash\n")
        (tmp_path / "jumpstarter-serial-helper.py").write_text("# stub\n")
        mock_cache = MagicMock()
        mock_cache.get_path.return_value = tmp_path
        ticket = _boot_time_ticket(
            {
                "resource_provider": "jumpstarter",
                "resource_provider_metadata": {
                    "lease_id": "lease-other",
                    "board_target": "some_other_board",
                    "selector": "board-type=other-board",
                },
                "directives": {"configure_bootdelay": True},
            }
        )
        runner, call_log = self._make_runner(uboot_rc=0)

        with (
            patch.object(server, "_initialized", True),
            patch.object(server, "_repo_cache", mock_cache),
            patch.object(server, "_ticket", ticket),
            patch("paths.create_artifact_dir", return_value=tmp_path),
            patch("socket.create_connection", return_value=MagicMock()),
            patch.object(harness, "AuditedSubprocessRunner", return_value=runner),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = json.loads(
                await server.execute_boot_time_test(sut_host="192.0.2.10", samples=1)
            )

        uboot_calls = [c for c in call_log if "set-uboot" in c]
        assert len(uboot_calls) == 1
        assert result.get("uboot_config_applied") == ["bootdelay=0"]

    async def test_uboot_failure_is_non_fatal_and_noted(self, tmp_path, monkeypatch):
        """A set-uboot failure should not abort the benchmark; a note is added."""
        from agents.benchmark import server

        monkeypatch.delenv("TICKET_ID", raising=False)
        (tmp_path / "boot-timings-test.sh").write_text("#!/bin/bash\n")
        (tmp_path / "jumpstarter-serial-helper.py").write_text("# stub\n")
        mock_cache = MagicMock()
        mock_cache.get_path.return_value = tmp_path
        runner, call_log = self._make_runner(uboot_rc=1)

        with (
            patch.object(server, "_initialized", True),
            patch.object(server, "_repo_cache", mock_cache),
            patch.object(server, "_ticket", self._make_s32g_ticket()),
            patch("paths.create_artifact_dir", return_value=tmp_path),
            patch("socket.create_connection", return_value=MagicMock()),
            patch.object(harness, "AuditedSubprocessRunner", return_value=runner),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = json.loads(
                await server.execute_boot_time_test(sut_host="192.0.2.10", samples=1)
            )

        # Benchmark still ran (failure was non-fatal)
        uboot_calls = [c for c in call_log if "set-uboot" in c]
        assert len(uboot_calls) == 1
        # No config applied — set-uboot failed
        assert "uboot_config_applied" not in result
        # But a note warns the user
        notes = result.get("notes", "")
        assert notes, "A failure note must be present"
        assert "failed" in notes.lower()

    async def test_missing_helper_script_skips_gracefully(self, tmp_path, monkeypatch):
        """Missing jumpstarter-serial-helper.py logs a warning but does not fail."""
        from agents.benchmark import server

        monkeypatch.delenv("TICKET_ID", raising=False)
        (tmp_path / "boot-timings-test.sh").write_text("#!/bin/bash\n")
        # Intentionally do NOT create jumpstarter-serial-helper.py
        mock_cache = MagicMock()
        mock_cache.get_path.return_value = tmp_path
        runner, call_log = self._make_runner()

        with (
            patch.object(server, "_initialized", True),
            patch.object(server, "_repo_cache", mock_cache),
            patch.object(server, "_ticket", self._make_s32g_ticket()),
            patch("paths.create_artifact_dir", return_value=tmp_path),
            patch("socket.create_connection", return_value=MagicMock()),
            patch.object(harness, "AuditedSubprocessRunner", return_value=runner),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = json.loads(
                await server.execute_boot_time_test(sut_host="192.0.2.10", samples=1)
            )

        uboot_calls = [c for c in call_log if "set-uboot" in c]
        assert uboot_calls == [], "No set-uboot call when helper is missing"
        assert "uboot_config_applied" not in result
        # Benchmark still ran
        assert "status" in result
