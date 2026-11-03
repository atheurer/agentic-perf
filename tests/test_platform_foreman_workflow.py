"""Foreman platform checkpoints, reconciliation, and authorization."""

from __future__ import annotations

import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import agents.platform.foreman_workflow as workflow
from providers.foreman import ForemanError
from providers.quads import QuadsClient


@pytest.fixture
def setup_workflow(monkeypatch):
    snapshots = []
    host = {
        "id": 12,
        "name": "node.example",
        "build": False,
        "architecture_id": 1,
        "operatingsystem_id": 2,
        "installed_at": "2026-01-01 00:00:00 UTC",
    }

    class Client:
        set_calls = 0
        uncertain = False

        async def find_host(self, name):
            assert name == host["name"]
            return dict(host)

        async def resolve_install_options(self, host_data, name, **kwargs):
            assert name == "RHEL 10.0"
            return {"operatingsystem_id": 17, "medium_id": 15, "ptable_id": 169}

        async def get_operating_system(self, os_id):
            return {"name": "RedHat", "major": "10", "minor": "0"}

        async def set_build(self, host_id, options):
            assert snapshots[-1]["hosts"]["node.example"]["phase"] == "build_unknown"
            self.set_calls += 1
            host.update(options)
            host["build"] = True
            if self.uncertain:
                self.uncertain = False
                raise ForemanError("build_unknown", "response lost")

        async def close(self):
            pass

    client = Client()
    reboots = []

    class SSH:
        key_path = "/unused"

        async def run(self, hostname, command, **kwargs):
            if command == "cat /proc/sys/kernel/random/boot_id":
                return SimpleNamespace(exit_code=0, stdout="old-boot\n")
            if command == "systemctl reboot":
                assert (
                    snapshots[-1]["hosts"]["node.example"]["phase"] == "reboot_unknown"
                )
                reboots.append(hostname)
                host["build"] = False
                host["installed_at"] = "2026-02-01 00:00:00 UTC"
                return SimpleNamespace(exit_code=0, stdout="")
            return SimpleNamespace(
                exit_code=0, stdout='new-boot\nID="rhel"\nVERSION_ID="10.0"\n'
            )

    quads = MagicMock()
    quads.uses_https = True
    quads.close = AsyncMock()
    resolver = MagicMock()
    resolver.has_runtime_config.return_value = False
    monkeypatch.setattr(workflow, "build_secrets_provider", lambda: object())
    monkeypatch.setattr(
        workflow,
        "build_organization_skill_resolver_async",
        AsyncMock(return_value=resolver),
    )
    monkeypatch.setattr(
        workflow,
        "resolve_foreman_config",
        AsyncMock(
            return_value=(
                {
                    "endpoint": "https://foreman.example",
                    "bootstrap": {"mode": "existing"},
                },
                {"site": "lab"},
            )
        ),
    )
    monkeypatch.setattr(
        workflow, "foreman_credentials", AsyncMock(return_value=("user", "secret"))
    )
    monkeypatch.setattr(workflow, "ForemanClient", lambda *a, **kw: client)
    monkeypatch.setattr(workflow, "verify_quads_ownership", AsyncMock())
    monkeypatch.setattr(workflow, "_assert_active", AsyncMock())
    monkeypatch.setattr(QuadsClient, "from_secrets", AsyncMock(return_value=quads))

    async def save(ticket, state):
        snapshot = copy.deepcopy(state)
        snapshots.append(snapshot)
        ticket["custom_fields"]["foreman_reinstall_state"] = snapshot

    monkeypatch.setattr(workflow, "_save_state", save)
    ticket = {
        "id": "PERF-TEST",
        "custom_fields": {
            "resource_provider": "quads",
            "resource_provider_metadata": {
                "assignment_id": 42,
                "cloud_name": "cloud02",
            },
            "assigned_hardware_ips": {"controller": "node.example", "targets": []},
            "required_hosts": [{"roles": ["client"]}],
            "directives": {
                "platform_reinstall": {
                    "provider": "foreman",
                    "os": "RHEL 10.0",
                    "reserve_without_os": True,
                }
            },
        },
    }
    return ticket, client, SSH(), snapshots, reboots


@pytest.mark.asyncio
async def test_reinstall_checkpoints_build_and_reboot_and_verifies_os(setup_workflow):
    ticket, client, ssh, snapshots, reboots = setup_workflow
    result = await workflow.provision_foreman(ticket, ssh, timeout=1, interval=0)
    assert result["success"] is True
    assert client.set_calls == 1
    assert reboots == ["node.example"]
    assert snapshots[-1]["status"] == "verified"
    assert workflow.reinstall_verified(ticket["custom_fields"])


@pytest.mark.asyncio
async def test_lost_build_response_is_reconciled_without_second_build(setup_workflow):
    ticket, client, ssh, snapshots, reboots = setup_workflow
    client.uncertain = True
    with pytest.raises(ForemanError, match="response lost"):
        await workflow.provision_foreman(ticket, ssh, timeout=1, interval=0)
    assert snapshots[-1]["hosts"]["node.example"]["phase"] == "build_unknown"
    result = await workflow.provision_foreman(ticket, ssh, timeout=1, interval=0)
    assert result["success"] is True
    assert client.set_calls == 1
    assert reboots == ["node.example"]


@pytest.mark.asyncio
async def test_timeout_resumes_without_issuing_a_second_reboot(
    setup_workflow, monkeypatch
):
    ticket, client, ssh, snapshots, reboots = setup_workflow
    monitor = workflow._monitor_host
    monkeypatch.setattr(
        workflow,
        "_monitor_host",
        AsyncMock(side_effect=ForemanError("timeout", "timed out")),
    )
    with pytest.raises(ForemanError, match="timed out"):
        await workflow.provision_foreman(ticket, ssh, timeout=1, interval=0)
    assert snapshots[-1]["hosts"]["node.example"]["phase"] == "reboot_unknown"
    monkeypatch.setattr(workflow, "_monitor_host", monitor)
    result = await workflow.provision_foreman(ticket, ssh, timeout=1, interval=0)
    assert result["success"] is True
    assert client.set_calls == 1
    assert reboots == ["node.example"]


@pytest.mark.asyncio
async def test_rejected_reboot_is_reported_and_not_repeated(
    setup_workflow, monkeypatch
):
    ticket, client, ssh, snapshots, _reboots = setup_workflow
    original = ssh.run
    reboot_calls = 0

    async def run(host, command, **kwargs):
        nonlocal reboot_calls
        if command == "systemctl reboot":
            reboot_calls += 1
            return SimpleNamespace(exit_code=1, stdout="")
        return await original(host, command, **kwargs)

    ssh.run = run
    monkeypatch.setattr(workflow, "tool_progress", AsyncMock())
    with pytest.raises(ForemanError, match="rejected reboot") as error:
        await workflow.provision_foreman(ticket, ssh, timeout=1, interval=0)
    assert error.value.category == "reboot"
    assert snapshots[-1]["hosts"]["node.example"]["phase"] == "reboot_unknown"
    with pytest.raises(ForemanError, match="Timed out"):
        await workflow.provision_foreman(ticket, ssh, timeout=0.001, interval=0)
    assert client.set_calls == 1
    assert reboot_calls == 1


@pytest.mark.asyncio
async def test_reboot_timeout_is_reconciled_as_an_unknown_outcome(setup_workflow):
    ticket, client, ssh, _snapshots, reboots = setup_workflow
    original = ssh.run

    async def run(host, command, **kwargs):
        result = await original(host, command, **kwargs)
        if command == "systemctl reboot":
            return SimpleNamespace(exit_code=-1, stdout="")
        return result

    ssh.run = run
    result = await workflow.provision_foreman(ticket, ssh, timeout=1, interval=0)
    assert result["success"] is True
    assert client.set_calls == 1
    assert reboots == ["node.example"]


@pytest.mark.asyncio
async def test_ticket_changes_block_a_destructive_step(monkeypatch):
    ticket = {
        "id": "PERF-TEST",
        "custom_fields": {
            "resource_provider": "quads",
            "resource_provider_metadata": {
                "assignment_id": 42,
                "cloud_name": "cloud02",
            },
            "assigned_hardware_ips": {"controller": "node.example", "targets": []},
            "directives": {
                "platform_reinstall": {"provider": "foreman", "os": "RHEL 10.0"}
            },
        },
    }
    fingerprint = workflow.request_fingerprint(ticket["custom_fields"])
    changed = copy.deepcopy(ticket)
    changed["custom_fields"]["directives"]["platform_reinstall"]["os"] = "RHEL 9.4"
    monkeypatch.setattr(
        workflow, "assert_ticket_active", AsyncMock(return_value=changed)
    )
    with pytest.raises(ForemanError, match="intent or allocation changed"):
        await workflow._assert_active(ticket["id"], fingerprint)


@pytest.mark.asyncio
async def test_changed_request_cannot_reuse_an_unknown_checkpoint(setup_workflow):
    ticket, client, ssh, _snapshots, _reboots = setup_workflow
    client.uncertain = True
    with pytest.raises(ForemanError, match="response lost"):
        await workflow.provision_foreman(ticket, ssh, timeout=1, interval=0)
    ticket["custom_fields"]["directives"]["platform_reinstall"]["os"] = "RHEL 9.4"
    assert not workflow.reinstall_verified(ticket["custom_fields"])
    with pytest.raises(ForemanError, match="changed after work started"):
        await workflow.provision_foreman(ticket, ssh, timeout=1, interval=0)
    assert client.set_calls == 1


@pytest.mark.asyncio
async def test_completed_checkpoint_is_reverified_without_reinstall(setup_workflow):
    ticket, client, ssh, _snapshots, reboots = setup_workflow
    await workflow.provision_foreman(ticket, ssh, timeout=1, interval=0)
    result = await workflow.provision_foreman(ticket, ssh, timeout=1, interval=0)
    assert result["success"] is True
    assert client.set_calls == 1
    assert reboots == ["node.example"]


@pytest.mark.asyncio
async def test_completed_checkpoint_does_not_skip_current_ownership(
    setup_workflow, monkeypatch
):
    ticket, client, ssh, _snapshots, _reboots = setup_workflow
    await workflow.provision_foreman(ticket, ssh, timeout=1, interval=0)
    monkeypatch.setattr(
        workflow,
        "verify_quads_ownership",
        AsyncMock(side_effect=ForemanError("host", "allocation mismatch")),
    )
    with pytest.raises(ForemanError, match="allocation mismatch"):
        await workflow.provision_foreman(ticket, ssh)
    assert client.set_calls == 1


@pytest.mark.asyncio
async def test_failed_ownership_does_not_begin_build(setup_workflow, monkeypatch):
    ticket, client, ssh, snapshots, _reboots = setup_workflow
    monkeypatch.setattr(
        workflow,
        "verify_quads_ownership",
        AsyncMock(side_effect=ForemanError("host", "allocation mismatch")),
    )
    with pytest.raises(ForemanError, match="allocation mismatch"):
        await workflow.provision_foreman(ticket, ssh)
    assert snapshots == []
    assert client.set_calls == 0


@pytest.mark.asyncio
async def test_missing_install_timestamp_stops_before_build(setup_workflow):
    ticket, client, ssh, snapshots, _reboots = setup_workflow
    client.find_host = AsyncMock(
        return_value={"id": 12, "name": "node.example", "build": False}
    )
    with pytest.raises(ForemanError, match="does not expose installation"):
        await workflow.provision_foreman(ticket, ssh)
    assert snapshots == []
    assert client.set_calls == 0


def test_selective_multi_host_roles_fail_closed():
    fields = {
        "assigned_hardware_ips": {
            "controller": "controller.example",
            "targets": ["client.example", "server.example"],
        }
    }
    with pytest.raises(ForemanError, match="cannot be mapped"):
        workflow.selected_hosts(fields, {"roles": ["client"]})


def test_old_or_missing_foreman_install_timestamp_cannot_prove_reinstall():
    record = {"previous_installed_at": "2026-01-01 00:00:00 UTC"}
    assert not workflow._installation_completed({"installed_at": None}, record)
    assert not workflow._installation_completed(
        {"installed_at": "2026-01-01T00:00:00Z"}, record
    )
    assert workflow._installation_completed(
        {"installed_at": "2026-01-01T00:00:01Z"}, record
    )


@pytest.mark.asyncio
async def test_monitor_retries_key_bootstrap_after_reaching_old_os(monkeypatch):
    monkeypatch.setattr(workflow, "_assert_active", AsyncMock())
    monkeypatch.setattr(workflow, "verify_quads_ownership", AsyncMock())
    monkeypatch.setattr(workflow, "tool_progress", AsyncMock())
    bootstrap = AsyncMock()
    monkeypatch.setattr(workflow, "_bootstrap", bootstrap)
    client = MagicMock()
    client.find_host = AsyncMock(
        return_value={
            "id": 12,
            "build": False,
            "operatingsystem_id": 17,
            "installed_at": "2026-02-01 00:00:00 UTC",
        }
    )
    ssh = MagicMock()
    ssh.run = AsyncMock(
        side_effect=[
            SimpleNamespace(
                exit_code=0, stdout='old-boot\nID="rhel"\nVERSION_ID="10.0"\n'
            ),
            SimpleNamespace(
                exit_code=0, stdout='new-boot\nID="rhel"\nVERSION_ID="10.0"\n'
            ),
        ]
    )
    await workflow._monitor_host(
        client=client,
        quads=object(),
        ssh=ssh,
        host="node.example",
        record={
            "host_id": 12,
            "options": {"operatingsystem_id": 17},
            "previous_boot_id": "old-boot",
            "previous_installed_at": "2026-01-01 00:00:00 UTC",
            "expected_release": {"id": "rhel", "version": "10.0"},
        },
        config={"bootstrap": {"mode": "quads"}},
        metadata={},
        ticket_id="PERF-TEST",
        fingerprint="fingerprint",
        timeout=1,
        interval=0,
    )
    assert bootstrap.await_count == 2


@pytest.mark.asyncio
async def test_wrong_os_never_marks_install_verified(monkeypatch):
    monkeypatch.setattr(workflow, "_assert_active", AsyncMock())
    monkeypatch.setattr(workflow, "verify_quads_ownership", AsyncMock())
    monkeypatch.setattr(workflow, "tool_progress", AsyncMock())
    client = MagicMock()
    client.find_host = AsyncMock(
        return_value={
            "id": 12,
            "build": False,
            "operatingsystem_id": 17,
            "installed_at": "2026-02-01 00:00:00 UTC",
        }
    )
    ssh = MagicMock()
    ssh.run = AsyncMock(
        return_value=SimpleNamespace(
            exit_code=0, stdout='new-boot\nID="rhel"\nVERSION_ID="9.4"\n'
        )
    )
    with pytest.raises(ForemanError, match="Timed out"):
        await workflow._monitor_host(
            client=client,
            quads=object(),
            ssh=ssh,
            host="node.example",
            record={
                "host_id": 12,
                "options": {"operatingsystem_id": 17},
                "previous_boot_id": "old-boot",
                "previous_installed_at": "2026-01-01 00:00:00 UTC",
                "expected_release": {"id": "rhel", "version": "10.0"},
            },
            config={"bootstrap": {"mode": "existing"}},
            metadata={},
            ticket_id="PERF-TEST",
            fingerprint="fingerprint",
            timeout=0.001,
            interval=0,
        )
