"""Controller host-key policy and targeted replacement behavior."""

from __future__ import annotations

import asyncio
import base64
import json
import shlex
import struct
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agents.benchmark.agent import BenchmarkAgent
from providers.directives import normalize_directives
from providers.host_trust import (
    inspect_controller_host_key,
    parse_host_keys,
    preferred_host_key,
    replace_controller_host_key,
    validate_host,
)
from providers.ssh import SSHResult


def host_key(byte: int) -> str:
    name = b"ssh-ed25519"
    blob = (
        struct.pack(">I", len(name)) + name + struct.pack(">I", 32) + bytes([byte]) * 32
    )
    return base64.b64encode(blob).decode()


def scanned(byte: int) -> str:
    return f"endpoint.example ssh-ed25519 {host_key(byte)}\n"


class FakeController:
    """Run only the helper's known-host operations in an isolated directory."""

    def __init__(self, root: Path, candidates: list[str] | None = None) -> None:
        self.ssh_dir = root / "ssh"
        self.ssh_dir.mkdir()
        self.known_hosts = self.ssh_dir / "known_hosts"
        self.known_hosts.write_text(
            f"endpoint.example ssh-ed25519 {host_key(1)}\n"
            f"other.example ssh-ed25519 {host_key(3)}\n"
        )
        self.candidates = candidates or [scanned(2), scanned(2)]
        self.mutations = 0

    async def run(self, host: str, command: str, **kwargs: object) -> SSHResult:
        assert host == "controller.example"
        if command.startswith("ssh-keyscan"):
            assert kwargs.get("mutating") is False
            return SSHResult(stdout=self.candidates.pop(0), stderr="", exit_code=0)
        if kwargs.get("mutating"):
            self.mutations += 1
        command = command.replace("/root/.ssh", shlex.quote(str(self.ssh_dir)))
        proc = await asyncio.create_subprocess_shell(
            command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await proc.communicate()
        return SSHResult(
            stdout=out.decode(), stderr=err.decode(), exit_code=proc.returncode
        )


def ticket(policy: str | None = None) -> dict:
    directives = {"ssh_host_key_policy": policy} if policy else {}
    return {
        "status": "executing_benchmark",
        "custom_fields": {
            "assigned_hardware_ips": {
                "controller": "controller.example",
                "targets": ["endpoint.example"],
            },
            "directives": directives,
        },
    }


@pytest.fixture
def make_agent(monkeypatch):
    import agents.server_utils as utils

    agent = BenchmarkAgent.__new__(BenchmarkAgent)
    agent._ticket_id = "PERF-123"
    agent.store_url = "http://localhost:8090"
    agent._add_comment = AsyncMock()
    agent._request_human_input = AsyncMock(return_value="yes")

    def install(ssh: FakeController, current: dict | None = None):
        async def active(*args):
            return current or ticket()

        async def build(*args):
            return ssh, current or ticket()

        monkeypatch.setattr(utils, "assert_ticket_active", active)
        monkeypatch.setattr(utils, "build_ssh_from_ticket", build)
        return agent

    return install


@pytest.mark.parametrize("invalid", ["-bad", "host;touch", "host name", "x\nother", ""])
def test_rejects_non_host_arguments(invalid):
    with pytest.raises(ValueError):
        validate_host(invalid)


def test_key_parser_checks_blob_and_rejects_ambiguous_scans():
    parsed = parse_host_keys(scanned(2))
    assert len(parsed) == 1
    assert parsed[0].fingerprint.startswith("SHA256:")
    assert not parse_host_keys("host ssh-ed25519 YWJj\n")
    assert preferred_host_key(scanned(1) + scanned(2)) is None


def test_directive_is_recognized():
    normalized, _, unknown = normalize_directives({"ssh_host_key_policy": "replace"})
    assert normalized["ssh_host_key_policy"] == "replace"
    assert not unknown


def test_policy_is_available_in_triage_and_benchmark_tool_contracts():
    from agents.benchmark.agent import _LOCAL_TOOLS as benchmark_tools
    from agents.triage.agent import _LOCAL_TOOLS as triage_tools

    triage = next(tool for tool in triage_tools if tool.name == "submit_triage_result")
    directive = triage.input_schema["properties"]["directives"]["properties"][
        "ssh_host_key_policy"
    ]
    assert directive["enum"] == ["ask", "replace"]
    assert any(tool.name == "replace_controller_host_key" for tool in benchmark_tools)


@pytest.mark.asyncio
async def test_inspection_finds_changed_key(tmp_path):
    ssh = FakeController(tmp_path)
    result = await inspect_controller_host_key(
        ssh, "controller.example", "endpoint.example"
    )
    assert result["status"] == "changed"
    assert result["fingerprint"] == parse_host_keys(scanned(2))[0].fingerprint
    assert ssh.mutations == 0


@pytest.mark.asyncio
async def test_replacement_preserves_other_hosts(tmp_path):
    ssh = FakeController(tmp_path)
    candidate = parse_host_keys(scanned(2))[0]
    result = await replace_controller_host_key(
        ssh, "controller.example", "endpoint.example", candidate
    )
    assert result["status"] == "replaced"
    contents = ssh.known_hosts.read_text()
    assert host_key(1) not in contents
    assert host_key(2) in contents
    assert f"other.example ssh-ed25519 {host_key(3)}" in contents


@pytest.mark.asyncio
async def test_key_changed_after_approval_is_not_installed(tmp_path):
    ssh = FakeController(tmp_path, [scanned(3)])
    result = await replace_controller_host_key(
        ssh, "controller.example", "endpoint.example", parse_host_keys(scanned(2))[0]
    )
    assert result["status"] == "rejected"
    assert ssh.mutations == 0
    assert host_key(1) in ssh.known_hosts.read_text()


@pytest.mark.asyncio
async def test_default_requires_user_confirmation(tmp_path, make_agent):
    ssh = FakeController(tmp_path)
    agent = make_agent(ssh)
    agent._request_human_input.return_value = "no"
    result = json.loads(
        await agent._replace_changed_host_key("controller.example", "endpoint.example")
    )
    assert result["status"] == "not_approved"
    assert ssh.mutations == 0
    assert (
        parse_host_keys(scanned(2))[0].fingerprint
        in agent._request_human_input.call_args.args[1]
    )


@pytest.mark.asyncio
async def test_affirmative_reply_replaces_target_only(tmp_path, make_agent):
    ssh = FakeController(tmp_path)
    agent = make_agent(ssh)
    result = json.loads(
        await agent._replace_changed_host_key("controller.example", "endpoint.example")
    )
    assert result["status"] == "replaced"
    assert ssh.mutations == 1
    assert host_key(3) in ssh.known_hosts.read_text()


@pytest.mark.asyncio
async def test_explicit_policy_replaces_without_prompt(tmp_path, make_agent):
    ssh = FakeController(tmp_path)
    agent = make_agent(ssh, ticket("replace"))
    result = json.loads(
        await agent._replace_changed_host_key("controller.example", "endpoint.example")
    )
    assert result["status"] == "replaced"
    agent._request_human_input.assert_not_awaited()


@pytest.mark.asyncio
async def test_unassigned_target_is_rejected_before_contact(tmp_path, make_agent):
    ssh = FakeController(tmp_path)
    agent = make_agent(ssh)
    result = json.loads(
        await agent._replace_changed_host_key("controller.example", "other.example")
    )
    assert result["status"] == "rejected"
    assert ssh.mutations == 0
    assert len(ssh.candidates) == 2


@pytest.mark.asyncio
async def test_remote_operations_keep_the_triggering_tool_trace(
    tmp_path, make_agent, monkeypatch
):
    import agents.benchmark.agent as agent_module

    ssh = FakeController(tmp_path)
    agent = make_agent(ssh, ticket("replace"))
    trace = object()
    monkeypatch.setattr(agent_module, "current_trace_context", lambda: trace)
    result = json.loads(
        await agent._replace_changed_host_key("controller.example", "endpoint.example")
    )
    assert result["status"] == "replaced"
    assert ssh.trace_context is trace


def test_key_parser_supports_rsa_only_hosts():
    fields = (b"ssh-rsa", b"\x01\x00\x01", b"\x01\x02\x03")
    blob = b"".join(struct.pack(">I", len(field)) + field for field in fields)
    key = base64.b64encode(blob).decode()
    chosen = preferred_host_key(f"endpoint.example ssh-rsa {key}\n")
    assert chosen is not None
    assert chosen.algorithm == "ssh-rsa"


@pytest.mark.asyncio
async def test_observed_fingerprint_selects_the_key_from_the_error(tmp_path):
    fields = (b"ssh-rsa", b"\x01\x00\x01", b"\x01\x02\x03")
    blob = b"".join(struct.pack(">I", len(field)) + field for field in fields)
    rsa_line = f"endpoint.example ssh-rsa {base64.b64encode(blob).decode()}\n"
    fingerprint = parse_host_keys(rsa_line)[0].fingerprint
    ssh = FakeController(tmp_path, [scanned(2) + rsa_line])
    result = await inspect_controller_host_key(
        ssh, "controller.example", "endpoint.example", fingerprint
    )
    assert result["status"] == "changed"
    assert result["algorithm"] == "ssh-rsa"
    assert result["fingerprint"] == fingerprint


@pytest.mark.asyncio
async def test_observed_fingerprint_must_match_the_current_scan(tmp_path):
    ssh = FakeController(tmp_path, [scanned(2)])
    fingerprint = parse_host_keys(scanned(3))[0].fingerprint
    result = await inspect_controller_host_key(
        ssh, "controller.example", "endpoint.example", fingerprint
    )
    assert result["status"] == "error"
    assert ssh.mutations == 0


@pytest.mark.asyncio
async def test_missing_record_uses_normal_first_contact(tmp_path):
    ssh = FakeController(tmp_path)
    ssh.known_hosts.unlink()
    result = await inspect_controller_host_key(
        ssh, "controller.example", "endpoint.example"
    )
    assert result["status"] == "not_found"
    assert len(ssh.candidates) == 2


@pytest.mark.asyncio
async def test_matching_key_needs_no_replacement(tmp_path):
    ssh = FakeController(tmp_path, [scanned(1)])
    result = await inspect_controller_host_key(
        ssh, "controller.example", "endpoint.example"
    )
    assert result["status"] == "already_trusted"
    assert ssh.mutations == 0


@pytest.mark.asyncio
async def test_symlinked_host_file_is_not_replaced(tmp_path):
    ssh = FakeController(tmp_path)
    original = tmp_path / "other-known-hosts"
    ssh.known_hosts.replace(original)
    ssh.known_hosts.symlink_to(original)
    result = await replace_controller_host_key(
        ssh, "controller.example", "endpoint.example", parse_host_keys(scanned(2))[0]
    )
    assert result["status"] == "error"
    assert ssh.known_hosts.is_symlink()
    assert host_key(1) in original.read_text()


@pytest.mark.asyncio
async def test_ambiguous_confirmation_does_not_authorize_replacement(
    tmp_path, make_agent
):
    ssh = FakeController(tmp_path)
    agent = make_agent(ssh)
    agent._request_human_input.return_value = "yes, but do not replace it"
    result = json.loads(
        await agent._replace_changed_host_key("controller.example", "endpoint.example")
    )
    assert result["status"] == "not_approved"
    assert ssh.mutations == 0


@pytest.mark.asyncio
async def test_generic_no_pause_directive_does_not_allow_replacement(
    tmp_path, make_agent
):
    ssh = FakeController(tmp_path)
    current = ticket()
    current["custom_fields"]["directives"]["user_pre_run_approval"] = False
    agent = make_agent(ssh, current)
    agent._request_human_input.return_value = "no"
    result = json.loads(
        await agent._replace_changed_host_key("controller.example", "endpoint.example")
    )
    assert result["status"] == "not_approved"
    agent._request_human_input.assert_awaited_once()
    assert ssh.mutations == 0


@pytest.mark.asyncio
async def test_policy_revocation_before_write_is_honored(
    tmp_path, make_agent, monkeypatch
):
    import agents.server_utils as utils

    ssh = FakeController(tmp_path)
    agent = make_agent(ssh, ticket("replace"))
    monkeypatch.setattr(
        utils,
        "assert_ticket_active",
        AsyncMock(side_effect=[ticket("replace"), ticket()]),
    )
    result = json.loads(
        await agent._replace_changed_host_key("controller.example", "endpoint.example")
    )
    assert result["status"] == "rejected"
    assert ssh.mutations == 0


@pytest.mark.asyncio
async def test_reassignment_after_user_approval_does_not_write(
    tmp_path, make_agent, monkeypatch
):
    import agents.server_utils as utils

    ssh = FakeController(tmp_path)
    agent = make_agent(ssh)
    reassigned = ticket()
    reassigned["custom_fields"]["assigned_hardware_ips"]["targets"] = ["other.example"]
    monkeypatch.setattr(
        utils, "assert_ticket_active", AsyncMock(side_effect=[ticket(), reassigned])
    )
    result = json.loads(
        await agent._replace_changed_host_key("controller.example", "endpoint.example")
    )
    assert result["status"] == "rejected"
    assert ssh.mutations == 0
