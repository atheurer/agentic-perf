"""Tests for root-cause enforcement on alert-triggered tickets.

Covers two enforcement points:
  1. AnalyzeAgent._validate_submit_call (agents/analyze/agent.py) — rejects
     conclusive=True with empty root_cause for anomaly_context tickets before
     _handle_completion runs, feeding the rejection back to the LLM.
  2. _check_awaiting_review handoff (orchestrator/handoff.py) — belt-and-
     suspenders gate that blocks the awaiting_review transition under the
     same condition regardless of how the agent run resolved.
"""

from __future__ import annotations

import pytest

from orchestrator.handoff import check_handoff

# ---------------------------------------------------------------------------
# Handoff enforcement (_check_awaiting_review)
# ---------------------------------------------------------------------------


class TestAwaitingReviewHandoff:
    """_check_awaiting_review: root_cause required for alert-triggered tickets."""

    # --- benchmark path (unaffected) ---

    def test_benchmark_path_passes(self):
        """Benchmark result alone is sufficient; alert guard does not apply."""
        ticket = {
            "custom_fields": {
                "run_id": "12345",
                "benchmark_status": "completed",
            }
        }
        ok, reason = check_handoff("awaiting_review", ticket)
        assert ok, reason

    def test_benchmark_path_no_run_id_blocked(self):
        """Missing run_id on benchmark path is still blocked."""
        ticket = {
            "custom_fields": {
                "benchmark_status": "failed",
            }
        }
        ok, _ = check_handoff("awaiting_review", ticket)
        assert not ok

    # --- analysis-only path, non-alert ticket ---

    def test_analysis_no_anomaly_context_passes(self):
        """Non-alert conclusive analysis without root_cause is allowed."""
        ticket = {
            "custom_fields": {
                "analysis_result": {
                    "conclusive": True,
                    "finding": "RHEL10 is 15% faster than RHEL9.",
                }
            }
        }
        ok, reason = check_handoff("awaiting_review", ticket)
        assert ok, reason

    def test_analysis_inconclusive_no_anomaly_passes(self):
        """Inconclusive non-alert analysis without root_cause is allowed."""
        ticket = {
            "custom_fields": {
                "analysis_result": {
                    "conclusive": False,
                    "finding": "Insufficient baseline data.",
                }
            }
        }
        ok, reason = check_handoff("awaiting_review", ticket)
        assert ok, reason

    # --- analysis-only path, alert-triggered ticket ---

    def test_alert_conclusive_with_root_cause_passes(self):
        """Alert-triggered conclusive analysis WITH root_cause advances."""
        ticket = {
            "custom_fields": {
                "anomaly_context": {
                    "source": "horreum",
                    "metric": "boot.phase.kernel_ms",
                    "platform": "NXP_S32G",
                },
                "analysis_result": {
                    "conclusive": True,
                    "finding": "Kernel boot time regressed 31%.",
                    "root_cause": (
                        "Kernel updated from 6.12.8 to 6.12.10 between "
                        "builds 20260501 and 20260508. Regression isolated "
                        "to kernel post-timer phase (driver probes)."
                    ),
                },
            }
        }
        ok, reason = check_handoff("awaiting_review", ticket)
        assert ok, reason

    def test_alert_conclusive_without_root_cause_blocked(self):
        """Alert-triggered conclusive analysis WITHOUT root_cause is blocked."""
        ticket = {
            "custom_fields": {
                "anomaly_context": {
                    "source": "horreum",
                    "metric": "boot.phase.kernel_ms",
                    "platform": "NXP_S32G",
                },
                "analysis_result": {
                    "conclusive": True,
                    "finding": "Kernel boot time regressed 31%.",
                    # root_cause intentionally absent
                },
            }
        }
        ok, reason = check_handoff("awaiting_review", ticket)
        assert not ok
        assert "root_cause" in reason
        assert "alert-triggered" in reason.lower() or "anomaly" in reason.lower()

    def test_alert_conclusive_empty_root_cause_blocked(self):
        """Explicit empty string for root_cause is also blocked."""
        ticket = {
            "custom_fields": {
                "anomaly_context": {
                    "source": "horreum",
                    "metric": "boot.time.total_ms",
                },
                "analysis_result": {
                    "conclusive": True,
                    "finding": "Total boot time regressed.",
                    "root_cause": "",
                },
            }
        }
        ok, reason = check_handoff("awaiting_review", ticket)
        assert not ok
        assert "root_cause" in reason

    def test_alert_inconclusive_without_root_cause_passes(self):
        """Alert-triggered INCONCLUSIVE analysis without root_cause is fine.

        Inconclusive means the agent couldn't answer the question and
        needs more data — no causal attribution is expected yet.
        """
        ticket = {
            "custom_fields": {
                "anomaly_context": {
                    "source": "horreum",
                    "metric": "boot.phase.kernel_ms",
                },
                "analysis_result": {
                    "conclusive": False,
                    "finding": "Historical data is too sparse to determine cause.",
                    "benchmark_needed": {
                        "reason": "Need A/B boot-time comparison across two kernel versions."
                    },
                },
            }
        }
        ok, reason = check_handoff("awaiting_review", ticket)
        assert ok, reason

    def test_alert_conclusive_root_cause_whitespace_blocked(self):
        """Whitespace-only root_cause does not satisfy the requirement."""
        ticket = {
            "custom_fields": {
                "anomaly_context": {
                    "source": "horreum",
                    "metric": "perf.fio.read.iops",
                },
                "analysis_result": {
                    "conclusive": True,
                    "finding": "IOPS regression confirmed.",
                    "root_cause": "   ",
                },
            }
        }
        ok, reason = check_handoff("awaiting_review", ticket)
        assert not ok
        assert "root_cause" in reason


# ---------------------------------------------------------------------------
# Agent-level enforcement (AnalyzeAgent._validate_submit_call)
# ---------------------------------------------------------------------------


def _make_tool_call(conclusive: bool, root_cause: str = ""):
    from providers.llm.base import ToolCall

    return ToolCall(
        id="tc-test",
        name="submit_analysis_result",
        input={"conclusive": conclusive, "root_cause": root_cause},
    )


def _make_agent(store_url: str = "http://fake:9090"):
    from unittest.mock import MagicMock

    from agents.analyze.agent import AnalyzeAgent
    from providers.llm.base import LLMProvider

    llm = MagicMock(spec=LLMProvider)
    return AnalyzeAgent(
        llm_provider=llm,
        state_store_url=store_url,
    )


class TestValidateSubmitCall:
    """AnalyzeAgent._validate_submit_call: enforces root_cause for alert tickets."""

    def _patch_get_ticket(self, monkeypatch, agent, has_anomaly: bool):
        cf: dict = {}
        if has_anomaly:
            cf["anomaly_context"] = {
                "source": "horreum",
                "metric": "boot.phase.kernel_ms",
                "platform": "SA8775P",
            }
        ticket = {"id": "PERF-TEST", "custom_fields": cf}

        async def _fake_get_ticket(_ticket_id):
            return ticket

        monkeypatch.setattr(agent, "_get_ticket", _fake_get_ticket)

    @pytest.mark.asyncio
    async def test_alert_conclusive_no_root_cause_rejected(self, monkeypatch):
        """conclusive=True + no root_cause + anomaly_context → rejection string."""
        agent = _make_agent()
        self._patch_get_ticket(monkeypatch, agent, has_anomaly=True)

        result = await agent._validate_submit_call(
            "PERF-TEST", _make_tool_call(conclusive=True, root_cause="")
        )
        assert result is not None
        assert "REJECTED" in result
        assert "root_cause" in result

    @pytest.mark.asyncio
    async def test_alert_conclusive_with_root_cause_accepted(self, monkeypatch):
        """conclusive=True + root_cause populated → None (allow)."""
        agent = _make_agent()
        self._patch_get_ticket(monkeypatch, agent, has_anomaly=True)

        result = await agent._validate_submit_call(
            "PERF-TEST",
            _make_tool_call(
                conclusive=True, root_cause="Kernel updated 6.12.8→6.12.10."
            ),
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_non_alert_conclusive_no_root_cause_accepted(self, monkeypatch):
        """Non-alert conclusive result without root_cause → None (allow)."""
        agent = _make_agent()
        self._patch_get_ticket(monkeypatch, agent, has_anomaly=False)

        result = await agent._validate_submit_call(
            "PERF-TEST", _make_tool_call(conclusive=True, root_cause="")
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_inconclusive_no_root_cause_accepted(self, monkeypatch):
        """Inconclusive result with no root_cause → None (allow)."""
        agent = _make_agent()
        self._patch_get_ticket(monkeypatch, agent, has_anomaly=True)

        result = await agent._validate_submit_call(
            "PERF-TEST", _make_tool_call(conclusive=False, root_cause="")
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_alert_whitespace_root_cause_rejected(self, monkeypatch):
        """Whitespace-only root_cause does not satisfy the requirement."""
        agent = _make_agent()
        self._patch_get_ticket(monkeypatch, agent, has_anomaly=True)

        result = await agent._validate_submit_call(
            "PERF-TEST", _make_tool_call(conclusive=True, root_cause="   ")
        )
        assert result is not None
        assert "REJECTED" in result

    @pytest.mark.asyncio
    async def test_get_ticket_returns_none_skips_check(self, monkeypatch):
        """If _get_ticket returns None, the check is skipped (fail open)."""
        agent = _make_agent()

        async def _null_get_ticket(_ticket_id):
            return None

        monkeypatch.setattr(agent, "_get_ticket", _null_get_ticket)

        result = await agent._validate_submit_call(
            "PERF-TEST", _make_tool_call(conclusive=True, root_cause="")
        )
        assert result is None
