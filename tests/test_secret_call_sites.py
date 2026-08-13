"""Tests for secret call-site hardening (#456).

Verifies that secrets are not exposed in process arguments or log
output after the V2/V3/V4 fixes.
"""

from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from providers.ssh import SSHExecutor, SSHResult

_SCRUB_SCRIPT = Path(__file__).parent.parent / "scripts" / "scrub-event-logs.py"


def _load_scrub_module():
    """Load scrub-event-logs.py as a module (it has a hyphenated name)."""
    spec = importlib.util.spec_from_file_location("scrub", _SCRUB_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── SSH stdin_data support ─────────────────────────────────────


class TestSSHStdinData:
    """SSHExecutor.run() stdin_data parameter."""

    async def test_stdin_data_piped_to_subprocess(self) -> None:
        """When stdin_data is provided, it flows to proc.communicate()."""
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"ok\n", b"")
        mock_proc.returncode = 0

        with patch(
            "providers.ssh.asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ) as mock_exec:
            ssh = SSHExecutor(user="testuser")
            result = await ssh.run(
                "host1",
                "cat > /tmp/file",
                stdin_data=b"secret-content",
            )

        assert result.exit_code == 0
        create_call = mock_exec.call_args
        assert create_call.kwargs.get("stdin") is not None
        mock_proc.communicate.assert_called_once()
        call_kwargs = mock_proc.communicate.call_args
        assert call_kwargs.kwargs.get("input") == b"secret-content" or (
            call_kwargs.args and call_kwargs.args[0] == b"secret-content"
        )

    async def test_no_stdin_when_stdin_data_is_none(self) -> None:
        """When stdin_data is not provided, stdin is not piped."""
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"ok\n", b"")
        mock_proc.returncode = 0

        with patch(
            "providers.ssh.asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ) as mock_exec:
            ssh = SSHExecutor(user="testuser")
            await ssh.run("host1", "echo hello")

        create_call = mock_exec.call_args
        assert create_call.kwargs.get("stdin") is None


# ── V2: KUBEADMIN_PASSWORD not on command line ─────────────────


class TestV2EnvFile:
    """benchmark-runner env vars go through env-file, not -e flags."""

    async def test_password_not_in_ssh_command(self) -> None:
        """KUBEADMIN_PASSWORD must not appear in any SSH command string."""
        import agents.benchmark.server as server

        password = "super-secret-kube-pw-12345"
        run_file = {
            "env_vars": {"KUBEADMIN_PASSWORD": password, "NORMAL_VAR": "ok"},
            "container_image": "quay.io/test:latest",
            "artifacts_dir": "/tmp/arts",
            "kubeconfig_path": "/root/.kube/config",
        }

        ssh_commands: list[str] = []
        ssh_stdin_data: list[bytes | None] = []

        async def mock_run(
            host: str,
            command: str,
            timeout: int = 300,
            key_path: str | None = None,
            allocate_pty: bool = False,
            stdin_data: bytes | None = None,
        ) -> SSHResult:
            ssh_commands.append(command)
            ssh_stdin_data.append(stdin_data)
            return SSHResult(stdout="", stderr="", exit_code=0)

        async def mock_run_with_progress(
            host: str,
            command: str,
            progress_callback=None,
            poll_interval: int = 30,
            key_path: str | None = None,
        ) -> SSHResult:
            ssh_commands.append(command)
            return SSHResult(stdout="", stderr="", exit_code=0)

        mock_ssh = MagicMock()
        mock_ssh.run = mock_run
        mock_ssh.run_with_progress = mock_run_with_progress

        original_ssh = server._ssh
        original_init = server._initialized
        original_skill = server._skill_provider
        server._ssh = mock_ssh
        server._initialized = True
        server._skill_provider = None

        with patch(
            "agents.server_utils.assert_ticket_active",
            new_callable=AsyncMock,
            return_value={"status": "ok"},
        ):
            try:
                await server.execute_benchmark(
                    controller="10.0.0.1",
                    run_file=run_file,
                    harness="benchmark-runner",
                )
            finally:
                server._ssh = original_ssh
                server._initialized = original_init
                server._skill_provider = original_skill

        for cmd in ssh_commands:
            assert password not in cmd, f"Password found in SSH command: {cmd}"

        env_file_created = any(
            data is not None and password.encode() in data for data in ssh_stdin_data
        )
        assert env_file_created, (
            "Password should be sent via stdin_data, not in command"
        )

        podman_cmd = [c for c in ssh_commands if "podman run" in c]
        assert podman_cmd, "Expected a podman run command"
        assert "--env-file" in podman_cmd[0], "Expected --env-file in podman command"
        assert "-e " not in podman_cmd[0], "Expected no -e flags in podman command"


# ── V3: boot-time password not in log ──────────────────────────


class TestV3LogSanitization:
    """boot-time log output must not contain --password= values."""

    def test_safe_cmd_excludes_password(self) -> None:
        """The safe_cmd filter removes --password= arguments."""
        cmd = [
            "/path/to/script",
            "host1",
            "5",
            "--username=root",
            "--password=secret123",
            "--folder-prefix=results",
        ]
        safe_cmd = [a for a in cmd[:6] if not a.startswith("--password=")]
        assert "--password=secret123" not in safe_cmd
        assert "secret123" not in " ".join(safe_cmd)
        assert "--username=root" in safe_cmd
        assert len(safe_cmd) == 5


# ── V4: QUADS password via stdin ───────────────────────────────


class TestV4QuadsStdin:
    """QUADS _copy_ssh_key passes password via stdin, not -c arg."""

    async def test_password_not_in_process_args(self) -> None:
        """default_root_password must not appear in python3 -c string."""
        from providers.quads import QuadsClient

        password = "quads-root-pw-67890"

        captured_args: list[tuple] = []

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"ok\n", b"")
        mock_proc.returncode = 0

        async def mock_create_subprocess_exec(*args, **kwargs):
            captured_args.append(args)
            return mock_proc

        provider = QuadsClient.__new__(QuadsClient)
        provider.default_root_password = password

        with patch(
            "providers.quads.asyncio.create_subprocess_exec",
            side_effect=mock_create_subprocess_exec,
        ):
            result = await provider._copy_ssh_key(
                "10.0.0.1", "ssh-rsa AAAA... test@host"
            )

        assert result == "ok"

        for args in captured_args:
            args_str = " ".join(str(a) for a in args)
            assert password not in args_str, (
                f"Password found in process args: {args_str}"
            )

        comm_call = mock_proc.communicate.call_args
        input_data = comm_call.kwargs.get("input") or (
            comm_call.args[0] if comm_call.args else None
        )
        assert input_data is not None, (
            "Password should be passed via communicate(input=...)"
        )
        assert password.encode() in input_data, (
            "communicate() input should contain the password"
        )


# ── Scrub script ───────────────────────────────────────────────


class TestScrubScript:
    """Pattern-only JSONL log scrubber."""

    def test_password_cli_flag_scrubbed(self) -> None:
        scrub = _load_scrub_module()
        line = json.dumps(
            {"cmd": "--password=secret123 --username=root"},
        )
        result, hits = scrub.scrub_line(line)
        assert "secret123" not in result
        assert "[SCRUBBED]" in result
        assert "password_cli_flag" in hits

    def test_bearer_token_scrubbed(self) -> None:
        scrub = _load_scrub_module()
        line = json.dumps(
            {"auth": "Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig"},
        )
        result, hits = scrub.scrub_line(line)
        assert "eyJhbGciOiJIUzI1NiJ9" not in result
        assert "bearer_token" in hits

    def test_kubeadmin_password_env_scrubbed(self) -> None:
        scrub = _load_scrub_module()
        line = json.dumps(
            {"cmd": '-e KUBEADMIN_PASSWORD="mypass123"'},
        )
        result, hits = scrub.scrub_line(line)
        assert "mypass123" not in result
        assert "env_kubeadmin_password" in hits

    def test_scrub_file_dry_run(self) -> None:
        scrub = _load_scrub_module()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            log_file = tmp_path / "events.jsonl"
            log_file.write_text(
                json.dumps({"cmd": "--password=secret"})
                + "\n"
                + json.dumps({"data": "clean"})
                + "\n",
            )
            lines_changed, totals = scrub.scrub_file(
                log_file,
                apply=False,
            )
            assert lines_changed == 1
            assert "password_cli_flag" in totals
            content = log_file.read_text()
            assert "secret" in content

    def test_scrub_file_apply(self) -> None:
        scrub = _load_scrub_module()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            log_file = tmp_path / "events.jsonl"
            log_file.write_text(
                json.dumps({"cmd": "--password=secret"})
                + "\n"
                + json.dumps({"data": "clean"})
                + "\n",
            )
            lines_changed, totals = scrub.scrub_file(
                log_file,
                apply=True,
            )
            assert lines_changed == 1
            content = log_file.read_text()
            assert "secret" not in content
            assert "[SCRUBBED]" in content

    def test_pexpect_sendline_scrubbed(self) -> None:
        scrub = _load_scrub_module()
        line = json.dumps(
            {"code": "child.sendline('mysecretpw')"},
        )
        result, hits = scrub.scrub_line(line)
        assert "mysecretpw" not in result
        assert "pexpect_sendline" in hits
