"""Tests for reboot_hosts_and_verify and verify_kernel_state."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from tests.conftest import make_provisioning_handlers
from tests.fake_rhel import FakeFleetSSH, FakeRHELHost

KERNEL_A = "5.14.0-503.14.1.el9_5.x86_64"
KERNEL_B = "5.14.0-503.16.1.el9_5.x86_64"

KERNEL_STEP_TICKET = {
    "id": "PERF-TEST",
    "status": "awaiting_provision",
    "custom_fields": {
        "execution_plan": {
            "current_step": 2,
            "steps": [
                {"id": 0, "agent_type": "resource", "params": {}},
                {"id": 1, "agent_type": "provision", "params": {"label": "harness"}},
                {
                    "id": 2,
                    "agent_type": "provision",
                    "params": {
                        "label": "kernel-B",
                        "kernel": {
                            "release": KERNEL_B,
                            "package": "kernel",
                        },
                    },
                },
            ],
        },
        "assigned_hardware_ips": {
            "controller": "10.0.0.100",
            "targets": ["10.0.0.1", "10.0.0.2"],
        },
    },
}


@pytest.fixture(autouse=True)
def mock_ticket_active():
    mock = AsyncMock(return_value={"status": "ok", "ticket": KERNEL_STEP_TICKET})
    with patch("agents.server_utils.assert_ticket_active", mock):
        yield


@pytest.fixture(autouse=True)
def patch_sleep():
    """Skip real sleeps in the poll loop."""
    with patch("asyncio.sleep", new_callable=AsyncMock):
        yield


@pytest.fixture
def two_hosts_ready():
    """Two hosts with KERNEL_B installed and set as default."""
    h1 = FakeRHELHost(
        "host1",
        running=KERNEL_A,
        installed=[KERNEL_A, KERNEL_B],
        default=f"/boot/vmlinuz-{KERNEL_B}",
    )
    h2 = FakeRHELHost(
        "host2",
        running=KERNEL_A,
        installed=[KERNEL_A, KERNEL_B],
        default=f"/boot/vmlinuz-{KERNEL_B}",
    )
    return {"10.0.0.1": h1, "10.0.0.2": h2}


@pytest.fixture
def fleet_ssh(two_hosts_ready):
    return FakeFleetSSH(two_hosts_ready)


@pytest.fixture
def handlers(fleet_ssh):
    return make_provisioning_handlers(fleet_ssh)


class TestRebootSuccess:
    @pytest.mark.asyncio
    async def test_two_host_serial_success(self, handlers, fleet_ssh):
        result = await handlers["reboot_hosts_and_verify"](
            hosts=["10.0.0.1", "10.0.0.2"],
            expected_kernel=KERNEL_B,
            approval_request_id="test",
            reconnect_timeout_seconds=300,
            poll_interval_seconds=1,
        )
        assert result["status"] == "verified"
        assert result["hosts"]["10.0.0.1"]["state"] == "verified"
        assert result["hosts"]["10.0.0.2"]["state"] == "verified"
        assert result["hosts"]["10.0.0.1"]["rebooted"] is True
        assert result["hosts"]["10.0.0.2"]["rebooted"] is True

        reboot_calls = [
            c for c in fleet_ssh.calls if "systemctl reboot" in c["command"]
        ]
        assert len(reboot_calls) == 2
        h1_reboot_idx = next(
            i
            for i, c in enumerate(fleet_ssh.calls)
            if "systemctl reboot" in c["command"] and c["host"] == "10.0.0.1"
        )
        h2_reboot_idx = next(
            i
            for i, c in enumerate(fleet_ssh.calls)
            if "systemctl reboot" in c["command"] and c["host"] == "10.0.0.2"
        )
        assert h1_reboot_idx < h2_reboot_idx

    @pytest.mark.asyncio
    async def test_ssh_disconnect_then_reconnect(self, handlers, two_hosts_ready):
        two_hosts_ready["10.0.0.1"].reconnect_after_polls = 5
        result = await handlers["reboot_hosts_and_verify"](
            hosts=["10.0.0.1"],
            expected_kernel=KERNEL_B,
            approval_request_id="test",
            reconnect_timeout_seconds=300,
            poll_interval_seconds=1,
        )
        assert result["hosts"]["10.0.0.1"]["state"] == "verified"
        assert result["hosts"]["10.0.0.1"]["went_down"] is True


class TestRebootFailures:
    @pytest.mark.asyncio
    async def test_never_returns(self, handlers, two_hosts_ready):
        two_hosts_ready["10.0.0.1"].behavior = "never_returns"
        result = await handlers["reboot_hosts_and_verify"](
            hosts=["10.0.0.1", "10.0.0.2"],
            expected_kernel=KERNEL_B,
            approval_request_id="test",
            reconnect_timeout_seconds=5,
            poll_interval_seconds=1,
        )
        assert result["status"] == "partial_failure"
        assert result["hosts"]["10.0.0.1"]["state"] == "reconnect_timeout"
        assert result["hosts"]["10.0.0.2"]["state"] == "not_attempted"

    @pytest.mark.asyncio
    async def test_fallback_boot(self, handlers, two_hosts_ready):
        two_hosts_ready["10.0.0.1"].behavior = "fallback"
        result = await handlers["reboot_hosts_and_verify"](
            hosts=["10.0.0.1"],
            expected_kernel=KERNEL_B,
            approval_request_id="test",
            reconnect_timeout_seconds=300,
            poll_interval_seconds=1,
        )
        assert result["hosts"]["10.0.0.1"]["state"] == "fallback_boot"

    @pytest.mark.asyncio
    async def test_wrong_kernel(self, handlers, two_hosts_ready):
        two_hosts_ready["10.0.0.1"].behavior = "wrong_kernel:6.0.0-1.el9.x86_64"
        result = await handlers["reboot_hosts_and_verify"](
            hosts=["10.0.0.1"],
            expected_kernel=KERNEL_B,
            approval_request_id="test",
            reconnect_timeout_seconds=300,
            poll_interval_seconds=1,
        )
        assert result["hosts"]["10.0.0.1"]["state"] == "kernel_mismatch"

    @pytest.mark.asyncio
    async def test_ignores_reboot(self, handlers, two_hosts_ready):
        two_hosts_ready["10.0.0.1"].behavior = "ignores_reboot"
        result = await handlers["reboot_hosts_and_verify"](
            hosts=["10.0.0.1"],
            expected_kernel=KERNEL_B,
            approval_request_id="test",
            reconnect_timeout_seconds=5,
            poll_interval_seconds=1,
            reboot_policy="always",
        )
        # With mocked sleep, time.monotonic barely advances so
        # reboot_not_observed (which needs > down_grace_seconds)
        # may not trigger — reconnect_timeout is also acceptable
        assert result["hosts"]["10.0.0.1"]["state"] in (
            "reboot_not_observed",
            "reconnect_timeout",
        )


class TestPrecheckAndSkip:
    @pytest.mark.asyncio
    async def test_precheck_wrong_default(self, handlers, two_hosts_ready):
        two_hosts_ready["10.0.0.1"].default = f"/boot/vmlinuz-{KERNEL_A}"
        result = await handlers["reboot_hosts_and_verify"](
            hosts=["10.0.0.1"],
            expected_kernel=KERNEL_B,
            approval_request_id="test",
        )
        assert result["hosts"]["10.0.0.1"]["state"] == "precheck_failed"

    @pytest.mark.asyncio
    async def test_if_needed_skips_when_running(
        self, handlers, two_hosts_ready, fleet_ssh
    ):
        two_hosts_ready["10.0.0.1"].running = KERNEL_B
        result = await handlers["reboot_hosts_and_verify"](
            hosts=["10.0.0.1"],
            expected_kernel=KERNEL_B,
            approval_request_id="test",
            reboot_policy="if_needed",
        )
        assert result["hosts"]["10.0.0.1"]["state"] == "verified"
        assert result["hosts"]["10.0.0.1"]["rebooted"] is False
        reboot_calls = [
            c for c in fleet_ssh.calls if "systemctl reboot" in c["command"]
        ]
        assert len(reboot_calls) == 0

    @pytest.mark.asyncio
    async def test_always_reboots_even_if_running(
        self, handlers, two_hosts_ready, fleet_ssh
    ):
        two_hosts_ready["10.0.0.1"].running = KERNEL_B
        result = await handlers["reboot_hosts_and_verify"](
            hosts=["10.0.0.1"],
            expected_kernel=KERNEL_B,
            approval_request_id="test",
            reboot_policy="always",
            reconnect_timeout_seconds=300,
            poll_interval_seconds=1,
        )
        assert result["hosts"]["10.0.0.1"]["state"] == "verified"
        assert result["hosts"]["10.0.0.1"]["rebooted"] is True


class TestRefusalAndValidation:
    @pytest.mark.asyncio
    async def test_controller_refused(self, handlers):
        result = await handlers["reboot_hosts_and_verify"](
            hosts=["10.0.0.100"],
            expected_kernel=KERNEL_B,
            approval_request_id="test",
        )
        h = result["hosts"]["10.0.0.100"]
        assert h["state"] == "refused"

    @pytest.mark.asyncio
    async def test_wrong_kernel_rejected(self, handlers):
        result = await handlers["reboot_hosts_and_verify"](
            hosts=["10.0.0.1"],
            expected_kernel=KERNEL_A,
            approval_request_id="test",
        )
        assert result["status"] == "rejected"

    @pytest.mark.asyncio
    async def test_parallel_strategy_rejected(self, handlers):
        result = await handlers["reboot_hosts_and_verify"](
            hosts=["10.0.0.1"],
            expected_kernel=KERNEL_B,
            approval_request_id="test",
            strategy="parallel",
        )
        assert result["status"] == "rejected"
        assert result["reason_code"] == "strategy_unsupported"

    @pytest.mark.asyncio
    async def test_duplicate_hosts_deduped(self, handlers, fleet_ssh):
        await handlers["reboot_hosts_and_verify"](
            hosts=["10.0.0.1", "10.0.0.1"],
            expected_kernel=KERNEL_B,
            approval_request_id="test",
            reconnect_timeout_seconds=300,
            poll_interval_seconds=1,
        )
        reboot_calls = [
            c for c in fleet_ssh.calls if "systemctl reboot" in c["command"]
        ]
        assert len(reboot_calls) == 1

    @pytest.mark.asyncio
    async def test_all_refused_not_verified(self, handlers):
        result = await handlers["reboot_hosts_and_verify"](
            hosts=["10.0.0.100"],
            expected_kernel=KERNEL_B,
            approval_request_id="test",
        )
        assert result["status"] == "rejected"
        assert result["reason_code"] == "all_hosts_refused"


class TestVerifyKernelState:
    @pytest.mark.asyncio
    async def test_verified_when_running_expected(self, handlers, two_hosts_ready):
        two_hosts_ready["10.0.0.1"].running = KERNEL_B
        result = await handlers["verify_kernel_state"](
            hosts=["10.0.0.1"],
            expected_kernel=KERNEL_B,
        )
        h1 = result["results"]["10.0.0.1"]
        assert h1["state"] == "verified"
        assert h1["reconciled"] is True

    @pytest.mark.asyncio
    async def test_mismatch_when_running_other(self, handlers):
        result = await handlers["verify_kernel_state"](
            hosts=["10.0.0.1"],
            expected_kernel=KERNEL_B,
        )
        h1 = result["results"]["10.0.0.1"]
        assert h1["state"] == "kernel_mismatch"
        assert h1["running"] == KERNEL_A

    @pytest.mark.asyncio
    async def test_never_reboots(self, handlers, fleet_ssh):
        await handlers["verify_kernel_state"](
            hosts=["10.0.0.1"],
            expected_kernel=KERNEL_B,
        )
        reboot_calls = [
            c for c in fleet_ssh.calls if "systemctl reboot" in c["command"]
        ]
        assert len(reboot_calls) == 0
