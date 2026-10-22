"""kernel_comparison directive → canonical A/B plan, and repo_url install.

The builder (providers/kernel_plan.py) is deterministic code: the triage
LLM only supplies intent and releases. These tests pin the plan topology,
the directive validation, the KernelSpec repo_url validation, and the
repo-bootstrapping install path on the fake RHEL hosts.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import agents.provisioning.kernel  # noqa: F401  (ensure module exists for patch)
import paths  # noqa: F401
from providers.directives import normalize_directives
from providers.kernel_plan import (
    build_kernel_comparison_plan,
    validate_kernel_comparison,
)
from providers.rhel_kernel import KernelSpec, build_repo_setup_command
from tests.test_provisioning_kernel_tools import (  # noqa: F401
    KERNEL_A,
    KERNEL_B,
    FakeFleetSSH,
    FakeKernelStore,
    kernel_step_ticket,
    make_provisioning_handlers,
)

REPO_URL = "https://download.eng.rdu.redhat.com/rhel-9/composes/RHEL-9/RHEL-9.10.0-20260907.1/compose/BaseOS/x86_64/os/"

GOOD = {
    "baseline": KERNEL_A,
    "candidate": KERNEL_B,
}


def default_plan():
    return {
        "current_step": 0,
        "run_ids": [],
        "steps": [
            {"id": 0, "agent_type": "resource", "params": {}},
            {"id": 1, "agent_type": "provision", "params": {"label": "harness"}},
            {"id": 2, "agent_type": "benchmark", "params": {"label": "sleep"}},
            {"id": 3, "agent_type": "review", "params": {}},
            {"id": 4, "agent_type": "teardown", "params": {}},
        ],
    }


# ---------------------------------------------------------------------------
# validate_kernel_comparison
# ---------------------------------------------------------------------------


class TestValidateKernelComparison:
    def test_minimal_directive_normalizes(self):
        normalized, err = validate_kernel_comparison(dict(GOOD))
        assert err == ""
        assert normalized["baseline"] == KERNEL_A
        assert normalized["candidate"] == KERNEL_B
        assert normalized["order"] == "AB"
        assert normalized["package"] == "kernel"
        assert normalized["repo_url"] == ""

    def test_full_directive(self):
        normalized, err = validate_kernel_comparison(
            {**GOOD, "order": "abba", "package": "kernel-core", "repo_url": REPO_URL}
        )
        assert err == ""
        assert normalized["order"] == "ABBA"
        assert normalized["package"] == "kernel-core"
        assert normalized["repo_url"] == REPO_URL

    @pytest.mark.parametrize(
        "directive,fragment",
        [
            ({}, "requires both"),
            ({"baseline": KERNEL_A}, "requires both"),
            ({**GOOD, "baseline": "5.14.0-742; rm -rf /"}, "uname -r"),
            ({**GOOD, "candidate": "not a kernel; rm -rf /"}, "uname -r"),
            ({**GOOD, "order": "AABB"}, "order"),
            ({**GOOD, "package": "kernel; rm"}, "package"),
            ({**GOOD, "repo_url": "https://x/' rm -rf"}, "plain http(s)"),
            ({**GOOD, "repo_url": "ftp://x"}, "http(s)"),
            ({**GOOD, "repo_url": "https://a b"}, "http(s)"),
        ],
    )
    def test_rejections(self, directive, fragment):
        normalized, err = validate_kernel_comparison(directive)
        assert normalized is None
        assert fragment in err

    def test_not_a_mapping(self):
        normalized, err = validate_kernel_comparison("5.14.0-742.el9")
        assert normalized is None
        assert "mapping" in err


# ---------------------------------------------------------------------------
# build_kernel_comparison_plan
# ---------------------------------------------------------------------------


class TestBuildKernelComparisonPlan:
    def test_ab_shape(self):
        plan, err = build_kernel_comparison_plan(default_plan(), dict(GOOD))
        assert err == ""
        kinds = [(s["agent_type"], s["params"].get("label")) for s in plan["steps"]]
        assert kinds == [
            ("resource", None),
            ("provision", "harness"),
            ("provision", "kernel-A"),
            ("benchmark", "sleep-kernel-baseline"),
            ("provision", "kernel-B"),
            ("benchmark", "sleep-kernel-candidate"),
            ("review", None),
            ("teardown", None),
        ]
        assert [s["id"] for s in plan["steps"]] == list(range(8))
        assert plan["current_step"] == 0

    def test_kernel_steps_carry_the_releases_and_serial_reboot(self):
        plan, _ = build_kernel_comparison_plan(default_plan(), dict(GOOD))
        k_a = plan["steps"][2]["params"]["kernel"]
        k_b = plan["steps"][4]["params"]["kernel"]
        assert k_a == {
            "release": KERNEL_A,
            "package": "kernel",
            "reboot_strategy": "serial",
            "hosts": None,
        }
        assert k_b["release"] == KERNEL_B
        # Kernel steps are pending provision steps with empty results.
        for idx in (2, 4):
            assert plan["steps"][idx]["status"] == "pending"
            assert plan["steps"][idx]["results"] == {}

    def test_benchmark_copies_preserve_params_and_add_role(self):
        template_params = {"label": "sleep", "required_hosts": [{"roles": ["client"]}]}
        plan_dict = default_plan()
        plan_dict["steps"][2]["params"] = dict(template_params)
        plan, _ = build_kernel_comparison_plan(plan_dict, dict(GOOD))
        bench_a = plan["steps"][3]["params"]
        bench_b = plan["steps"][5]["params"]
        assert bench_a["required_hosts"] == template_params["required_hosts"]
        assert bench_b["required_hosts"] == template_params["required_hosts"]
        assert bench_a["kernel_role"] == "baseline"
        assert bench_b["kernel_role"] == "candidate"

    def test_repo_url_propagates_into_kernel_steps(self):
        plan, _ = build_kernel_comparison_plan(
            default_plan(), {**GOOD, "repo_url": REPO_URL}
        )
        for idx in (2, 4):
            kernel = plan["steps"][idx]["params"]["kernel"]
            assert kernel["repo_url"] == REPO_URL

    def test_abba_order(self):
        plan, err = build_kernel_comparison_plan(
            default_plan(), {**GOOD, "order": "ABBA"}
        )
        assert err == ""
        labels = [s["params"].get("label") for s in plan["steps"]]
        assert labels == [
            None,
            "harness",
            "kernel-A",
            "sleep-kernel-baseline",
            "kernel-B",
            "sleep-kernel-candidate",
            "kernel-B",
            "sleep-kernel-candidate",
            "kernel-A",
            "sleep-kernel-baseline",
            None,
            None,
        ]
        assert plan["steps"][8]["params"]["kernel"]["release"] == KERNEL_A

    def test_baab_order(self):
        plan, _ = build_kernel_comparison_plan(
            default_plan(), {**GOOD, "order": "BAAB"}
        )
        assert plan["steps"][2]["params"]["kernel"]["release"] == KERNEL_B

    def test_no_benchmark_step_is_rejected(self):
        plan_dict = default_plan()
        plan_dict["steps"] = plan_dict["steps"][:2]
        _, err = build_kernel_comparison_plan(plan_dict, dict(GOOD))
        assert "no benchmark step" in err

    def test_plan_past_first_benchmark_is_rejected(self):
        plan_dict = default_plan()
        plan_dict["current_step"] = 2
        _, err = build_kernel_comparison_plan(plan_dict, dict(GOOD))
        assert "already progressed" in err

    def test_invalid_directive_returns_error_not_exception(self):
        _, err = build_kernel_comparison_plan(default_plan(), {"baseline": "x"})
        assert "requires both" in err


# ---------------------------------------------------------------------------
# Directive normalization passthrough
# ---------------------------------------------------------------------------


class TestDirectiveNormalization:
    def test_kernel_comparison_is_recognized_and_untouched(self):
        directive = {**GOOD, "repo_url": REPO_URL}
        normalized, applied, unrecognized = normalize_directives(
            {"kernel_comparison": directive}
        )
        assert normalized["kernel_comparison"] == directive
        assert unrecognized == []

    def test_kernel_comparison_alias_variants_resolve(self):
        normalized, _, unrecognized = normalize_directives(
            {"kernel-comparison": dict(GOOD)}
        )
        assert normalized.get("kernel_comparison") == dict(GOOD)
        assert unrecognized == []


# ---------------------------------------------------------------------------
# KernelSpec.repo_url and the repo setup command
# ---------------------------------------------------------------------------


class TestKernelSpecRepoUrl:
    def test_parse_from_step_dict(self):
        spec = KernelSpec.parse(
            {"release": KERNEL_B, "package": "kernel", "repo_url": REPO_URL}
        )
        assert spec.repo_url == REPO_URL

    def test_default_empty(self):
        assert KernelSpec.parse(KERNEL_B).repo_url == ""

    @pytest.mark.parametrize(
        "bad",
        [
            "https://host/path' rm -rf /",  # single quote
            "https://host/path rm",  # whitespace
            "https://host/path\\nrm",  # backslash
            "ftp://host/path",  # scheme
            "a" * 513,  # too long
        ],
    )
    def test_invalid_repo_url_rejected(self, bad):
        with pytest.raises(ValueError):
            KernelSpec(release=KERNEL_B, repo_url=bad)

    def test_to_dict_round_trip(self):
        spec = KernelSpec(release=KERNEL_B, repo_url=REPO_URL)
        parsed = KernelSpec.parse(spec.to_dict())
        assert parsed.repo_url == REPO_URL

    def test_build_repo_setup_command(self):
        spec = KernelSpec(release=KERNEL_B, repo_url=REPO_URL)
        cmd = build_repo_setup_command(spec)
        assert f"'baseurl={REPO_URL}'" in cmd
        assert "sslverify=0" in cmd
        assert "gpgcheck=0" in cmd
        assert f"dnf -q list --available {spec.nevra}" in cmd

    def test_build_repo_setup_command_requires_url(self):
        with pytest.raises(ValueError):
            build_repo_setup_command(KernelSpec(release=KERNEL_B))


# ---------------------------------------------------------------------------
# install_kernel bootstraps the repo before dnf (fake RHEL hosts)
# ---------------------------------------------------------------------------


def fresh_repo_hosts():
    from tests.fake_rhel import FakeRHELHost

    # Fresh lab image: baseline kernel installed, nothing else available
    # until the step's repo is configured.
    h1 = FakeRHELHost(
        "host1",
        running=KERNEL_A,
        installed=[KERNEL_A],
        available=[],
        repo_packages=[KERNEL_B],
    )
    h2 = FakeRHELHost(
        "host2",
        running=KERNEL_A,
        installed=[KERNEL_A],
        available=[],
        repo_packages=[KERNEL_B],
    )
    return {"10.0.0.1": h1, "10.0.0.2": h2}


class TestInstallKernelRepoBootstrap:
    @pytest.fixture
    def repo_ticket(self):
        return kernel_step_ticket(
            kernel={
                "release": KERNEL_B,
                "package": "kernel",
                "repo_url": REPO_URL,
            }
        )

    @pytest.fixture
    def fleet_ssh(self):
        ssh = FakeFleetSSH(fresh_repo_hosts())
        self._ssh = ssh
        return ssh

    @pytest.fixture
    def handlers(self, fleet_ssh):
        return make_provisioning_handlers(fleet_ssh)

    @pytest.fixture(autouse=True)
    def fake_store(self, repo_ticket):
        FakeKernelStore.reset(repo_ticket)

        async def _active(**kwargs):
            return {"status": "ok", "ticket": FakeKernelStore.ticket}

        from unittest.mock import patch

        with (
            patch("agents.provisioning.kernel.AuditedAsyncHTTPClient", FakeKernelStore),
            patch(
                "agents.server_utils.assert_ticket_active",
                AsyncMock(side_effect=_active),
            ),
            patch("agents.server_utils.tool_progress", AsyncMock()),
        ):
            yield FakeKernelStore

    async def prepared_intent(self, handlers):
        result = await handlers["prepare_kernel_change"](
            hosts=["10.0.0.1", "10.0.0.2"], kernel=KERNEL_B
        )
        assert result["status"] == "ok", result
        return result["intent_id"]

    @pytest.mark.asyncio
    async def test_repo_bootstrap_runs_before_install(
        self, handlers, fake_store, fleet_ssh
    ):
        intent_id = await self.prepared_intent(handlers)
        approval_id = fake_store.seed_approval(fake_store.intent(intent_id))
        result = await handlers["install_kernel"](
            hosts=["10.0.0.1", "10.0.0.2"],
            kernel=KERNEL_B,
            approval_request_id=approval_id,
        )
        for host in ("10.0.0.1", "10.0.0.2"):
            assert result["results"][host]["state"] == "installed", result["results"]
        # The bootstrap command ran on every host before dnf install.
        repo_calls = [
            c
            for c in self._ssh.calls
            if "agentic-perf kernel repository" in c["command"]
        ]
        install_calls = [c for c in self._ssh.calls if "dnf install" in c["command"]]
        assert len(repo_calls) == 2
        assert len(install_calls) == 2
        assert self._ssh.calls.index(repo_calls[0]) < self._ssh.calls.index(
            install_calls[0]
        )

    @pytest.mark.asyncio
    async def test_unreachable_repo_fails_the_install_with_clear_error(
        self, handlers, fake_store, fleet_ssh
    ):
        # Host with the repo configured but the package not served by it.
        for host in self._ssh._hosts.values():
            host.repo_packages = []
        intent_id = await self.prepared_intent(handlers)
        approval_id = fake_store.seed_approval(fake_store.intent(intent_id))
        result = await handlers["install_kernel"](
            hosts=["10.0.0.1", "10.0.0.2"],
            kernel=KERNEL_B,
            approval_request_id=approval_id,
        )
        for host in ("10.0.0.1", "10.0.0.2"):
            record = result["results"][host]
            assert record["state"] == "install_failed"
            assert "repository bootstrap failed" in record["error"]


# ---------------------------------------------------------------------------
# Deterministic runner: not_found handling with and without repo_url
# ---------------------------------------------------------------------------


def _runner_imports():
    from tests.test_provisioning_kernel_step import HOSTS, ScriptedMCP, _agent

    return HOSTS, ScriptedMCP, _agent


def runner_ticket(with_repo: bool):
    kernel = {"release": KERNEL_B, "package": "kernel"}
    if with_repo:
        kernel["repo_url"] = REPO_URL
    return kernel_step_ticket(kernel=kernel)


def not_found_script():
    return {
        "get_kernel_inventory": {
            "results": {
                "10.0.0.1": {
                    "host": "10.0.0.1",
                    "state": "ok",
                    "inventory": {"running": KERNEL_A},
                    "requested": {"release": KERNEL_B, "state": "not_found"},
                },
                "10.0.0.2": {
                    "host": "10.0.0.2",
                    "state": "ok",
                    "inventory": {"running": KERNEL_A},
                    "requested": {"release": KERNEL_B, "state": "not_found"},
                },
            }
        },
        "prepare_kernel_change": {
            "status": "ok",
            "intent_id": "kci-" + "2" * 32,
            "actions_required": ["install", "select_default", "reboot"],
        },
        "install_kernel": {
            "results": {
                h: {"host": h, "state": "installed"} for h in ("10.0.0.1", "10.0.0.2")
            }
        },
        "select_default_kernel": {
            "results": {h: {"host": h, "state": "ok"} for h in ("10.0.0.1", "10.0.0.2")}
        },
        "reboot_hosts_and_verify": {
            "status": "verified",
            "hosts": {h: {"state": "verified"} for h in ("10.0.0.1", "10.0.0.2")},
        },
    }


class TestRunnerNotFoundHandling:
    @pytest.mark.asyncio
    async def test_without_repo_url_not_found_stops_before_prepare(self):
        HOSTS, ScriptedMCP, _agent = _runner_imports()
        ticket = runner_ticket(with_repo=False)
        agent = _agent(ticket)
        mcp = ScriptedMCP(not_found_script())

        await agent._run_kernel_step(ticket["id"], ticket, mcp)

        names = [n for n, _ in mcp.calls]
        assert names == ["get_kernel_inventory"]
        agent._transition_ticket.assert_awaited_once()
        comment = agent._add_comment.await_args.args[1]
        assert "add the repository first" in comment

    @pytest.mark.asyncio
    async def test_with_repo_url_not_found_proceeds_to_install(self):
        HOSTS, ScriptedMCP, _agent = _runner_imports()
        ticket = runner_ticket(with_repo=True)
        agent = _agent(ticket)
        mcp = ScriptedMCP(not_found_script())

        await agent._run_kernel_step(ticket["id"], ticket, mcp)

        names = [n for n, _ in mcp.calls]
        assert names == [
            "get_kernel_inventory",
            "prepare_kernel_change",
            "install_kernel",
            "select_default_kernel",
            "reboot_hosts_and_verify",
        ]
        comment = agent._add_comment.await_args.args[1]
        assert "repo_url" in comment
        agent._handle_completion.assert_awaited_once()


# ---------------------------------------------------------------------------
# Handoff-gate compatibility: the gate imports helpers from this module
# ---------------------------------------------------------------------------


class TestHandoffGateCompatibility:
    """Regression: rewriting kernel_plan.py dropped preceding_kernel_step,
    which orchestrator/handoff.py imports; any handoff check on a plan with
    a kernel step then raised ImportError. These tests pin the gate against
    the transformer's output so the module cannot silently break it again.
    """

    @staticmethod
    def _ticket(plan):
        return {
            "custom_fields": {
                "provisioning_complete": True,
                "hosts_provisioned": ["10.0.0.1"],
                "assigned_hardware_ips": {
                    "controller": "10.0.0.100",
                    "targets": ["10.0.0.1"],
                },
                "provisioning_verification": {
                    "status": "verified",
                    "details": "harness verified",
                },
                "execution_plan": plan,
                "kernel_transitions": {"2": {"state": "verified"}},
            }
        }

    def test_transformed_plan_passes_the_gate_when_verified(self):
        from orchestrator.handoff import check_handoff

        plan, _ = build_kernel_comparison_plan(default_plan(), dict(GOOD))
        # Mid-run: the plan advanced to the first benchmark (id 3), which
        # follows the kernel-A step (id 2).
        plan["current_step"] = 3
        ok, reason = check_handoff("executing_benchmark", self._ticket(plan))
        assert ok, reason

    def test_transformed_plan_blocks_the_gate_when_unverified(self):
        from orchestrator.handoff import check_handoff

        plan, _ = build_kernel_comparison_plan(default_plan(), dict(GOOD))
        plan["current_step"] = 3
        ticket = self._ticket(plan)
        ticket["custom_fields"]["kernel_transitions"] = {
            "2": {"state": "reconnect_timeout"}
        }
        ok, reason = check_handoff("executing_benchmark", ticket)
        assert not ok
        assert "reconnect_timeout" in reason

    def test_helpers_survive_module_rewrite(self):
        from providers.kernel_plan import kernel_steps, preceding_kernel_step

        plan, _ = build_kernel_comparison_plan(default_plan(), dict(GOOD))
        steps = kernel_steps(plan)
        assert [s["id"] for s in steps] == [2, 4]
        assert preceding_kernel_step(plan, 3)["id"] == 2
        assert preceding_kernel_step(plan, 2) is None
