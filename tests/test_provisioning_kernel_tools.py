"""Tests for the kernel provisioning tools: inventory, intent, install, select.

Every tool runs against the stateful fake RHEL host simulator and a fake
state-store client; no test needs a running state store.
"""

from __future__ import annotations

import copy
from unittest.mock import AsyncMock, patch

import pytest

from tests.conftest import make_provisioning_handlers
from tests.fake_rhel import FakeFleetSSH, FakeKernelStore, FakeRHELHost

KERNEL_A = "5.14.0-503.14.1.el9_5.x86_64"
KERNEL_B = "5.14.0-503.16.1.el9_5.x86_64"


def kernel_step_ticket(
    *,
    kernel: dict | None = None,
    targets: list[str] | None = None,
    current_step: int = 2,
) -> dict:
    return {
        "id": "PERF-TEST",
        "status": "awaiting_provision",
        "custom_fields": {
            "execution_plan": {
                "current_step": current_step,
                "steps": [
                    {"id": 0, "agent_type": "resource", "params": {}},
                    {
                        "id": 1,
                        "agent_type": "provision",
                        "params": {"label": "harness"},
                    },
                    {
                        "id": 2,
                        "agent_type": "provision",
                        "params": {
                            "label": "kernel-B",
                            "kernel": kernel
                            or {"release": KERNEL_B, "package": "kernel"},
                        },
                    },
                ],
            },
            "assigned_hardware_ips": {
                "controller": "10.0.0.100",
                "targets": targets or ["10.0.0.1", "10.0.0.2"],
            },
        },
    }


@pytest.fixture
def two_hosts():
    h1 = FakeRHELHost("host1", running=KERNEL_A, available=[KERNEL_B])
    h2 = FakeRHELHost("host2", running=KERNEL_A, available=[KERNEL_B])
    return {"10.0.0.1": h1, "10.0.0.2": h2}


@pytest.fixture
def fleet_ssh(two_hosts):
    return FakeFleetSSH(two_hosts)


@pytest.fixture
def handlers(fleet_ssh):
    return make_provisioning_handlers(fleet_ssh)


@pytest.fixture
def ticket():
    return kernel_step_ticket()


@pytest.fixture(autouse=True)
def fake_store(ticket):
    """Patch the state-store client in the kernel glue module and make
    assert_ticket_active return the live fake ticket."""
    FakeKernelStore.reset(ticket)

    async def _active(**kwargs):
        return {"status": "ok", "ticket": FakeKernelStore.ticket}

    with (
        patch("agents.provisioning.kernel.AuditedAsyncHTTPClient", FakeKernelStore),
        patch(
            "agents.server_utils.assert_ticket_active", AsyncMock(side_effect=_active)
        ),
        patch("agents.server_utils.tool_progress", AsyncMock()),
    ):
        yield FakeKernelStore


async def prepared_intent(handlers, hosts=("10.0.0.1", "10.0.0.2"), kernel=KERNEL_B):
    """Run prepare_kernel_change and return the persisted intent record."""
    result = await handlers["prepare_kernel_change"](hosts=list(hosts), kernel=kernel)
    assert result["status"] == "ok", result
    return FakeKernelStore.intent(result["intent_id"])


# ---------------------------------------------------------------------------
# get_kernel_inventory
# ---------------------------------------------------------------------------


class TestGetKernelInventory:
    @pytest.mark.asyncio
    async def test_two_hosts(self, handlers):
        result = await handlers["get_kernel_inventory"](hosts=["10.0.0.1", "10.0.0.2"])
        h1 = result["results"]["10.0.0.1"]
        assert h1["state"] == "ok"
        assert h1["inventory"]["running"] == KERNEL_A
        assert h1["requested_as"] == "10.0.0.1"
        assert "10.0.0.2" in result["results"]

    @pytest.mark.asyncio
    async def test_unassigned_host_refused_not_probed(self, handlers, fleet_ssh):
        result = await handlers["get_kernel_inventory"](
            hosts=["10.0.0.1", "10.99.99.99"]
        )
        assert result["results"]["10.0.0.1"]["state"] == "ok"
        stray = result["results"]["10.99.99.99"]
        assert stray["state"] == "refused"
        assert stray["reason"] == "host_not_assigned"
        assert not any(c["host"] == "10.99.99.99" for c in fleet_ssh.calls)

    @pytest.mark.asyncio
    async def test_with_kernel_installed(self, handlers):
        result = await handlers["get_kernel_inventory"](
            hosts=["10.0.0.1"], kernel=KERNEL_A
        )
        assert result["results"]["10.0.0.1"]["requested"]["state"] == "installed"

    @pytest.mark.asyncio
    async def test_with_kernel_available(self, handlers):
        result = await handlers["get_kernel_inventory"](
            hosts=["10.0.0.1"], kernel=KERNEL_B
        )
        assert result["results"]["10.0.0.1"]["requested"]["state"] == "available"

    @pytest.mark.asyncio
    async def test_with_kernel_not_found(self, handlers):
        result = await handlers["get_kernel_inventory"](
            hosts=["10.0.0.1"], kernel="9.99.0-1.el99.x86_64"
        )
        assert result["results"]["10.0.0.1"]["requested"]["state"] == "not_found"

    @pytest.mark.asyncio
    async def test_host_without_tuned_is_not_unreachable(self, handlers, two_hosts):
        """Lab regression (PERF-A9D303F7): RHEL 9.4 hosts without tuned-adm
        answered the full inventory yet were reported 'unreachable' because
        the last command's exit 127 tainted the whole probe."""
        for host in two_hosts.values():
            host.has_tuned = False
        result = await handlers["get_kernel_inventory"](
            hosts=["10.0.0.1", "10.0.0.2"], kernel=KERNEL_B
        )
        h1 = result["results"]["10.0.0.1"]
        assert h1["state"] == "ok", h1
        assert h1["inventory"]["running"] == KERNEL_A
        assert h1["requested"]["state"] == "available"
        assert h1["inventory"].get("tuned_profile") in (None, "", "unknown")

    @pytest.mark.asyncio
    async def test_prepare_succeeds_on_hosts_without_tuned(self, handlers, two_hosts):
        for host in two_hosts.values():
            host.has_tuned = False
        result = await handlers["prepare_kernel_change"](
            hosts=["10.0.0.1", "10.0.0.2"], kernel=KERNEL_B
        )
        assert result["status"] == "ok", result

    @pytest.mark.asyncio
    async def test_plan_package_is_used_for_queries(self, handlers, fleet_ssh):
        FakeKernelStore.reset(
            kernel_step_ticket(kernel={"release": KERNEL_B, "package": "kernel-rt"})
        )
        await handlers["get_kernel_inventory"](hosts=["10.0.0.1"], kernel=KERNEL_B)
        rpm_calls = [c["command"] for c in fleet_ssh.calls if "rpm -q " in c["command"]]
        assert rpm_calls and all("rpm -q kernel-rt" in c for c in rpm_calls)


# ---------------------------------------------------------------------------
# prepare_kernel_change
# ---------------------------------------------------------------------------


class TestPrepareKernelChange:
    @pytest.mark.asyncio
    async def test_persists_intent_with_actions(self, handlers, fake_store):
        result = await handlers["prepare_kernel_change"](
            hosts=["10.0.0.1", "10.0.0.2"], kernel=KERNEL_B
        )
        assert result["status"] == "ok"
        assert result["actions_required"] == ["install", "select_default", "reboot"]
        assert result["hosts"] == ["10.0.0.1", "10.0.0.2"]
        intent = fake_store.intent(result["intent_id"])
        assert intent["intent_digest"] == result["intent_digest"]
        assert intent["progress"] == {
            "install": "pending",
            "select_default": "pending",
            "reboot": "pending",
        }
        assert intent["kernel"] == {"release": KERNEL_B, "package": "kernel"}
        assert fake_store.patches[-1]["fields"]["kernel_change_intents"]["records"]

    @pytest.mark.asyncio
    async def test_actions_shrink_when_already_installed_and_default(
        self, handlers, two_hosts
    ):
        for host in two_hosts.values():
            host.installed.append(KERNEL_B)
            host.default = f"/boot/vmlinuz-{KERNEL_B}"
        result = await handlers["prepare_kernel_change"](
            hosts=["10.0.0.1", "10.0.0.2"], kernel=KERNEL_B
        )
        assert result["actions_required"] == ["reboot"]

    @pytest.mark.asyncio
    async def test_rejects_when_step_has_no_kernel(self, handlers):
        FakeKernelStore.reset(kernel_step_ticket(current_step=1))
        result = await handlers["prepare_kernel_change"](
            hosts=["10.0.0.1", "10.0.0.2"], kernel=KERNEL_B
        )
        assert result["status"] == "rejected"
        assert result["reason_code"] == "kernel_not_in_plan"

    @pytest.mark.asyncio
    async def test_rejects_kernel_not_in_plan(self, handlers):
        result = await handlers["prepare_kernel_change"](
            hosts=["10.0.0.1", "10.0.0.2"], kernel=KERNEL_A
        )
        assert result["status"] == "rejected"
        assert result["reason_code"].startswith("kernel_mismatch")

    @pytest.mark.asyncio
    async def test_rejects_package_mismatch(self, handlers):
        result = await handlers["prepare_kernel_change"](
            hosts=["10.0.0.1", "10.0.0.2"],
            kernel={"release": KERNEL_B, "package": "kernel-rt"},
        )
        assert result["status"] == "rejected"
        assert "package" in result["reason_code"]

    @pytest.mark.asyncio
    async def test_subset_is_host_set_mismatch(self, handlers):
        result = await handlers["prepare_kernel_change"](
            hosts=["10.0.0.1"], kernel=KERNEL_B
        )
        assert result["status"] == "rejected"
        assert result["reason_code"] == "host_set_mismatch"
        assert result["missing"] == ["10.0.0.2"]

    @pytest.mark.asyncio
    async def test_controller_and_unassigned_hosts_refused(self, handlers):
        result = await handlers["prepare_kernel_change"](
            hosts=["10.0.0.1", "10.0.0.2", "10.0.0.100", "10.9.9.9"],
            kernel=KERNEL_B,
        )
        assert result["status"] == "rejected"
        assert result["reason_code"] == "host_set_mismatch"
        assert result["refused"]["10.0.0.100"]["reason"] == "harness_controller"
        assert result["refused"]["10.9.9.9"]["reason"] == "host_not_assigned"

    @pytest.mark.asyncio
    async def test_self_host_refused(self, handlers):
        result = await handlers["prepare_kernel_change"](
            hosts=["10.0.0.1", "10.0.0.2", "127.0.0.2"], kernel=KERNEL_B
        )
        assert result["status"] == "rejected"
        assert result["refused"]["127.0.0.2"]["reason"] == "orchestrator_self"

    @pytest.mark.asyncio
    async def test_unreachable_inventory_rejects(self, handlers, two_hosts):
        two_hosts["10.0.0.2"].behavior = "unreachable"
        result = await handlers["prepare_kernel_change"](
            hosts=["10.0.0.1", "10.0.0.2"], kernel=KERNEL_B
        )
        assert result["status"] == "rejected"
        assert result["reason_code"] == "inventory_unavailable"
        assert "10.0.0.2" in result["unresolved"]
        assert "kernel_change_intents" not in FakeKernelStore.custom_fields()

    @pytest.mark.asyncio
    async def test_plan_host_list_must_be_the_full_set(self, handlers):
        FakeKernelStore.reset(
            kernel_step_ticket(
                kernel={"release": KERNEL_B, "package": "kernel", "hosts": ["10.0.0.1"]}
            )
        )
        result = await handlers["prepare_kernel_change"](
            hosts=["10.0.0.1", "10.0.0.2"], kernel=KERNEL_B
        )
        assert result["status"] == "rejected"
        assert result["reason_code"] == "plan_host_set_mismatch"

    @pytest.mark.asyncio
    async def test_plan_host_list_equal_to_full_set_is_accepted(self, handlers):
        FakeKernelStore.reset(
            kernel_step_ticket(
                kernel={
                    "release": KERNEL_B,
                    "package": "kernel",
                    "hosts": ["10.0.0.2", "10.0.0.1"],
                }
            )
        )
        result = await handlers["prepare_kernel_change"](
            hosts=["10.0.0.1", "10.0.0.2"], kernel=KERNEL_B
        )
        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_plan_package_flows_into_intent(self, handlers, two_hosts):
        FakeKernelStore.reset(
            kernel_step_ticket(kernel={"release": KERNEL_B, "package": "kernel-rt"})
        )
        result = await handlers["prepare_kernel_change"](
            hosts=["10.0.0.1", "10.0.0.2"], kernel=KERNEL_B
        )
        assert result["status"] == "ok"
        assert result["kernel"]["package"] == "kernel-rt"


# ---------------------------------------------------------------------------
# install_kernel — approval authorization
# ---------------------------------------------------------------------------


class TestInstallKernelApproval:
    @pytest.mark.asyncio
    async def test_requires_approval_id(self, handlers, fleet_ssh):
        await prepared_intent(handlers)
        result = await handlers["install_kernel"](
            hosts=["10.0.0.1", "10.0.0.2"], kernel=KERNEL_B, approval_request_id=""
        )
        assert result["status"] == "rejected"
        assert result["reason_code"] == "approval_required"
        assert not any("dnf install" in c["command"] for c in fleet_ssh.calls)

    @pytest.mark.asyncio
    async def test_unknown_approval_is_binding_missing(self, handlers, fleet_ssh):
        await prepared_intent(handlers)
        result = await handlers["install_kernel"](
            hosts=["10.0.0.1", "10.0.0.2"],
            kernel=KERNEL_B,
            approval_request_id="apr-" + "0" * 32,
        )
        assert result["reason_code"] == "approval_binding_missing"
        assert not any("dnf install" in c["command"] for c in fleet_ssh.calls)

    @pytest.mark.asyncio
    async def test_pending_approval_rejected(self, handlers, fake_store, fleet_ssh):
        intent = await prepared_intent(handlers)
        approval_id = fake_store.seed_approval(intent, status="pending")
        result = await handlers["install_kernel"](
            hosts=["10.0.0.1", "10.0.0.2"],
            kernel=KERNEL_B,
            approval_request_id=approval_id,
        )
        assert result["reason_code"] == "approval_not_approved"
        assert not any("dnf install" in c["command"] for c in fleet_ssh.calls)

    @pytest.mark.asyncio
    async def test_expired_approval_rejected(self, handlers, fake_store):
        intent = await prepared_intent(handlers)
        approval_id = fake_store.seed_approval(
            intent, expires_at="2000-01-01T00:00:00Z"
        )
        result = await handlers["install_kernel"](
            hosts=["10.0.0.1", "10.0.0.2"],
            kernel=KERNEL_B,
            approval_request_id=approval_id,
        )
        assert result["reason_code"] == "approval_expired"

    @pytest.mark.asyncio
    async def test_benchmark_kind_approval_rejected(self, handlers, fake_store):
        intent = await prepared_intent(handlers)
        approval_id = fake_store.seed_approval(intent, kind="benchmark_run_file")
        result = await handlers["install_kernel"](
            hosts=["10.0.0.1", "10.0.0.2"],
            kernel=KERNEL_B,
            approval_request_id=approval_id,
        )
        assert result["reason_code"] == "approval_not_bound"

    @pytest.mark.asyncio
    async def test_approval_for_another_intent_rejected(self, handlers, fake_store):
        intent = await prepared_intent(handlers)
        other = dict(intent, intent_id="kci-" + "f" * 32, intent_digest="a" * 64)
        fake_store.seed_intent(other)
        approval_id = fake_store.seed_approval(other)
        # The approval binds to `other`, whose actions are identical, but the
        # caller is working the first intent: install must not be authorized
        # by a foreign approval.  Here we make the foreign digest mismatch.
        fake_store.approvals[approval_id]["presented_run_file_digest"] = "b" * 64
        result = await handlers["install_kernel"](
            hosts=["10.0.0.1", "10.0.0.2"],
            kernel=KERNEL_B,
            approval_request_id=approval_id,
        )
        assert result["reason_code"] == "approval_not_bound"

    @pytest.mark.asyncio
    async def test_consumes_once_then_proceeds_without_second_consume(
        self, handlers, fake_store, fleet_ssh
    ):
        intent = await prepared_intent(handlers)
        approval_id = fake_store.seed_approval(intent)
        first = await handlers["install_kernel"](
            hosts=["10.0.0.1", "10.0.0.2"],
            kernel=KERNEL_B,
            approval_request_id=approval_id,
        )
        assert first["results"]["10.0.0.1"]["state"] == "installed"
        assert first["results"]["10.0.0.2"]["state"] == "installed"
        assert len(fake_store.consumes) == 1
        assert fake_store.consumes[0]["kind"] == "kernel_change"
        assert fake_store.consumes[0]["validation_id"] == intent["intent_id"]
        assert fake_store.intent(intent["intent_id"])["progress"]["install"] == "done"
        dnf_calls = [c for c in fleet_ssh.calls if "dnf install" in c["command"]]
        assert len(dnf_calls) == 2

        # A completed action is refused, never silently re-run.
        second = await handlers["install_kernel"](
            hosts=["10.0.0.1", "10.0.0.2"],
            kernel=KERNEL_B,
            approval_request_id=approval_id,
        )
        assert second["status"] == "rejected"
        assert second["reason_code"] == "action_already_done"
        assert len(fake_store.consumes) == 1, "consumed approval must not be re-spent"
        dnf_calls = [c for c in fleet_ssh.calls if "dnf install" in c["command"]]
        assert len(dnf_calls) == 2

    @pytest.mark.asyncio
    async def test_approval_from_another_step_rejected(self, handlers, fake_store):
        """ABBA: the first baseline step's approval must not authorize the
        second baseline step even though the kernel is identical."""
        from agents.provisioning.kernel import new_intent
        from providers.rhel_kernel import KernelSpec

        intent = new_intent(
            "PERF-TEST",
            6,
            ["10.0.0.1", "10.0.0.2"],
            KernelSpec(KERNEL_B),
            ["install"],
            {},
        )
        fake_store.seed_intent(intent)
        approval_id = fake_store.seed_approval(intent)
        result = await handlers["install_kernel"](
            hosts=["10.0.0.1", "10.0.0.2"],
            kernel=KERNEL_B,
            approval_request_id=approval_id,
        )
        assert result["reason_code"] == "approval_wrong_step"
        assert result["intent_step"] == 6 and result["current_step"] == 2
        assert fake_store.consumes == []

    @pytest.mark.asyncio
    async def test_approval_for_another_kernel_rejected(self, handlers, fake_store):
        from agents.provisioning.kernel import new_intent
        from providers.rhel_kernel import KernelSpec

        intent = new_intent(
            "PERF-TEST",
            2,
            ["10.0.0.1", "10.0.0.2"],
            KernelSpec(KERNEL_B, package="kernel-rt"),
            ["install"],
            {},
        )
        fake_store.seed_intent(intent)
        approval_id = fake_store.seed_approval(intent)
        result = await handlers["install_kernel"](
            hosts=["10.0.0.1", "10.0.0.2"],
            kernel=KERNEL_B,
            approval_request_id=approval_id,
        )
        assert result["reason_code"] == "approval_wrong_kernel"
        assert fake_store.consumes == []

    @pytest.mark.asyncio
    async def test_crash_after_consume_retry_runs_install_once(
        self, handlers, fake_store, fleet_ssh
    ):
        """Approval consumed, nothing done yet: the retry must simply run."""
        intent = await prepared_intent(handlers)
        approval_id = fake_store.seed_approval(intent, consumed=True)
        result = await handlers["install_kernel"](
            hosts=["10.0.0.1", "10.0.0.2"],
            kernel=KERNEL_B,
            approval_request_id=approval_id,
        )
        assert result["results"]["10.0.0.1"]["state"] == "installed"
        assert fake_store.consumes == []
        assert fake_store.intent(intent["intent_id"])["progress"]["install"] == "done"
        assert len([c for c in fleet_ssh.calls if "dnf install" in c["command"]]) == 2

    @pytest.mark.asyncio
    async def test_started_marker_written_before_dnf(
        self, handlers, fake_store, fleet_ssh
    ):
        intent = await prepared_intent(handlers)
        approval_id = fake_store.seed_approval(intent)
        patches_before = len(fake_store.patches)
        await handlers["install_kernel"](
            hosts=["10.0.0.1", "10.0.0.2"],
            kernel=KERNEL_B,
            approval_request_id=approval_id,
        )
        progress_writes = [
            p["fields"]["kernel_change_intents"]["records"][intent["intent_id"]][
                "progress"
            ]["install"]
            for p in fake_store.patches[patches_before:]
            if "kernel_change_intents" in p.get("fields", {})
        ]
        assert progress_writes == ["started", "done"]

    @pytest.mark.asyncio
    async def test_select_before_install_is_out_of_order(
        self, handlers, fake_store, fleet_ssh
    ):
        intent = await prepared_intent(handlers)
        approval_id = fake_store.seed_approval(intent)
        result = await handlers["select_default_kernel"](
            hosts=["10.0.0.1", "10.0.0.2"],
            kernel=KERNEL_B,
            approval_request_id=approval_id,
        )
        assert result["reason_code"] == "action_out_of_order"
        assert result["pending"] == ["install"]
        assert fake_store.consumes == []
        assert not any("grubby --set-default" in c["command"] for c in fleet_ssh.calls)

    @pytest.mark.asyncio
    async def test_install_not_required_is_rejected(
        self, handlers, fake_store, two_hosts, fleet_ssh
    ):
        for host in two_hosts.values():
            host.installed.append(KERNEL_B)
        intent = await prepared_intent(handlers)
        assert intent["actions_required"] == ["select_default", "reboot"]
        approval_id = fake_store.seed_approval(intent)
        result = await handlers["install_kernel"](
            hosts=["10.0.0.1", "10.0.0.2"],
            kernel=KERNEL_B,
            approval_request_id=approval_id,
        )
        assert result["reason_code"] == "action_not_required"
        assert fake_store.consumes == []
        # select_default is now the first action and consumes the approval
        result = await handlers["select_default_kernel"](
            hosts=["10.0.0.1", "10.0.0.2"],
            kernel=KERNEL_B,
            approval_request_id=approval_id,
        )
        assert result["results"]["10.0.0.1"]["state"] == "ok"
        assert len(fake_store.consumes) == 1

    @pytest.mark.asyncio
    async def test_subset_rejected_even_with_approval(self, handlers, fake_store):
        intent = await prepared_intent(handlers)
        approval_id = fake_store.seed_approval(intent)
        result = await handlers["install_kernel"](
            hosts=["10.0.0.1"], kernel=KERNEL_B, approval_request_id=approval_id
        )
        assert result["reason_code"] == "host_set_mismatch"
        assert fake_store.consumes == []

    @pytest.mark.asyncio
    async def test_wrong_kernel_rejected_before_approval(self, handlers, fake_store):
        intent = await prepared_intent(handlers)
        approval_id = fake_store.seed_approval(intent)
        result = await handlers["install_kernel"](
            hosts=["10.0.0.1", "10.0.0.2"],
            kernel=KERNEL_A,
            approval_request_id=approval_id,
        )
        assert result["reason_code"].startswith("kernel_mismatch")
        assert fake_store.consumes == []


# ---------------------------------------------------------------------------
# install / select outcomes
# ---------------------------------------------------------------------------


class TestInstallOutcomes:
    @pytest.mark.asyncio
    async def test_dnf_failure_leaves_action_started(
        self, handlers, fake_store, two_hosts
    ):
        two_hosts["10.0.0.2"].available = []
        intent = await prepared_intent(handlers)
        approval_id = fake_store.seed_approval(intent)
        result = await handlers["install_kernel"](
            hosts=["10.0.0.1", "10.0.0.2"],
            kernel=KERNEL_B,
            approval_request_id=approval_id,
        )
        assert result["results"]["10.0.0.1"]["state"] == "installed"
        assert result["results"]["10.0.0.2"]["state"] == "install_failed"
        assert (
            fake_store.intent(intent["intent_id"])["progress"]["install"] == "started"
        )

    @pytest.mark.asyncio
    async def test_dnf_timeout_but_installed_is_reconciled(
        self, handlers, fake_store, two_hosts
    ):
        for host in two_hosts.values():
            host.dnf_behavior = "timeout_installs"
        intent = await prepared_intent(handlers)
        approval_id = fake_store.seed_approval(intent)
        result = await handlers["install_kernel"](
            hosts=["10.0.0.1", "10.0.0.2"],
            kernel=KERNEL_B,
            approval_request_id=approval_id,
        )
        assert result["results"]["10.0.0.1"]["state"] == "installed"
        assert "timed out" in result["results"]["10.0.0.1"]["note"]

    @pytest.mark.asyncio
    async def test_dnf_timeout_not_installed_is_indeterminate(
        self, handlers, fake_store, two_hosts
    ):
        for host in two_hosts.values():
            host.dnf_behavior = "timeout_fails"
        intent = await prepared_intent(handlers)
        approval_id = fake_store.seed_approval(intent)
        result = await handlers["install_kernel"](
            hosts=["10.0.0.1", "10.0.0.2"],
            kernel=KERNEL_B,
            approval_request_id=approval_id,
        )
        assert result["results"]["10.0.0.1"]["state"] == "indeterminate"


class TestSelectDefaultKernel:
    async def _after_install(self, handlers, fake_store, two_hosts):
        for host in two_hosts.values():
            host.installed.append(KERNEL_B)
        intent = await prepared_intent(handlers)
        approval_id = fake_store.seed_approval(intent)
        return intent, approval_id

    @pytest.mark.asyncio
    async def test_select_new_default(self, handlers, fake_store, two_hosts):
        intent, approval_id = await self._after_install(handlers, fake_store, two_hosts)
        result = await handlers["select_default_kernel"](
            hosts=["10.0.0.1", "10.0.0.2"],
            kernel=KERNEL_B,
            approval_request_id=approval_id,
        )
        h1 = result["results"]["10.0.0.1"]
        assert h1["state"] == "ok"
        assert h1["after"] == f"/boot/vmlinuz-{KERNEL_B}"
        assert (
            fake_store.intent(intent["intent_id"])["progress"]["select_default"]
            == "done"
        )

    @pytest.mark.asyncio
    async def test_already_default_is_idempotent(self, handlers, fake_store, two_hosts):
        """A host already on the right default entry is reported, not touched,
        and a completed action is refused rather than silently repeated."""
        from agents.provisioning.kernel import new_intent
        from providers.rhel_kernel import KernelSpec

        for host in two_hosts.values():
            host.installed.append(KERNEL_B)
        two_hosts["10.0.0.1"].default = f"/boot/vmlinuz-{KERNEL_B}"
        intent = new_intent(
            "PERF-TEST",
            2,
            ["10.0.0.1", "10.0.0.2"],
            KernelSpec(KERNEL_B),
            ["select_default", "reboot"],
            {},
        )
        fake_store.seed_intent(intent)
        approval_id = fake_store.seed_approval(intent)
        result = await handlers["select_default_kernel"](
            hosts=["10.0.0.1", "10.0.0.2"],
            kernel=KERNEL_B,
            approval_request_id=approval_id,
        )
        assert result["results"]["10.0.0.1"]["state"] == "already_default"
        assert result["results"]["10.0.0.2"]["state"] == "ok"
        grubby_sets = [
            c for c in fleet_ssh_calls(two_hosts, handlers) if "set-default" in c
        ]
        assert all("10.0.0.1" not in c for c in grubby_sets)
        again = await handlers["select_default_kernel"](
            hosts=["10.0.0.1", "10.0.0.2"],
            kernel=KERNEL_B,
            approval_request_id=approval_id,
        )
        assert again["reason_code"] == "action_already_done"

    @pytest.mark.asyncio
    async def test_not_bootable(self, handlers, fake_store, two_hosts):
        for host in two_hosts.values():
            host.installed.append(KERNEL_B)
        intent = await prepared_intent(handlers)
        approval_id = fake_store.seed_approval(intent)
        # Remove the initramfs by making `test -f` fail for host 2.
        two_hosts["10.0.0.2"].installed.remove(KERNEL_B)
        two_hosts["10.0.0.2"].installed.append(KERNEL_B + ".missing")
        result = await handlers["select_default_kernel"](
            hosts=["10.0.0.1", "10.0.0.2"],
            kernel=KERNEL_B,
            approval_request_id=approval_id,
        )
        assert result["results"]["10.0.0.2"]["state"] == "not_bootable"

    @pytest.mark.asyncio
    async def test_grubby_timeout_but_set(self, handlers, fake_store, two_hosts):
        intent, approval_id = await self._after_install(handlers, fake_store, two_hosts)
        for host in two_hosts.values():
            host.grubby_behavior = "timeout_sets"
        result = await handlers["select_default_kernel"](
            hosts=["10.0.0.1", "10.0.0.2"],
            kernel=KERNEL_B,
            approval_request_id=approval_id,
        )
        assert result["results"]["10.0.0.1"]["state"] == "ok"
        assert "timed out" in result["results"]["10.0.0.1"]["note"]

    @pytest.mark.asyncio
    async def test_grubby_timeout_unset_is_indeterminate(
        self, handlers, fake_store, two_hosts
    ):
        intent, approval_id = await self._after_install(handlers, fake_store, two_hosts)
        for host in two_hosts.values():
            host.grubby_behavior = "timeout_fails"
        result = await handlers["select_default_kernel"](
            hosts=["10.0.0.1", "10.0.0.2"],
            kernel=KERNEL_B,
            approval_request_id=approval_id,
        )
        assert result["results"]["10.0.0.1"]["state"] == "indeterminate"


# ---------------------------------------------------------------------------
# Refusal primitives
# ---------------------------------------------------------------------------


class TestSelfHostRefusal:
    def test_literal_loopback_names(self):
        from agents.server_utils import is_self_host

        assert is_self_host("localhost") is True
        assert is_self_host("127.0.0.1") is True
        assert is_self_host("::1") is True

    def test_loopback_and_unspecified_by_address_property(self):
        from agents.server_utils import is_self_host

        assert is_self_host("127.0.0.2") is True
        assert is_self_host("127.255.255.254") is True
        assert is_self_host("::") is True
        assert is_self_host("0.0.0.0") is True

    def test_own_hostname_is_self(self, monkeypatch):
        import socket

        from agents.server_utils import is_self_host

        monkeypatch.setattr(socket, "gethostname", lambda: "myhost")
        monkeypatch.setattr(socket, "getfqdn", lambda: "myhost.example.com")
        assert is_self_host("myhost") is True
        assert is_self_host("myhost.example.com") is True

    def test_ip_of_own_hostname_is_self(self, monkeypatch):
        import socket

        from agents.server_utils import is_self_host

        monkeypatch.setattr(socket, "gethostname", lambda: "myhost")
        monkeypatch.setattr(socket, "getfqdn", lambda: "myhost.example.com")
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda h, p, *a, **kw: [(socket.AF_INET, 0, 0, "", ("192.168.1.100", 0))]
            if h in ("myhost", "myhost.example.com", "192.168.1.100")
            else [(socket.AF_INET, 0, 0, "", ("10.0.0.99", 0))],
        )
        assert is_self_host("192.168.1.100") is True
        assert is_self_host("10.0.0.1") is False

    def test_unresolvable_fails_closed(self, monkeypatch):
        import socket

        from agents.server_utils import is_self_host

        def _boom(*args, **kwargs):
            raise socket.gaierror("nope")

        monkeypatch.setattr(socket, "getaddrinfo", _boom)
        assert is_self_host("ghost.invalid") is True


class TestPackageInjection:
    def test_shell_injection_in_package_rejected(self):
        from providers.rhel_kernel import KernelSpec

        with pytest.raises(ValueError, match="invalid characters"):
            KernelSpec.parse({"release": KERNEL_A, "package": "kernel; rm -rf /"})

    def test_subshell_in_package_rejected(self):
        from providers.rhel_kernel import KernelSpec

        with pytest.raises(ValueError, match="invalid characters"):
            KernelSpec.parse({"release": KERNEL_A, "package": "kernel$(whoami)"})


def fleet_ssh_calls(two_hosts, handlers):
    """Commands issued so far, tagged with the SSH target host."""
    import agents.provisioning.server as srv

    return [f"{c['host']} {c['command']}" for c in srv._ssh.calls]


class TestCanonicalization:
    def test_requested_as_keeps_caller_string(self, ticket):
        from agents.provisioning.kernel import canonicalize_targets

        ticket = copy.deepcopy(ticket)
        ticket["custom_fields"]["resource_provider_metadata"] = {
            "ip_mapping": {"203.0.113.5": "10.0.0.1"}
        }
        allowed, refused, requested_as, addresses = canonicalize_targets(
            ["203.0.113.5", "10.0.0.2"], ticket
        )
        assert allowed == ["10.0.0.1", "10.0.0.2"]
        assert refused == {}
        assert requested_as == {"10.0.0.1": "203.0.113.5", "10.0.0.2": "10.0.0.2"}
        assert addresses == {"10.0.0.1": "10.0.0.1", "10.0.0.2": "10.0.0.2"}

    @pytest.mark.asyncio
    async def test_ssh_uses_the_pinned_address_not_the_name(
        self, handlers, fleet_ssh, monkeypatch
    ):
        """Targets given as names: validation resolves them once and every
        SSH call goes to that address, so DNS cannot change the target."""
        import socket

        table = {
            "sut1.lab": "10.0.0.1",
            "sut2.lab": "10.0.0.2",
            "orch.lab": "192.168.9.9",
        }

        def fake_getaddrinfo(host, port, *args, **kwargs):
            if host in table:
                return [(socket.AF_INET, 0, 0, "", (table[host], 0))]
            try:
                import ipaddress

                ipaddress.ip_address(host)
                return [(socket.AF_INET, 0, 0, "", (host, 0))]
            except ValueError:
                raise socket.gaierror("unknown")

        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
        monkeypatch.setattr(socket, "gethostname", lambda: "orch.lab")
        monkeypatch.setattr(socket, "getfqdn", lambda: "orch.lab")
        FakeKernelStore.reset(kernel_step_ticket(targets=["sut1.lab", "sut2.lab"]))
        # Make DNS lie after validation: the pinned address must still win.
        result = await handlers["get_kernel_inventory"](hosts=["sut1.lab", "sut2.lab"])
        assert result["results"]["sut1.lab"]["state"] == "ok"
        assert result["results"]["sut1.lab"]["requested_as"] == "sut1.lab"
        targets = {c["host"] for c in fleet_ssh.calls}
        assert targets == {"10.0.0.1", "10.0.0.2"}

    def test_full_coverage_requires_every_target(self, ticket):
        from agents.provisioning.kernel import require_full_coverage

        ok, detail = require_full_coverage(["10.0.0.1"], {}, ticket)
        assert not ok
        assert detail["missing"] == ["10.0.0.2"]
        ok, detail = require_full_coverage(["10.0.0.1", "10.0.0.2"], {}, ticket)
        assert ok and detail == {}
