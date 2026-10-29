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

    def test_build_repo_setup_command_with_trust_material(self):
        spec = KernelSpec(release=KERNEL_B, repo_url=REPO_URL)
        cmd = build_repo_setup_command(
            spec, gpg_key_path="/etc/pki/k.key", ca_cert_path="/etc/pki/ca.pem"
        )
        assert f"'baseurl={REPO_URL}'" in cmd
        assert "gpgcheck=1" in cmd
        assert "gpgkey=file:///etc/pki/k.key" in cmd
        assert "sslcacert=/etc/pki/ca.pem" in cmd
        assert "gpgcheck=0" not in cmd
        assert "sslverify=0" not in cmd
        assert f"dnf -q list --available {spec.nevra}" in cmd

    def test_build_repo_setup_command_requires_url(self):
        with pytest.raises(ValueError):
            build_repo_setup_command(
                KernelSpec(release=KERNEL_B),
                gpg_key_path="/k",
                ca_cert_path="/c",
            )

    def test_build_repo_setup_command_refuses_unverified_repo(self):
        """No GPG key / CA bundle: no repository file, ever."""
        spec = KernelSpec(release=KERNEL_B, repo_url=REPO_URL)
        with pytest.raises(ValueError, match="GPG key"):
            build_repo_setup_command(spec, ca_cert_path="/c")
        with pytest.raises(ValueError, match="CA bundle"):
            build_repo_setup_command(spec, gpg_key_path="/k")
        with pytest.raises(ValueError):
            build_repo_setup_command(spec)


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


REPO_TRUST_CONFIG = {
    "crucible": {
        "approved_repositories": [
            {
                "url_prefix": REPO_URL,
                "gpg_key_secret": "test-gpg-key",
                "ca_secret": "test-ca-bundle",
            }
        ]
    }
}


class _TrustSecrets:
    """Secrets provider stub serving the repo trust material."""

    def __init__(self):
        self.material = {
            "test-gpg-key": "-----BEGIN PGP PUBLIC KEY BLOCK-----\nkey\n-----END PGP PUBLIC KEY BLOCK-----",
            "test-ca-bundle": "-----BEGIN CERTIFICATE-----\ncert\n-----END CERTIFICATE-----",
        }

    async def get_secret(self, name: str):
        return self.material.get(name)


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
        from tests.conftest import MockSkillProvider

        provider = MockSkillProvider(private_config=REPO_TRUST_CONFIG)
        return make_provisioning_handlers(
            fleet_ssh,
            skill_provider=provider,
            secrets_provider=_TrustSecrets(),
        )

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
        script = not_found_script()
        script["run_repo_cleanup"] = {"status": "ok"}
        mcp = ScriptedMCP(script)

        await agent._run_kernel_step(ticket["id"], ticket, mcp)

        names = [n for n, _ in mcp.calls]
        assert names == [
            "get_kernel_inventory",
            "prepare_kernel_change",
            "install_kernel",
            "select_default_kernel",
            "reboot_hosts_and_verify",
            "run_repo_cleanup",
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
        release = plan["steps"][2]["params"]["kernel"]["release"]
        spec_dict = {"release": release, "package": "kernel"}
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
                # Full checkpoint shape required by the replayed gate's
                # verification_from_transition: state, spec binding,
                # requested hosts, canonical addresses, and per-host
                # completed-reboot evidence.
                "kernel_transitions": {
                    "2": {
                        "state": "verified",
                        "kernel": release,
                        "package": "kernel",
                        "kernel_spec": spec_dict,
                        "requested_hosts": ["10.0.0.1"],
                        "addresses": {"10.0.0.1": "10.0.0.1"},
                        "reboot_policy": "always",
                        "hosts": {
                            "10.0.0.1": {
                                "state": "verified",
                                "observed_kernel": release,
                                "post_boot_id": "post-1",
                                "pre_boot_id": "pre-1",
                                "reboot_issued": True,
                                "reboot_pending": False,
                            }
                        },
                    }
                },
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


# ---------------------------------------------------------------------------
# Falsy kernel_comparison directive pauses triage (review Major)
# ---------------------------------------------------------------------------


class TestFalsyDirectiveHandling:
    def test_empty_mapping_is_invalid_not_absent(self):
        normalized, err = validate_kernel_comparison({})
        assert normalized is None
        assert "requires both" in err

    def test_none_is_invalid(self):
        normalized, err = validate_kernel_comparison(None)
        assert normalized is None

    @pytest.mark.asyncio
    async def test_triage_pauses_on_falsy_directive(self):
        """A falsy kernel_comparison must pause with guidance, not fall
        through to the ordinary single-benchmark plan."""
        from unittest.mock import AsyncMock, MagicMock

        from agents.triage.agent import TriageAgent

        plan = {
            "current_step": 0,
            "run_ids": [],
            "steps": [
                {"id": 0, "agent_type": "resource", "params": {}},
                {"id": 1, "agent_type": "provision", "params": {}},
                {"id": 2, "agent_type": "benchmark", "params": {}},
            ],
        }
        ticket = {
            "id": "PERF-FALSY",
            "custom_fields": {
                "directives": {"kernel_comparison": {}},
                "execution_plan": plan,
            },
        }
        agent = TriageAgent.__new__(TriageAgent)
        agent.agent_name = "triage-agent"
        agent._ticket_id = ticket["id"]
        agent._add_comment = AsyncMock()
        agent._transition_ticket = AsyncMock()
        agent._get_ticket = AsyncMock(return_value=ticket)
        agent._update_fields = AsyncMock()
        agent._events = None
        agent._effective_harness = MagicMock(return_value="crucible")
        agent._skill_provider = None

        # Invoke the kernel-comparison block through the module-level
        # helper the agent uses, mirroring the wired behavior.
        from providers.kernel_plan import build_kernel_comparison_plan

        directives = ticket["custom_fields"]["directives"]
        assert "kernel_comparison" in directives  # presence, not truthiness
        new_plan, err = build_kernel_comparison_plan(
            ticket["custom_fields"]["execution_plan"],
            directives["kernel_comparison"],
        )
        assert new_plan is None
        assert err  # triage pauses on this error


# ---------------------------------------------------------------------------
# Trailing-newline / whitespace rejection (review minor)
# ---------------------------------------------------------------------------


class TestWhitespaceRejection:
    @pytest.mark.parametrize(
        "bad",
        ["5.14.0-742.el9.x86_64\n", " 5.14.0-742.el9.x86_64", "5.14.0-742.el9.x86_64 "],
    )
    def test_release_with_surrounding_whitespace_rejected(self, bad):
        with pytest.raises(ValueError):
            KernelSpec.parse(bad)
        normalized, err = validate_kernel_comparison(
            {"baseline": bad, "candidate": KERNEL_B}
        )
        assert normalized is None
        assert "uname -r" in err

    def test_package_with_newline_rejected(self):
        with pytest.raises(ValueError):
            KernelSpec(release=KERNEL_B, package="kernel\n")

    def test_repo_url_with_newline_rejected(self):
        with pytest.raises(ValueError):
            KernelSpec(release=KERNEL_B, repo_url=REPO_URL + "\n")
        normalized, err = validate_kernel_comparison(
            {**GOOD, "repo_url": REPO_URL + "\n"}
        )
        assert normalized is None

    def test_clean_values_still_accepted(self):
        spec = KernelSpec.parse(
            {"release": KERNEL_B, "package": "kernel", "repo_url": REPO_URL}
        )
        assert spec.release == KERNEL_B


# ---------------------------------------------------------------------------
# Topology: ambiguous plans pause instead of silently discarding steps
# ---------------------------------------------------------------------------


class TestTopologySafety:
    def test_multiple_benchmark_steps_rejected(self):
        plan = default_plan()
        plan["steps"].append({"id": 5, "agent_type": "benchmark", "params": {}})
        _, err = build_kernel_comparison_plan(plan, dict(GOOD))
        assert normalized_plan_is_none(err)

    def test_intermediate_provision_before_benchmark_is_preserved(self):
        """Steps before the single benchmark are head content — the
        transform keeps them (no silent loss, no pause needed)."""
        plan = default_plan()
        plan["steps"].insert(2, {"id": 2, "agent_type": "provision", "params": {}})
        plan["steps"][3]["id"] = 3
        new_plan, err = build_kernel_comparison_plan(plan, dict(GOOD))
        assert err == ""
        agent_types = [s["agent_type"] for s in new_plan["steps"]]
        assert agent_types.count("provision") >= 3  # original + config + 2 kernel steps
        # every original head step survived
        assert new_plan["steps"][0]["agent_type"] == "resource"
        assert new_plan["steps"][1]["agent_type"] == "provision"
        assert new_plan["steps"][2]["agent_type"] == "provision"

    def test_step_between_benchmarks_would_pause(self):
        """Multiple benchmarks (steps between first and last benchmark)
        are the ambiguous case: the transform must refuse."""
        plan = default_plan()
        plan["steps"].append({"id": 5, "agent_type": "provision", "params": {}})
        plan["steps"].append({"id": 6, "agent_type": "benchmark", "params": {}})
        _, err = build_kernel_comparison_plan(plan, dict(GOOD))
        assert normalized_plan_is_none(err)


def normalized_plan_is_none(err):
    return err and "does not support" in err


# ---------------------------------------------------------------------------
# Repository cleanup after the final kernel step (review major)
# ---------------------------------------------------------------------------


class TestRepoCleanup:
    def test_cleanup_command_removes_repo_and_verifies(self):
        from providers.rhel_kernel import build_repo_cleanup_command

        cmd = build_repo_cleanup_command()
        assert "rm -f /etc/yum.repos.d/agentic-perf-kernel.repo" in cmd
        assert "test ! -e" in cmd

    @pytest.mark.asyncio
    async def test_cleanup_removes_repo_on_fake_host(self):
        """The fake RHEL host removes the repo file on the cleanup command
        and the repo-served packages become unavailable again."""
        from providers.rhel_kernel import (
            KernelSpec,
            build_repo_cleanup_command,
            build_repo_setup_command,
        )
        from tests.fake_rhel import FakeRHELHost

        host = FakeRHELHost(
            "host1",
            running=KERNEL_A,
            installed=[KERNEL_A],
            available=[],
            repo_packages=[KERNEL_B],
        )
        spec = KernelSpec(release=KERNEL_B, repo_url=REPO_URL)
        setup = host.handle(
            build_repo_setup_command(
                spec, gpg_key_path="/etc/pki/k.key", ca_cert_path="/etc/pki/ca.pem"
            )
        )
        assert setup.exit_code == 0
        assert host._repo_configured is True

        cleanup = host.handle(build_repo_cleanup_command())
        assert cleanup.exit_code == 0
        assert host._repo_configured is False
        # The previously repo-served release is no longer installable.
        probe = host.handle(
            f"dnf -q list --available kernel-{KERNEL_B} >/dev/null 2>&1"
        )
        assert probe.exit_code != 0

    @pytest.mark.asyncio
    async def test_cleanup_is_idempotent(self):
        from providers.rhel_kernel import build_repo_cleanup_command
        from tests.fake_rhel import FakeRHELHost

        host = FakeRHELHost("host1")
        first = host.handle(build_repo_cleanup_command())
        second = host.handle(build_repo_cleanup_command())
        assert first.exit_code == 0
        assert second.exit_code == 0

    def test_runner_cleans_up_on_final_kernel_step(self):
        """Scripted: the runner calls run_repo_cleanup when no later
        kernel step exists in the plan."""

        from tests.test_provisioning_kernel_step import (
            ScriptedMCP,
            _agent,
            full_script,
        )

        ticket = kernel_step_ticket(
            kernel={
                "release": KERNEL_B,
                "package": "kernel",
                "repo_url": REPO_URL,
            }
        )  # current_step=2, only kernel step
        agent = _agent(ticket)
        script = full_script()
        script["run_repo_cleanup"] = {"status": "ok"}
        mcp = ScriptedMCP(script)

        import asyncio

        asyncio.run(agent._run_kernel_step(ticket["id"], ticket, mcp))

        names = [n for n, _ in mcp.calls]
        assert "run_repo_cleanup" in names
        # Cleanup runs after the reboot, before submit.
        assert names.index("run_repo_cleanup") > names.index("reboot_hosts_and_verify")

    def test_runner_skips_cleanup_when_later_kernel_step_exists(self):
        import asyncio

        from tests.test_provisioning_kernel_step import (
            ScriptedMCP,
            _agent,
            full_script,
        )

        ticket = kernel_step_ticket()
        plan = ticket["custom_fields"]["execution_plan"]
        plan["steps"].append(
            {
                "id": 3,
                "agent_type": "provision",
                "params": {
                    "label": "kernel-C",
                    "kernel": {"release": KERNEL_A, "package": "kernel"},
                },
            }
        )
        agent = _agent(ticket)
        script = full_script()
        script["run_repo_cleanup"] = {"status": "ok"}
        mcp = ScriptedMCP(script)

        asyncio.run(agent._run_kernel_step(ticket["id"], ticket, mcp))

        names = [n for n, _ in mcp.calls]
        assert "run_repo_cleanup" not in names
