"""Cancellation leaves destructive actions reconcilable without replay."""

from __future__ import annotations

import asyncio

import pytest

import agents.platform.foreman_workflow as workflow
from tests.test_platform_foreman_workflow import setup_workflow as _shared_fixture

_workflow_fixture = pytest.fixture(name="setup_workflow")(_shared_fixture.__wrapped__)


async def test_cancellation_during_build_preserves_unknown_and_does_not_resend(
    setup_workflow,
):
    ticket, client, ssh, snapshots, reboots = setup_workflow
    original = client.set_build

    async def cancel_after_send(host_id, options):
        await original(host_id, options)
        raise asyncio.CancelledError

    client.set_build = cancel_after_send
    with pytest.raises(asyncio.CancelledError):
        await workflow.provision_foreman(
            ticket, ssh, build_timeout=1, ssh_timeout=1, interval=0
        )
    assert snapshots[-1]["hosts"]["node.example"]["phase"] == "build_unknown"
    assert client.set_calls == 1
    client.set_build = original
    result = await workflow.provision_foreman(
        ticket, ssh, build_timeout=1, ssh_timeout=1, interval=0
    )
    assert result["success"] is True
    assert client.set_calls == 1
    assert reboots == ["node.example"]


async def test_cancellation_during_reboot_preserves_unknown_and_does_not_resend(
    setup_workflow,
):
    ticket, client, ssh, snapshots, reboots = setup_workflow
    original = ssh.run

    async def cancel_after_send(host, command, **kwargs):
        result = await original(host, command, **kwargs)
        if command == workflow.build_reboot_command():
            raise asyncio.CancelledError
        return result

    ssh.run = cancel_after_send
    with pytest.raises(asyncio.CancelledError):
        await workflow.provision_foreman(
            ticket, ssh, build_timeout=1, ssh_timeout=1, interval=0
        )
    assert snapshots[-1]["hosts"]["node.example"]["phase"] == "reboot_unknown"
    assert reboots == ["node.example"]
    ssh.run = original
    result = await workflow.provision_foreman(
        ticket, ssh, build_timeout=1, ssh_timeout=1, interval=0
    )
    assert result["success"] is True
    assert client.set_calls == 1
    assert reboots == ["node.example"]
