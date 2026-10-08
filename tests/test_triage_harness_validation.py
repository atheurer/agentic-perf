"""Tests for triage harness and benchmark suite validation (issue #1086).

Validates that the triage agent rejects invalid harness names and
auto-corrects misnamed benchmark suites before they propagate
downstream and create stuck tickets.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from agents.triage.agent import TriageAgent, _validate_harness_and_suite
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
) -> tuple[TriageAgent, dict, dict]:
    """Run completion against an in-memory ticket with legal transitions."""
    ticket = {
        "id": "PERF-TRIAGE-VALIDATION",
        "status": "triage_pending",
        "summary": "Benchmark request",
        "description": "Run a benchmark",
        "custom_fields": {},
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
