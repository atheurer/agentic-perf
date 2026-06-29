"""Unit tests for execute_command HTML unescaping and background mode."""

from __future__ import annotations

import html
import json
from unittest.mock import AsyncMock, patch

import pytest

from providers.ssh import SSHResult


def _make_ssh_result(stdout="", stderr="", exit_code=0):
    return SSHResult(stdout=stdout, stderr=stderr, exit_code=exit_code)


class TestHtmlUnescape:
    """Verify HTML entities in commands are unescaped before execution."""

    def test_amp_entity(self):
        assert html.unescape("echo a &amp; echo b") == "echo a & echo b"

    def test_lt_gt_entities(self):
        assert html.unescape("test &lt; /dev/null &gt; out") == (
            "test < /dev/null > out"
        )

    def test_quot_entity(self):
        assert html.unescape("echo &quot;hello&quot;") == 'echo "hello"'

    def test_no_entities(self):
        assert html.unescape("ls -la /tmp") == "ls -la /tmp"

    def test_mixed_entities(self):
        cmd = "nc -l 30002 &amp; sleep 1; echo &quot;done&quot;"
        assert html.unescape(cmd) == 'nc -l 30002 & sleep 1; echo "done"'


class TestTrailingAmpDetection:
    """Verify trailing & detection and stripping logic."""

    def _detect_background(self, command):
        command = html.unescape(command)
        stripped = command.rstrip()
        background = False
        if stripped.endswith("&"):
            background = True
            command = stripped[:-1].rstrip()
        return background, command

    def test_trailing_amp(self):
        bg, cmd = self._detect_background("nc -l 30002 &")
        assert bg is True
        assert cmd == "nc -l 30002"

    def test_trailing_amp_entity(self):
        bg, cmd = self._detect_background("nc -l 30002 &amp;")
        assert bg is True
        assert cmd == "nc -l 30002"

    def test_no_trailing_amp(self):
        bg, cmd = self._detect_background("hostname -f")
        assert bg is False
        assert cmd == "hostname -f"

    def test_mid_command_amp_not_detected(self):
        bg, cmd = self._detect_background("echo a & echo b")
        assert bg is False
        assert cmd == "echo a & echo b"

    def test_amp_in_middle_entity(self):
        bg, cmd = self._detect_background("echo a &amp;&amp; echo b")
        assert bg is False
        assert cmd == "echo a && echo b"

    def test_trailing_whitespace(self):
        bg, cmd = self._detect_background("nc -l 30002 &  ")
        assert bg is True
        assert cmd == "nc -l 30002"


@pytest.mark.asyncio
async def test_execute_command_unescapes_html():
    """The real execute_command should unescape HTML entities."""
    import agents.infra.server as infra

    mock_ssh = AsyncMock()
    mock_ssh.run = AsyncMock(return_value=_make_ssh_result(stdout="ok\n"))

    with (
        patch.object(infra, "_ssh", mock_ssh),
        patch.object(infra, "_policy", None),
    ):
        await infra.execute_command(
            host="10.0.0.1",
            command="echo &amp; done",
        )
        call_args = mock_ssh.run.call_args
        actual_cmd = call_args[0][1]
        assert "&amp;" not in actual_cmd
        assert "& done" in actual_cmd


@pytest.mark.asyncio
async def test_execute_command_background_trailing_amp():
    """Trailing & triggers background mode."""
    import agents.infra.server as infra

    mock_ssh = AsyncMock()
    mock_ssh.run = AsyncMock(return_value=_make_ssh_result(stdout="12345\n"))

    with (
        patch.object(infra, "_ssh", mock_ssh),
        patch.object(infra, "_policy", None),
        patch.object(infra, "_background_pids", {}),
    ):
        result = await infra.execute_command(
            host="10.0.0.1",
            command="nc -l 30002 &",
        )
        data = json.loads(result)
        assert data["status"] == "backgrounded"
        assert data["pid"] == 12345
        assert "bg_id" in data
        assert data["host"] == "10.0.0.1"

        call_args = mock_ssh.run.call_args
        actual_cmd = call_args[0][1]
        assert "nohup" in actual_cmd
        assert "echo $!" in actual_cmd


@pytest.mark.asyncio
async def test_execute_command_background_explicit():
    """background=True parameter triggers background mode."""
    import agents.infra.server as infra

    mock_ssh = AsyncMock()
    mock_ssh.run = AsyncMock(return_value=_make_ssh_result(stdout="99999\n"))

    with (
        patch.object(infra, "_ssh", mock_ssh),
        patch.object(infra, "_policy", None),
        patch.object(infra, "_background_pids", {}),
    ):
        result = await infra.execute_command(
            host="10.0.0.1",
            command="nc -l 30002",
            background=True,
        )
        data = json.loads(result)
        assert data["status"] == "backgrounded"
        assert data["pid"] == 99999


@pytest.mark.asyncio
async def test_execute_command_background_amp_entity():
    """Trailing &amp; (HTML entity) triggers background mode."""
    import agents.infra.server as infra

    mock_ssh = AsyncMock()
    mock_ssh.run = AsyncMock(return_value=_make_ssh_result(stdout="55555\n"))

    with (
        patch.object(infra, "_ssh", mock_ssh),
        patch.object(infra, "_policy", None),
        patch.object(infra, "_background_pids", {}),
    ):
        result = await infra.execute_command(
            host="10.0.0.1",
            command="nc -l 30002 &amp;",
        )
        data = json.loads(result)
        assert data["status"] == "backgrounded"
        assert data["pid"] == 55555


@pytest.mark.asyncio
async def test_stop_background_command():
    """stop_background_command kills the process and cleans up."""
    import agents.infra.server as infra

    mock_ssh = AsyncMock()
    mock_ssh.run = AsyncMock(return_value=_make_ssh_result(exit_code=1))

    bg_id = "bg-test1234"
    pids = {bg_id: {"host": "10.0.0.1", "pid": 12345, "command": "nc -l"}}

    with (
        patch.object(infra, "_ssh", mock_ssh),
        patch.object(infra, "_background_pids", pids),
    ):
        result = await infra.stop_background_command(bg_id)
        data = json.loads(result)
        assert data["status"] == "stopped"
        assert data["pid"] == 12345
        assert bg_id not in pids


@pytest.mark.asyncio
async def test_stop_background_command_unknown_id():
    """stop_background_command returns error for unknown bg_id."""
    import agents.infra.server as infra

    mock_ssh = AsyncMock()

    with (
        patch.object(infra, "_ssh", mock_ssh),
        patch.object(infra, "_background_pids", {}),
    ):
        result = await infra.stop_background_command("bg-nonexistent")
        data = json.loads(result)
        assert data["status"] == "error"
        assert "Unknown" in data["message"]


@pytest.mark.asyncio
async def test_check_background_command():
    """check_background_command reports process status and output."""
    import agents.infra.server as infra

    mock_ssh = AsyncMock()
    mock_ssh.run = AsyncMock(
        side_effect=[
            _make_ssh_result(exit_code=0),
            _make_ssh_result(stdout="listening on port 30002\n"),
        ]
    )

    bg_id = "bg-check123"
    pids = {bg_id: {"host": "10.0.0.1", "pid": 67890, "command": "nc -l"}}

    with (
        patch.object(infra, "_ssh", mock_ssh),
        patch.object(infra, "_background_pids", pids),
    ):
        result = await infra.check_background_command(bg_id)
        data = json.loads(result)
        assert data["running"] is True
        assert data["pid"] == 67890
        assert "listening" in data["output"]


@pytest.mark.asyncio
async def test_check_background_command_not_running():
    """check_background_command detects stopped process."""
    import agents.infra.server as infra

    mock_ssh = AsyncMock()
    mock_ssh.run = AsyncMock(
        side_effect=[
            _make_ssh_result(exit_code=1),
            _make_ssh_result(stdout=""),
        ]
    )

    bg_id = "bg-dead1234"
    pids = {bg_id: {"host": "10.0.0.1", "pid": 11111, "command": "nc -l"}}

    with (
        patch.object(infra, "_ssh", mock_ssh),
        patch.object(infra, "_background_pids", pids),
    ):
        result = await infra.check_background_command(bg_id)
        data = json.loads(result)
        assert data["running"] is False


class TestNcInBenchmarkPolicy:
    """Verify nc and ncat are in the benchmark agent's command policy."""

    def test_nc_allowed(self):
        from agents.infra.command_policy import load_policy

        policy = load_policy("benchmark-agent")
        assert "nc" in policy.allowed_binaries
        assert "ncat" in policy.allowed_binaries


@pytest.mark.asyncio
async def test_check_hosts_batch():
    """check_hosts returns per-host results for multiple hosts."""
    import agents.infra.server as infra

    call_count = 0

    async def _mock_run(host, cmd, timeout=300):
        nonlocal call_count
        call_count += 1
        if "SSH_OK" in cmd:
            if host == "10.0.0.3":
                return _make_ssh_result(exit_code=1, stderr="Connection refused")
            return _make_ssh_result(stdout="SSH_OK\n")
        return _make_ssh_result(stdout="mockhost\nNAME=RHEL\n4\n16\n")

    mock_ssh = AsyncMock()
    mock_ssh.run = _mock_run

    with (
        patch.object(infra, "_ssh", mock_ssh),
    ):
        result = await infra.check_hosts(["10.0.0.1", "10.0.0.2", "10.0.0.3"])
        data = json.loads(result)

        assert "10.0.0.1" in data["reachable"]
        assert "10.0.0.2" in data["reachable"]
        assert "10.0.0.3" in data["unreachable"]
        assert data["results"]["10.0.0.1"]["reachable"] is True
        assert data["results"]["10.0.0.3"]["reachable"] is False


@pytest.mark.asyncio
async def test_check_hosts_empty_list():
    """check_hosts handles empty list."""
    import agents.infra.server as infra

    mock_ssh = AsyncMock()

    with (
        patch.object(infra, "_ssh", mock_ssh),
    ):
        result = await infra.check_hosts([])
        data = json.loads(result)
        assert data["reachable"] == []
        assert data["unreachable"] == []


@pytest.mark.asyncio
async def test_port_connectivity_forward_only():
    """test_port_connectivity tests client -> server."""
    import agents.infra.server as infra

    call_log = []

    async def _mock_run(host, cmd, timeout=300):
        call_log.append((host, cmd))
        if "nc -l" in cmd:
            return _make_ssh_result(stdout="12345\n")
        if "nc -z" in cmd:
            return _make_ssh_result(exit_code=0)
        if "kill" in cmd:
            return _make_ssh_result(exit_code=0)
        return _make_ssh_result()

    mock_ssh = AsyncMock()
    mock_ssh.run = _mock_run

    with (
        patch.object(infra, "_ssh", mock_ssh),
    ):
        result = await infra.test_port_connectivity(
            server_ssh_host="1.1.1.1",
            client_ssh_host="2.2.2.2",
            server_test_ip="192.168.1.1",
            port=30002,
        )
        data = json.loads(result)
        assert data["all_reachable"] is True
        assert len(data["tests"]) == 1
        assert data["tests"][0]["port"] == 30002
        assert data["tests"][0]["reachable"] is True

        listener_cmds = [(h, c) for h, c in call_log if "nc -l" in c]
        assert any("1.1.1.1" == h for h, _ in listener_cmds)
        assert any("192.168.1.1" in c for _, c in listener_cmds)


@pytest.mark.asyncio
async def test_port_connectivity_bidirectional():
    """test_port_connectivity tests both directions when client_test_ip given."""
    import agents.infra.server as infra

    async def _mock_run(host, cmd, timeout=300):
        if "nc -l" in cmd:
            return _make_ssh_result(stdout="99999\n")
        if "nc -z" in cmd:
            return _make_ssh_result(exit_code=0)
        if "kill" in cmd:
            return _make_ssh_result(exit_code=0)
        return _make_ssh_result()

    mock_ssh = AsyncMock()
    mock_ssh.run = _mock_run

    with (
        patch.object(infra, "_ssh", mock_ssh),
    ):
        result = await infra.test_port_connectivity(
            server_ssh_host="1.1.1.1",
            client_ssh_host="2.2.2.2",
            server_test_ip="192.168.1.1",
            port=30002,
            client_test_ip="192.168.1.2",
        )
        data = json.loads(result)
        assert data["all_reachable"] is True
        assert len(data["tests"]) == 2


@pytest.mark.asyncio
async def test_port_connectivity_failure():
    """test_port_connectivity reports unreachable when nc fails."""
    import agents.infra.server as infra

    async def _mock_run(host, cmd, timeout=300):
        if "nc -l" in cmd:
            return _make_ssh_result(stdout="12345\n")
        if "nc -z" in cmd:
            return _make_ssh_result(exit_code=1, stderr="Connection refused")
        if "kill" in cmd:
            return _make_ssh_result(exit_code=0)
        return _make_ssh_result()

    mock_ssh = AsyncMock()
    mock_ssh.run = _mock_run

    with (
        patch.object(infra, "_ssh", mock_ssh),
    ):
        result = await infra.test_port_connectivity(
            server_ssh_host="1.1.1.1",
            client_ssh_host="2.2.2.2",
            server_test_ip="192.168.1.1",
            port=30002,
        )
        data = json.loads(result)
        assert data["all_reachable"] is False
        assert data["tests"][0]["reachable"] is False


@pytest.mark.asyncio
async def test_port_connectivity_listener_fails():
    """test_port_connectivity handles listener start failure."""
    import agents.infra.server as infra

    async def _mock_run(host, cmd, timeout=300):
        if "nc -l" in cmd:
            return _make_ssh_result(stdout="not_a_pid\n")
        return _make_ssh_result()

    mock_ssh = AsyncMock()
    mock_ssh.run = _mock_run

    with (
        patch.object(infra, "_ssh", mock_ssh),
    ):
        result = await infra.test_port_connectivity(
            server_ssh_host="1.1.1.1",
            client_ssh_host="2.2.2.2",
            server_test_ip="192.168.1.1",
            port=30002,
        )
        data = json.loads(result)
        assert data["all_reachable"] is False
        assert "Failed to start" in data["tests"][0]["error"]
