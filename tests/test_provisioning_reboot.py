"""Tests for reboot_hosts_and_verify and verify_kernel_state.

Covers the serial reboot flow, every per-host outcome, the recovery table
(probe first, never a blind second reboot), approval consumption, strict
host coverage, checkpoint persistence and the pause on failure.  No test
needs a running state store.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from tests.conftest import make_provisioning_handlers
from tests.fake_rhel import FakeFleetSSH, FakeKernelStore, FakeRHELHost
from tests.test_provisioning_kernel_tools import KERNEL_A, KERNEL_B, kernel_step_ticket

HOSTS = ["10.0.0.1", "10.0.0.2"]


@pytest.fixture
def two_hosts_ready():
    """Two hosts with KERNEL_B installed and set as default."""
    return {
        ip: FakeRHELHost(
            f"host{i}",
            running=KERNEL_A,
            installed=[KERNEL_A, KERNEL_B],
            default=f"/boot/vmlinuz-{KERNEL_B}",
        )
        for i, ip in enumerate(HOSTS, start=1)
    }


@pytest.fixture
def fleet_ssh(two_hosts_ready):
    return FakeFleetSSH(two_hosts_ready)


@pytest.fixture
def handlers(fleet_ssh):
    return make_provisioning_handlers(fleet_ssh)


@pytest.fixture(autouse=True)
def fake_store():
    FakeKernelStore.reset(kernel_step_ticket())

    async def _active(**kwargs):
        return {"status": "ok", "ticket": FakeKernelStore.ticket}

    with (
        patch("agents.provisioning.kernel.AuditedAsyncHTTPClient", FakeKernelStore),
        patch(
            "agents.server_utils.assert_ticket_active", AsyncMock(side_effect=_active)
        ),
        patch("agents.server_utils.tool_progress", AsyncMock()),
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        yield FakeKernelStore


@pytest.fixture
def approval(fake_store):
    """An approved kernel_change approval whose intent only requires a reboot."""
    from agents.provisioning.kernel import new_intent
    from providers.rhel_kernel import KernelSpec

    intent = new_intent("PERF-TEST", 2, HOSTS, KernelSpec(KERNEL_B), ["reboot"], {})
    fake_store.seed_intent(intent)
    return fake_store.seed_approval(intent)


def reboot_calls(fleet_ssh, host=None):
    return [
        c
        for c in fleet_ssh.calls
        if "systemctl reboot" in c["command"] and (host is None or c["host"] == host)
    ]


def checkpoints(fake_store):
    """Every persisted kernel_transitions record for step 2, in order."""
    return [
        p["fields"]["kernel_transitions"]["2"]
        for p in fake_store.patches
        if "kernel_transitions" in p.get("fields", {})
    ]


async def run_reboot(handlers, approval_id, **overrides):
    kwargs = dict(
        hosts=HOSTS,
        expected_kernel=KERNEL_B,
        approval_request_id=approval_id,
        reconnect_timeout_seconds=300,
        poll_interval_seconds=1,
    )
    kwargs.update(overrides)
    return await handlers["reboot_hosts_and_verify"](**kwargs)


# ---------------------------------------------------------------------------
# Serial success and the ordinary outcomes
# ---------------------------------------------------------------------------


class TestRebootSuccess:
    @pytest.mark.asyncio
    async def test_two_host_serial_success(self, handlers, fleet_ssh, approval):
        result = await run_reboot(handlers, approval)
        assert result["status"] == "verified"
        for host in HOSTS:
            assert result["hosts"][host]["state"] == "verified"
            assert result["hosts"][host]["rebooted"] is True
        assert len(reboot_calls(fleet_ssh)) == 2
        order = [c["host"] for c in reboot_calls(fleet_ssh)]
        assert order == HOSTS

    @pytest.mark.asyncio
    async def test_second_host_rebooted_only_after_first_verified(
        self, handlers, fleet_ssh, approval
    ):
        await run_reboot(handlers, approval)
        idx = {
            c["host"]: i
            for i, c in enumerate(fleet_ssh.calls)
            if "systemctl reboot" in c["command"]
        }
        # A probe of host1 that observed the new kernel happens before host2's reboot.
        probes_after_h1 = [
            i
            for i, c in enumerate(fleet_ssh.calls)
            if c["host"] == "10.0.0.1"
            and "@@boot_id" in c["command"]
            and i > idx["10.0.0.1"]
        ]
        assert probes_after_h1 and max(probes_after_h1) < idx["10.0.0.2"]

    @pytest.mark.asyncio
    async def test_ssh_disconnect_then_reconnect(
        self, handlers, two_hosts_ready, approval
    ):
        two_hosts_ready["10.0.0.1"].reconnect_after_polls = 5
        result = await run_reboot(handlers, approval)
        assert result["hosts"]["10.0.0.1"]["state"] == "verified"
        assert result["hosts"]["10.0.0.1"]["went_down"] is True

    @pytest.mark.asyncio
    async def test_checkpoint_rebooting_written_before_reboot_command(
        self, handlers, fleet_ssh, fake_store, approval
    ):
        calls_at_patch: list[int] = []
        original_patch = FakeKernelStore.patch

        async def _recording_patch(self, url, *args, **kwargs):
            calls_at_patch.append(len(reboot_calls(fleet_ssh)))
            return await original_patch(self, url, *args, **kwargs)

        with patch.object(FakeKernelStore, "patch", _recording_patch):
            await run_reboot(handlers, approval)
        records = checkpoints(fake_store)
        first_rebooting = next(
            i
            for i, r in enumerate(records)
            if r["hosts"].get("10.0.0.1", {}).get("state") == "rebooting"
        )
        rebooting_record = records[first_rebooting]["hosts"]["10.0.0.1"]
        assert rebooting_record["pre_boot_id"]
        # The checkpoint PATCH that wrote "rebooting" happened before any reboot command.
        transition_patch_indexes = [
            i
            for i, p in enumerate(fake_store.patches)
            if "kernel_transitions" in p.get("fields", {})
        ]
        assert calls_at_patch[transition_patch_indexes[first_rebooting]] == 0

    @pytest.mark.asyncio
    async def test_verified_checkpoint_persisted(self, handlers, fake_store, approval):
        await run_reboot(handlers, approval)
        final = checkpoints(fake_store)[-1]
        assert final["state"] == "verified"
        assert final["kernel"] == KERNEL_B
        assert set(final["hosts"]) == set(HOSTS)
        assert all(h["state"] == "verified" for h in final["hosts"].values())
        assert all(h["post_boot_id"] for h in final["hosts"].values())
        assert final["record_version"] >= 4


class TestRebootFailures:
    @pytest.mark.asyncio
    async def test_never_returns_pauses_ticket(
        self, handlers, two_hosts_ready, fake_store, approval
    ):
        two_hosts_ready["10.0.0.1"].behavior = "never_returns"
        result = await run_reboot(handlers, approval, reconnect_timeout_seconds=3)
        assert result["status"] == "paused"
        assert result["reason_code"] == "reconnect_timeout"
        assert result["stopped_at"] == "10.0.0.1"
        assert result["hosts"]["10.0.0.1"]["state"] == "reconnect_timeout"
        assert result["hosts"]["10.0.0.2"]["state"] == "not_attempted"
        assert "submit_provisioning_result" in result["instruction"]
        assert fake_store.transitions[-1]["status"] == "awaiting_customer_guidance"
        assert checkpoints(fake_store)[-1]["state"] == "failed"

    @pytest.mark.asyncio
    async def test_fallback_boot(self, handlers, two_hosts_ready, approval):
        two_hosts_ready["10.0.0.1"].behavior = "fallback"
        result = await run_reboot(handlers, approval)
        assert result["status"] == "paused"
        assert result["hosts"]["10.0.0.1"]["state"] == "fallback_boot"

    @pytest.mark.asyncio
    async def test_wrong_kernel(self, handlers, two_hosts_ready, approval):
        two_hosts_ready["10.0.0.1"].behavior = "wrong_kernel:6.0.0-1.el9.x86_64"
        result = await run_reboot(handlers, approval)
        assert result["hosts"]["10.0.0.1"]["state"] == "kernel_mismatch"
        assert result["hosts"]["10.0.0.1"]["observed_kernel"] == "6.0.0-1.el9.x86_64"

    @pytest.mark.asyncio
    async def test_ignores_reboot(self, handlers, two_hosts_ready, approval):
        two_hosts_ready["10.0.0.1"].behavior = "ignores_reboot"
        result = await run_reboot(handlers, approval, reconnect_timeout_seconds=3)
        assert result["status"] == "paused"
        # With mocked sleep the grace window may not elapse inside the budget.
        assert result["hosts"]["10.0.0.1"]["state"] in (
            "reboot_not_observed",
            "reconnect_timeout",
        )

    @pytest.mark.asyncio
    async def test_reboot_command_timeout_is_treated_as_sent(
        self, handlers, two_hosts_ready, fleet_ssh, approval
    ):
        """An SSH timeout on the nohup'd reboot means the reboot may well be
        queued: poll for it instead of declaring the host indeterminate."""
        for host in two_hosts_ready.values():
            host.reboot_behavior = "timeout"
        result = await run_reboot(handlers, approval)
        assert result["status"] == "verified"
        assert "timed out" in result["hosts"]["10.0.0.1"]["note"]
        assert len(reboot_calls(fleet_ssh)) == 2

    @pytest.mark.asyncio
    async def test_earlier_failure_preserves_later_hosts_prior_record(
        self, handlers, two_hosts_ready, fake_store, approval
    ):
        """Host 1 fails; host 2 was verified in an earlier invocation.  Its
        record must survive (verified -> not_attempted is illegal and would
        have crashed before the pause)."""
        two_hosts_ready["10.0.0.1"].behavior = "never_returns"
        h2 = two_hosts_ready["10.0.0.2"]
        h2.running = KERNEL_B
        seed_transition(
            fake_store,
            {"10.0.0.2": host_record("10.0.0.2", "verified", post_boot_id=h2.boot_id)},
        )
        result = await run_reboot(handlers, approval, reconnect_timeout_seconds=3)
        assert result["status"] == "paused"
        assert result["hosts"]["10.0.0.2"]["state"] == "verified"
        assert result["hosts"]["10.0.0.2"]["recovery"] == "untouched"
        final = checkpoints(fake_store)[-1]
        assert final["hosts"]["10.0.0.2"]["state"] == "verified"
        assert fake_store.transitions[-1]["status"] == "awaiting_customer_guidance"

    @pytest.mark.asyncio
    async def test_cancellation_mid_reboot_checkpoints_indeterminate(
        self, handlers, two_hosts_ready, fake_store, approval
    ):
        two_hosts_ready["10.0.0.1"].behavior = "cancel_after_reboot"
        with pytest.raises(asyncio.CancelledError):
            await run_reboot(handlers, approval)
        last = checkpoints(fake_store)[-1]
        assert last["hosts"]["10.0.0.1"]["state"] == "indeterminate"
        assert last["hosts"]["10.0.0.1"]["pre_boot_id"]


class TestPrecheckAndPolicy:
    @pytest.mark.asyncio
    async def test_precheck_wrong_default(
        self, handlers, two_hosts_ready, fleet_ssh, approval
    ):
        two_hosts_ready["10.0.0.1"].default = f"/boot/vmlinuz-{KERNEL_A}"
        result = await run_reboot(handlers, approval)
        assert result["hosts"]["10.0.0.1"]["state"] == "precheck_failed"
        assert reboot_calls(fleet_ssh) == []

    @pytest.mark.asyncio
    async def test_if_needed_skips_when_running(
        self, handlers, two_hosts_ready, fleet_ssh, approval
    ):
        for host in two_hosts_ready.values():
            host.running = KERNEL_B
        result = await run_reboot(handlers, approval, reboot_policy="if_needed")
        assert result["status"] == "verified"
        assert all(not h["rebooted"] for h in result["hosts"].values())
        assert reboot_calls(fleet_ssh) == []

    @pytest.mark.asyncio
    async def test_always_reboots_even_if_running(
        self, handlers, two_hosts_ready, fleet_ssh, approval
    ):
        for host in two_hosts_ready.values():
            host.running = KERNEL_B
        result = await run_reboot(handlers, approval, reboot_policy="always")
        assert result["status"] == "verified"
        assert len(reboot_calls(fleet_ssh)) == 2

    @pytest.mark.asyncio
    async def test_parallel_strategy_rejected(self, handlers, approval):
        result = await run_reboot(handlers, approval, strategy="parallel")
        assert result["status"] == "rejected"
        assert result["reason_code"] == "strategy_unsupported"

    @pytest.mark.asyncio
    async def test_invalid_policy_rejected(self, handlers, approval):
        result = await run_reboot(handlers, approval, reboot_policy="sometimes")
        assert result["reason_code"] == "invalid_reboot_policy"


# ---------------------------------------------------------------------------
# Strict host identity
# ---------------------------------------------------------------------------


class TestHostIdentity:
    @pytest.mark.asyncio
    async def test_subset_rejected(self, handlers, fleet_ssh, approval):
        result = await run_reboot(handlers, approval, hosts=["10.0.0.1"])
        assert result["status"] == "rejected"
        assert result["reason_code"] == "host_set_mismatch"
        assert result["missing"] == ["10.0.0.2"]
        assert reboot_calls(fleet_ssh) == []

    @pytest.mark.asyncio
    async def test_controller_in_list_rejected(self, handlers, fleet_ssh, approval):
        result = await run_reboot(handlers, approval, hosts=HOSTS + ["10.0.0.100"])
        assert result["status"] == "rejected"
        assert result["refused"]["10.0.0.100"]["reason"] == "harness_controller"
        assert reboot_calls(fleet_ssh) == []

    @pytest.mark.asyncio
    async def test_unassigned_host_rejected(self, handlers, fleet_ssh, approval):
        result = await run_reboot(handlers, approval, hosts=HOSTS + ["10.7.7.7"])
        assert result["status"] == "rejected"
        assert result["refused"]["10.7.7.7"]["reason"] == "host_not_assigned"

    @pytest.mark.asyncio
    async def test_duplicate_hosts_deduped(self, handlers, fleet_ssh, approval):
        result = await run_reboot(handlers, approval, hosts=HOSTS + ["10.0.0.1"])
        assert result["status"] == "verified"
        assert len(reboot_calls(fleet_ssh, "10.0.0.1")) == 1

    @pytest.mark.asyncio
    async def test_wrong_kernel_rejected(self, handlers, approval):
        result = await run_reboot(handlers, approval, expected_kernel=KERNEL_A)
        assert result["status"] == "rejected"
        assert result["reason_code"].startswith("kernel_mismatch")

    @pytest.mark.asyncio
    async def test_intent_host_set_must_match(self, handlers, fake_store, fleet_ssh):
        from agents.provisioning.kernel import new_intent
        from providers.rhel_kernel import KernelSpec

        intent = new_intent(
            "PERF-TEST", 2, ["10.0.0.1"], KernelSpec(KERNEL_B), ["reboot"], {}
        )
        fake_store.seed_intent(intent)
        approval_id = fake_store.seed_approval(intent)
        result = await run_reboot(handlers, approval_id)
        assert result["status"] == "rejected"
        assert result["reason_code"] == "host_set_mismatch"
        assert reboot_calls(fleet_ssh) == []


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------


class TestApproval:
    @pytest.mark.asyncio
    async def test_consumed_exactly_once(self, handlers, fake_store, approval):
        result = await run_reboot(handlers, approval)
        assert result["status"] == "verified"
        assert len(fake_store.consumes) == 1
        assert fake_store.approvals[approval]["consumed_at"] is not None
        intent_id = fake_store.approvals[approval]["validation_id"]
        assert fake_store.intent(intent_id)["progress"]["reboot"] == "done"

    @pytest.mark.asyncio
    async def test_missing_approval_rejected_before_any_reboot(
        self, handlers, fleet_ssh
    ):
        result = await run_reboot(handlers, "")
        assert result["status"] == "rejected"
        assert result["reason_code"] == "approval_required"
        assert reboot_calls(fleet_ssh) == []

    @pytest.mark.asyncio
    async def test_already_consumed_by_install_is_still_authorized(
        self, handlers, fake_store, fleet_ssh
    ):
        from agents.provisioning.kernel import new_intent
        from providers.rhel_kernel import KernelSpec

        intent = new_intent(
            "PERF-TEST", 2, HOSTS, KernelSpec(KERNEL_B), ["install", "reboot"], {}
        )
        intent["progress"] = {"install": "done", "reboot": "pending"}
        fake_store.seed_intent(intent)
        approval_id = fake_store.seed_approval(intent, consumed=True)
        result = await run_reboot(handlers, approval_id)
        assert result["status"] == "verified"
        assert fake_store.consumes == []
        assert len(reboot_calls(fleet_ssh)) == 2

    @pytest.mark.asyncio
    async def test_reboot_before_install_done_is_out_of_order(
        self, handlers, fake_store, fleet_ssh
    ):
        from agents.provisioning.kernel import new_intent
        from providers.rhel_kernel import KernelSpec

        intent = new_intent(
            "PERF-TEST", 2, HOSTS, KernelSpec(KERNEL_B), ["install", "reboot"], {}
        )
        fake_store.seed_intent(intent)
        approval_id = fake_store.seed_approval(intent)
        result = await run_reboot(handlers, approval_id)
        assert result["reason_code"] == "action_out_of_order"
        assert reboot_calls(fleet_ssh) == []

    @pytest.mark.asyncio
    async def test_pending_approval_rejected(self, handlers, fake_store, fleet_ssh):
        from agents.provisioning.kernel import new_intent
        from providers.rhel_kernel import KernelSpec

        intent = new_intent("PERF-TEST", 2, HOSTS, KernelSpec(KERNEL_B), ["reboot"], {})
        fake_store.seed_intent(intent)
        approval_id = fake_store.seed_approval(intent, status="pending")
        result = await run_reboot(handlers, approval_id)
        assert result["reason_code"] == "approval_not_approved"
        assert reboot_calls(fleet_ssh) == []


# ---------------------------------------------------------------------------
# Recovery table — probe first, never a blind second reboot
# ---------------------------------------------------------------------------


def seed_transition(fake_store, host_records, *, kernel=KERNEL_B):
    fake_store.custom_fields()["kernel_transitions"] = {
        "2": {
            "kernel": kernel,
            "package": "kernel",
            "requested_hosts": HOSTS,
            "strategy": "serial",
            "state": "in_progress",
            "hosts": host_records,
            "record_version": 3,
        }
    }


def host_record(host, state, **extra):
    record = {
        "host": host,
        "state": state,
        "rebooted": state in ("rebooting", "reconnecting", "verified"),
        "pre_boot_id": None,
        "post_boot_id": None,
        "pre_kernel": KERNEL_A,
        "observed_kernel": None,
        "default_entry": f"/boot/vmlinuz-{KERNEL_B}",
        "went_down": False,
        "attempts": 1,
        "started_at": "2026-01-01T00:00:00Z",
        "finished_at": None,
        "error": None,
    }
    record.update(extra)
    return record


class TestRecovery:
    @pytest.mark.asyncio
    async def test_crash_after_reboot_reconciles_without_second_reboot(
        self, handlers, two_hosts_ready, fleet_ssh, fake_store
    ):
        """Checkpoint says rebooting; the host already came back on kernel B."""
        h1 = two_hosts_ready["10.0.0.1"]
        old_boot_id = h1.boot_id
        h1.running = KERNEL_B
        h1.boot_id = "new-boot-id-after-crash"
        seed_transition(
            fake_store,
            {"10.0.0.1": host_record("10.0.0.1", "rebooting", pre_boot_id=old_boot_id)},
        )
        result = await run_reboot(handlers, "")  # no approval: host 2 still needs one
        # host 2 needs a real reboot and there is no approval → rejected before anything.
        assert result["status"] == "rejected"
        assert result["reason_code"] == "approval_required"
        assert reboot_calls(fleet_ssh) == []

    @pytest.mark.asyncio
    async def test_recovery_only_invocation_needs_no_approval(
        self, handlers, two_hosts_ready, fleet_ssh, fake_store
    ):
        """Both hosts already rebooted during a crashed invocation."""
        records = {}
        for ip, host in two_hosts_ready.items():
            old = host.boot_id
            host.running = KERNEL_B
            host.boot_id = f"new-{ip}"
            records[ip] = host_record(
                ip, "reconnecting", pre_boot_id=old, went_down=True
            )
        seed_transition(fake_store, records)
        result = await run_reboot(handlers, "")
        assert result["status"] == "verified", result
        assert all(h["reconciled"] is True for h in result["hosts"].values())
        assert reboot_calls(fleet_ssh) == []
        assert fake_store.consumes == []
        final = checkpoints(fake_store)[-1]
        assert final["state"] == "verified"

    @pytest.mark.asyncio
    async def test_already_verified_host_is_skipped(
        self, handlers, two_hosts_ready, fleet_ssh, fake_store, approval
    ):
        h1 = two_hosts_ready["10.0.0.1"]
        h1.running = KERNEL_B
        seed_transition(
            fake_store,
            {
                "10.0.0.1": host_record(
                    "10.0.0.1",
                    "verified",
                    post_boot_id=h1.boot_id,
                    observed_kernel=KERNEL_B,
                )
            },
        )
        result = await run_reboot(handlers, approval)
        assert result["status"] == "verified"
        assert result["hosts"]["10.0.0.1"]["state"] == "already_verified"
        assert reboot_calls(fleet_ssh, "10.0.0.1") == []
        assert len(reboot_calls(fleet_ssh, "10.0.0.2")) == 1

    @pytest.mark.asyncio
    async def test_stale_post_boot_id_reboots_again_under_always(
        self, handlers, two_hosts_ready, fleet_ssh, fake_store, approval
    ):
        h1 = two_hosts_ready["10.0.0.1"]
        h1.running = KERNEL_B
        seed_transition(
            fake_store,
            {
                "10.0.0.1": host_record(
                    "10.0.0.1",
                    "verified",
                    post_boot_id="stale",
                    observed_kernel=KERNEL_B,
                )
            },
        )
        result = await run_reboot(handlers, approval, reboot_policy="always")
        assert result["status"] == "verified"
        assert len(reboot_calls(fleet_ssh, "10.0.0.1")) == 1
        assert "boot_drift" in result["hosts"]["10.0.0.1"]["note"]

    @pytest.mark.asyncio
    async def test_stale_post_boot_id_refreshes_under_if_needed(
        self, handlers, two_hosts_ready, fleet_ssh, fake_store, approval
    ):
        for host in two_hosts_ready.values():
            host.running = KERNEL_B
        seed_transition(
            fake_store,
            {
                ip: host_record(
                    ip, "verified", post_boot_id="stale", observed_kernel=KERNEL_B
                )
                for ip in HOSTS
            },
        )
        result = await run_reboot(handlers, approval, reboot_policy="if_needed")
        assert result["status"] == "verified"
        assert reboot_calls(fleet_ssh) == []
        assert (
            result["hosts"]["10.0.0.1"]["post_boot_id"]
            == two_hosts_ready["10.0.0.1"].boot_id
        )

    @pytest.mark.asyncio
    async def test_in_flight_reboot_is_not_resent(
        self, handlers, two_hosts_ready, fleet_ssh, fake_store, approval
    ):
        """Checkpoint says rebooting, host still on the old boot: wait, don't resend."""
        h1 = two_hosts_ready["10.0.0.1"]
        h1.behavior = "ignores_reboot"
        seed_transition(
            fake_store,
            {"10.0.0.1": host_record("10.0.0.1", "rebooting", pre_boot_id=h1.boot_id)},
        )
        result = await run_reboot(handlers, approval, reconnect_timeout_seconds=3)
        assert reboot_calls(fleet_ssh, "10.0.0.1") == []
        assert result["hosts"]["10.0.0.1"]["state"] in (
            "reboot_not_observed",
            "reconnect_timeout",
        )
        assert result["hosts"]["10.0.0.1"]["recovery"] == "in_flight"

    @pytest.mark.asyncio
    async def test_reconnecting_and_unreachable_continues_then_times_out(
        self, handlers, two_hosts_ready, fleet_ssh, fake_store, approval
    ):
        h1 = two_hosts_ready["10.0.0.1"]
        h1.behavior = "unreachable"
        seed_transition(
            fake_store,
            {
                "10.0.0.1": host_record(
                    "10.0.0.1", "reconnecting", pre_boot_id="old", went_down=True
                )
            },
        )
        result = await run_reboot(handlers, approval, reconnect_timeout_seconds=3)
        assert result["hosts"]["10.0.0.1"]["state"] == "reconnect_timeout"
        assert result["hosts"]["10.0.0.1"]["recovery"] == "continue_reconnect"
        assert reboot_calls(fleet_ssh) == []

    @pytest.mark.asyncio
    async def test_indeterminate_prior_on_old_boot_is_not_resent(
        self, handlers, two_hosts_ready, fleet_ssh, fake_store, approval
    ):
        """A timed-out reboot was recorded indeterminate; the host still shows
        the old boot_id.  The command may be queued, so never resend it."""
        h1 = two_hosts_ready["10.0.0.1"]
        h1.behavior = "ignores_reboot"
        seed_transition(
            fake_store,
            {
                "10.0.0.1": host_record(
                    "10.0.0.1", "indeterminate", pre_boot_id=h1.boot_id
                )
            },
        )
        result = await run_reboot(handlers, approval, reconnect_timeout_seconds=3)
        assert reboot_calls(fleet_ssh, "10.0.0.1") == []
        assert result["hosts"]["10.0.0.1"]["recovery"] == "in_flight"

    @pytest.mark.asyncio
    async def test_indeterminate_prior_with_fallback_kernel(
        self, handlers, two_hosts_ready, fleet_ssh, fake_store, approval
    ):
        h1 = two_hosts_ready["10.0.0.1"]
        h1.running = KERNEL_A  # came back on the previous kernel
        h1.boot_id = "after"
        seed_transition(
            fake_store,
            {
                "10.0.0.1": host_record(
                    "10.0.0.1",
                    "indeterminate",
                    pre_boot_id="before",
                    pre_kernel=KERNEL_A,
                )
            },
        )
        result = await run_reboot(handlers, approval)
        assert result["hosts"]["10.0.0.1"]["state"] == "fallback_boot"
        assert reboot_calls(fleet_ssh) == []

    @pytest.mark.asyncio
    async def test_verified_prior_but_wrong_kernel_now_reboots(
        self, handlers, two_hosts_ready, fleet_ssh, fake_store, approval
    ):
        h1 = two_hosts_ready["10.0.0.1"]
        seed_transition(
            fake_store,
            {
                "10.0.0.1": host_record(
                    "10.0.0.1",
                    "verified",
                    post_boot_id=h1.boot_id,
                    observed_kernel=KERNEL_B,
                )
            },
        )
        # Live host runs kernel A again.
        result = await run_reboot(handlers, approval)
        assert result["status"] == "verified"
        assert len(reboot_calls(fleet_ssh, "10.0.0.1")) == 1

    @pytest.mark.asyncio
    async def test_checkpoint_for_other_kernel_is_replaced(
        self, handlers, fleet_ssh, fake_store, approval
    ):
        seed_transition(
            fake_store,
            {ip: host_record(ip, "verified", post_boot_id="x") for ip in HOSTS},
            kernel=KERNEL_A,
        )
        result = await run_reboot(handlers, approval)
        assert result["status"] == "verified"
        assert len(reboot_calls(fleet_ssh)) == 2
        assert checkpoints(fake_store)[-1]["kernel"] == KERNEL_B


# ---------------------------------------------------------------------------
# verify_kernel_state
# ---------------------------------------------------------------------------


class TestVerifyKernelState:
    @pytest.mark.asyncio
    async def test_reconciles_checkpoint_when_running_expected(
        self, handlers, two_hosts_ready, fake_store
    ):
        for host in two_hosts_ready.values():
            host.running = KERNEL_B
        seed_transition(
            fake_store,
            {
                ip: host_record(ip, "reconnect_timeout", pre_boot_id="old")
                for ip in HOSTS
            },
        )
        result = await handlers["verify_kernel_state"](
            hosts=HOSTS, expected_kernel=KERNEL_B
        )
        assert result["results"]["10.0.0.1"]["state"] == "verified"
        assert result["results"]["10.0.0.1"]["reconciled"] is True
        assert result["transition_state"] == "verified"
        final = checkpoints(fake_store)[-1]
        assert final["state"] == "verified"
        assert final["hosts"]["10.0.0.1"]["reconciled"] is True
        assert (
            final["hosts"]["10.0.0.1"]["post_boot_id"]
            == two_hosts_ready["10.0.0.1"].boot_id
        )

    @pytest.mark.asyncio
    async def test_mismatch_when_running_other(self, handlers, fake_store):
        result = await handlers["verify_kernel_state"](
            hosts=HOSTS, expected_kernel=KERNEL_B
        )
        assert result["results"]["10.0.0.1"]["state"] == "kernel_mismatch"
        assert result["results"]["10.0.0.1"]["running"] == KERNEL_A
        assert result["transition_state"] != "verified"

    @pytest.mark.asyncio
    async def test_subset_is_rejected_so_stale_records_cannot_hide(
        self, handlers, two_hosts_ready, fake_store, fleet_ssh
    ):
        """Reconciling only some hosts could leave an old verified record for
        an unprobed host behind the gate; the tool requires full coverage."""
        two_hosts_ready["10.0.0.1"].running = KERNEL_B
        seed_transition(
            fake_store,
            {
                "10.0.0.1": host_record("10.0.0.1", "kernel_mismatch"),
                "10.0.0.2": host_record("10.0.0.2", "verified", post_boot_id="stale"),
            },
        )
        result = await handlers["verify_kernel_state"](
            hosts=["10.0.0.1"], expected_kernel=KERNEL_B
        )
        assert result["status"] == "rejected"
        assert result["reason_code"] == "host_set_mismatch"
        assert result["missing"] == ["10.0.0.2"]
        assert not any("@@boot_id" in c["command"] for c in fleet_ssh.calls)
        assert checkpoints(fake_store) == []

    @pytest.mark.asyncio
    async def test_never_reboots_and_needs_no_approval(
        self, handlers, fleet_ssh, fake_store
    ):
        await handlers["verify_kernel_state"](hosts=HOSTS, expected_kernel=KERNEL_B)
        assert reboot_calls(fleet_ssh) == []
        assert fake_store.consumes == []

    @pytest.mark.asyncio
    async def test_unassigned_host_refused(self, handlers, fleet_ssh):
        result = await handlers["verify_kernel_state"](
            hosts=HOSTS + ["10.5.5.5"], expected_kernel=KERNEL_B
        )
        assert result["status"] == "rejected"
        assert result["refused"]["10.5.5.5"]["reason"] == "host_not_assigned"
        assert not any(c["host"] == "10.5.5.5" for c in fleet_ssh.calls)

    @pytest.mark.asyncio
    async def test_reconciles_every_host_and_supersedes_failed_records(
        self, handlers, two_hosts_ready, fake_store
    ):
        for host in two_hosts_ready.values():
            host.running = KERNEL_B
        seed_transition(
            fake_store,
            {
                "10.0.0.1": host_record("10.0.0.1", "kernel_mismatch"),
                "10.0.0.2": host_record("10.0.0.2", "verified", post_boot_id="stale"),
            },
        )
        result = await handlers["verify_kernel_state"](
            hosts=HOSTS, expected_kernel=KERNEL_B
        )
        assert result["transition_state"] == "verified"
        final = checkpoints(fake_store)[-1]
        assert (
            final["hosts"]["10.0.0.2"]["post_boot_id"]
            == two_hosts_ready["10.0.0.2"].boot_id
        )


# ---------------------------------------------------------------------------
# Checkpoint writer and state table
# ---------------------------------------------------------------------------


class TestCheckpointWriter:
    @pytest.mark.asyncio
    async def test_overwritten_checkpoint_raises(self, fake_store):
        from agents.provisioning.kernel import new_transition, write_transition
        from providers.rhel_kernel import KernelSpec

        fake_store.tamper_versions = True
        transition = new_transition(
            2, KernelSpec(KERNEL_B), HOSTS, intent_id=None, approval_request_id=None
        )
        with pytest.raises(RuntimeError, match="overwritten"):
            await write_transition("PERF-TEST", 2, transition)

    @pytest.mark.asyncio
    async def test_fresh_read_before_every_write(self, fake_store):
        from agents.provisioning.kernel import new_transition, write_transition
        from providers.rhel_kernel import KernelSpec

        transition = new_transition(
            2, KernelSpec(KERNEL_B), HOSTS, intent_id=None, approval_request_id=None
        )
        await write_transition("PERF-TEST", 2, transition)
        await write_transition("PERF-TEST", 2, transition)
        versions = [r["record_version"] for r in checkpoints(fake_store)]
        assert versions == [1, 2]

    def test_state_table_covers_thirteen_states(self):
        from providers.rhel_kernel import (
            HOST_STATE_TRANSITIONS,
            HOST_STATES,
            TERMINAL_STATES,
            validate_host_transition,
        )

        assert len(HOST_STATES) == 13
        assert TERMINAL_STATES < HOST_STATES
        for source, targets in HOST_STATE_TRANSITIONS.items():
            assert source in HOST_STATES
            assert targets <= HOST_STATES
        validate_host_transition("pending", "rebooting")
        validate_host_transition("verified", "verified")
        with pytest.raises(ValueError, match="illegal"):
            validate_host_transition("verified", "rebooting")
        with pytest.raises(ValueError, match="unknown"):
            validate_host_transition("pending", "exploded")

    def test_recovery_decisions_are_closed_set(self):
        from providers.rhel_kernel import (
            RECOVERY_DECISIONS,
            KernelSpec,
            ProbeResult,
            recover_host,
        )

        spec = KernelSpec(KERNEL_B)
        probe = ProbeResult(boot_id="b2", kernel=KERNEL_B, default_entry=spec.vmlinuz)
        cases = [
            (None, probe, True, "always"),
            ({"state": "verified", "post_boot_id": "b2"}, probe, True, "always"),
            ({"state": "verified", "post_boot_id": "b1"}, probe, True, "if_needed"),
            ({"state": "rebooting", "pre_boot_id": "b1"}, probe, True, "always"),
            ({"state": "rebooting", "pre_boot_id": "b2"}, probe, True, "always"),
            ({"state": "reconnecting", "pre_boot_id": "b1"}, None, False, "always"),
            (
                {"state": "rebooting", "pre_boot_id": "b1", "pre_kernel": KERNEL_B},
                probe,
                True,
                "always",
            ),
            ({"state": "precheck_failed"}, probe, True, "always"),
        ]
        for prior, p, reachable, policy in cases:
            decision, _ = recover_host(prior, p, reachable, spec, policy)
            assert decision in RECOVERY_DECISIONS
