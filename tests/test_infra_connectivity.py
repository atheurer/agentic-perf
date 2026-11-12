"""Tests for TCP compatibility and receiver-verified UDP port preflight."""

from __future__ import annotations

import asyncio
import json
import shlex

import pytest

import agents.infra.server as srv
from tests.conftest import MockSSHExecutor, SSHResult

_PID_REPLY = "__PID:12345"


@pytest.fixture(autouse=True)
def patch_ssh(monkeypatch):
    """Wire a MockSSHExecutor into the infra server module."""
    mock = MockSSHExecutor(
        results={
            "nc -l": SSHResult(stdout=_PID_REPLY),
            "nc -z": SSHResult(exit_code=0),
            "kill": SSHResult(exit_code=0),
        }
    )
    monkeypatch.setattr(srv, "_ssh", mock)
    return mock


class TestPortConnectivitySinglePort:
    """Backward-compatible single-port calls."""

    @pytest.mark.asyncio
    async def test_single_port_param(self, patch_ssh):
        result = json.loads(
            await srv.test_port_connectivity(
                server_ssh_host="10.0.0.1",
                client_ssh_host="10.0.0.2",
                server_test_ip="192.168.1.1",
                port=30002,
            )
        )
        assert result["all_reachable"] is True
        assert len(result["tests"]) == 1
        assert result["tests"][0]["port"] == 30002
        assert result["tests"][0]["reachable"] is True

    @pytest.mark.asyncio
    async def test_existing_positional_arguments_remain_compatible(self, patch_ssh):
        result = json.loads(
            await srv.test_port_connectivity(
                "10.0.0.1",
                "10.0.0.2",
                "192.168.1.1",
                30002,
                "192.168.1.2",
                10,
            )
        )
        assert result["all_reachable"] is True
        assert len(result["tests"]) == 2

    @pytest.mark.asyncio
    async def test_single_port_in_ports_list_returns_multi_shape(self, patch_ssh):
        result = json.loads(
            await srv.test_port_connectivity(
                server_ssh_host="10.0.0.1",
                client_ssh_host="10.0.0.2",
                server_test_ip="192.168.1.1",
                ports=[30002],
            )
        )
        assert result["all_ports_ok"] is True
        assert result["failed_ports"] == []
        assert "30002" in result["results"]

    @pytest.mark.asyncio
    async def test_single_port_with_reverse(self, patch_ssh):
        result = json.loads(
            await srv.test_port_connectivity(
                server_ssh_host="10.0.0.1",
                client_ssh_host="10.0.0.2",
                server_test_ip="192.168.1.1",
                port=30002,
                client_test_ip="192.168.1.2",
            )
        )
        assert result["all_reachable"] is True
        assert len(result["tests"]) == 2


class TestPortConnectivityMultiPort:
    """Multi-port concurrent testing."""

    @pytest.mark.asyncio
    async def test_all_ports_ok(self, patch_ssh):
        result = json.loads(
            await srv.test_port_connectivity(
                server_ssh_host="10.0.0.1",
                client_ssh_host="10.0.0.2",
                server_test_ip="192.168.1.1",
                ports=[30002, 30003, 30004],
            )
        )
        assert result["all_ports_ok"] is True
        assert result["failed_ports"] == []
        assert "30002" in result["results"]
        assert "30003" in result["results"]
        assert "30004" in result["results"]

    @pytest.mark.asyncio
    async def test_one_port_fails(self, patch_ssh):
        original_run = patch_ssh.run

        async def fail_on_port(host, cmd, **kwargs):
            if "nc -z" in cmd and "30003" in cmd:
                return SSHResult(
                    exit_code=1,
                    stderr="Connection refused",
                )
            return await original_run(host, cmd, **kwargs)

        patch_ssh.run = fail_on_port

        result = json.loads(
            await srv.test_port_connectivity(
                server_ssh_host="10.0.0.1",
                client_ssh_host="10.0.0.2",
                server_test_ip="192.168.1.1",
                ports=[30002, 30003, 30004],
            )
        )
        assert result["all_ports_ok"] is False
        assert 30003 in result["failed_ports"]
        assert 30002 not in result["failed_ports"]
        assert 30004 not in result["failed_ports"]


class TestPortConnectivityErrors:
    """Argument validation."""

    @pytest.mark.asyncio
    async def test_neither_port_nor_ports(self, patch_ssh):
        result = json.loads(
            await srv.test_port_connectivity(
                server_ssh_host="10.0.0.1",
                client_ssh_host="10.0.0.2",
                server_test_ip="192.168.1.1",
            )
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_both_port_and_ports(self, patch_ssh):
        result = json.loads(
            await srv.test_port_connectivity(
                server_ssh_host="10.0.0.1",
                client_ssh_host="10.0.0.2",
                server_test_ip="192.168.1.1",
                port=30002,
                ports=[30003],
            )
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_empty_ports_list(self, patch_ssh):
        result = json.loads(
            await srv.test_port_connectivity(
                server_ssh_host="10.0.0.1",
                client_ssh_host="10.0.0.2",
                server_test_ip="192.168.1.1",
                ports=[],
            )
        )
        assert "error" in result

    @pytest.mark.asyncio
    async def test_duplicate_ports_deduplicated(self, patch_ssh):
        result = json.loads(
            await srv.test_port_connectivity(
                server_ssh_host="10.0.0.1",
                client_ssh_host="10.0.0.2",
                server_test_ip="192.168.1.1",
                ports=[30002, 30002, 30003],
            )
        )
        assert result["all_ports_ok"] is True
        assert len(result["results"]) == 2

    @pytest.mark.asyncio
    async def test_cancellation_still_kills_listener(self, patch_ssh):
        started = asyncio.Event()

        async def block_probe(host, command, **kwargs):
            if "nc -z" in command:
                started.set()
                await asyncio.Event().wait()
            return await MockSSHExecutor.run(patch_ssh, host, command, **kwargs)

        patch_ssh.run = block_probe
        task = asyncio.create_task(
            srv.test_port_connectivity(
                server_ssh_host="10.0.0.1",
                client_ssh_host="10.0.0.2",
                server_test_ip="192.168.1.1",
                port=30002,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert any(call["command"].startswith("kill 12345") for call in patch_ssh.calls)


_UDP_DIRECTORY = "/tmp/agentic-perf-udp.ABCDE12345"
_UDP_NONCE = "a1" * 16
_UDP_LAUNCH_REPLY = f"__UDP_DIR:{_UDP_DIRECTORY}\n{_PID_REPLY}\n"


@pytest.fixture
def udp_ssh(monkeypatch):
    """Simulate each independently observable UDP probe phase."""
    mock = MockSSHExecutor(
        results={
            "mktemp -d": SSHResult(stdout=_UDP_LAUNCH_REPLY),
            "'Listening on '": SSHResult(),
            "--send-only": SSHResult(),
            "grep -Fxq": SSHResult(),
            "kill": SSHResult(),
        }
    )
    monkeypatch.setattr(srv, "_ssh", mock)
    monkeypatch.setattr(srv.secrets, "token_hex", lambda _: _UDP_NONCE)
    return mock


async def _udp_probe(**kwargs) -> dict:
    arguments = {
        "server_ssh_host": "10.0.0.1",
        "client_ssh_host": "10.0.0.2",
        "server_test_ip": "192.168.1.1",
        "port": 5201,
        "protocol": "udp",
    }
    arguments.update(kwargs)
    return json.loads(await srv.test_port_connectivity(**arguments))


class TestPortConnectivityUDP:
    async def test_launcher_self_cleans_after_listener_exit(
        self, udp_ssh: MockSSHExecutor
    ) -> None:
        await _udp_probe()
        command = udp_ssh.calls[0]["command"]
        assert "LC_ALL=C nohup sh -c " in command
        launcher = shlex.split(command)
        supervisor = launcher[launcher.index("-c") + 1]
        assert supervisor.startswith("trap 'rm -rf -- \"$1\"' EXIT; ")
        assert "timeout 50s ncat -u -l -v 192.168.1.1 5201 & " in supervisor
        assert (
            "listener_pid=$!; trap 'kill \"$listener_pid\" 2>/dev/null' TERM INT; "
            in supervisor
        )
        assert supervisor.endswith('wait "$listener_pid"')
        assert launcher[launcher.index("-c") + 2 :][:2] == ["sh", "$probe_dir"]

    @pytest.mark.parametrize("transport_exception", [False, True])
    async def test_lost_launch_reply_remains_structured(
        self, udp_ssh: MockSSHExecutor, transport_exception: bool
    ) -> None:
        original = udp_ssh.run

        async def lose_launch_reply(
            host: str, command: str, *, timeout: int
        ) -> SSHResult:
            # Record the launch as sent before losing its identifiers.
            await original(host, command, timeout=timeout)
            if transport_exception:
                raise OSError("launch reply lost")
            return SSHResult(exit_code=255, stderr="launch reply lost")

        udp_ssh.run = lose_launch_reply
        result = await _udp_probe()
        assert result["all_reachable"] is False
        assert result["tests"][0]["protocol"] == "udp"
        assert result["tests"][0]["reason"] == "listener_start_failed"
        assert result["tests"][0]["error"] == "launch reply lost"
        # Without identifiers, cleanup relies on the detached supervisor.
        assert len(udp_ssh.calls) == 1

    async def test_received_payload_and_protocol_commands(self, udp_ssh):
        result = await _udp_probe()
        assert result["all_reachable"] is True
        assert result["tests"][0]["protocol"] == "udp"
        assert result["tests"][0]["reason"] == ""
        commands = [call["command"] for call in udp_ssh.calls]
        assert "ncat -u -l" in commands[0]
        assert "LC_ALL=C" in commands[0]
        assert '"$probe_dir/payload" 2> "$probe_dir/error"' in commands[0]
        assert "umask 077" in commands[0]
        assert "mktemp -d" in commands[0]
        assert "ncat -u --send-only -w 10 192.168.1.1 5201" in commands[2]
        assert _UDP_NONCE in commands[2]
        assert f"grep -Fxq -- {_UDP_NONCE} {_UDP_DIRECTORY}/payload" in commands[3]
        assert commands[4] == f"kill 12345 2>/dev/null; rm -rf -- {_UDP_DIRECTORY}"
        assert [call["host"] for call in udp_ssh.calls] == [
            "10.0.0.1",
            "10.0.0.1",
            "10.0.0.2",
            "10.0.0.1",
            "10.0.0.1",
        ]

    @pytest.mark.parametrize(
        ("phase", "response", "reason"),
        [
            ("mktemp -d", SSHResult(exit_code=1), "listener_start_failed"),
            (
                "'Listening on '",
                SSHResult(exit_code=1, stderr="Address already in use"),
                "listener_start_failed",
            ),
            ("grep -Fxq", SSHResult(exit_code=1), "no_payload_received"),
            (
                "grep -Fxq",
                SSHResult(exit_code=255, stderr="SSH failed"),
                "verification_error",
            ),
            (
                "grep -Fxq",
                SSHResult(exit_code=-1, stderr="Command timed out"),
                "verification_error",
            ),
        ],
    )
    async def test_failure_reasons(self, udp_ssh, phase, response, reason):
        udp_ssh._results[phase] = response
        result = await _udp_probe()
        assert result["all_reachable"] is False
        assert result["tests"][0]["reason"] == reason
        assert result["tests"][0]["error"]
        if phase != "mktemp -d":
            assert udp_ssh.calls[-1]["command"].startswith("kill 12345")
        if reason == "listener_start_failed":
            assert not any("--send-only" in c["command"] for c in udp_ssh.calls)

    @pytest.mark.parametrize("received", [True, False])
    async def test_sender_error_cannot_override_receiver_evidence(
        self, udp_ssh, received
    ):
        udp_ssh._results["--send-only"] = SSHResult(exit_code=1, stderr="send failed")
        udp_ssh._results["grep -Fxq"] = SSHResult(exit_code=0 if received else 1)
        result = await _udp_probe()
        assert result["all_reachable"] is received
        assert result["tests"][0]["reason"] == ("" if received else "sender_error")
        assert result["tests"][0]["error"] == ("" if received else "send failed")

    @pytest.mark.parametrize(
        ("phase", "reason"),
        [
            ("mktemp -d", "listener_start_failed"),
            ("'Listening on '", "listener_start_failed"),
            ("--send-only", "sender_error"),
            ("grep -Fxq", "verification_error"),
        ],
    )
    async def test_transport_exceptions_are_structured(self, udp_ssh, phase, reason):
        original = udp_ssh.run
        udp_ssh._results["grep -Fxq"] = SSHResult(exit_code=1)

        async def fail_transport(host, command, **kwargs):
            if phase in command:
                raise OSError("transport unavailable")
            return await original(host, command, **kwargs)

        udp_ssh.run = fail_transport
        result = await _udp_probe()
        assert result["tests"][0]["reason"] == reason
        assert result["tests"][0]["error"] == "transport unavailable"

    async def test_reverse_direction(self, udp_ssh):
        result = await _udp_probe(client_test_ip="192.168.1.2")
        assert result["all_reachable"] is True
        assert len(result["tests"]) == 2
        launches = [c for c in udp_ssh.calls if "mktemp -d" in c["command"]]
        senders = [c for c in udp_ssh.calls if "--send-only" in c["command"]]
        assert [c["host"] for c in launches] == ["10.0.0.1", "10.0.0.2"]
        assert [c["host"] for c in senders] == ["10.0.0.2", "10.0.0.1"]
        assert "192.168.1.2 5201" in senders[1]["command"]

    async def test_multiport_runs_concurrently_and_reports_failed_ports(self, udp_ssh):
        original = udp_ssh.run
        both_sending = asyncio.Event()
        sender_count = 0

        async def concurrent_probe(host, command, **kwargs):
            nonlocal sender_count
            if "--send-only" in command:
                sender_count += 1
                if sender_count == 2:
                    both_sending.set()
                await asyncio.wait_for(both_sending.wait(), timeout=1)
                if command.endswith(" 5202"):
                    raise OSError("sender unavailable")
            if "grep -Fxq" in command:
                return SSHResult(exit_code=1)
            return await original(host, command, **kwargs)

        udp_ssh.run = concurrent_probe
        result = await _udp_probe(port=None, ports=[5201, 5202, 5201])
        assert result["all_ports_ok"] is False
        assert result["failed_ports"] == [5201, 5202]
        assert result["results"]["5201"][0]["reason"] == "no_payload_received"
        assert result["results"]["5202"][0]["reason"] == "sender_error"
        assert sender_count == 2

    @pytest.mark.parametrize(
        "phase", ["mktemp -d", "'Listening on '", "--send-only", "grep -Fxq"]
    )
    async def test_cancellation_cleans_up_each_phase(self, udp_ssh, phase):
        entered = asyncio.Event()
        release = asyncio.Event()
        original = udp_ssh.run

        async def block_phase(host, command, **kwargs):
            if phase in command:
                entered.set()
                await release.wait()
            return await original(host, command, **kwargs)

        udp_ssh.run = block_phase
        task = asyncio.create_task(_udp_probe())
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        assert udp_ssh.calls[-1]["command"] == (
            f"kill 12345 2>/dev/null; rm -rf -- {_UDP_DIRECTORY}"
        )

    async def test_repeated_cancellation_waits_for_cleanup(self, udp_ssh):
        cleaning = asyncio.Event()
        release = asyncio.Event()
        original = udp_ssh.run

        async def block_cleanup(host, command, **kwargs):
            if command.startswith("kill 12345"):
                cleaning.set()
                await release.wait()
            return await original(host, command, **kwargs)

        udp_ssh.run = block_cleanup
        task = asyncio.create_task(_udp_probe())
        await asyncio.wait_for(cleaning.wait(), timeout=1)
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        assert udp_ssh.calls[-1]["command"].startswith("kill 12345")

    async def test_cleanup_transport_failure_preserves_verdict(self, udp_ssh, caplog):
        original = udp_ssh.run

        async def fail_cleanup(host, command, **kwargs):
            if command.startswith("kill 12345"):
                raise OSError("cleanup SSH failed")
            return await original(host, command, **kwargs)

        udp_ssh.run = fail_cleanup
        result = await _udp_probe()
        assert result["all_reachable"] is True
        assert "cleanup SSH failed" in caplog.text

    @pytest.mark.parametrize(
        "arguments",
        [
            {"protocol": "sctp"},
            {"protocol": "UDP"},
            {"server_test_ip": "-e/bin/sh"},
            {"client_test_ip": "192.0.2.2; exit 0"},
            {"server_test_ip": "host.example.com"},
            {"port": 0},
            {"port": 65536},
            {"timeout": 0},
        ],
    )
    async def test_invalid_arguments_do_not_reach_ssh(self, udp_ssh, arguments):
        assert "error" in await _udp_probe(**arguments)
        assert udp_ssh.calls == []

    @pytest.mark.parametrize("protocol", ["tcp", "udp"])
    async def test_port_cap_retains_existing_error(self, udp_ssh, protocol):
        result = await _udp_probe(
            port=None, ports=list(range(1000, 1065)), protocol=protocol
        )
        assert result == {"error": "'ports' exceeds maximum of 64 entries."}
        assert udp_ssh.calls == []

    async def test_explicit_tcp_matches_default(self, patch_ssh):
        arguments = ("10.0.0.1", "10.0.0.2", "192.168.1.1", 5201)
        default = await srv.test_port_connectivity(*arguments)
        explicit = await srv.test_port_connectivity(*arguments, protocol="tcp")
        assert default == explicit
        assert "protocol" not in json.loads(default)["tests"][0]
