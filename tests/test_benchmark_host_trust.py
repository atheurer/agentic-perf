"""Controller host-key policy and targeted replacement behavior."""

from __future__ import annotations

import asyncio
import base64
import copy
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


async def allow_replacement() -> None:
    return None


class FakeController:
    """Run only the helper's known-host operations in an isolated directory."""

    def __init__(self, root: Path, candidates: list[str] | None = None) -> None:
        self.controller = "controller.example"
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
        assert host == self.controller
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
        ssh,
        "controller.example",
        "endpoint.example",
        candidate,
        authorize=allow_replacement,
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
        ssh,
        "controller.example",
        "endpoint.example",
        parse_host_keys(scanned(2))[0],
        authorize=allow_replacement,
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
async def test_recorded_ssh_aliases_are_accepted_for_assigned_roles(
    tmp_path, make_agent
):
    ssh = FakeController(tmp_path)
    ssh.controller = "controller-ssh.example"
    ssh.known_hosts.write_text(f"endpoint-ssh.example ssh-ed25519 {host_key(1)}\n")
    current = ticket("replace")
    current["custom_fields"]["ssh_hardware_ips"] = {
        "controller": "controller-ssh.example",
        "targets": ["endpoint-ssh.example", "unassigned.example"],
    }
    agent = make_agent(ssh, current)
    rejected = json.loads(
        await agent._replace_changed_host_key(
            "controller-ssh.example", "unassigned.example"
        )
    )
    assert rejected["status"] == "rejected"
    assert ssh.mutations == 0
    result = json.loads(
        await agent._replace_changed_host_key(
            "controller-ssh.example", "endpoint-ssh.example"
        )
    )
    assert result["status"] == "replaced"


@pytest.mark.asyncio
async def test_unassigned_ssh_roles_cannot_authorize_host_replacement(
    tmp_path, make_agent
):
    ssh = FakeController(tmp_path)
    current = ticket("replace")
    current["custom_fields"]["assigned_hardware_ips"]["targets"] = []
    current["custom_fields"]["ssh_hardware_ips"] = {
        "controller": "controller.example",
        "targets": ["endpoint.example"],
    }
    agent = make_agent(ssh, current)
    result = json.loads(
        await agent._replace_changed_host_key("controller.example", "endpoint.example")
    )
    assert result["status"] == "rejected"
    assert ssh.mutations == 0


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


def rsa_host_key(byte: int) -> str:
    fields = (b"ssh-rsa", b"\x01\x00\x01", bytes([byte]) * 16)
    blob = b"".join(struct.pack(">I", len(field)) + field for field in fields)
    return base64.b64encode(blob).decode()


@pytest.mark.asyncio
async def test_matching_preferred_key_does_not_hide_changed_other_algorithm(tmp_path):
    rsa_old, rsa_new = rsa_host_key(1), rsa_host_key(2)
    ssh = FakeController(
        tmp_path, [scanned(1) + f"endpoint.example ssh-rsa {rsa_new}\n"]
    )
    ssh.known_hosts.write_text(
        f"endpoint.example ssh-ed25519 {host_key(1)}\n"
        f"endpoint.example ssh-rsa {rsa_old}\n"
    )
    result = await inspect_controller_host_key(
        ssh, "controller.example", "endpoint.example"
    )
    assert result["status"] == "changed"
    assert result["algorithm"] == "ssh-rsa"
    assert result["key"] == rsa_new


@pytest.mark.asyncio
async def test_ambiguous_changed_algorithm_is_not_accepted(tmp_path):
    keys = f"endpoint.example ssh-rsa {rsa_host_key(2)}\nendpoint.example ssh-rsa {rsa_host_key(3)}\n"
    ssh = FakeController(tmp_path, [scanned(1) + keys])
    ssh.known_hosts.write_text(
        f"endpoint.example ssh-ed25519 {host_key(1)}\n"
        f"endpoint.example ssh-rsa {rsa_host_key(1)}\n"
    )
    result = await inspect_controller_host_key(
        ssh, "controller.example", "endpoint.example"
    )
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_partial_scan_cannot_claim_all_stored_algorithms_are_trusted(tmp_path):
    ssh = FakeController(tmp_path, [scanned(1)])
    ssh.known_hosts.write_text(
        f"endpoint.example ssh-ed25519 {host_key(1)}\n"
        f"endpoint.example ssh-rsa {rsa_host_key(1)}\n"
    )
    result = await inspect_controller_host_key(
        ssh, "controller.example", "endpoint.example"
    )
    assert result["status"] == "error"
    assert "omitted previously recorded algorithms" in result["message"]


@pytest.mark.asyncio
async def test_concurrent_change_before_rename_is_preserved_and_rejected(tmp_path):
    external = f"new-host.example ssh-ed25519 {host_key(4)}"

    class ConcurrentWriter(FakeController):
        async def run(self, host: str, command: str, **kwargs: object) -> SSHResult:
            if kwargs.get("mutating"):
                command = command.replace(
                    "ssh-keygen -R ",
                    f"printf '%s\\n' {shlex.quote(external)} >> /root/.ssh/known_hosts; ssh-keygen -R ",
                    1,
                )
            return await super().run(host, command, **kwargs)

    ssh = ConcurrentWriter(tmp_path, [scanned(2)])
    approved = parse_host_keys(scanned(2))[0]
    result = await replace_controller_host_key(
        ssh,
        "controller.example",
        "endpoint.example",
        approved,
        authorize=allow_replacement,
    )
    assert result["status"] == "rejected"
    assert "changed during replacement" in result["message"]
    content = ssh.known_hosts.read_text()
    assert external in content
    assert host_key(1) in content
    assert host_key(2) not in content


@pytest.mark.asyncio
async def test_busy_host_key_lock_returns_without_waiting(tmp_path):
    import fcntl

    ssh = FakeController(tmp_path, [scanned(2)])
    before = ssh.known_hosts.read_text()
    with (ssh.ssh_dir / ".agentic-perf-hostkeys.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = await asyncio.wait_for(
            replace_controller_host_key(
                ssh,
                "controller.example",
                "endpoint.example",
                parse_host_keys(scanned(2))[0],
                authorize=allow_replacement,
            ),
            timeout=3,
        )
    assert result["status"] == "busy"
    assert "retry" in result["message"]
    assert ssh.known_hosts.read_text() == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "record",
    [
        "endpoint.example,other.example",
        "*.example",
        "!blocked.example,*.example",
        "@cert-authority endpoint.example",
        "@revoked endpoint.example",
    ],
)
async def test_shared_or_marked_records_require_operator_assistance(tmp_path, record):
    ssh = FakeController(tmp_path)
    original = f"{record} ssh-ed25519 {host_key(1)}\n"
    ssh.known_hosts.write_text(original)
    result = await inspect_controller_host_key(
        ssh, "controller.example", "endpoint.example"
    )
    assert result["status"] == "rejected"
    assert "operator assistance" in result["message"]
    assert ssh.known_hosts.read_text() == original
    assert ssh.mutations == 0


@pytest.mark.asyncio
async def test_shared_record_added_after_inspection_is_not_removed(tmp_path):
    ssh = FakeController(tmp_path, [scanned(2)])
    original = f"endpoint.example,other.example ssh-ed25519 {host_key(1)}\n"
    ssh.known_hosts.write_text(original)
    result = await replace_controller_host_key(
        ssh,
        "controller.example",
        "endpoint.example",
        parse_host_keys(scanned(2))[0],
        authorize=allow_replacement,
    )
    assert result["status"] == "rejected"
    assert "operator assistance" in result["message"]
    assert ssh.known_hosts.read_text() == original


@pytest.mark.asyncio
async def test_single_host_hashed_records_remain_supported(tmp_path):
    ssh = FakeController(tmp_path, [scanned(2), scanned(2)])
    process = await asyncio.create_subprocess_exec(
        "ssh-keygen",
        "-H",
        "-f",
        str(ssh.known_hosts),
        "-N",
        "",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await process.communicate()
    assert process.returncode == 0
    inspection = await inspect_controller_host_key(
        ssh, "controller.example", "endpoint.example"
    )
    assert inspection["status"] == "changed"
    result = await replace_controller_host_key(
        ssh,
        "controller.example",
        "endpoint.example",
        parse_host_keys(scanned(2))[0],
        authorize=allow_replacement,
    )
    assert result["status"] == "replaced"
    process = await asyncio.create_subprocess_exec(
        "ssh-keygen",
        "-F",
        "other.example",
        "-f",
        str(ssh.known_hosts),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await process.communicate()
    assert process.returncode == 0


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
@pytest.mark.parametrize("file_exists", [True, False])
async def test_record_disappearing_before_replacement_is_rejected(
    tmp_path, file_exists
):
    ssh = FakeController(tmp_path, [scanned(2)])
    original = f"other.example ssh-ed25519 {host_key(3)}\n"
    if file_exists:
        ssh.known_hosts.write_text(original)
    else:
        ssh.known_hosts.unlink()
    result = await replace_controller_host_key(
        ssh,
        "controller.example",
        "endpoint.example",
        parse_host_keys(scanned(2))[0],
        authorize=allow_replacement,
    )
    assert result["status"] == "rejected"
    if file_exists:
        assert ssh.known_hosts.read_text() == original
    else:
        assert not ssh.known_hosts.exists()


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
        ssh,
        "controller.example",
        "endpoint.example",
        parse_host_keys(scanned(2))[0],
        authorize=allow_replacement,
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


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_role", ["controller", "target"])
async def test_reassigned_host_cannot_keep_a_stale_ssh_alias_authorized(
    tmp_path, make_agent, monkeypatch, changed_role
):
    import agents.server_utils as utils

    ssh = FakeController(tmp_path)
    initial = ticket()
    initial["custom_fields"]["assigned_hardware_ips"] = {
        "controller": "10.0.0.1",
        "targets": ["10.0.0.2"],
    }
    initial["custom_fields"]["ssh_hardware_ips"] = {
        "controller": "controller.example",
        "targets": ["endpoint.example"],
    }
    changed = copy.deepcopy(initial)
    if changed_role == "controller":
        changed["custom_fields"]["assigned_hardware_ips"]["controller"] = "10.0.0.3"
    else:
        changed["custom_fields"]["assigned_hardware_ips"]["targets"] = ["10.0.0.4"]
    agent = make_agent(ssh, initial)
    monkeypatch.setattr(
        utils, "assert_ticket_active", AsyncMock(side_effect=[initial, changed])
    )
    result = json.loads(
        await agent._replace_changed_host_key("controller.example", "endpoint.example")
    )
    assert result["status"] == "rejected"
    assert "assignment changed" in result["message"]
    assert ssh.mutations == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["controller", "target", "policy", "status"])
async def test_authorization_revoked_during_final_scan_prevents_write(
    tmp_path, make_agent, monkeypatch, change
):
    import agents.server_utils as utils

    state = ticket("replace")

    class RevokingController(FakeController):
        scans = 0

        async def run(self, host: str, command: str, **kwargs: object) -> SSHResult:
            if command.startswith("ssh-keyscan"):
                self.scans += 1
                if self.scans == 2:
                    if change == "controller":
                        state["custom_fields"]["assigned_hardware_ips"][
                            "controller"
                        ] = "other.example"
                    elif change == "target":
                        state["custom_fields"]["assigned_hardware_ips"]["targets"] = [
                            "other.example"
                        ]
                    elif change == "policy":
                        state["custom_fields"]["directives"]["ssh_host_key_policy"] = (
                            "ask"
                        )
                    else:
                        state["status"] = "cancelled"
            return await super().run(host, command, **kwargs)

    async def active(*_args):
        if state["status"] != "executing_benchmark":
            return {"status": "rejected", "message": "Ticket no longer active."}
        return copy.deepcopy(state)

    ssh = RevokingController(tmp_path)
    agent = make_agent(ssh, state)
    monkeypatch.setattr(utils, "assert_ticket_active", active)
    result = json.loads(
        await agent._replace_changed_host_key("controller.example", "endpoint.example")
    )
    assert result["status"] == "rejected"
    assert ssh.scans == 2
    assert ssh.mutations == 0
    assert host_key(1) in ssh.known_hosts.read_text()
