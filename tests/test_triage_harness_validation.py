"""Tests for triage harness and benchmark suite validation (issue #1086).

Validates that the triage agent rejects invalid harness names and
auto-corrects misnamed benchmark suites before they propagate
downstream and create stuck tickets.
"""

from __future__ import annotations

import pytest

from agents.triage.agent import _validate_harness_and_suite


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
        """Empty harness means 'use default' — should not be rejected."""
        provider = _make_provider()
        result = await _validate_harness_and_suite("", "uperf", False, provider)
        assert result is None

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
        result = await _validate_harness_and_suite(
            "arcaflow", "some-suite", False, provider
        )
        assert result is None


class TestSuiteAutoCorrection:
    """Auto-correct misnamed benchmark suites when absent_suite is set."""

    @pytest.mark.asyncio
    async def test_prefixed_suite_corrected(self):
        """Issue #1086 case: 'jumpstarter-boot-time' → 'boot-time'."""
        provider = _make_provider()
        result = await _validate_harness_and_suite(
            "boot-time", "jumpstarter-boot-time", True, provider
        )
        assert result is not None
        assert "correction" in result
        assert result["correction"]["benchmark_suite"] == "boot-time"
        assert result["correction"]["absent_suite"] is False

    @pytest.mark.asyncio
    async def test_no_correction_when_suite_exists(self):
        """absent_suite=True but suite actually exists → correct absent_suite flag."""
        provider = _make_provider()
        result = await _validate_harness_and_suite("crucible", "uperf", True, provider)
        assert result is not None
        assert "correction" in result
        assert result["correction"]["absent_suite"] is False
        assert result["correction"]["benchmark_suite"] == "uperf"

    @pytest.mark.asyncio
    async def test_no_correction_when_truly_absent(self):
        """Genuinely absent suite — no auto-correction possible."""
        provider = _make_provider()
        result = await _validate_harness_and_suite(
            "crucible", "totally-unknown-benchmark", True, provider
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_absent_suite_false_skips_validation(self):
        """When absent_suite is False, skip suite existence checks."""
        provider = _make_provider()
        result = await _validate_harness_and_suite(
            "crucible", "any-suite-name", False, provider
        )
        assert result is None

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
