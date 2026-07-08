"""Tests for tool_progress helper in server_utils."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Ensure env vars are clean between tests."""
    monkeypatch.delenv("TICKET_ID", raising=False)
    monkeypatch.delenv("STATE_STORE_URL", raising=False)
    monkeypatch.delenv("AGENT_NAME", raising=False)


async def test_posts_comment_when_ticket_id_set(monkeypatch):
    """tool_progress should POST a comment to the state store."""
    monkeypatch.setenv("TICKET_ID", "PERF-TEST123")
    monkeypatch.setenv("STATE_STORE_URL", "http://localhost:9999")
    monkeypatch.setenv("AGENT_NAME", "resource-agent")

    mock_response = AsyncMock()
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        from agents.server_utils import tool_progress

        await tool_progress("SSH ready on 10.0.0.1", "setup_ssh")

    mock_client.post.assert_called_once_with(
        "http://localhost:9999/api/v1/tickets/PERF-TEST123/comments",
        json={
            "author": "resource-agent/setup_ssh",
            "body": "SSH ready on 10.0.0.1",
        },
    )


async def test_noops_without_ticket_id():
    """tool_progress should silently do nothing when no ticket_id."""
    with patch("httpx.AsyncClient") as mock_cls:
        from agents.server_utils import tool_progress

        await tool_progress("some message", "some_tool")

    mock_cls.assert_not_called()


async def test_uses_system_when_no_agent_name(monkeypatch):
    """Falls back to 'system' when AGENT_NAME is not set."""
    monkeypatch.setenv("TICKET_ID", "PERF-TEST456")

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=AsyncMock())
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        from agents.server_utils import tool_progress

        await tool_progress("Installing...", "install_harness")

    call_kwargs = mock_client.post.call_args
    assert call_kwargs[1]["json"]["author"] == "system/install_harness"


async def test_swallows_exceptions(monkeypatch):
    """tool_progress should not raise even if the HTTP call fails."""
    monkeypatch.setenv("TICKET_ID", "PERF-TEST789")

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=Exception("connection refused"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        from agents.server_utils import tool_progress

        await tool_progress("this should not raise", "some_tool")


async def test_explicit_params_override_env(monkeypatch):
    """Explicit ticket_id and state_store_url override env vars."""
    monkeypatch.setenv("TICKET_ID", "PERF-WRONG")
    monkeypatch.setenv("STATE_STORE_URL", "http://wrong:1234")
    monkeypatch.setenv("AGENT_NAME", "test-agent")

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=AsyncMock())
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=mock_client):
        from agents.server_utils import tool_progress

        await tool_progress(
            "msg",
            "mytool",
            ticket_id="PERF-RIGHT",
            state_store_url="http://correct:5678",
        )

    call_args = mock_client.post.call_args
    assert "PERF-RIGHT" in call_args[0][0]
    assert "http://correct:5678" in call_args[0][0]


class TestRunWithProgress:
    """Tests for SSHExecutor.run_with_progress."""

    @pytest.fixture
    def ssh(self):
        from providers.ssh import SSHExecutor

        return SSHExecutor(user="root", key_path="/tmp/test.pem")

    async def test_calls_callback_with_new_output(self, ssh):
        """Callback is invoked with new output lines."""
        from providers.ssh import SSHResult

        call_log = []

        async def cb(line: str, elapsed: int) -> None:
            call_log.append((line, elapsed))

        call_count = 0

        async def mock_run(host, cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            # 1: launch — return PID
            if "nohup" in cmd:
                return SSHResult(stdout="12345\n", stderr="", exit_code=0)
            # 2: first poll — rc file not yet present
            if "test -f" in cmd and call_count <= 4:
                return SSHResult(stdout="", stderr="", exit_code=1)
            # 3: tail output
            if "tail -5" in cmd and call_count <= 5:
                return SSHResult(
                    stdout="Starting sample 1\n", stderr="", exit_code=0,
                )
            # 4: second poll — rc file present (done)
            if "test -f" in cmd:
                return SSHResult(stdout="", stderr="", exit_code=0)
            if "tail -5" in cmd:
                return SSHResult(
                    stdout="Benchmark complete\n", stderr="", exit_code=0,
                )
            # 5: cat output
            if "cat" in cmd and ".out" in cmd:
                return SSHResult(
                    stdout="Starting sample 1\nBenchmark complete\n",
                    stderr="",
                    exit_code=0,
                )
            # 6: cat rc
            if "cat" in cmd and ".rc" in cmd:
                return SSHResult(stdout="0\n", stderr="", exit_code=0)
            # 7: cleanup
            if "rm -f" in cmd:
                return SSHResult(stdout="", stderr="", exit_code=0)
            return SSHResult(stdout="", stderr="", exit_code=0)

        ssh.run = mock_run

        async def noop_sleep(_): pass

        with patch("asyncio.sleep", noop_sleep):
            result = await ssh.run_with_progress(
                "10.0.0.1", "benchmark run", progress_callback=cb,
                poll_interval=1,
            )

        assert result.exit_code == 0
        assert "Benchmark complete" in result.stdout
        assert len(call_log) >= 1
        assert call_log[0][0] == "Starting sample 1"

    async def test_returns_correct_exit_code(self, ssh):
        """Non-zero exit code is captured from the rc file."""
        from providers.ssh import SSHResult

        async def mock_run(host, cmd, **kwargs):
            if "nohup" in cmd:
                return SSHResult(stdout="99\n", stderr="", exit_code=0)
            if "test -f" in cmd:
                return SSHResult(stdout="", stderr="", exit_code=0)
            if "cat" in cmd and ".out" in cmd:
                return SSHResult(stdout="error output\n", stderr="", exit_code=0)
            if "cat" in cmd and ".rc" in cmd:
                return SSHResult(stdout="42\n", stderr="", exit_code=0)
            if "rm -f" in cmd:
                return SSHResult(stdout="", stderr="", exit_code=0)
            return SSHResult(stdout="", stderr="", exit_code=0)

        ssh.run = mock_run

        async def noop_sleep(_): pass

        with patch("asyncio.sleep", noop_sleep):
            result = await ssh.run_with_progress(
                "10.0.0.1", "failing cmd", poll_interval=1,
            )

        assert result.exit_code == 42

    async def test_skips_callback_on_duplicate_output(self, ssh):
        """Callback is not called when output hasn't changed."""
        from providers.ssh import SSHResult

        call_log = []

        async def cb(line: str, elapsed: int) -> None:
            call_log.append(line)

        poll_count = 0

        async def mock_run(host, cmd, **kwargs):
            nonlocal poll_count
            if "nohup" in cmd:
                return SSHResult(stdout="1\n", stderr="", exit_code=0)
            if "test -f" in cmd:
                poll_count += 1
                # Finish after 3 polls
                if poll_count >= 3:
                    return SSHResult(stdout="", stderr="", exit_code=0)
                return SSHResult(stdout="", stderr="", exit_code=1)
            if "tail -5" in cmd:
                return SSHResult(
                    stdout="same line\n", stderr="", exit_code=0,
                )
            if "cat" in cmd and ".out" in cmd:
                return SSHResult(stdout="same line\n", stderr="", exit_code=0)
            if "cat" in cmd and ".rc" in cmd:
                return SSHResult(stdout="0\n", stderr="", exit_code=0)
            if "rm -f" in cmd:
                return SSHResult(stdout="", stderr="", exit_code=0)
            return SSHResult(stdout="", stderr="", exit_code=0)

        ssh.run = mock_run

        async def noop_sleep(_): pass

        with patch("asyncio.sleep", noop_sleep):
            await ssh.run_with_progress(
                "10.0.0.1", "cmd", progress_callback=cb, poll_interval=1,
            )

        assert len(call_log) == 1

    async def test_handles_launch_failure(self, ssh):
        """Returns error result when background launch fails."""
        from providers.ssh import SSHResult

        async def mock_run(host, cmd, **kwargs):
            return SSHResult(
                stdout="", stderr="Permission denied", exit_code=255,
            )

        ssh.run = mock_run

        result = await ssh.run_with_progress("10.0.0.1", "cmd")
        assert result.exit_code == 255
        assert "Permission denied" in result.stderr
