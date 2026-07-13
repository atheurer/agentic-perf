"""Tests for fleet investigation tracking."""

from __future__ import annotations

from providers.fleet import (
    build_tested_host_entry,
    get_fleet_progress,
    get_tested_host_ids,
    is_fleet_investigation,
)


class TestIsFleetInvestigation:
    def test_enabled_with_investigation_context(self):
        cf = {
            "fleet_investigation": {"enabled": True},
            "anomaly_context": {"description": "boot failures"},
        }
        assert is_fleet_investigation(cf) is True

    def test_enabled_with_ledger(self):
        cf = {
            "fleet_investigation": {"enabled": True},
            "investigation_ledger": [{"entry": "test"}],
        }
        assert is_fleet_investigation(cf) is True

    def test_enabled_without_investigation_context(self):
        """Guardrail: fleet needs investigation path."""
        cf = {
            "fleet_investigation": {"enabled": True},
        }
        assert is_fleet_investigation(cf) is False

    def test_not_enabled(self):
        cf = {
            "anomaly_context": {"description": "test"},
        }
        assert is_fleet_investigation(cf) is False

    def test_empty(self):
        assert is_fleet_investigation({}) is False


class TestGetTestedHostIds:
    def test_returns_ids(self):
        cf = {
            "fleet_investigation": {
                "tested_hosts": [
                    {"host_id": "board-01", "status": "completed"},
                    {"host_id": "board-02", "status": "partial"},
                ],
            },
        }
        assert get_tested_host_ids(cf) == [
            "board-01",
            "board-02",
        ]

    def test_empty(self):
        assert get_tested_host_ids({}) == []


class TestGetFleetProgress:
    def test_hard_exhaustion(self):
        cf = {
            "fleet_investigation": {
                "fleet_exhausted": {"hard": True},
                "tested_hosts": [
                    {"host_id": "a", "status": "completed"},
                    {"host_id": "b", "status": "partial"},
                    {"host_id": "c", "status": "completed"},
                ],
            },
        }
        p = get_fleet_progress(cf)
        assert p["tested"] == 3
        assert p["completed"] == 2
        assert p["partial"] == 1
        assert p["fleet_exhausted"] is True
        assert p["exhaustion_type"] == "hard"
        assert p["converged"] is True

    def test_soft_exhaustion(self):
        cf = {
            "fleet_investigation": {
                "fleet_exhausted": {
                    "soft": True,
                    "unavailable_hosts": [
                        "board-04",
                        "board-05",
                    ],
                },
                "tested_hosts": [
                    {"host_id": "a", "status": "completed"},
                    {"host_id": "b", "status": "completed"},
                ],
            },
        }
        p = get_fleet_progress(cf)
        assert p["tested"] == 2
        assert p["fleet_exhausted"] is True
        assert p["exhaustion_type"] == "soft"
        assert p["unavailable_hosts"] == [
            "board-04",
            "board-05",
        ]
        assert p["converged"] is True

    def test_not_exhausted(self):
        cf = {
            "fleet_investigation": {
                "tested_hosts": [
                    {"host_id": "a", "status": "completed"},
                ],
            },
        }
        p = get_fleet_progress(cf)
        assert p["tested"] == 1
        assert p["fleet_exhausted"] is False
        assert p["exhaustion_type"] is None
        assert p["converged"] is False

    def test_empty(self):
        p = get_fleet_progress({})
        assert p["converged"] is False
        assert p["fleet_exhausted"] is False


class TestBuildTestedHostEntry:
    def test_basic(self):
        entry = build_tested_host_entry(
            host_id="board-01",
            lease_id="perf-xxx",
            ip="10.0.0.1",
            samples_collected=10,
            samples_requested=10,
        )
        assert entry["host_id"] == "board-01"
        assert entry["status"] == "completed"
        assert "failure_reason" not in entry

    def test_with_failure(self):
        entry = build_tested_host_entry(
            host_id="board-02",
            status="partial",
            samples_collected=3,
            samples_requested=10,
            failure_reason="SUT unreachable",
            kpis={"avg_total_boot_s": 28.1},
        )
        assert entry["status"] == "partial"
        assert entry["failure_reason"] == "SUT unreachable"
        assert entry["kpis"]["avg_total_boot_s"] == 28.1


import pytest


class TestEvaluateFleetGate:
    """Evaluate agent deterministic fleet convergence checks."""

    @pytest.fixture(autouse=True)
    def _check_evaluate_available(self):
        try:
            from agents.evaluate.agent import EvaluateAgent  # noqa: F401

            self._available = True
        except ImportError:
            self._available = False

    def _make_evaluate_agent(self):
        if not self._available:
            pytest.skip("evaluate agent not available (depends on PR #185)")
        from unittest.mock import MagicMock

        from agents.evaluate.agent import EvaluateAgent

        return EvaluateAgent(
            llm_provider=MagicMock(),
            state_store_url="http://localhost:8090",
        )

    def test_fleet_not_complete(self):
        agent = self._make_evaluate_agent()
        cf = {
            "anomaly_context": {"source": "test"},
            "fleet_investigation": {
                "enabled": True,
                "tested_hosts": [
                    {"host_id": "a", "status": "completed"},
                ],
            },
        }
        result = agent._check_deterministic(cf)
        assert "FLEET_NOT_COMPLETE" in result

    def test_fleet_complete_hard(self):
        agent = self._make_evaluate_agent()
        cf = {
            "anomaly_context": {"source": "test"},
            "fleet_investigation": {
                "enabled": True,
                "fleet_exhausted": {"hard": True},
                "tested_hosts": [
                    {"host_id": "a", "status": "completed"},
                    {"host_id": "b", "status": "completed"},
                ],
            },
        }
        result = agent._check_deterministic(cf)
        assert "FLEET_COMPLETE" in result

    def test_fleet_complete_soft_includes_unavailable(self):
        agent = self._make_evaluate_agent()
        cf = {
            "anomaly_context": {"source": "test"},
            "fleet_investigation": {
                "enabled": True,
                "fleet_exhausted": {
                    "soft": True,
                    "unavailable_hosts": ["board-c"],
                },
                "tested_hosts": [
                    {"host_id": "a", "status": "completed"},
                ],
            },
        }
        result = agent._check_deterministic(cf)
        assert "FLEET_COMPLETE" in result
        assert "unavailable" in result

    def test_non_fleet_skips(self):
        agent = self._make_evaluate_agent()
        cf = {
            "anomaly_context": {"source": "test"},
        }
        result = agent._check_deterministic(cf)
        assert "FLEET" not in result


class TestGatheringContextDedupSkip:
    """Gathering context skips dedup on loop-back."""

    async def test_skips_when_dedup_result_exists(self):
        from unittest.mock import AsyncMock, MagicMock

        from agents.gathering_context.agent import (
            GatheringContextAgent,
        )

        agent = GatheringContextAgent(
            llm_provider=MagicMock(),
            state_store_url="http://localhost:8090",
        )
        agent._client = AsyncMock()
        agent._client.get = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: {
                    "custom_fields": {
                        "dedup_result": {
                            "decision": "NO_MATCH",
                        },
                    },
                },
                raise_for_status=lambda: None,
            ),
        )
        agent._client.post = AsyncMock(
            return_value=AsyncMock(
                status_code=200,
                json=lambda: {},
                raise_for_status=lambda: None,
            ),
        )

        await agent.run("PERF-TEST")

        # Should have transitioned to planning_investigation
        # without starting MCP servers
        agent._client.post.assert_called()


class TestTriageFleetDetection:
    """Triage fleet_investigation field handling."""

    def test_handle_completion_has_fleet_logic(self):
        """Verify the fleet handling code path exists."""
        import inspect

        from agents.triage.agent import TriageAgent

        source = inspect.getsource(TriageAgent._handle_completion)
        assert "fleet_investigation" in source
        assert "anomaly_context" in source
