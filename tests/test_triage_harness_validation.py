"""Tests for triage harness and benchmark suite validation (issue #1086).

Validates that the triage agent rejects invalid harness names and
auto-corrects misnamed benchmark suites before they propagate
downstream and create stuck tickets.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from agents.triage.agent import (
    TriageAgent,
    _description_harness_intent,
    _description_requested_harnesses,
    _description_requests_harness,
    _validate_harness_and_suite,
)
from providers.llm.base import LLMResponse, ToolCall
from state_store.models import VALID_TRANSITIONS, TicketStatus


class _FakeSkillProvider:
    """Minimal skill provider stub for validation tests."""

    def __init__(self, harnesses: list[str], suites: dict[str, dict] | None = None):
        self._harnesses = harnesses
        self._suites = suites or {}

    def list_harnesses(self) -> list[str]:
        return list(self._harnesses)

    async def get_benchmark(self, name: str):
        from providers.skills.base import BenchmarkSuite

        data = self._suites.get(name)
        if data is None:
            return None
        return BenchmarkSuite(
            name=data["name"],
            description=data.get("description", ""),
            harness=data.get("harness", ""),
        )


def _make_provider(
    harnesses: list[str] | None = None,
    suites: dict[str, dict] | None = None,
) -> _FakeSkillProvider:
    harnesses = harnesses or [
        "crucible",
        "zathras",
        "kube-burner",
        "k8s-netperf",
        "benchmark-runner",
        "clusterbuster",
        "vstorm",
        "ioscale",
        "forge",
        "arcaflow-plugins",
        "arcaflow-workflows",
    ]
    suites = suites or {
        "uperf": {"name": "uperf", "harness": "crucible"},
        "fio": {"name": "fio", "harness": "crucible"},
        "boot-time": {"name": "boot-time", "harness": "boot-time"},
    }
    return _FakeSkillProvider(harnesses, suites)


async def _complete_triage_result(
    result: dict,
    provider: _FakeSkillProvider,
    custom_fields: dict | None = None,
    description: str = "Run a benchmark",
) -> tuple[TriageAgent, dict, dict]:
    """Run completion against an in-memory ticket with legal transitions."""
    ticket = {
        "id": "PERF-TRIAGE-VALIDATION",
        "status": "triage_pending",
        "summary": "Benchmark request",
        "description": description,
        "custom_fields": custom_fields or {},
    }
    agent = TriageAgent(
        llm_provider=AsyncMock(),
        state_store_url="http://localhost:8090",
        skill_provider=provider,
    )
    agent._client = AsyncMock()
    agent._get_ticket = AsyncMock(return_value=ticket)
    agent._add_comment = AsyncMock()
    updated_fields: dict = {}

    async def update_fields(_ticket_id: str, fields: dict) -> None:
        updated_fields.update(fields)
        ticket["custom_fields"].update(fields)

    async def transition(_ticket_id: str, new_status: str, comment=None) -> None:
        current = TicketStatus(ticket["status"])
        target = TicketStatus(new_status)
        assert target in VALID_TRANSITIONS[current]
        ticket["status"] = target.value

    agent._update_fields = AsyncMock(side_effect=update_fields)
    agent._transition_ticket = AsyncMock(side_effect=transition)

    response = LLMResponse(
        text=None,
        tool_calls=[
            ToolCall(
                id="submit-triage-result",
                name="submit_triage_result",
                input=result,
            )
        ],
        stop_reason="tool_use",
    )
    await agent._handle_completion(ticket["id"], response)
    return agent, ticket, updated_fields


class TestHarnessValidation:
    """Reject harness names that don't exist in the registry."""

    def test_description_harness_detection_is_conservative_and_alias_aware(self):
        assert _description_requests_harness("Use zathras", "zathras")
        assert _description_requests_harness("Use arcaflow", "arcaflow-plugins")
        assert not _description_requests_harness("Use zathras-like tooling", "zathras")
        assert not _description_requests_harness("Do not use zathras", "zathras")
        assert not _description_requests_harness("Avoid using zathras", "zathras")
        assert not _description_requests_harness(
            "The prior run used zathras", "zathras"
        )
        names = {"zathras", "crucible"}
        assert _description_requested_harnesses("Use zathras, not crucible", names) == {
            "zathras"
        }
        assert _description_requested_harnesses("Not zathras but crucible", names) == {
            "crucible"
        }
        assert _description_requested_harnesses(
            "Use zathras rather than crucible", names
        ) == {"zathras"}
        assert _description_requested_harnesses("Use zathras or crucible", names) == {
            "zathras",
            "crucible",
        }

    def test_description_harness_intent_preserves_exclusions_and_alternatives(self):
        names = {"zathras", "crucible"}

        excluded = _description_harness_intent("Do not use zathras", names)
        assert excluded.required == frozenset()
        assert excluded.excluded == {"zathras"}

        excluded_alternatives = _description_harness_intent(
            "Do not use either zathras or crucible", names
        )
        assert excluded_alternatives.required == frozenset()
        assert excluded_alternatives.alternatives == ()
        assert excluded_alternatives.excluded == names

        avoid = _description_harness_intent("Avoid using zathras", names)
        assert avoid.excluded == {"zathras"}

        avoid_alternatives = _description_harness_intent(
            "Avoid using either zathras or crucible", names
        )
        assert avoid_alternatives.required == frozenset()
        assert avoid_alternatives.alternatives == ()
        assert avoid_alternatives.excluded == names

        only = _description_harness_intent("Use only zathras", names)
        assert only.required == {"zathras"}

        contrast = _description_harness_intent("Use zathras, not crucible", names)
        assert contrast.required == {"zathras"}
        assert contrast.excluded == {"crucible"}

        rather_than = _description_harness_intent(
            "Use zathras rather than crucible", names
        )
        assert rather_than.required == {"zathras"}
        assert rather_than.excluded == {"crucible"}

        either_or = _description_harness_intent("Use either zathras or crucible", names)
        assert either_or.required == frozenset()
        assert either_or.alternatives == (frozenset(names),)

        contrast = _description_harness_intent("Not zathras but crucible", names)
        assert contrast.required == {"crucible"}
        assert contrast.excluded == {"zathras"}

    @pytest.mark.asyncio
    async def test_valid_harness_passes(self):
        provider = _make_provider()
        result = await _validate_harness_and_suite("crucible", "uperf", False, provider)
        assert result is None

    @pytest.mark.asyncio
    async def test_jumpstarter_rejected_as_harness(self):
        """Issue #1086 case: 'jumpstarter' is a resource provider, not a harness."""
        provider = _make_provider()
        result = await _validate_harness_and_suite(
            "jumpstarter", "boot-time", False, provider
        )
        assert result is not None
        assert "error" in result
        assert "jumpstarter" in result["error"]
        assert "available_harnesses" in result

    @pytest.mark.asyncio
    async def test_nonexistent_harness_rejected(self):
        provider = _make_provider()
        result = await _validate_harness_and_suite(
            "nonexistent-harness", "uperf", False, provider
        )
        assert result is not None
        assert "error" in result

    @pytest.mark.asyncio
    async def test_empty_harness_passes(self):
        """An empty harness can be filled from the exact suite catalog entry."""
        provider = _make_provider()
        result = await _validate_harness_and_suite("", "uperf", False, provider)
        assert result is not None
        assert result["correction"]["harness"] == "crucible"

    @pytest.mark.asyncio
    async def test_boot_time_standalone_harness_valid(self):
        """'boot-time' is a standalone benchmark harness, not in list_harnesses."""
        provider = _make_provider(harnesses=["crucible", "zathras"])
        result = await _validate_harness_and_suite(
            "boot-time", "boot-time", False, provider
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_alias_accepted(self):
        """'arcaflow' is an alias for 'arcaflow-plugins'."""
        provider = _make_provider()
        result = await _validate_harness_and_suite("arcaflow", "", False, provider)
        assert result is None


class TestSuiteAutoCorrection:
    """Auto-correct misnamed benchmark suites when absent_suite is set."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("absent_suite", [True, False])
    async def test_prefixed_suite_corrected(self, absent_suite):
        """Issue #1086 case: 'jumpstarter-boot-time' → 'boot-time'."""
        provider = _make_provider()
        result = await _validate_harness_and_suite(
            "boot-time", "jumpstarter-boot-time", absent_suite, provider
        )
        assert result is not None
        assert "correction" in result
        assert result["correction"]["benchmark_suite"] == "boot-time"
        assert result["correction"]["absent_suite"] is False

    @pytest.mark.asyncio
    async def test_exact_match_clears_absent_and_corrects_harness(self):
        """Exact matches clear absent_suite and use the catalog harness."""
        provider = _make_provider()
        result = await _validate_harness_and_suite("zathras", "uperf", True, provider)
        assert result is not None
        assert "correction" in result
        assert result["correction"]["absent_suite"] is False
        assert result["correction"]["benchmark_suite"] == "uperf"
        assert result["correction"]["harness"] == "crucible"

    @pytest.mark.asyncio
    async def test_no_correction_when_truly_absent(self):
        """Genuinely absent suite — no auto-correction possible."""
        provider = _make_provider()
        result = await _validate_harness_and_suite(
            "crucible", "totally-unknown-benchmark", True, provider
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_unlisted_suite_is_marked_absent_even_when_flag_is_false(self):
        """An invented nonempty suite must be blocked even if LLM says present."""
        provider = _make_provider()
        result = await _validate_harness_and_suite(
            "crucible", "any-suite-name", False, provider
        )
        assert result == {"correction": {"absent_suite": True}}

    @pytest.mark.asyncio
    async def test_unrecognized_prefix_is_not_stripped(self):
        """Only known harness/provider prefixes may be removed from suite names."""
        provider = _make_provider()
        result = await _validate_harness_and_suite(
            "crucible", "custom-uperf", False, provider
        )
        assert result == {"correction": {"absent_suite": True}}

    @pytest.mark.asyncio
    async def test_correction_includes_harness_from_catalog(self):
        """Auto-correction should set the harness from the catalog entry."""
        provider = _make_provider()
        result = await _validate_harness_and_suite(
            "", "crucible-boot-time", True, provider
        )
        assert result is not None
        assert "correction" in result
        assert result["correction"]["benchmark_suite"] == "boot-time"
        assert result["correction"]["harness"] == "boot-time"

    @pytest.mark.asyncio
    async def test_correction_note_present(self):
        """Auto-correction should include an explanatory note."""
        provider = _make_provider()
        result = await _validate_harness_and_suite(
            "boot-time", "jumpstarter-boot-time", True, provider
        )
        assert "correction" in result
        assert "note" in result["correction"]
        assert "jumpstarter-boot-time" in result["correction"]["note"]
        assert "boot-time" in result["correction"]["note"]

    @pytest.mark.asyncio
    async def test_hyphenated_prefix_corrected(self):
        """e.g. 'kube-burner-uperf' should correct to 'uperf'."""
        provider = _make_provider()
        result = await _validate_harness_and_suite(
            "crucible", "kube-burner-uperf", True, provider
        )
        assert result is not None
        assert "correction" in result
        assert result["correction"]["benchmark_suite"] == "uperf"
        assert result["correction"]["harness"] == "crucible"

    @pytest.mark.asyncio
    async def test_invalid_harness_rejection_pauses_ticket(self):
        """Early rejection leaves triage_pending for customer guidance."""
        agent, ticket, updated_fields = await _complete_triage_result(
            {
                "benchmark_suite": "boot-time",
                "absent_suite": False,
                "directives": {"harness": "jumpstarter"},
            },
            _make_provider(),
        )

        assert ticket["status"] == "awaiting_customer_guidance"
        assert updated_fields == {}
        agent._transition_ticket.assert_awaited_once_with(
            ticket["id"],
            "awaiting_customer_guidance",
            comment="Triage validation failed; waiting for a valid harness.",
        )
        assert "Triage validation failed" in agent._add_comment.await_args.args[1]

    @pytest.mark.asyncio
    async def test_exact_suite_applies_catalog_harness_to_ticket(self):
        """A mismatched harness is corrected before triage writes the ticket."""
        _agent, ticket, updated_fields = await _complete_triage_result(
            {
                "benchmark_suite": "uperf",
                "absent_suite": True,
                "directives": {"harness": "zathras"},
            },
            _make_provider(),
        )

        assert ticket["status"] == "awaiting_hardware"
        assert updated_fields["absent_suite"] is False
        assert updated_fields["directives"]["harness"] == "crucible"

    @pytest.mark.asyncio
    async def test_explicit_user_harness_conflict_pauses_for_guidance(self):
        """Catalog correction must not override a user-selected harness."""
        agent, ticket, updated_fields = await _complete_triage_result(
            {
                "benchmark_suite": "uperf",
                "absent_suite": False,
                "directives": {"harness": "crucible"},
            },
            _make_provider(),
            custom_fields={"directives": {"harness": "zathras"}},
        )

        assert ticket["status"] == "awaiting_customer_guidance"
        assert updated_fields == {}
        agent._transition_ticket.assert_awaited_once_with(
            ticket["id"],
            "awaiting_customer_guidance",
            comment="Triage validation failed; awaiting harness guidance.",
        )
        comment = agent._add_comment.await_args.args[1]
        assert "requested harness 'zathras'" in comment
        assert "catalog harness 'crucible'" in comment
        assert "benchmark suite 'uperf'" in comment

    @pytest.mark.asyncio
    async def test_description_harness_conflict_pauses_for_guidance(self):
        """A request must be checked even when triage selects another harness."""
        agent, ticket, updated_fields = await _complete_triage_result(
            {
                "benchmark_suite": "uperf",
                "absent_suite": False,
                "directives": {"harness": "crucible"},
            },
            _make_provider(),
            description="Use zathras",
        )

        assert ticket["status"] == "awaiting_customer_guidance"
        assert updated_fields == {}
        agent._transition_ticket.assert_awaited_once_with(
            ticket["id"],
            "awaiting_customer_guidance",
            comment="Triage validation failed; awaiting harness guidance.",
        )
        comment = agent._add_comment.await_args.args[1]
        assert "specifies harness 'zathras'" in comment
        assert "selected 'crucible'" in comment

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("selected_harness", "catalog_harness"),
        [("zathras", "zathras"), ("crucible", "zathras")],
    )
    async def test_excluded_harness_pauses_before_writing_fields(
        self, selected_harness, catalog_harness
    ):
        """Neither triage nor suite correction may select an excluded harness."""
        suite = "zperf"
        provider = _make_provider(
            suites={suite: {"name": suite, "harness": catalog_harness}}
        )
        agent, ticket, updated_fields = await _complete_triage_result(
            {
                "benchmark_suite": suite,
                "absent_suite": True,
                "directives": {"harness": selected_harness},
            },
            provider,
            description="Do not use zathras",
        )

        assert ticket["status"] == "awaiting_customer_guidance"
        assert updated_fields == {}
        assert (
            "explicitly excludes harness 'zathras'"
            in (agent._add_comment.await_args.args[1])
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("selected_harness", ["zathras", "crucible"])
    async def test_either_or_request_accepts_each_listed_harness(
        self, selected_harness
    ):
        suite = f"{selected_harness}-suite"
        provider = _make_provider(
            suites={suite: {"name": suite, "harness": selected_harness}}
        )
        _agent, ticket, updated_fields = await _complete_triage_result(
            {
                "benchmark_suite": suite,
                "absent_suite": True,
                "directives": {"harness": selected_harness},
            },
            provider,
            description="Use either zathras or crucible",
        )

        assert ticket["status"] == "awaiting_hardware"
        assert updated_fields["directives"]["harness"] == selected_harness

    @pytest.mark.asyncio
    @pytest.mark.parametrize("selected_harness", ["zathras", "crucible"])
    async def test_negated_either_or_pauses_for_each_prohibited_harness(
        self, selected_harness
    ):
        suite = f"{selected_harness}-suite"
        provider = _make_provider(
            suites={suite: {"name": suite, "harness": selected_harness}}
        )
        agent, ticket, updated_fields = await _complete_triage_result(
            {
                "benchmark_suite": suite,
                "absent_suite": True,
                "directives": {"harness": selected_harness},
            },
            provider,
            description="Do not use either zathras or crucible",
        )

        assert ticket["status"] == "awaiting_customer_guidance"
        assert updated_fields == {}
        assert (
            f"explicitly excludes harness '{selected_harness}'"
            in agent._add_comment.await_args.args[1]
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("selected_harness", ["zathras", "crucible"])
    async def test_avoided_either_or_pauses_for_each_prohibited_harness(
        self, selected_harness
    ):
        suite = f"{selected_harness}-suite"
        provider = _make_provider(
            suites={suite: {"name": suite, "harness": selected_harness}}
        )
        agent, ticket, updated_fields = await _complete_triage_result(
            {
                "benchmark_suite": suite,
                "absent_suite": True,
                "directives": {"harness": selected_harness},
            },
            provider,
            description="Avoid using either zathras or crucible",
        )

        assert ticket["status"] == "awaiting_customer_guidance"
        assert updated_fields == {}
        assert (
            f"explicitly excludes harness '{selected_harness}'"
            in agent._add_comment.await_args.args[1]
        )

    @pytest.mark.asyncio
    async def test_only_harness_request_rejects_other_triage_selection(self):
        agent, ticket, updated_fields = await _complete_triage_result(
            {
                "benchmark_suite": "uperf",
                "absent_suite": False,
                "directives": {"harness": "crucible"},
            },
            _make_provider(),
            description="Use only zathras",
        )

        assert ticket["status"] == "awaiting_customer_guidance"
        assert updated_fields == {}
        comment = agent._add_comment.await_args.args[1]
        assert "specifies harness 'zathras'" in comment
        assert "selected 'crucible'" in comment

    @pytest.mark.asyncio
    async def test_either_or_request_pauses_for_unlisted_harness(self):
        agent, ticket, updated_fields = await _complete_triage_result(
            {
                "benchmark_suite": "uperf",
                "absent_suite": False,
                "directives": {"harness": "vstorm"},
            },
            _make_provider(),
            description="Use either zathras or crucible",
        )

        assert ticket["status"] == "awaiting_customer_guidance"
        assert updated_fields == {}
        comment = agent._add_comment.await_args.args[1]
        assert "permits harness alternatives (crucible, zathras)" in comment
        assert "selected 'vstorm'" in comment

    @pytest.mark.asyncio
    async def test_invented_suite_is_written_absent_before_dispatch(self):
        """A false-negative LLM flag reaches the existing absent-suite gate."""
        _agent, ticket, updated_fields = await _complete_triage_result(
            {
                "benchmark_suite": "invented-suite",
                "absent_suite": False,
                "directives": {"harness": "crucible"},
            },
            _make_provider(),
        )

        assert ticket["status"] == "awaiting_hardware"
        assert updated_fields["benchmark_suite"] == "invented-suite"
        assert updated_fields["absent_suite"] is True
