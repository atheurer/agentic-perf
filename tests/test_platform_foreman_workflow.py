"""Foreman platform checkpoints, reconciliation, and authorization."""

from __future__ import annotations

import asyncio
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
            if command == workflow.build_reboot_command():
                assert (
                    snapshots[-1]["hosts"]["node.example"]["phase"] == "reboot_unknown"
                )
                reboots.append(hostname)
                host["build"] = False
                host["installed_at"] = "2026-02-01 00:00:00 UTC"
                return SimpleNamespace(exit_code=0, stdout="@@rebooting\n")
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


@pytest.fixture
def setup_multi_host_workflow(monkeypatch):
    snapshots = []
    names = ["controller.example", "target.example"]
    hosts = {
        name: {
            "id": 10 + index,
            "name": name,
            "build": False,
            "architecture_id": 1,
            "operatingsystem_id": 2,
            "installed_at": "2026-01-01 00:00:00 UTC",
        }
        for index, name in enumerate(names, 1)
    }

    class Client:
        set_calls = 0

        async def find_host(self, name):
            return dict(hosts[name])

        async def resolve_install_options(self, host_data, name, **kwargs):
            return {"operatingsystem_id": 17, "medium_id": 15, "ptable_id": 169}

        async def get_operating_system(self, os_id):
            return {"name": "RedHat", "major": "10", "minor": "0"}

        async def set_build(self, host_id, options):
            self.set_calls += 1
            host = next(value for value in hosts.values() if value["id"] == host_id)
            host.update(options)
            host["build"] = True

        async def close(self):
            pass

    client = Client()
    reboots = []

    class SSH:
        key_path = "/unused"

        async def run(self, hostname, command, **kwargs):
            if command == "cat /proc/sys/kernel/random/boot_id":
                return SimpleNamespace(exit_code=0, stdout=f"old-{hostname}\n")
            if command == workflow.build_reboot_command():
                assert snapshots[-1]["hosts"][hostname]["phase"] == "reboot_unknown"
                reboots.append(hostname)
                hosts[hostname]["build"] = False
                hosts[hostname]["installed_at"] = "2026-02-01 00:00:00 UTC"
                return SimpleNamespace(exit_code=0, stdout="@@rebooting\n")
            return SimpleNamespace(
                exit_code=0,
                stdout=f'new-{hostname}\nID="rhel"\nVERSION_ID="10.0"\n',
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
                "ticket": "5000",
            },
            "assigned_hardware_ips": {
                "controller": names[0],
                "targets": [names[1]],
            },
            "required_hosts": [
                {"roles": ["controller"]},
                {"roles": ["target"]},
            ],
            "directives": {
                "platform_reinstall": {
                    "provider": "foreman",
                    "os": "RHEL 10.0",
                }
            },
        },
    }
    return ticket, client, SSH(), snapshots, reboots


@pytest.mark.asyncio
async def test_reinstall_checkpoints_build_and_reboot_and_verifies_os(setup_workflow):
    ticket, client, ssh, snapshots, reboots = setup_workflow
    result = await workflow.provision_foreman(
        ticket, ssh, build_timeout=1, ssh_timeout=1, interval=0
    )
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
        await workflow.provision_foreman(
            ticket, ssh, build_timeout=1, ssh_timeout=1, interval=0
        )
    assert snapshots[-1]["hosts"]["node.example"]["phase"] == "build_unknown"
    result = await workflow.provision_foreman(
        ticket, ssh, build_timeout=1, ssh_timeout=1, interval=0
    )
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
    result = await workflow.provision_foreman(
        ticket, ssh, build_timeout=1, ssh_timeout=1, interval=0
    )
    assert result["success"] is False
    assert result["status"] == "failed"
    assert "timed out" in result["host_results"]["node.example"]["message"]
    assert snapshots[-1]["hosts"]["node.example"]["phase"] == "reboot_unknown"
    monkeypatch.setattr(workflow, "_monitor_host", monitor)
    result = await workflow.provision_foreman(
        ticket, ssh, build_timeout=1, ssh_timeout=1, interval=0
    )
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
        if command == workflow.build_reboot_command():
            reboot_calls += 1
            return SimpleNamespace(exit_code=1, stdout="")
        return await original(host, command, **kwargs)

    ssh.run = run
    monkeypatch.setattr(workflow, "tool_progress", AsyncMock())
    with pytest.raises(ForemanError, match="detached reboot") as error:
        await workflow.provision_foreman(
            ticket, ssh, build_timeout=1, ssh_timeout=1, interval=0
        )
    assert error.value.category == "reboot"
    assert snapshots[-1]["hosts"]["node.example"]["phase"] == "reboot_unknown"
    result = await workflow.provision_foreman(
        ticket, ssh, build_timeout=0.001, ssh_timeout=0.001, interval=0
    )
    assert result["success"] is False
    assert result["host_results"]["node.example"]["category"] == "timeout"
    assert client.set_calls == 1
    assert reboot_calls == 1


@pytest.mark.asyncio
async def test_reboot_timeout_is_reconciled_as_an_unknown_outcome(setup_workflow):
    ticket, client, ssh, _snapshots, reboots = setup_workflow
    original = ssh.run

    async def run(host, command, **kwargs):
        result = await original(host, command, **kwargs)
        if command == workflow.build_reboot_command():
            return SimpleNamespace(exit_code=-1, stdout="@@rebooting\n")
        return result

    ssh.run = run
    result = await workflow.provision_foreman(
        ticket, ssh, build_timeout=1, ssh_timeout=1, interval=0
    )
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
        await workflow.provision_foreman(
            ticket, ssh, build_timeout=1, ssh_timeout=1, interval=0
        )
    ticket["custom_fields"]["directives"]["platform_reinstall"]["os"] = "RHEL 9.4"
    assert not workflow.reinstall_verified(ticket["custom_fields"])
    with pytest.raises(ForemanError, match="changed after work started"):
        await workflow.provision_foreman(
            ticket, ssh, build_timeout=1, ssh_timeout=1, interval=0
        )
    assert client.set_calls == 1


@pytest.mark.asyncio
async def test_completed_checkpoint_is_reverified_without_reinstall(setup_workflow):
    ticket, client, ssh, _snapshots, reboots = setup_workflow
    await workflow.provision_foreman(
        ticket, ssh, build_timeout=1, ssh_timeout=1, interval=0
    )
    result = await workflow.provision_foreman(
        ticket, ssh, build_timeout=1, ssh_timeout=1, interval=0
    )
    assert result["success"] is True
    assert client.set_calls == 1
    assert reboots == ["node.example"]


@pytest.mark.asyncio
async def test_completed_checkpoint_does_not_skip_current_ownership(
    setup_workflow, monkeypatch
):
    ticket, client, ssh, _snapshots, _reboots = setup_workflow
    await workflow.provision_foreman(
        ticket, ssh, build_timeout=1, ssh_timeout=1, interval=0
    )
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
        build_timeout=1,
        ssh_timeout=1,
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
            build_timeout=0.001,
            ssh_timeout=0.001,
            interval=0,
        )


@pytest.mark.asyncio
async def test_formal_assignment_directive_overrides_stale_metadata(setup_workflow):
    ticket, client, ssh, _snapshots, reboots = setup_workflow
    cf = ticket["custom_fields"]
    cf["resource_provider"] = "user_provided"
    cf["required_hosts"] = [{"host": "node.example", "roles": ["client"]}]
    cf["resource_provider_metadata"] = {
        "assignment_id": 99,
        "cloud_name": "cloud02",
        "ticket": "5065",
    }
    cf["directives"]["quads"] = {"cloud": "cloud14", "ticket": "5113"}

    result = await workflow.provision_foreman(
        ticket, ssh, build_timeout=1, ssh_timeout=1, interval=0
    )

    assert result["success"] is True
    assert reboots == ["node.example"]
    first = workflow.verify_quads_ownership.await_args_list[0]
    assert first.args[1] == {"cloud_name": "cloud14", "ticket": "5113"}
    assert first.kwargs["require_active_assignment"] is False


@pytest.mark.asyncio
async def test_formal_assignment_rejects_hosts_outside_required_hosts(setup_workflow):
    ticket, client, ssh, snapshots, _reboots = setup_workflow
    cf = ticket["custom_fields"]
    cf["resource_provider"] = "user_provided"
    cf["required_hosts"] = [{"host": "other.example", "roles": ["client"]}]
    cf["directives"]["quads"] = {"cloud": "cloud14", "ticket": "5113"}

    with pytest.raises(ForemanError, match="not named in required_hosts"):
        await workflow.provision_foreman(ticket, ssh)
    assert snapshots == []
    assert client.set_calls == 0


@pytest.mark.asyncio
async def test_quads_directive_is_rejected_for_non_quads_provider(setup_workflow):
    ticket, client, ssh, _snapshots, _reboots = setup_workflow
    cf = ticket["custom_fields"]
    cf["resource_provider"] = "aws"
    cf["required_hosts"] = [{"host": "node.example", "roles": ["client"]}]
    cf["directives"]["quads"] = {"cloud": "cloud14", "ticket": "5113"}

    with pytest.raises(ForemanError, match="not valid for this provider"):
        await workflow.provision_foreman(ticket, ssh)
    assert client.set_calls == 0
    QuadsClient.from_secrets.assert_not_awaited()


@pytest.mark.asyncio
async def test_unrelated_ticket_and_cloud_fields_are_not_assignment_identity(
    setup_workflow,
):
    ticket, client, ssh, _snapshots, _reboots = setup_workflow
    cf = ticket["custom_fields"]
    cf["resource_provider"] = "user_provided"
    cf["ticket"] = "PERF-123"
    cf["cloud"] = "cloud99"
    cf["required_hosts"] = [{"host": "node.example", "roles": ["client"]}]

    with pytest.raises(ForemanError, match="does not support verified"):
        await workflow.provision_foreman(ticket, ssh)
    assert client.set_calls == 0
    QuadsClient.from_secrets.assert_not_awaited()


@pytest.mark.asyncio
async def test_incomplete_quads_directive_fails_closed(setup_workflow):
    ticket, _client, ssh, _snapshots, _reboots = setup_workflow
    ticket["custom_fields"]["directives"]["quads"] = {"cloud": "cloud14"}

    with pytest.raises(ForemanError, match="requires cloud and ticket"):
        await workflow.provision_foreman(ticket, ssh)


@pytest.mark.asyncio
async def test_normal_quads_selection_preserves_self_serve_reinstall(setup_workflow):
    ticket, client, ssh, _snapshots, reboots = setup_workflow
    ticket["custom_fields"]["quads_selection"] = {
        "os": "RHEL 10.0",
        "hostnames": ["node.example"],
    }

    result = await workflow.provision_foreman(
        ticket, ssh, build_timeout=1, ssh_timeout=1, interval=0
    )

    assert result["success"] is True
    assert client.set_calls == 1
    assert reboots == ["node.example"]


@pytest.mark.asyncio
async def test_monitor_runs_eligible_hosts_concurrently(setup_multi_host_workflow):
    ticket, client, ssh, snapshots, reboots = setup_multi_host_workflow
    original = workflow._monitor_host
    active = 0
    max_active = 0

    async def monitor(**kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.01)
            return await original(**kwargs)
        finally:
            active -= 1

    workflow._monitor_host = monitor
    try:
        result = await workflow.provision_foreman(
            ticket, ssh, build_timeout=1, ssh_timeout=1, interval=0
        )
    finally:
        workflow._monitor_host = original

    assert result["success"] is True
    assert max_active >= 2
    assert sorted(reboots) == ["controller.example", "target.example"]
    assert client.set_calls == 2
    assert all(
        record["phase"] == "verified" for record in snapshots[-1]["hosts"].values()
    )


@pytest.mark.asyncio
async def test_partial_monitor_failure_reports_each_host(setup_multi_host_workflow):
    ticket, _client, ssh, snapshots, _reboots = setup_multi_host_workflow
    original = workflow._monitor_host

    async def monitor(**kwargs):
        if kwargs["host"] == "target.example":
            raise ForemanError("timeout", "target timed out")
        return await original(**kwargs)

    workflow._monitor_host = monitor
    try:
        result = await workflow.provision_foreman(
            ticket, ssh, build_timeout=1, ssh_timeout=1, interval=0
        )
    finally:
        workflow._monitor_host = original

    assert result["success"] is False
    assert result["status"] == "partial"
    assert result["host_results"]["controller.example"]["status"] == "verified"
    assert result["host_results"]["target.example"] == {
        "status": "failed",
        "category": "timeout",
        "message": "target timed out",
    }
    assert snapshots[-1]["hosts"]["controller.example"]["phase"] == "verified"
    assert snapshots[-1]["hosts"]["target.example"]["phase"] == "reboot_unknown"


@pytest.mark.asyncio
async def test_monitor_retries_transient_foreman_poll_errors(monkeypatch):
    monkeypatch.setattr(workflow, "_assert_active", AsyncMock())
    monkeypatch.setattr(workflow, "tool_progress", AsyncMock())
    failed = False

    async def find_host(_name):
        nonlocal failed
        if not failed:
            failed = True
            raise ForemanError("transport", "temporary outage")
        return {
            "id": 12,
            "build": False,
            "operatingsystem_id": 17,
            "installed_at": "2026-02-01 00:00:00 UTC",
        }

    client = MagicMock()
    client.find_host = AsyncMock(side_effect=find_host)
    ssh = MagicMock()
    ssh.run = AsyncMock(
        return_value=SimpleNamespace(
            exit_code=0, stdout='new-boot\nID="rhel"\nVERSION_ID="10.0"\n'
        )
    )

    await workflow._monitor_host(
        client=client,
        quads=None,
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
        build_timeout=1,
        ssh_timeout=1,
        interval=0,
    )

    assert client.find_host.await_count == 3


@pytest.mark.asyncio
async def test_monitor_does_not_retry_authentication_failures(monkeypatch):
    monkeypatch.setattr(workflow, "_assert_active", AsyncMock())
    client = MagicMock()
    client.find_host = AsyncMock(
        side_effect=ForemanError("authentication", "credentials rejected")
    )
    ssh = MagicMock()

    with pytest.raises(ForemanError, match="credentials rejected"):
        await workflow._monitor_host(
            client=client,
            quads=None,
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
            build_timeout=1,
            ssh_timeout=1,
            interval=0,
        )
    assert client.find_host.await_count == 1


@pytest.mark.asyncio
async def test_build_and_ssh_timeouts_are_separate(monkeypatch):
    monkeypatch.setattr(workflow, "_assert_active", AsyncMock())
    monkeypatch.setattr(workflow, "tool_progress", AsyncMock())
    build_client = MagicMock()
    build_client.find_host = AsyncMock(
        return_value={"id": 12, "build": True, "operatingsystem_id": 17}
    )
    record = {
        "host_id": 12,
        "options": {"operatingsystem_id": 17},
        "previous_boot_id": "old-boot",
        "previous_installed_at": "2026-01-01 00:00:00 UTC",
        "expected_release": {"id": "rhel", "version": "10.0"},
    }

    with pytest.raises(ForemanError, match="Timed out waiting for Foreman build"):
        await workflow._monitor_host(
            client=build_client,
            quads=None,
            ssh=MagicMock(),
            host="node.example",
            record=record,
            config={"bootstrap": {"mode": "existing"}},
            metadata={},
            ticket_id="PERF-TEST",
            fingerprint="fingerprint",
            build_timeout=0.001,
            ssh_timeout=10,
            interval=0,
        )

    ssh_client = MagicMock()
    ssh_client.find_host = AsyncMock(
        return_value={
            "id": 12,
            "build": False,
            "operatingsystem_id": 17,
            "installed_at": "2026-02-01 00:00:00 UTC",
        }
    )
    ssh = MagicMock()
    ssh.run = AsyncMock(return_value=SimpleNamespace(exit_code=1, stdout=""))

    with pytest.raises(ForemanError, match="Timed out verifying Foreman installation"):
        await workflow._monitor_host(
            client=ssh_client,
            quads=None,
            ssh=ssh,
            host="node.example",
            record=record,
            config={"bootstrap": {"mode": "existing"}},
            metadata={},
            ticket_id="PERF-TEST",
            fingerprint="fingerprint",
            build_timeout=10,
            ssh_timeout=0.001,
            interval=0,
        )


@pytest.mark.asyncio
async def test_reboot_requires_the_detached_command_marker(setup_workflow, monkeypatch):
    ticket, client, ssh, snapshots, _reboots = setup_workflow
    original = ssh.run

    async def run(host, command, **kwargs):
        if command == workflow.build_reboot_command():
            return SimpleNamespace(exit_code=0, stdout="")
        return await original(host, command, **kwargs)

    ssh.run = run
    monkeypatch.setattr(workflow, "tool_progress", AsyncMock())
    with pytest.raises(ForemanError, match="detached reboot") as error:
        await workflow.provision_foreman(
            ticket, ssh, build_timeout=1, ssh_timeout=1, interval=0
        )
    assert error.value.category == "reboot"
    assert snapshots[-1]["hosts"]["node.example"]["phase"] == "reboot_unknown"
    assert client.set_calls == 1
