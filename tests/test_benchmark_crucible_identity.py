"""Tests for the Crucible identity execution preflight (#1358).

Crucible post-processing requires ``/root/.crucible/identity`` with a
non-empty ``CRUCIBLE_EMAIL``. Without a preflight, a run completes all its
samples and then discards post-processing with a schema error that never names
the identity. ``execute_benchmark`` now rejects before consuming the approval
when the identity is not ready; the probe is read-only and fails open.
"""

from __future__ import annotations

import pytest

import agents.benchmark.server as benchmark_server
from agents.benchmark.server import _crucible_identity_preflight
from providers.crucible_identity import crucible_identity_probe_command
from tests.conftest import MockSSHExecutor, SSHResult


@pytest.fixture(autouse=True)
def patch_benchmark_server(monkeypatch):
    """Wire a MockSSHExecutor into the benchmark server module for each test."""
    mock = MockSSHExecutor()
    monkeypatch.setattr(benchmark_server, "_ssh", mock)
    monkeypatch.setattr(benchmark_server, "_initialized", True)
    return mock


def test_probe_command_is_read_only_and_targets_the_identity_file():
    cmd = crucible_identity_probe_command()
    # Read-only: no redirect into the file, no mutating command.
    assert ">" not in cmd
    assert "identity" in cmd
    assert "CRUCIBLE_EMAIL" in cmd


@pytest.mark.asyncio
async def test_preflight_passes_when_identity_is_ready():
    ssh = MockSSHExecutor(
        results={
            "identity": SSHResult(exit_code=0, stdout="identity=1"),
        }
    )
    benchmark_server._ssh = ssh
    assert await _crucible_identity_preflight("10.0.0.1") is None


@pytest.mark.asyncio
async def test_preflight_rejects_when_identity_is_missing():
    ssh = MockSSHExecutor(
        results={
            "identity": SSHResult(exit_code=0, stdout="identity=0"),
        }
    )
    benchmark_server._ssh = ssh
    rejection = await _crucible_identity_preflight("10.0.0.1")
    assert rejection is not None
    assert rejection["status"] == "rejected"
    assert rejection["reason_code"] == "crucible_identity_missing"
    # The message must name the file and the remediation — the lab failure
    # surfaced as a bare schema pattern error with no hint of the cause.
    assert "/root/.crucible/identity" in rejection["message"]
    assert "CRUCIBLE_EMAIL" in rejection["message"]


@pytest.mark.asyncio
async def test_preflight_fails_open_on_ssh_error():
    class FailingSSH(MockSSHExecutor):
        async def run(self, host, command, timeout=300):
            raise ConnectionError("ssh unreachable")

    benchmark_server._ssh = FailingSSH()
    assert await _crucible_identity_preflight("10.0.0.1") is None


@pytest.mark.asyncio
async def test_preflight_fails_open_on_probe_failure_and_junk_output():
    benchmark_server._ssh = MockSSHExecutor(
        results={
            "identity": SSHResult(exit_code=1, stdout=""),
        }
    )
    assert await _crucible_identity_preflight("10.0.0.1") is None

    benchmark_server._ssh = MockSSHExecutor(
        results={
            "identity": SSHResult(exit_code=0, stdout="something-else"),
        }
    )
    assert await _crucible_identity_preflight("10.0.0.1") is None
